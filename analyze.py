#!/usr/bin/env python3
"""Build an orbit-style contributor overlap analysis centered on a GitHub repo."""

import argparse
import concurrent.futures
import fnmatch
import html
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from loguru import logger  # type: ignore[reportMissingImports]
from tqdm.auto import tqdm  # type: ignore[reportMissingImports]


def configure_logger() -> None:
    logger.remove()
    logger.add(
        lambda msg: print(msg, end=""),
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )


def get_header_int(headers: dict[str, str], name: str) -> int | None:
    value = headers.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_retry_wait_seconds(exc: urllib.error.HTTPError, attempt: int) -> int:
    headers = {key: value for key, value in exc.headers.items()} if exc.headers else {}
    retry_after = get_header_int(headers, "Retry-After")
    if retry_after is not None:
        return max(1, retry_after + 1)

    remaining = headers.get("X-RateLimit-Remaining")
    reset_at = get_header_int(headers, "X-RateLimit-Reset")
    if exc.code == 403 and remaining == "0" and reset_at is not None:
        return max(1, reset_at - int(time.time()) + 1)

    if exc.code in (403, 429):
        return min(60, 2 ** min(attempt, 5))

    return 0


def github_request(
    url: str,
    token: str,
    accept: str = "application/vnd.github+json",
) -> tuple[list | dict, dict[str, str]]:
    attempt = 0
    while True:
        req = urllib.request.Request(url)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "dress-orbit-analyzer/2.0")
        try:
            with urllib.request.urlopen(req) as resp:
                headers = {key: value for key, value in resp.headers.items()}
                return json.loads(resp.read()), headers
        except urllib.error.HTTPError as exc:
            wait_seconds = get_retry_wait_seconds(exc, attempt)
            if wait_seconds > 0:
                logger.warning(
                    "[rate-limit] {} for {}; sleeping {}s before retry",
                    exc.code,
                    url,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                attempt += 1
                continue
            raise
        except urllib.error.URLError as exc:
            if attempt >= 5:
                raise
            wait_seconds = min(30, 2 ** min(attempt, 5))
            logger.warning(
                "[network] {} for {}; sleeping {}s before retry",
                exc.reason,
                url,
                wait_seconds,
            )
            time.sleep(wait_seconds)
            attempt += 1


def get_rate_limit_resource(token: str, resource: str = "search") -> dict | None:
    try:
        data, _ = github_request("https://api.github.com/rate_limit", token)
    except urllib.error.HTTPError as exc:
        logger.warning("failed to fetch rate limit snapshot: {}", exc)
        return None

    resources = data.get("resources", {}) if isinstance(data, dict) else {}
    bucket = resources.get(resource, {}) if isinstance(resources, dict) else {}
    if not isinstance(bucket, dict):
        return None

    limit = int(bucket.get("limit", 0) or 0)
    remaining = int(bucket.get("remaining", 0) or 0)
    reset = int(bucket.get("reset", 0) or 0)
    now = int(time.time())
    return {
        "resource": resource,
        "limit": limit,
        "remaining": remaining,
        "reset": reset,
        "reset_in_seconds": max(0, reset - now),
        "observed_at": now,
    }


def wait_until_reset(reset_at: int, reason: str) -> None:
    wait_seconds = max(1, reset_at - int(time.time()) + 1)
    logger.warning("[rate-limit] {}; sleeping {}s until reset", reason, wait_seconds)
    time.sleep(wait_seconds)


def get_contributor_identity(entry: dict) -> tuple[str | None, str, bool]:
    """Return (stable_identity_key, display_name, is_login_identity)."""
    login = entry.get("login")
    if login:
        login_text = str(login).strip()
        if login_text:
            return f"login:{login_text.lower()}", login_text, True

    for field in ("name", "email"):
        raw_value = entry.get(field)
        if raw_value:
            display = str(raw_value).strip()
            if display:
                if display.lower() == "undefined":
                    continue
                normalized = re.sub(r"\s+", " ", display).lower()
                return f"anon:{field}:{normalized}", display, False

    return None, "", False


def load_project_blacklist(blacklist_path: str) -> list[str]:
    if not blacklist_path:
        return []

    try:
        with open(blacklist_path, encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except FileNotFoundError:
        logger.warning("project blacklist file not found: {}", blacklist_path)
        return []

    deduped_rules: dict[str, str] = {}
    for raw_line in raw_lines:
        rule = raw_line.strip()
        if not rule or rule.startswith("#"):
            continue
        deduped_rules.setdefault(rule.lower(), rule)

    rules = list(deduped_rules.values())
    logger.info("loaded {} project blacklist rules from {}", len(rules), blacklist_path)
    return rules


def match_project_blacklist(full_name: str, blacklist_rules: list[str]) -> str | None:
    candidate = full_name.lower()
    for rule in blacklist_rules:
        normalized_rule = rule.lower()
        if any(token in normalized_rule for token in "*?["):
            if fnmatch.fnmatch(candidate, normalized_rule):
                return rule
            continue
        if candidate == normalized_rule:
            return rule
    return None


def filter_blacklisted_projects(
    projects: list[dict],
    blacklist_rules: list[str],
) -> tuple[list[dict], list[dict]]:
    if not blacklist_rules:
        return projects, []

    kept: list[dict] = []
    excluded: list[dict] = []
    for project in projects:
        matched_rule = match_project_blacklist(project["full_name"], blacklist_rules)
        if matched_rule:
            excluded_project = dict(project)
            excluded_project["blacklist_rule"] = matched_rule
            excluded.append(excluded_project)
            continue
        kept.append(project)

    return kept, excluded


def get_all_contributors(owner_repo: str, token: str) -> tuple[dict[str, dict], int]:
    """Return ({identity_key: contributor_info}, total_contributors_including_anonymous)."""
    owner, repo = owner_repo.split("/", 1)
    contributors: dict[str, dict] = {}
    total_contributors = 0
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}"
            f"/contributors?per_page=100&page={page}&anon=1"
        )
        try:
            data, _ = github_request(url, token)
        except urllib.error.HTTPError as exc:
            logger.error("{} contributors page {}: {}", owner_repo, page, exc)
            break
        if not data:
            break
        for entry in data:
            total_contributors += 1
            identity_key, display_name, is_login = get_contributor_identity(entry)
            if not identity_key:
                continue

            if identity_key not in contributors:
                contributors[identity_key] = {
                    "display": display_name,
                    "is_login": is_login,
                    "contributions": 0,
                }

            contributors[identity_key]["contributions"] += int(entry.get("contributions", 0))
        page += 1
    return contributors, total_contributors


def get_repo_meta(owner_repo: str, token: str) -> dict:
    owner, repo = owner_repo.split("/", 1)
    try:
        data, _ = github_request(f"https://api.github.com/repos/{owner}/{repo}", token)
        parent = data.get("parent") if isinstance(data, dict) else {}
        source = data.get("source") if isinstance(data, dict) else {}
        return {
            "full_name": data.get("full_name", owner_repo),
            "html_url": data.get("html_url", f"https://github.com/{owner_repo}"),
            "description": data.get("description", "") or "",
            "stargazers_count": data.get("stargazers_count", 0),
            "forks_count": data.get("forks_count", 0),
            "language": (data.get("language") or "") if isinstance(data, dict) else "",
            "fork": bool(data.get("fork", False)),
            "mirror_url": data.get("mirror_url") or "",
            "homepage": data.get("homepage") or "",
            "default_branch": data.get("default_branch") or "",
            "pushed_at": data.get("pushed_at") or "",
            "archived": bool(data.get("archived", False)),
            "disabled": bool(data.get("disabled", False)),
            "parent_full_name": (parent or {}).get("full_name", "") if isinstance(parent, dict) else "",
            "source_full_name": (source or {}).get("full_name", "") if isinstance(source, dict) else "",
        }
    except urllib.error.HTTPError:
        return {
            "full_name": owner_repo,
            "html_url": f"https://github.com/{owner_repo}",
            "description": "",
            "stargazers_count": 0,
            "forks_count": 0,
            "language": "",
            "fork": False,
            "mirror_url": "",
            "homepage": "",
            "default_branch": "",
            "pushed_at": "",
            "archived": False,
            "disabled": False,
            "parent_full_name": "",
            "source_full_name": "",
        }


def get_default_branch_head_sha(owner_repo: str, token: str, default_branch: str = "") -> str:
    owner, repo = owner_repo.split("/", 1)
    branch = default_branch.strip() or "HEAD"
    ref = urllib.parse.quote(branch, safe="")
    try:
        data, _ = github_request(f"https://api.github.com/repos/{owner}/{repo}/commits/{ref}", token)
        sha = data.get("sha", "") if isinstance(data, dict) else ""
        return sha or ""
    except urllib.error.HTTPError:
        return ""


def repo_has_commit(owner_repo: str, token: str, sha: str) -> bool:
    if not sha:
        return False
    owner, repo = owner_repo.split("/", 1)
    quoted_sha = urllib.parse.quote(sha, safe="")
    try:
        github_request(f"https://api.github.com/repos/{owner}/{repo}/commits/{quoted_sha}", token)
        return True
    except urllib.error.HTTPError:
        return False


def detect_mirror_like_projects(
    base_repo: str,
    projects: list[dict],
    token: str,
    mirror_check_limit: int,
    workers: int,
    min_score: int,
) -> tuple[list[dict], list[dict], str]:
    """Return (kept_projects, excluded_mirror_like_projects, base_head_sha)."""
    if mirror_check_limit <= 0 or not projects:
        return projects, [], ""

    check_targets = projects[: mirror_check_limit]
    check_map = {project["full_name"]: project for project in check_targets}

    logger.info(
        "mirror check: scanning top {} projects with score threshold >= {}",
        len(check_targets),
        min_score,
    )

    base_meta = get_repo_meta(base_repo, token)
    base_default_branch = base_meta.get("default_branch", "")
    base_head_sha = get_default_branch_head_sha(base_repo, token, base_default_branch)

    def evaluate_project(project: dict) -> tuple[str, dict]:
        full_name = project["full_name"]
        meta = get_repo_meta(full_name, token)
        reasons: list[str] = []
        score = 0

        parent_full_name = (meta.get("parent_full_name") or "").lower()
        source_full_name = (meta.get("source_full_name") or "").lower()
        base_repo_lower = base_repo.lower()

        if meta.get("fork") and (parent_full_name == base_repo_lower or source_full_name == base_repo_lower):
            score += 3
            reasons.append("direct fork from base repository")

        if meta.get("mirror_url"):
            score += 3
            reasons.append("mirror_url is set")

        text_blob = " ".join(
            [
                full_name,
                meta.get("description", "") or "",
                meta.get("homepage", "") or "",
            ]
        ).lower()
        if re.search(r"\b(mirror|backup|archive|fork\s*sync|sync)\b", text_blob):
            score += 1
            reasons.append("name/description/homepage contains mirror-like keyword")

        branch = meta.get("default_branch", "")
        head_sha = get_default_branch_head_sha(full_name, token, branch)
        if head_sha and base_head_sha and head_sha == base_head_sha:
            score += 3
            reasons.append("default branch head commit equals base repository head")
        elif head_sha and repo_has_commit(base_repo, token, head_sha):
            score += 2
            reasons.append("default branch head commit exists in base repository history")

        result = {
            "score": score,
            "reasons": reasons,
            "head_sha": head_sha,
            "mirror_like": score >= min_score,
            "meta": meta,
        }
        return full_name, result

    if workers <= 1 or len(check_targets) == 1:
        for project in tqdm(check_targets, desc="Mirror detection", unit="repo", dynamic_ncols=True, leave=False):
            full_name, result = evaluate_project(project)
            project.update(result["meta"])
            project["mirror_detection"] = {
                "checked": True,
                "score": result["score"],
                "reasons": result["reasons"],
                "head_sha": result["head_sha"],
                "mirror_like": result["mirror_like"],
            }
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(check_targets))) as executor:
            future_map = {
                executor.submit(evaluate_project, project): project
                for project in check_targets
            }
            with tqdm(
                total=len(check_targets),
                desc="Mirror detection",
                unit="repo",
                dynamic_ncols=True,
                leave=False,
            ) as progress:
                for future in concurrent.futures.as_completed(future_map):
                    project = future_map[future]
                    try:
                        full_name, result = future.result()
                        target = check_map.get(full_name, project)
                        target.update(result["meta"])
                        target["mirror_detection"] = {
                            "checked": True,
                            "score": result["score"],
                            "reasons": result["reasons"],
                            "head_sha": result["head_sha"],
                            "mirror_like": result["mirror_like"],
                        }
                    except Exception as exc:
                        logger.warning("mirror detection failed for {}: {}", project["full_name"], exc)
                        project["mirror_detection"] = {
                            "checked": False,
                            "score": 0,
                            "reasons": [f"detection error: {exc}"],
                            "head_sha": "",
                            "mirror_like": False,
                        }
                    finally:
                        progress.update(1)

    excluded: list[dict] = []
    kept: list[dict] = []
    for project in projects:
        detection = project.get("mirror_detection") if isinstance(project, dict) else None
        if isinstance(detection, dict) and detection.get("mirror_like"):
            excluded.append(project)
        else:
            kept.append(project)

    return kept, excluded, base_head_sha


def search_user_public_repos(login: str, token: str, max_pages: int) -> tuple[dict[str, dict], int]:
    """Discover public repositories via commit search results authored by the user."""
    discovered: dict[str, dict] = {}
    api_calls = 0

    for page in range(1, max_pages + 1):
        query = urllib.parse.quote(f"author:{login}")
        url = f"https://api.github.com/search/commits?q={query}&per_page=100&page={page}"
        try:
            data, _ = github_request(
                url,
                token,
                accept="application/vnd.github.cloak-preview+json",
            )
            api_calls += 1
        except urllib.error.HTTPError as exc:
            logger.warning("commit search failed for {} page {}: {}", login, page, exc)
            break

        items = data.get("items", []) if isinstance(data, dict) else []
        if not items:
            break

        for item in items:
            repository = item.get("repository") or {}
            full_name = repository.get("full_name")
            if not full_name:
                continue

            repo_entry = discovered.setdefault(
                full_name,
                {
                    "full_name": full_name,
                    "html_url": repository.get("html_url", f"https://github.com/{full_name}"),
                    "commit_hits": 0,
                },
            )
            repo_entry["commit_hits"] += 1

        if len(items) < 100:
            break

    return discovered, api_calls


def scan_contributor_projects(contributor: dict, token: str, search_pages: int) -> tuple[dict, int]:
    login = contributor["login"]
    repos, api_calls = search_user_public_repos(login, token, search_pages)
    sorted_repos = sorted(
        repos.values(),
        key=lambda item: (-item["commit_hits"], item["full_name"].lower()),
    )
    return (
        {
            "login": login,
            "dress_commits": contributor["dress_commits"],
            "discovered_repo_count": len(sorted_repos),
            "projects": sorted_repos,
        },
        api_calls,
    )


def merge_developer_record(project_index: dict[str, dict], developer_record: dict) -> None:
    login = developer_record["login"]
    dress_commits = developer_record["dress_commits"]

    for repo in developer_record["projects"]:
        full_name = repo["full_name"]
        entry = project_index.setdefault(
            full_name,
            {
                "full_name": full_name,
                "html_url": repo["html_url"],
                "shared_developer_count": 0,
                "shared_dress_commits": 0,
                "developer_samples": [],
                "developers": [],
                "search_commit_hits": 0,
            },
        )
        entry["shared_developer_count"] += 1
        entry["shared_dress_commits"] += dress_commits
        entry["search_commit_hits"] += repo["commit_hits"]
        entry["developers"].append(
            {
                "login": login,
                "dress_commits": dress_commits,
                "repo_commit_hits": repo["commit_hits"],
            }
        )

        if len(entry["developer_samples"]) < 6:
            entry["developer_samples"].append(login)


def aggregate_project_overlap(
    base_repo: str,
    contributors: dict[str, dict],
    token: str,
    search_pages: int,
    pause_seconds: float,
    workers: int,
    rate_reserve: int,
) -> tuple[list[dict], list[dict], int, dict | None]:
    project_index: dict[str, dict] = {}
    developer_records: list[dict] = []
    total_search_calls = 0
    last_rate_snapshot: dict | None = None

    login_contributors = sorted(
        [
            {
                "login": info["display"],
                "dress_commits": info["contributions"],
            }
            for info in contributors.values()
            if info["is_login"]
        ],
        key=lambda item: (-item["dress_commits"], item["login"].lower()),
    )

    pending = list(login_contributors)
    completed = 0
    total = len(login_contributors)
    estimated_cost_per_developer = max(1, search_pages)
    queue_window = max(workers, workers * 4)

    with tqdm(total=total, desc="Scanning developers", unit="dev", dynamic_ncols=True) as progress:
        while pending:
            snapshot = get_rate_limit_resource(token, "search")
            if snapshot:
                last_rate_snapshot = snapshot
                remaining = snapshot["remaining"]
                limit = snapshot["limit"]
                reset_at = snapshot["reset"]
                progress.set_postfix(
                    remaining=remaining,
                    reserve=rate_reserve,
                    refresh=False,
                )
                if remaining <= rate_reserve:
                    wait_until_reset(reset_at, f"search quota {remaining}/{limit}")
                    continue

                available_budget = max(1, remaining - rate_reserve)
                budget_batch_size = max(1, available_budget // estimated_cost_per_developer)
                batch_size = min(len(pending), budget_batch_size, queue_window)
                logger.info(
                    "batch scheduling: search quota {}/{}; queueing {} developers",
                    remaining,
                    limit,
                    batch_size,
                )
            else:
                batch_size = min(len(pending), queue_window)

            batch = pending[:batch_size]
            pending = pending[batch_size:]

            if workers <= 1 or len(batch) == 1:
                for contributor in batch:
                    login = contributor["login"]
                    completed += 1
                    progress.set_postfix_str(f"@{login}", refresh=False)
                    try:
                        developer_record, api_calls = scan_contributor_projects(
                            contributor,
                            token,
                            search_pages,
                        )
                    except Exception as exc:
                        logger.warning("contributor scan failed for {}: {}", login, exc)
                        progress.update(1)
                        continue
                    total_search_calls += api_calls
                    developer_records.append(developer_record)
                    merge_developer_record(project_index, developer_record)
                    progress.update(1)
            else:
                future_to_contributor: dict[concurrent.futures.Future, dict] = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
                    for contributor in batch:
                        future = executor.submit(
                            scan_contributor_projects,
                            contributor,
                            token,
                            search_pages,
                        )
                        future_to_contributor[future] = contributor

                    for future in concurrent.futures.as_completed(future_to_contributor):
                        contributor = future_to_contributor[future]
                        login = contributor["login"]
                        completed += 1
                        progress.set_postfix_str(f"@{login}", refresh=False)
                        try:
                            developer_record, api_calls = future.result()
                        except Exception as exc:
                            logger.warning("contributor scan failed for {}: {}", login, exc)
                            progress.update(1)
                            continue
                        total_search_calls += api_calls
                        developer_records.append(developer_record)
                        merge_developer_record(project_index, developer_record)
                        progress.update(1)

            if pause_seconds > 0 and pending:
                time.sleep(pause_seconds)

    developer_records.sort(key=lambda item: (-item["dress_commits"], item["login"].lower()))

    project_index.pop(base_repo, None)
    projects = sorted(
        project_index.values(),
        key=lambda item: (
            -item["shared_developer_count"],
            -item["shared_dress_commits"],
            item["full_name"].lower(),
        ),
    )
    return projects, developer_records, total_search_calls, last_rate_snapshot


def enrich_projects(projects: list[dict], token: str, workers: int) -> None:
    if not projects:
        return

    if workers <= 1:
        for project in tqdm(
            projects,
            desc="Enriching metadata",
            unit="repo",
            dynamic_ncols=True,
            leave=False,
        ):
            project.update(get_repo_meta(project["full_name"], token))
        return

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(projects))) as executor:
        future_to_project = {
            executor.submit(get_repo_meta, project["full_name"], token): project
            for project in projects
        }
        with tqdm(
            total=len(projects),
            desc="Enriching metadata",
            unit="repo",
            dynamic_ncols=True,
            leave=False,
        ) as progress:
            for future in concurrent.futures.as_completed(future_to_project):
                project = future_to_project[future]
                try:
                    project.update(future.result())
                except Exception as exc:
                    logger.warning("metadata enrichment failed for {}: {}", project["full_name"], exc)
                finally:
                    progress.update(1)


def format_samples(sample_logins: list[str]) -> str:
    return ", ".join(f"@{login}" for login in sample_logins)


def get_markdown_relpath(target_path: str, readme_path: str) -> str:
    readme_dir = os.path.dirname(os.path.abspath(readme_path)) or "."
    return os.path.relpath(os.path.abspath(target_path), start=readme_dir).replace("\\", "/")


def build_readme_section(
    base_repo: str,
    total_contributors: int,
    matchable_contributors: int,
    login_contributors: int,
    anonymous_matchable_contributors: int,
    search_pages: int,
    shared_projects: list[dict],
    clean_projects: list[dict],
    mirror_excluded_count: int,
    blacklist_excluded_count: int,
    blacklist_rule_count: int,
    mirror_check_limit: int,
    mirror_score_threshold: int,
    updated_at: str,
    svg_markdown_path: str,
    clean_svg_markdown_path: str,
    blacklist_markdown_path: str,
) -> str:
    lines: list[str] = []
    lines.append("## 分析结果\n")
    lines.append(f"> 最后更新：{updated_at}\n")
    lines.append(
        "> 说明：项目共现基于 Dress 的可独立追踪开发者（GitHub 登录名），"
        "通过 GitHub 公开提交检索反查其参与过的公开仓库。匿名身份会计入 Dress 贡献者总量，但无法跨仓库稳定追踪。\n"
    )
    lines.append("### Dress 开发者扫描概况\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|:--|--:|")
    lines.append(f"| 基准仓库 | [{base_repo}](https://github.com/{base_repo}) |")
    lines.append(f"| 贡献者总数（含匿名） | {total_contributors} |")
    lines.append(f"| 可匹配身份贡献者数（登录名或匿名署名） | {matchable_contributors} |")
    lines.append(f"| 可独立追踪开发者数（GitHub 登录） | {login_contributors} |")
    lines.append(f"| 匿名但可匹配身份数 | {anonymous_matchable_contributors} |")
    lines.append(f"| 发现的共现项目数 | {len(shared_projects)} |")
    lines.append(f"| 去噪后共现项目数 | {len(clean_projects)} |")
    lines.append(f"| 每位开发者检索页数 | {search_pages} |")
    lines.append(f"| 排除镜像疑似项目数 | {mirror_excluded_count} |")
    lines.append(f"| 项目黑名单规则数 | {blacklist_rule_count} |")
    lines.append(f"| 项目黑名单排除数 | {blacklist_excluded_count} |")
    lines.append(f"| 镜像检测范围（Top N） | {mirror_check_limit} |")
    lines.append(f"| 镜像判定阈值（score >=） | {mirror_score_threshold} |")
    lines.append("\n### 去噪 Orbit 图\n")
    lines.append(f"![Dress 开发者项目去噪 Orbit 图]({clean_svg_markdown_path})")
    lines.append(
        f"> 去噪规则：自动镜像检测 + 项目黑名单 [{blacklist_markdown_path}]({blacklist_markdown_path})。"
    )
    lines.append(f"> 原始对照图仍输出为 [{svg_markdown_path}]({svg_markdown_path})。")

    if clean_projects:
        lines.append("\n### 去噪后共同贡献最多的项目\n")
        lines.append("| 项目 | 共同开发者数 | 这些开发者在 Dress 的提交数 | Stars | 示例开发者 |")
        lines.append("|:--|--:|--:|--:|:--|")
        for project in clean_projects[:15]:
            samples = format_samples(project.get("developer_samples", []))
            stars = project.get("stargazers_count", 0)
            lines.append(
                f"| [{project['full_name']}](https://github.com/{project['full_name']})"
                f" | {project['shared_developer_count']}"
                f" | {project['shared_dress_commits']}"
                f" | {stars}"
                f" | {samples} |"
            )
    else:
        lines.append("\n> 去噪后未发现可统计的共现项目。\n")

    return "\n".join(lines)


def build_svg_chart(
    base_repo: str,
    total_developers: int,
    projects: list[dict],
    updated_at: str,
    chart_title: str,
    chart_description: str,
) -> str:
    top_projects = projects[:12]
    ranking_projects = projects[:8]
    width = 1280
    panel_top = 126
    left_panel_x = 30
    left_panel_w = 680
    right_panel_x = 740
    right_panel_w = 500
    rank_title_y = 416
    rank_start_y = 454
    rank_row_height = 56
    rank_count = len(ranking_projects)
    rank_bottom = rank_start_y + (rank_count - 1) * rank_row_height + 44 if rank_count else rank_start_y
    panel_bottom = max(856, rank_bottom + 24)
    panel_height = panel_bottom - panel_top
    footer_y = panel_bottom + 40
    height = footer_y + 32
    center_x = 360
    center_y = 430
    max_shared = max((project["shared_developer_count"] for project in top_projects), default=1)
    ring_radii = [145, 215, 280]
    angle_step = (2 * math.pi / len(top_projects)) if top_projects else 0
    project_nodes: list[str] = []
    ranking_rows: list[str] = []

    def clip_text(value: str, limit: int) -> str:
        return value if len(value) <= limit else value[: limit - 3] + "..."

    def orbit_label(full_name: str) -> str:
        owner, repo = full_name.split("/", 1)
        if repo in {"-", "-nz"} or len(repo) <= 3:
            return clip_text(f"{owner}/{repo}", 20)
        return clip_text(repo, 18)

    for index, project in enumerate(top_projects):
        ring = ring_radii[index % len(ring_radii)]
        angle = -math.pi / 2 + angle_step * index
        x = center_x + math.cos(angle) * ring
        y = center_y + math.sin(angle) * ring
        radius = 17 + (project["shared_developer_count"] / max_shared) * 27
        color = ["#2563eb", "#0ea5e9", "#14b8a6", "#f97316"][index % 4]
        raw_label = orbit_label(project["full_name"])
        label_char_limit = max(5, min(14, int((radius * 2 - 6) / 6)))
        label = html.escape(clip_text(raw_label, label_char_limit))
        full_name = html.escape(project["full_name"])
        name_font_size = 12 if radius >= 30 else 11 if radius >= 24 else 10
        count_font_size = 11 if radius >= 30 else 10 if radius >= 24 else 9

        project_nodes.append(
            f"  <line x1=\"{center_x}\" y1=\"{center_y}\" x2=\"{x:.1f}\" y2=\"{y:.1f}\" stroke=\"#cbd5e1\" stroke-width=\"2\" stroke-dasharray=\"7 9\"/>"
        )
        project_nodes.append(
            f"  <circle cx=\"{x:.1f}\" cy=\"{y:.1f}\" r=\"{radius:.1f}\" fill=\"{color}\" fill-opacity=\"0.18\" stroke=\"{color}\" stroke-width=\"2\"/>"
        )
        project_nodes.append(
            f"  <text x=\"{x:.1f}\" y=\"{y - 2:.1f}\" text-anchor=\"middle\" fill=\"#0f172a\" font-size=\"{name_font_size}\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"700\">{label}</text>"
        )
        project_nodes.append(
            f"  <text x=\"{x:.1f}\" y=\"{y + 14:.1f}\" text-anchor=\"middle\" fill=\"#334155\" font-size=\"{count_font_size}\" font-family=\"Segoe UI, Arial, sans-serif\">{project['shared_developer_count']}人</text>"
        )
        project_nodes.append(f"  <title>{full_name}: {project['shared_developer_count']} shared developers</title>")

    for index, project in enumerate(ranking_projects, start=1):
        bar_y = rank_start_y + (index - 1) * rank_row_height
        bar_width = int(round(438 * project["shared_developer_count"] / max_shared))
        label = html.escape(clip_text(project["full_name"], 42))
        samples = html.escape(format_samples(project.get("developer_samples", [])[:2]))
        ranking_rows.append(
            f"  <text x=\"770\" y=\"{bar_y}\" fill=\"#0f172a\" font-size=\"14\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"600\">{index}. {label}</text>"
        )
        ranking_rows.append(
            f"  <rect x=\"770\" y=\"{bar_y + 12}\" width=\"438\" height=\"12\" rx=\"6\" fill=\"#e2e8f0\"/>"
        )
        ranking_rows.append(
            f"  <rect x=\"770\" y=\"{bar_y + 12}\" width=\"{bar_width}\" height=\"12\" rx=\"6\" fill=\"#0f766e\"/>"
        )
        ranking_rows.append(
            f"  <text x=\"1210\" y=\"{bar_y + 23}\" text-anchor=\"end\" fill=\"#0f172a\" font-size=\"13\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"700\">{project['shared_developer_count']}</text>"
        )
        ranking_rows.append(
            f"  <text x=\"770\" y=\"{bar_y + 40}\" fill=\"#64748b\" font-size=\"12\" font-family=\"Segoe UI, Arial, sans-serif\">示例: {samples}</text>"
        )

    orbit_nodes_svg = "\n".join(project_nodes)
    ranking_svg = "\n".join(ranking_rows)
    title = html.escape(chart_title)
    chart_description_esc = html.escape(chart_description)
    updated_at_esc = html.escape(updated_at)
    base_repo_esc = html.escape(base_repo)

    return f"""<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" role=\"img\" aria-labelledby=\"title desc\">\n  <title id=\"title\">{title}</title>\n  <desc id=\"desc\">{chart_description_esc}</desc>\n  <defs>\n    <linearGradient id=\"bg\" x1=\"0\" y1=\"0\" x2=\"1\" y2=\"1\">\n      <stop offset=\"0%\" stop-color=\"#f7fbff\"/>\n      <stop offset=\"55%\" stop-color=\"#eefbf6\"/>\n      <stop offset=\"100%\" stop-color=\"#fff7ed\"/>\n    </linearGradient>\n    <filter id=\"softShadow\" x=\"-20%\" y=\"-20%\" width=\"140%\" height=\"140%\">\n      <feDropShadow dx=\"0\" dy=\"8\" stdDeviation=\"16\" flood-color=\"#0f172a\" flood-opacity=\"0.18\"/>\n    </filter>\n  </defs>\n\n  <rect x=\"0\" y=\"0\" width=\"{width}\" height=\"{height}\" fill=\"url(#bg)\"/>\n  <text x=\"50\" y=\"68\" fill=\"#0f172a\" font-size=\"34\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"700\">{title}</text>\n  <text x=\"50\" y=\"100\" fill=\"#475569\" font-size=\"15\" font-family=\"Segoe UI, Arial, sans-serif\">Base repository: {base_repo_esc} | Trackable developers: {total_developers} | Updated at {updated_at_esc}</text>\n\n  <rect x=\"{left_panel_x}\" y=\"{panel_top}\" width=\"{left_panel_w}\" height=\"{panel_height}\" rx=\"28\" fill=\"#ffffff\" fill-opacity=\"0.82\" filter=\"url(#softShadow)\"/>\n  <circle cx=\"{center_x}\" cy=\"{center_y}\" r=\"280\" fill=\"none\" stroke=\"#dbeafe\" stroke-width=\"2\" stroke-dasharray=\"10 12\"/>\n  <circle cx=\"{center_x}\" cy=\"{center_y}\" r=\"215\" fill=\"none\" stroke=\"#bfdbfe\" stroke-width=\"2\" stroke-dasharray=\"8 12\"/>\n  <circle cx=\"{center_x}\" cy=\"{center_y}\" r=\"145\" fill=\"none\" stroke=\"#93c5fd\" stroke-width=\"2\" stroke-dasharray=\"6 10\"/>\n{orbit_nodes_svg}\n  <circle cx=\"{center_x}\" cy=\"{center_y}\" r=\"74\" fill=\"#f59e0b\" fill-opacity=\"0.2\" stroke=\"#f59e0b\" stroke-width=\"3\" filter=\"url(#softShadow)\"/>\n  <text x=\"{center_x}\" y=\"{center_y - 7}\" text-anchor=\"middle\" fill=\"#0f172a\" font-size=\"30\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"800\">Dress</text>\n  <text x=\"{center_x}\" y=\"{center_y + 21}\" text-anchor=\"middle\" fill=\"#334155\" font-size=\"13\" font-family=\"Segoe UI, Arial, sans-serif\">developer orbit center</text>\n\n  <rect x=\"{right_panel_x}\" y=\"{panel_top}\" width=\"{right_panel_w}\" height=\"{panel_height}\" rx=\"28\" fill=\"#ffffff\" fill-opacity=\"0.88\" filter=\"url(#softShadow)\"/>\n  <text x=\"770\" y=\"174\" fill=\"#0f172a\" font-size=\"22\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"700\">Top Shared Projects</text>\n  <text x=\"770\" y=\"202\" fill=\"#64748b\" font-size=\"13\" font-family=\"Segoe UI, Arial, sans-serif\">按共同开发者数量排序，展示最强共现项目</text>\n  <text x=\"770\" y=\"246\" fill=\"#0f172a\" font-size=\"16\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"700\">统计摘要</text>\n  <text x=\"770\" y=\"278\" fill=\"#334155\" font-size=\"14\" font-family=\"Segoe UI, Arial, sans-serif\">共现项目数: {len(projects)}</text>\n  <text x=\"770\" y=\"306\" fill=\"#334155\" font-size=\"14\" font-family=\"Segoe UI, Arial, sans-serif\">轨道中展示项目数: {len(top_projects)}</text>\n  <text x=\"770\" y=\"334\" fill=\"#334155\" font-size=\"14\" font-family=\"Segoe UI, Arial, sans-serif\">最大共同开发者数: {max_shared}</text>\n  <text x=\"770\" y=\"362\" fill=\"#334155\" font-size=\"14\" font-family=\"Segoe UI, Arial, sans-serif\">中心仓库: {base_repo_esc}</text>\n  <text x=\"770\" y=\"{rank_title_y}\" fill=\"#0f172a\" font-size=\"16\" font-family=\"Segoe UI, Arial, sans-serif\" font-weight=\"700\">排行 (Top {len(ranking_projects)})</text>\n{ranking_svg}\n\n  <text x=\"50\" y=\"{footer_y}\" fill=\"#64748b\" font-size=\"13\" font-family=\"Segoe UI, Arial, sans-serif\">{chart_description_esc}</text>\n</svg>\n"""


def update_readme(section: str, readme_path: str) -> None:
    start = "<!-- ANALYSIS_START -->"
    end = "<!-- ANALYSIS_END -->"
    block = f"{start}\n{section}\n{end}"

    try:
        content = readme_path and open(readme_path, encoding="utf-8").read() or ""
    except FileNotFoundError:
        content = ""

    if start in content:
        content = re.sub(
            rf"{re.escape(start)}.*?{re.escape(end)}",
            block,
            content,
            flags=re.DOTALL,
        )
    else:
        content = content.rstrip("\n") + "\n\n" + block + "\n"

    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    logger.success("README updated: {}", readme_path)


def main() -> None:
    configure_logger()
    parser = argparse.ArgumentParser(
        description="Analyze which public GitHub projects most overlap with Dress contributors."
    )
    parser.add_argument(
        "--base-repo",
        default="Cute-Dress/Dress",
        help="Base repo in owner/repo format (default: Cute-Dress/Dress)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN", ""),
        help="GitHub personal access token (or set GITHUB_TOKEN env var)",
    )
    parser.add_argument(
        "--readme",
        default="README.md",
        help="Path to README.md to update (default: README.md)",
    )
    parser.add_argument(
        "--json-out",
        default="dress_orbit.json",
        help="Path to output JSON data (default: dress_orbit.json)",
    )
    parser.add_argument(
        "--clean-json-out",
        default="dress_orbit_clean.json",
        help="Path to output de-noised JSON data (default: dress_orbit_clean.json)",
    )
    parser.add_argument(
        "--svg-out",
        default="dress_orbit.svg",
        help="Path to output SVG chart (default: dress_orbit.svg)",
    )
    parser.add_argument(
        "--clean-svg-out",
        default="dress_orbit_clean.svg",
        help="Path to output de-noised SVG chart (default: dress_orbit_clean.svg)",
    )
    parser.add_argument(
        "--project-blacklist",
        default="blacklist/projects.txt",
        help="Path to a project blacklist file; one owner/repo or glob per line (default: blacklist/projects.txt)",
    )
    parser.add_argument(
        "--search-pages",
        type=int,
        default=1,
        help="How many commit-search pages to inspect per developer (default: 1)",
    )
    parser.add_argument(
        "--meta-project-limit",
        type=int,
        default=15,
        help="How many top projects to enrich with repository metadata (default: 15)",
    )
    parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.0,
        help="Optional delay between developer scans to reduce API pressure (default: 0)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of worker threads for GitHub API scanning (default: 8)",
    )
    parser.add_argument(
        "--rate-reserve",
        type=int,
        default=2,
        help="How many search requests to keep as safety reserve before waiting reset (default: 2)",
    )
    parser.add_argument(
        "--mirror-check-limit",
        type=int,
        default=160,
        help="How many top overlap projects to evaluate for mirror-like git traits (default: 160)",
    )
    parser.add_argument(
        "--mirror-score-threshold",
        type=int,
        default=3,
        help="Mirror-like exclusion threshold; exclude when score >= this value (default: 3)",
    )
    args = parser.parse_args()

    workers = max(1, args.workers)

    if not args.token:
        logger.warning(
            "GITHUB_TOKEN not set; authenticated access is strongly recommended for commit search."
        )

    project_blacklist_rules = load_project_blacklist(args.project_blacklist)

    logger.info("Fetching contributors: {}", args.base_repo)
    contributors, total_contributors = get_all_contributors(args.base_repo, args.token)
    login_contributors = sum(1 for contributor in contributors.values() if contributor["is_login"])
    anonymous_matchable_contributors = len(contributors) - login_contributors
    logger.info(
        "{} contributors found (including anonymous), {} matchable identities, {} login-trackable developers",
        total_contributors,
        len(contributors),
        login_contributors,
    )

    shared_projects, developer_records, total_search_calls, rate_snapshot = aggregate_project_overlap(
        args.base_repo,
        contributors,
        args.token,
        args.search_pages,
        args.pause_seconds,
        workers,
        max(0, args.rate_reserve),
    )
    filtered_projects, excluded_mirror_projects, base_head_sha = detect_mirror_like_projects(
        args.base_repo,
        shared_projects,
        args.token,
        max(0, args.mirror_check_limit),
        workers,
        max(1, args.mirror_score_threshold),
    )
    shared_projects = filtered_projects
    clean_projects, excluded_blacklisted_projects = filter_blacklisted_projects(
        shared_projects,
        project_blacklist_rules,
    )

    projects_to_enrich: list[dict] = []
    seen_project_names: set[str] = set()
    for project_list in (shared_projects[: args.meta_project_limit], clean_projects[: args.meta_project_limit]):
        for project in project_list:
            full_name = project["full_name"]
            if full_name in seen_project_names:
                continue
            seen_project_names.add(full_name)
            projects_to_enrich.append(project)

    enrich_projects(projects_to_enrich, args.token, workers)

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    top_project = shared_projects[0] if shared_projects else None
    clean_top_project = clean_projects[0] if clean_projects else None

    result = {
        "updated_at": updated_at,
        "base_repo": args.base_repo,
        "contributors": {
            "total_including_anonymous": total_contributors,
            "matchable_identities": len(contributors),
            "login_trackable": login_contributors,
            "anonymous_matchable": anonymous_matchable_contributors,
        },
        "scan": {
            "search_pages_per_developer": args.search_pages,
            "workers": workers,
            "rate_reserve": max(0, args.rate_reserve),
            "total_commit_search_calls": total_search_calls,
            "developers_scanned": len(developer_records),
            "last_search_rate_snapshot": rate_snapshot,
            "mirror_detection": {
                "check_limit": max(0, args.mirror_check_limit),
                "score_threshold": max(1, args.mirror_score_threshold),
                "excluded_count": len(excluded_mirror_projects),
                "base_default_branch_head_sha": base_head_sha,
            },
            "project_blacklist": {
                "path": args.project_blacklist,
                "rule_count": len(project_blacklist_rules),
                "excluded_count": len(excluded_blacklisted_projects),
            },
        },
        "top_project": top_project,
        "projects": shared_projects,
        "excluded_mirror_like_projects": excluded_mirror_projects,
        "clean": {
            "top_project": clean_top_project,
            "projects": clean_projects,
            "excluded_blacklisted_projects": excluded_blacklisted_projects,
        },
        "developers": developer_records,
    }

    clean_result = {
        "updated_at": updated_at,
        "base_repo": args.base_repo,
        "contributors": result["contributors"],
        "scan": result["scan"],
        "top_project": clean_top_project,
        "projects": clean_projects,
        "excluded_blacklisted_projects": excluded_blacklisted_projects,
        "excluded_mirror_like_projects": excluded_mirror_projects,
        "developers": developer_records,
    }

    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    logger.success("Saved: {}", args.json_out)

    with open(args.clean_json_out, "w", encoding="utf-8") as fh:
        json.dump(clean_result, fh, ensure_ascii=False, indent=2)
    logger.success("Saved: {}", args.clean_json_out)

    svg = build_svg_chart(
        args.base_repo,
        login_contributors,
        shared_projects,
        updated_at,
        "Dress Developer Orbit",
        "Method: public commit search by Dress contributors' GitHub logins; anonymous contributors are excluded from orbit matching.",
    )
    with open(args.svg_out, "w", encoding="utf-8") as fh:
        fh.write(svg)
    logger.success("Saved: {}", args.svg_out)

    clean_svg = build_svg_chart(
        args.base_repo,
        login_contributors,
        clean_projects,
        updated_at,
        "Dress Developer Orbit (Clean)",
        "Method: public commit search by Dress contributors' GitHub logins, then exclude mirror-like projects and manually blacklisted noise repositories.",
    )
    with open(args.clean_svg_out, "w", encoding="utf-8") as fh:
        fh.write(clean_svg)
    logger.success("Saved: {}", args.clean_svg_out)

    svg_markdown_path = get_markdown_relpath(args.svg_out, args.readme)
    clean_svg_markdown_path = get_markdown_relpath(args.clean_svg_out, args.readme)
    blacklist_markdown_path = get_markdown_relpath(args.project_blacklist, args.readme)

    section = build_readme_section(
        args.base_repo,
        total_contributors,
        len(contributors),
        login_contributors,
        anonymous_matchable_contributors,
        args.search_pages,
        shared_projects,
        clean_projects,
        len(excluded_mirror_projects),
        len(excluded_blacklisted_projects),
        len(project_blacklist_rules),
        max(0, args.mirror_check_limit),
        max(1, args.mirror_score_threshold),
        updated_at,
        svg_markdown_path,
        clean_svg_markdown_path,
        blacklist_markdown_path,
    )
    update_readme(section, args.readme)

    if clean_top_project:
        logger.success(
            "Done - clean top shared project is {} with {} shared Dress developers.",
            clean_top_project["full_name"],
            clean_top_project["shared_developer_count"],
        )
    else:
        logger.success("Done - no de-noised shared public projects were discovered.")


if __name__ == "__main__":
    main()

