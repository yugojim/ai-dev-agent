from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from codex_runner import CodexRunner


DEFAULT_CODEX_BIN = os.environ.get("CODEX_BIN", "codex").strip() or "codex"
DEFAULT_CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.4").strip() or "gpt-5.4"
DEFAULT_CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip() or "medium"
DEFAULT_TICKET_REWRITE_ENABLED = os.environ.get("CODEX_PREPARE_TICKET", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _extract_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None

    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _normalize_string_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for item in values:
        if item is None:
            continue
        value = str(item).strip()
        if value:
            normalized.append(value)
    return normalized


def _normalize_steps(values: Any) -> list[dict[str, str]]:
    if not isinstance(values, list):
        return []
    normalized: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict) or len(item) != 1:
            continue
        key, value = next(iter(item.items()))
        key_text = str(key).strip()
        value_text = str(value).strip()
        if key_text and value_text:
            normalized.append({key_text: value_text})
    return normalized


def _rewrite_warnings(parsed: dict[str, Any], raw: dict[str, Any], merged_validation: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    raw_description = str(raw.get("description") or "").strip()
    parsed_summary = str(parsed.get("summary") or "").strip()
    parsed_requirements = _normalize_string_list(parsed.get("requirements"))
    prompt_focus = _normalize_string_list(parsed.get("prompt_focus"))

    if not raw_description:
        warnings.append("Original issue description is empty.")
    if not parsed_summary:
        warnings.append("Codex rewrite did not produce a usable summary.")
    if not parsed_requirements:
        warnings.append("Codex rewrite did not produce actionable requirements.")
    if not (merged_validation.get("steps") or []):
        warnings.append("No structured validation steps were produced.")
    if not (merged_validation.get("expected") or []):
        warnings.append("No explicit expected outcomes were produced.")
    if not prompt_focus:
        warnings.append("No implementation focus notes were produced.")
    return warnings


def _rewrite_quality_ok(parsed: dict[str, Any], merged_validation: dict[str, Any]) -> tuple[bool, str]:
    requirements = _normalize_string_list(parsed.get("requirements"))
    steps = merged_validation.get("steps") or []
    expected = merged_validation.get("expected") or []
    summary = str(parsed.get("summary") or "").strip()

    if not summary:
        return False, "rewrite summary is empty"
    if not requirements:
        return False, "rewrite requirements are empty"
    if not steps and not expected:
        return False, "rewrite validation is too weak"
    return True, "rewrite quality accepted"


def build_prompt_from_issue(issue: dict) -> str:
    subject = issue.get("summary", "") or issue.get("subject", "")
    raw = issue.get("raw", {})
    description = raw.get("description", "")
    requirements = issue.get("requirements", []) or []
    validation = issue.get("validation", {}) or {}
    attachment_files = issue.get("downloaded_attachments", []) or []
    raw_attachments = raw.get("attachments", []) or []
    prompt_focus = issue.get("prompt_focus", []) or []
    rewrite_warnings = issue.get("rewrite_warnings", []) or []

    requirement_lines = (
        "\n".join(f"- {item}" for item in requirements)
        if requirements
        else "- No structured requirements were parsed from the Redmine description."
    )
    validation_lines = "\n".join(
        [
            f"- URL: {validation.get('url', '/')}",
            f"- Role: {validation.get('role', '') or '(not specified)'}",
            f"- Expected: {', '.join(validation.get('expected', [])) or '(none)'}",
            f"- Forbidden: {', '.join(validation.get('forbidden', [])) or '(none)'}",
        ]
    )
    steps = validation.get("steps", []) or []
    step_lines = (
        "\n".join(f"- {json.dumps(step, ensure_ascii=False)}" for step in steps)
        if steps
        else "- No structured steps were parsed from the Redmine description."
    )
    attachment_lines = []
    for path in attachment_files:
        attachment_lines.append(f"- {path}")
    if not attachment_lines:
        for attachment in raw_attachments:
            filename = attachment.get("filename", "")
            if filename:
                attachment_lines.append(
                    f"- attachments/{filename} (referenced by Redmine, may require download)"
                )
    if not attachment_lines:
        attachment_lines.append("- No attachments downloaded.")

    prompt_focus_lines = (
        "\n".join(f"- {item}" for item in prompt_focus)
        if prompt_focus
        else "- No extra implementation focus was generated."
    )
    warning_lines = (
        "\n".join(f"- {item}" for item in rewrite_warnings)
        if rewrite_warnings
        else "- No rewrite warnings."
    )

    return f"""Please read these files first:
- ../task_context/issue.json
- ../task_context/prompt.txt

Task:
Implement Redmine issue #{issue["issue_id"]}.

Issue subject:
{subject}

Issue description:
{description}

Structured requirements:
{requirement_lines}

Validation targets:
{validation_lines}

Validation steps:
{step_lines}

Implementation focus:
{prompt_focus_lines}

Rewrite warnings:
{warning_lines}

Available attachments:
{chr(10).join(attachment_lines)}

Rules:
1. Keep changes minimal and limited to this ticket.
2. Before editing, identify relevant files and logic.
3. Then apply the patch in the repo.
4. Do not commit or push anything.
5. After editing, summarize modified files and suggested verification commands.
6. Do not start long-running servers unless necessary.
7. If UI verification requires login, menu navigation, or role-specific entry points, update `../task_context/issue.json` `validation` fields so Playwright can execute the flow.
8. Keep `validation.steps` as a JSON array of single-key objects such as `{{"open": "/feature"}}`, `{{"click": "text=查詢"}}`, `{{"check": "text=結果清單"}}`.

CRITICAL EXECUTION RULES:

You MUST apply the change by editing real files in the repository.
Do not stop after analysis.
Do not only describe the change.
Do not propose pseudo-code.
You must actually write and apply the patch.
If no file is modified, the task will be considered FAILED.
""".strip()


def build_context_issue(issue: dict, *, use_codex_rewrite: bool | None = None) -> dict:
    result = dict(issue)
    existing_rewrite = result.get("ticket_rewrite", {}) or {}
    if existing_rewrite.get("executed"):
        return result

    enabled = DEFAULT_TICKET_REWRITE_ENABLED if use_codex_rewrite is None else use_codex_rewrite
    if not enabled:
        return result

    raw = result.get("raw", {}) or {}
    rewrite_prompt = f"""Read the Redmine issue below and convert it into a stricter implementation brief.

Return exactly one JSON object. Do not wrap it in markdown. Do not add explanation text.

Required JSON schema:
{{
  "summary": "brief summary for developers",
  "requirements": ["explicit implementation requirement"],
  "validation": {{
    "url": "/",
    "role": "",
    "expected": ["visible text or outcome"],
    "forbidden": ["text or outcome that must not appear"],
    "steps": [{{"open": "/path"}}, {{"click": "text=查詢"}}, {{"check": "text=結果"}}]
  }},
  "prompt_focus": ["important coding focus or ambiguity resolution"],
  "rewrite_warnings": ["uncertainty, ambiguity, or missing information"]
}}

Rules:
- Keep every requirement concrete and code-actionable.
- If the original ticket is vague, infer the most likely implementation intent from the subject, description, and attachments metadata.
- Do not invent APIs, tables, fields, pages, or routes unless strongly implied.
- Prefer preserving the original meaning over adding extra scope.
- If critical information is missing, put that into `rewrite_warnings` instead of making it up.
- `validation.steps` must be a list of single-key objects.
- If information is missing, use conservative defaults instead of hallucinating.

Original issue JSON:
{json.dumps(raw, ensure_ascii=False, indent=2)}
""".strip()

    runner = CodexRunner(
        codex_bin=DEFAULT_CODEX_BIN,
        model=DEFAULT_CODEX_MODEL,
        reasoning_effort=DEFAULT_CODEX_REASONING_EFFORT,
        sandbox_mode="read-only",
    )
    response = runner.run(rewrite_prompt, Path.cwd())
    parsed = _extract_json_object(response.stdout)
    if not parsed:
        result["ticket_rewrite"] = {
            "executed": True,
            "passed": False,
            "returncode": response.returncode,
            "summary": "codex ticket rewrite returned non-json output",
            "stdout": response.stdout,
            "stderr": response.stderr,
        }
        return result

    validation = parsed.get("validation", {}) if isinstance(parsed.get("validation"), dict) else {}
    merged_validation = dict(result.get("validation", {}) or {})
    merged_validation.update(
        {
            "url": str(validation.get("url") or merged_validation.get("url") or "/").strip() or "/",
            "role": str(validation.get("role") or merged_validation.get("role") or "").strip(),
            "expected": _normalize_string_list(validation.get("expected")) or merged_validation.get("expected", []) or [],
            "forbidden": _normalize_string_list(validation.get("forbidden")) or merged_validation.get("forbidden", []) or [],
            "steps": _normalize_steps(validation.get("steps")) or merged_validation.get("steps", []) or [],
        }
    )
    quality_ok, quality_reason = _rewrite_quality_ok(parsed, merged_validation)
    rewrite_warnings = _normalize_string_list(parsed.get("rewrite_warnings"))
    rewrite_warnings.extend(_rewrite_warnings(parsed, raw, merged_validation))
    deduped_warnings: list[str] = []
    seen: set[str] = set()
    for item in rewrite_warnings:
        if item not in seen:
            deduped_warnings.append(item)
            seen.add(item)

    if not quality_ok:
        result["rewrite_warnings"] = deduped_warnings
        result["ticket_rewrite"] = {
            "executed": True,
            "passed": False,
            "returncode": response.returncode,
            "summary": f"codex ticket rewrite rejected: {quality_reason}",
            "stdout": response.stdout,
            "stderr": response.stderr,
        }
        return result

    result["summary"] = str(parsed.get("summary") or result.get("summary") or raw.get("subject") or "").strip()
    result["requirements"] = _normalize_string_list(parsed.get("requirements")) or result.get("requirements", []) or []
    result["validation"] = merged_validation
    result["prompt_focus"] = _normalize_string_list(parsed.get("prompt_focus"))
    result["rewrite_warnings"] = deduped_warnings
    result["ticket_rewrite"] = {
        "executed": True,
        "passed": response.returncode == 0,
        "returncode": response.returncode,
        "summary": f"codex ticket rewrite applied: {quality_reason}",
        "stdout": response.stdout,
        "stderr": response.stderr,
    }
    return result
