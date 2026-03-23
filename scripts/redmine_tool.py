from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()


REDMINE_BASE_URL = os.environ.get("REDMINE_BASE_URL", "").strip().rstrip("/")
REDMINE_API_KEY = os.environ.get("REDMINE_API_KEY", "").strip()
REDMINE_VERIFY_SSL = os.environ.get("REDMINE_VERIFY_SSL", "true").strip().lower() not in {
    "0", "false", "no"
}


class RedmineToolError(RuntimeError):
    pass


def _request(method: str, path: str, *, params: dict[str, Any] | None = None) -> dict:
    if not REDMINE_BASE_URL:
        raise RedmineToolError("Missing REDMINE_BASE_URL")
    if not REDMINE_API_KEY:
        raise RedmineToolError("Missing REDMINE_API_KEY")

    url = f"{REDMINE_BASE_URL}{path}"
    resp = requests.request(
        method=method,
        url=url,
        params=params,
        headers={"X-Redmine-API-Key": REDMINE_API_KEY},
        timeout=60,
        verify=REDMINE_VERIFY_SSL,
    )

    if resp.status_code != 200:
        raise RedmineToolError(
            f"Redmine API error {resp.status_code} for {method} {url}: {resp.text}"
        )

    return resp.json()


def fetch_my_issues(limit: int = 20) -> list[dict]:
    """
    抓目前指派給 API key 所屬使用者的 issue。
    """
    data = _request(
        "GET",
        "/issues.json",
        params={
            "assigned_to_id": "me",
            "status_id": "open",
            "sort": "priority:desc,updated_on:asc",
            "limit": str(limit),
        },
    )
    return data.get("issues", [])


def get_first_low_priority_issue() -> dict | None:
    """
    抓第一張 priority = Low 且 assigned_to = me 的 ticket
    """
    issues = fetch_my_issues(limit=50)

    for issue in issues:
        priority = issue.get("priority", {}) or {}
        priority_name = (priority.get("name") or "").strip().lower()
        if priority_name == "low":
            return {
                "id": issue["id"],
                "issue_id": str(issue["id"]),
                "subject": issue.get("subject", ""),
                "raw": issue,
            }

    return None


if __name__ == "__main__":
    issue = get_first_low_priority_issue()
    print(issue)