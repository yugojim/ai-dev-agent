import argparse
import json
import os
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from scripts.config import settings

load_dotenv()

# 若公司 Redmine 憑證在 WSL 內不被信任，可先暫時用 False
# 正式環境建議改成公司 CA bundle 路徑或 True
VERIFY_SSL = False


def _base_url() -> str:
    base = settings.redmine_base_url.strip().rstrip("/")
    if not base:
        raise RuntimeError("REDMINE_BASE_URL is empty")
    return base


def _api_key() -> str:
    key = settings.redmine_api_key.strip()
    if not key:
        raise RuntimeError("REDMINE_API_KEY is empty")
    return key


def _headers() -> dict[str, str]:
    return {
        "X-Redmine-API-Key": _api_key(),
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, **kwargs) -> requests.Response:
    url = f"{_base_url()}{path}"
    kwargs.setdefault("headers", _headers())
    kwargs.setdefault("verify", VERIFY_SSL)

    resp = requests.request(method, url, **kwargs)
    if resp.status_code >= 400:
        raise RuntimeError(
            f"Redmine API error: {resp.status_code} {resp.text}"
        )
    return resp


def fetch_my_issues(
    limit: int = 100,
    status_id: str = "open",
    assigned_to_id: str = "me",
) -> list[dict[str, Any]]:
    params = {
        "assigned_to_id": assigned_to_id,
        "status_id": status_id,
        "limit": limit,
    }

    resp = _request("GET", "/issues.json", params=params)
    data = resp.json()
    return data.get("issues", [])


def filter_low_priority_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    for issue in issues:
        priority = (issue.get("priority") or {}).get("name", "")
        if priority.lower() == "low":
            result.append(issue)

    return result


def print_issue_summary(issue: dict[str, Any]) -> None:
    issue_id = issue.get("id")
    subject = issue.get("subject", "")
    priority = (issue.get("priority") or {}).get("name", "")
    status = (issue.get("status") or {}).get("name", "")
    assignee = (issue.get("assigned_to") or {}).get("name", "")
    project = (issue.get("project") or {}).get("name", "")

    print(
        f"[#{issue_id}] {subject} | "
        f"project={project} | priority={priority} | "
        f"status={status} | assignee={assignee}"
    )


def print_ai_task_queue() -> None:
    issues = fetch_my_issues()
    low_issues = filter_low_priority_issues(issues)

    print(f"Total my issues: {len(issues)}")
    print(f"Low priority issues: {len(low_issues)}")

    for issue in low_issues:
        print_issue_summary(issue)

    print("\n=== AI TASK QUEUE ===")
    for issue in low_issues:
        issue_id = issue.get("id")
        subject = issue.get("subject", "")
        project = (issue.get("project") or {}).get("name", "")
        priority = (issue.get("priority") or {}).get("name", "")
        print(
            f"[#{issue_id}] {subject} | project={project} | "
            f"priority={priority} | action=triage"
        )


def get_first_low_priority_issue() -> dict[str, Any] | None:
    issues = fetch_my_issues()
    low_issues = filter_low_priority_issues(issues)
    if not low_issues:
        return None
    return low_issues[0]


def get_issue_detail(issue_id: int | str) -> dict[str, Any]:
    params = {
        "include": "attachments,journals,children,relations",
    }
    resp = _request("GET", f"/issues/{issue_id}.json", params=params)
    data = resp.json()
    issue = data.get("issue")
    if not issue:
        raise RuntimeError(f"Issue #{issue_id} not found in Redmine response")
    return issue


def _safe_filename(name: str) -> str:
    bad = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
    safe = name
    for ch in bad:
        safe = safe.replace(ch, "_")
    return safe.strip() or "attachment.bin"


def download_issue_attachments(
    issue_id: int | str,
    target_dir: str | Path,
) -> list[Path]:
    issue = get_issue_detail(issue_id)
    attachments = issue.get("attachments", [])

    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    downloaded: list[Path] = []

    if not attachments:
        print(f"No attachments found for issue #{issue_id}")
        return downloaded

    print(f"Downloading {len(attachments)} attachment(s) for issue #{issue_id}...")

    for att in attachments:
        filename = _safe_filename(att.get("filename", "attachment.bin"))
        content_url = att.get("content_url")
        if not content_url:
            print(f"Skip attachment without content_url: {filename}")
            continue

        file_path = target / filename

        # 有些 Redmine content_url 可能是完整 URL，有些可能是相對路徑
        if content_url.startswith("http://") or content_url.startswith("https://"):
            url = content_url
        else:
            url = f"{_base_url()}{content_url}"

        print(f"- {filename}")
        resp = requests.get(
            url,
            headers={"X-Redmine-API-Key": _api_key()},
            verify=VERIFY_SSL,
            stream=True,
        )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Attachment download failed: {resp.status_code} {resp.text}"
            )

        with open(file_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        downloaded.append(file_path)

    return downloaded


def write_issue_json(issue_id: int | str, output_path: str | Path) -> Path:
    issue = get_issue_detail(issue_id)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(issue, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd", required=False)

    subparsers.add_parser("queue")

    p_detail = subparsers.add_parser("detail")
    p_detail.add_argument("issue_id")

    p_download = subparsers.add_parser("download-attachments")
    p_download.add_argument("issue_id")
    p_download.add_argument("target_dir")

    p_issue_json = subparsers.add_parser("write-issue-json")
    p_issue_json.add_argument("issue_id")
    p_issue_json.add_argument("output_path")

    args = parser.parse_args()

    if not args.cmd or args.cmd == "queue":
        print_ai_task_queue()
        return

    if args.cmd == "detail":
        issue = get_issue_detail(args.issue_id)
        print(json.dumps(issue, ensure_ascii=False, indent=2))
        return

    if args.cmd == "download-attachments":
        files = download_issue_attachments(args.issue_id, args.target_dir)
        print("\nDownloaded files:")
        for f in files:
            print(f"- {f}")
        return

    if args.cmd == "write-issue-json":
        out = write_issue_json(args.issue_id, args.output_path)
        print(f"Wrote issue json to: {out}")
        return


if __name__ == "__main__":
    main()