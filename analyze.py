#!/usr/bin/env python3
"""Analyze contributor overlap between two GitHub repositories via GitHub REST API."""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def github_request(url: str, token: str) -> tuple[list | dict, str]:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "contributor-overlap-analyzer/1.0")
    with urllib.request.urlopen(req) as resp:
        link_header = resp.headers.get("Link", "")
        return json.loads(resp.read()), link_header


def get_all_contributors(owner_repo: str, token: str) -> tuple[dict[str, int], int]:
    """Return ({login: commit_count}, total_contributors_including_anonymous)."""
    owner, repo = owner_repo.split("/", 1)
    contributors: dict[str, int] = {}
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
            print(f"[error] {owner_repo} contributors page {page}: {exc}", file=sys.stderr)
            break
        if not data:
            break
        for entry in data:
            total_contributors += 1
            login = entry.get("login")
            if login:
                contributors[login] = entry.get("contributions", 0)
        page += 1
    return contributors, total_contributors


def get_repo_meta(owner_repo: str, token: str) -> dict:
    owner, repo = owner_repo.split("/", 1)
    try:
        data, _ = github_request(
            f"https://api.github.com/repos/{owner}/{repo}", token
        )
        return {
            "full_name": data.get("full_name", owner_repo),
            "description": data.get("description", ""),
            "stargazers_count": data.get("stargazers_count", 0),
            "forks_count": data.get("forks_count", 0),
        }
    except urllib.error.HTTPError:
        return {"full_name": owner_repo}


def build_readme_section(
    left_repo: str,
    right_repo: str,
    left_total_all: int,
    right_total_all: int,
    left_total_login: int,
    right_total_login: int,
    shared: list[dict],
    updated_at: str,
) -> str:
    overlap = len(shared)
    left_ratio = overlap / left_total_login * 100 if left_total_login else 0.0
    right_ratio = overlap / right_total_login * 100 if right_total_login else 0.0

    left_name = left_repo.split("/")[1]
    right_name = right_repo.split("/")[1]

    lines: list[str] = []
    lines.append("## 分析结果\n")
    lines.append(f"> 最后更新：{updated_at}\n")
    lines.append("### 仓库概况\n")
    lines.append(
        f"| 指标 | [{left_repo}](https://github.com/{left_repo})"
        f" | [{right_repo}](https://github.com/{right_repo}) |"
    )
    lines.append("|:--|--:|--:|")
    lines.append(f"| 贡献者总数（含匿名） | {left_total_all} | {right_total_all} |")
    lines.append(f"| 可匹配 GitHub 登录名贡献者数 | {left_total_login} | {right_total_login} |")
    lines.append(f"| 重叠贡献者数 | {overlap} | {overlap} |")
    lines.append(
        f"| 占可匹配登录名贡献者比例 | {left_ratio:.2f}% | {right_ratio:.2f}% |"
    )

    if shared:
        lines.append("\n### 重叠贡献者明细\n")
        lines.append(
            f"| GitHub 用户 | 在 {left_name} 的提交数"
            f" | 在 {right_name} 的提交数 | 合计 |"
        )
        lines.append("|:--|--:|--:|--:|")
        for c in shared:
            login = c["login"]
            lines.append(
                f"| [@{login}](https://github.com/{login})"
                f" | {c['left_commits']}"
                f" | {c['right_commits']}"
                f" | {c['combined_commits']} |"
            )
    else:
        lines.append("\n> 未发现共同贡献者。\n")

    return "\n".join(lines)


def update_readme(section: str, readme_path: str) -> None:
    START = "<!-- ANALYSIS_START -->"
    END = "<!-- ANALYSIS_END -->"
    block = f"{START}\n{section}\n{END}"

    try:
        content = readme_path and open(readme_path, encoding="utf-8").read() or ""
    except FileNotFoundError:
        content = ""

    if START in content:
        content = re.sub(
            rf"{re.escape(START)}.*?{re.escape(END)}",
            block,
            content,
            flags=re.DOTALL,
        )
    else:
        content = content.rstrip("\n") + "\n\n" + block + "\n"

    with open(readme_path, "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"README updated: {readme_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze contributor overlap between two GitHub repos."
    )
    parser.add_argument(
        "left_repo",
        nargs="?",
        default="Cute-Dress/Dress",
        help="Left repo in owner/repo format (default: Cute-Dress/Dress)",
    )
    parser.add_argument(
        "right_repo",
        nargs="?",
        default="OI-wiki/OI-wiki",
        help="Right repo in owner/repo format (default: OI-wiki/OI-wiki)",
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
    args = parser.parse_args()

    if not args.token:
        print(
            "Warning: GITHUB_TOKEN not set — unauthenticated requests are rate-limited to 60/hour.",
            file=sys.stderr,
        )

    print(f"Fetching contributors: {args.left_repo} …")
    left_contributors, left_total_all = get_all_contributors(args.left_repo, args.token)
    print(
        f"  {left_total_all} contributors found (including anonymous), "
        f"{len(left_contributors)} with GitHub login"
    )

    print(f"Fetching contributors: {args.right_repo} …")
    right_contributors, right_total_all = get_all_contributors(args.right_repo, args.token)
    print(
        f"  {right_total_all} contributors found (including anonymous), "
        f"{len(right_contributors)} with GitHub login"
    )

    shared_logins = set(left_contributors) & set(right_contributors)
    shared = sorted(
        [
            {
                "login": login,
                "left_commits": left_contributors[login],
                "right_commits": right_contributors[login],
                "combined_commits": left_contributors[login] + right_contributors[login],
            }
            for login in shared_logins
        ],
        key=lambda x: x["combined_commits"],
        reverse=True,
    )

    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    result = {
        "updated_at": updated_at,
        "left_repo": {
            "full_name": args.left_repo,
            "contributors_total_including_anonymous": left_total_all,
            "contributors_with_login": len(left_contributors),
        },
        "right_repo": {
            "full_name": args.right_repo,
            "contributors_total_including_anonymous": right_total_all,
            "contributors_with_login": len(right_contributors),
        },
        "overlap": {
            "shared_count": len(shared),
            "left_overlap_ratio": len(shared) / len(left_contributors) if left_contributors else 0,
            "right_overlap_ratio": len(shared) / len(right_contributors) if right_contributors else 0,
            "shared": shared,
        },
    }

    with open("contributor_overlap.json", "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print("Saved: contributor_overlap.json")

    section = build_readme_section(
        args.left_repo,
        args.right_repo,
        left_total_all,
        right_total_all,
        len(left_contributors),
        len(right_contributors),
        shared,
        updated_at,
    )
    update_readme(section, args.readme)

    print(
        f"\nDone — {len(shared)} shared contributor(s) out of "
        f"{len(left_contributors)} / {len(right_contributors)} "
        f"(login-matchable contributors)"
    )


if __name__ == "__main__":
    main()
