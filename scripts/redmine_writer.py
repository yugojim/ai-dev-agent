# scripts/redmine_writer.py
from __future__ import annotations

import json
import mimetypes
import os
import requests
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional



DEFAULT_TIMEOUT = 60


class RedmineWriterError(RuntimeError):
    pass


@dataclass
class RedmineConfig:
    base_url: str
    api_key: str
    verify_ssl: bool = True
    timeout: int = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "RedmineConfig":
        base_url = os.environ.get("REDMINE_BASE_URL", "").strip().rstrip("/")
        api_key = os.environ.get("REDMINE_API_KEY", "").strip()

        if not base_url:
            raise RedmineWriterError("Missing REDMINE_BASE_URL")
        if not api_key:
            raise RedmineWriterError("Missing REDMINE_API_KEY")

        verify_ssl_raw = os.environ.get("REDMINE_VERIFY_SSL", "true").strip().lower()
        verify_ssl = verify_ssl_raw not in {"0", "false", "no"}

        timeout_raw = os.environ.get("REDMINE_TIMEOUT", str(DEFAULT_TIMEOUT)).strip()
        try:
            timeout = int(timeout_raw)
        except ValueError:
            timeout = DEFAULT_TIMEOUT

        return cls(
            base_url=base_url,
            api_key=api_key,
            verify_ssl=verify_ssl,
            timeout=timeout,
        )


class RedmineWriter:
    def __init__(self, config: RedmineConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "X-Redmine-API-Key": self.config.api_key,
        })

    def _url(self, path: str) -> str:
        return f"{self.config.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        data=None,
        headers: Optional[dict] = None,
        expected=(200, 201, 204),
    ):
        url = self._url(path)
        resp = self.session.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            data=data,
            headers=headers,
            timeout=self.config.timeout,
            verify=self.config.verify_ssl,
        )
        if resp.status_code not in expected:
            raise RedmineWriterError(
                f"Redmine API error {resp.status_code} for {method} {url}: {resp.text}"
            )

        if resp.status_code == 204 or not resp.text.strip():
            return {}

        content_type = resp.headers.get("Content-Type", "")
        if "application/json" in content_type:
            return resp.json()
        return resp.text

    def upload_file(self, file_path: str | Path) -> dict:
        file_path = Path(file_path)
        if not file_path.exists():
            raise RedmineWriterError(f"Attachment file not found: {file_path}")

        content_type, _ = mimetypes.guess_type(str(file_path))
        if not content_type:
            content_type = "application/octet-stream"

        raw = file_path.read_bytes()

        # Redmine uploads.json on this server expects raw octet-stream body,
        # not multipart/form-data.
        result = self._request(
            "POST",
            "/uploads.json",
            params={"filename": file_path.name},
            data=raw,
            headers={"Content-Type": "application/octet-stream"},
            expected=(201,),
        )

        upload = result.get("upload")
        if not upload or not upload.get("token"):
            raise RedmineWriterError(f"Redmine upload token missing for {file_path}")

        return {
            "token": upload["token"],
            "filename": file_path.name,
            "content_type": content_type,
        }

    def upload_files(self, file_paths: Iterable[str | Path]) -> list[dict]:
        uploads = []
        for path in file_paths:
            uploads.append(self.upload_file(path))
        return uploads

    def update_issue(
        self,
        issue_id: int | str,
        *,
        notes: str = "",
        status_id: Optional[int] = None,
        priority_id: Optional[int] = None,
        uploads: Optional[list[dict]] = None,
    ) -> dict:
        issue_payload: dict = {}

        if notes:
            issue_payload["notes"] = notes
        if status_id is not None:
            issue_payload["status_id"] = int(status_id)
        if priority_id is not None:
            issue_payload["priority_id"] = int(priority_id)
        if uploads:
            issue_payload["uploads"] = uploads

        payload = {"issue": issue_payload}

        return self._request(
            "PUT",
            f"/issues/{issue_id}.json",
            json_body=payload,
            expected=(204,),
        )

    def get_issue(self, issue_id: int | str) -> dict:
        return self._request("GET", f"/issues/{issue_id}.json", expected=(200,))


def load_json_file(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def find_latest_runtime_log(workspace_dir: str | Path) -> Optional[Path]:
    workspace_dir = Path(workspace_dir)
    runtime_logs_dir = workspace_dir / "report" / "runtime_logs"
    if not runtime_logs_dir.exists():
        return None
    candidates = sorted(
        runtime_logs_dir.glob("*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def find_screenshots(workspace_dir: str | Path) -> list[Path]:
    workspace_dir = Path(workspace_dir)
    candidates: list[Path] = []

    search_dirs = [
        workspace_dir / "report" / "screenshots",
        workspace_dir / "test_results",
        workspace_dir / "runtime",
    ]

    for base in search_dirs:
        if not base.exists():
            continue
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            candidates.extend(base.rglob(ext))

    unique = {}
    for p in candidates:
        unique[str(p.resolve())] = p

    result = list(unique.values())
    result.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return result


def build_agent_comment(
    issue_id: int | str,
    workspace_dir: str | Path,
    report_json_path: str | Path,
    report_md_path: str | Path,
    screenshot_paths: Optional[list[str | Path]] = None,
) -> str:
    report = load_json_file(report_json_path)
    attempt_count = report.get("attempt_count", "")
    final_passed = report.get("final_passed", "")
    mode = report.get("mode", "")
    generated_at = report.get("generated_at", "")

    lines: list[str] = []
    lines.append(f"Agent execution update for issue #{issue_id}")
    lines.append("")
    lines.append(f"- mode: {mode}")
    lines.append(f"- attempt_count: {attempt_count}")
    lines.append(f"- final_passed: {final_passed}")
    lines.append(f"- generated_at: {generated_at}")

    attempts = report.get("attempts", [])
    if attempts:
        last_attempt = attempts[-1]
        codex = last_attempt.get("codex", {})
        build = last_attempt.get("build", {})
        runtime = last_attempt.get("runtime", {})

        modified_files = codex.get("modified_files", []) or []
        if modified_files:
            lines.append("")
            lines.append("Modified files:")
            for f in modified_files[:20]:
                lines.append(f"- {f}")
            if len(modified_files) > 20:
                lines.append(f"- ... and {len(modified_files) - 20} more")

        lines.append("")
        lines.append("Execution summary:")
        lines.append(f"- codex_returncode: {codex.get('returncode', '')}")
        lines.append(f"- build_passed: {build.get('passed', '')}")
        lines.append(f"- build_returncode: {build.get('returncode', '')}")
        lines.append(f"- runtime_ready: {runtime.get('ready', '')}")
        lines.append(f"- runtime_passed: {runtime.get('passed', '')}")
        if runtime.get("base_url"):
            lines.append(f"- runtime_base_url: {runtime.get('base_url')}")
        if runtime.get("health_url"):
            lines.append(f"- runtime_health_url: {runtime.get('health_url')}")

    screenshot_paths = screenshot_paths or []
    if screenshot_paths:
        lines.append("")
        lines.append("Attached screenshots:")
        for s in screenshot_paths[:10]:
            lines.append(f"- {Path(s).name}")

    lines.append("")
    lines.append("Attachments uploaded by agent:")
    if Path(report_md_path).exists():
        lines.append(f"- {Path(report_md_path).name}")
    if Path(report_json_path).exists():
        lines.append(f"- {Path(report_json_path).name}")
    if screenshot_paths:
        for s in screenshot_paths[:10]:
            lines.append(f"- {Path(s).name}")

    return "\n".join(lines).strip()


def attach_and_update_from_workspace(
    *,
    issue_id: int | str,
    workspace_dir: str | Path,
    status_id: Optional[int] = None,
    priority_id: Optional[int] = None,
    include_report_json: bool = True,
    include_report_md: bool = True,
    include_screenshots: bool = True,
    include_latest_runtime_log: bool = False,
) -> dict:
    workspace_dir = Path(workspace_dir)
    report_json_path = workspace_dir / "report" / "agent_report.json"
    report_md_path = workspace_dir / "report" / "agent_report.md"

    files_to_upload: list[Path] = []

    if include_report_md and report_md_path.exists():
        files_to_upload.append(report_md_path)

    if include_report_json and report_json_path.exists():
        files_to_upload.append(report_json_path)

    screenshot_paths: list[Path] = []
    if include_screenshots:
        screenshot_paths = find_screenshots(workspace_dir)
        files_to_upload.extend(screenshot_paths[:10])

    if include_latest_runtime_log:
        latest_runtime_log = find_latest_runtime_log(workspace_dir)
        if latest_runtime_log:
            files_to_upload.append(latest_runtime_log)

    if not files_to_upload:
        raise RedmineWriterError(
            f"No files found to upload under workspace: {workspace_dir}"
        )

    cfg = RedmineConfig.from_env()
    writer = RedmineWriter(cfg)

    uploads = writer.upload_files(files_to_upload)

    notes = build_agent_comment(
        issue_id=issue_id,
        workspace_dir=workspace_dir,
        report_json_path=report_json_path,
        report_md_path=report_md_path,
        screenshot_paths=screenshot_paths,
    )

    writer.update_issue(
        issue_id=issue_id,
        notes=notes,
        status_id=status_id,
        priority_id=priority_id,
        uploads=uploads,
    )

    return {
        "issue_id": str(issue_id),
        "uploaded_files": [str(p) for p in files_to_upload],
        "status_id": status_id,
        "priority_id": priority_id,
        "notes_preview": notes[:1000],
    }