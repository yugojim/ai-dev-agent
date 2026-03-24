from __future__ import annotations

import os
import re
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


# ---------- Spec Parser ----------

def _parse_list_block(text: str) -> list[str]:
    items = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r"^[-*\d\.\s]+", "", line)
        if line:
            items.append(line)
    return items


def _parse_steps(text: str) -> list[dict]:
    steps = []
    for line in text.splitlines():
        line = line.strip()
        line = re.sub(r"^\d+\.\s*", "", line)
        if "=" in line:
            k, v = line.split("=", 1)
            steps.append({k.strip(): v.strip()})
    return steps


def parse_description(desc: str) -> dict:
    desc = desc or ""

    def block(name):
        m = re.search(rf"\[{name}\](.*?)(?:\n\[|\Z)", desc, re.S)
        return m.group(1).strip() if m else ""

    requirements = _parse_list_block(block("Requirements"))

    validation_text = block("Validation")
    validation = {
        "url": "/",
        "role": "",
        "expected": [],
        "forbidden": [],
        "steps": []
    }

    for line in validation_text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip().lower()
        v = v.strip()

        if k == "url":
            validation["url"] = v
        elif k == "role":
            validation["role"] = v
        elif k == "expected":
            validation["expected"] = [x.strip() for x in v.split(",") if x.strip()]
        elif k == "forbidden":
            validation["forbidden"] = [x.strip() for x in v.split(",") if x.strip()]

    validation["steps"] = _parse_steps(block("Steps"))

    return {
        "requirements": requirements,
        "validation": validation,
    }


# ---------- Public API ----------

def get_first_low_priority_issue() -> dict | None:
    issues = fetch_my_issues(limit=50)

    for issue in issues:
        priority = issue.get("priority", {}) or {}
        priority_name = (priority.get("name") or "").strip().lower()

        if priority_name == "low":
            parsed = parse_description(issue.get("description", ""))

            return {
                "id": issue["id"],
                "issue_id": str(issue["id"]),
                "summary": issue.get("subject", ""),
                "requirements": parsed["requirements"],
                "validation": parsed["validation"],
                "raw": issue,
            }

    return None


if __name__ == "__main__":
    issue = get_first_low_priority_issue()
    print(issue)