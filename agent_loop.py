from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from codex_runner import CodexRunner
from dotenv import load_dotenv
from scripts.config import resolve_workspace_base_dir
from scripts.redmine_tool import (
    build_issue_payload,
    download_issue_attachments,
    get_first_low_priority_issue,
    get_issue_detail,
)
from scripts.workspace import get_issue_branch

load_dotenv()

DEFAULT_WORKSPACE_ROOT = resolve_workspace_base_dir(os.environ.get("WORKSPACE_BASE_DIR", ""))

DEFAULT_REPORT_STATUS_ID = os.environ.get("REDMINE_STATUS_ID_IN_PROGRESS", "2").strip()
DEFAULT_REPORT_PRIORITY_ID = os.environ.get("REDMINE_PRIORITY_ID_NORMAL", "4").strip()

DEFAULT_REPO_SSH = os.environ.get("REPO_SSH_URL", "").strip()
DEFAULT_REPO_HTTPS = os.environ.get("REPO_URL", "").strip()
DEFAULT_AGENT_SOURCE_REPO = os.environ.get("AGENT_SOURCE_REPO", "").strip()
DEFAULT_REPO_GIT_URL = (DEFAULT_REPO_SSH or DEFAULT_REPO_HTTPS).strip()
APP_START_TIMEOUT = int(os.environ.get("APP_START_TIMEOUT", "600").strip())
HEALTHCHECK_INTERVAL = int(os.environ.get("HEALTHCHECK_INTERVAL", "3").strip())
DEFAULT_APP_PORT = int(os.environ.get("APP_PORT", "8080").strip())


def gradle_command(repo_dir: Path, *args: str) -> list[str]:
    if os.name == "nt":
        wrapper = repo_dir / "gradlew.bat"
        if wrapper.exists():
            return [str(wrapper), *args]
    else:
        wrapper = repo_dir / "gradlew"
        if wrapper.exists():
            return [str(wrapper), *args]

    return ["gradle", *args]


def normalize_repo_git_url() -> str:
    """
    優先順序：
    1. REPO_SSH_URL
    2. REPO_URL
    """
    repo = DEFAULT_REPO_GIT_URL

    if repo.startswith("https:/") and not repo.startswith("https://"):
        print(f"[debug] 自動修正網址斜線: {repo}")
        repo = repo.replace("https:/", "https://", 1)

    return repo


DEFAULT_REPO_GIT_URL = normalize_repo_git_url()
DEFAULT_PLAYWRIGHT_TEST_LOGIN_USERNAME = os.environ.get(
    "PLAYWRIGHT_TEST_LOGIN_USERNAME", "tester"
).strip() or "tester"
DEFAULT_PLAYWRIGHT_TEST_LOGIN_CHINESE_NAME = os.environ.get(
    "PLAYWRIGHT_TEST_LOGIN_CHINESE_NAME", "測試使用者"
).strip() or "測試使用者"
DEFAULT_CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.4").strip() or "gpt-5.4"
DEFAULT_CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "medium").strip() or "medium"
DEFAULT_CODEX_BIN = os.environ.get("CODEX_BIN", "codex").strip() or "codex"

AGENT_STEPS = [
    "FETCH_ISSUE",
    "PREPARE_WORKSPACE",
    "PREPARE_TASK_CONTEXT",
    "PREPARE_REPO",
    "RUN_CODEX",
    "RUN_BUILD",
    "RUN_RUNTIME",
    "GIT_COMMIT_PUSH",
    "WRITE_REPORT",
    "UPDATE_REDMINE",
    "DONE",
]


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def set_step(report_data: dict, step: str, detail: str = ""):
    report_data["current_step"] = step
    report_data["current_step_detail"] = detail
    report_data["updated_at"] = now_iso()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("=" * 80, flush=True)
    print(f"[agent][{ts}] CURRENT STEP: {step}", flush=True)
    if detail:
        print(f"[agent][{ts}] DETAIL      : {detail}", flush=True)
    print("=" * 80, flush=True)


def emit_completion_signal(issue_id: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print("\a", end="", flush=True)
    print("=" * 80, flush=True)
    print(f"[agent][{ts}] ISSUE COMPLETED SIGNAL: #{issue_id}", flush=True)
    print("=" * 80, flush=True)


def write_json(path: str | Path, data: dict):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def tail_text(text: str, max_lines: int = 120) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:])


def tail_file(path: str | Path, max_lines: int = 120) -> str:
    path = Path(path)
    if not path.exists():
        return ""
    return tail_text(path.read_text(encoding="utf-8", errors="ignore"), max_lines=max_lines)


def write_md(path: str | Path, report_data: dict):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# 執行報告")
    lines.append("")
    lines.append("## 處理結果")
    lines.append("")
    lines.append(f"- Issue 編號: {report_data.get('issue_id', '')}")
    lines.append(f"- 執行模式: {report_data.get('mode', '')}")
    lines.append(f"- 嘗試次數: {report_data.get('attempt_count', '')}")
    lines.append(f"- 最終是否通過: {report_data.get('final_passed', '')}")
    lines.append(f"- 報告產生時間: {report_data.get('generated_at', '')}")
    lines.append(f"- 目前步驟: {report_data.get('current_step', '')}")
    lines.append(f"- 步驟說明: {report_data.get('current_step_detail', '')}")
    lines.append("")

    modified_files = report_data.get("modified_files", []) or []
    if modified_files:
        lines.append("## Codex 變更檔案")
        lines.append("")
        for f in modified_files:
            lines.append(f"- `{f}`")
        lines.append("")

    attempts = report_data.get("attempts", [])
    for i, attempt in enumerate(attempts, 1):
        lines.append(f"## 第 {i} 次嘗試")
        lines.append("")

        codex = attempt.get("codex", {})
        build = attempt.get("build", {})
        runtime = attempt.get("runtime", {})

        lines.append("### Codex")
        lines.append("")
        lines.append(f"- 是否執行: {codex.get('executed', '')}")
        lines.append(f"- 是否通過: {codex.get('passed', '')}")
        lines.append(f"- 返回碼: {codex.get('returncode', '')}")
        lines.append(f"- 摘要: {codex.get('summary', '')}")
        codex_modified_files = codex.get("modified_files", []) or []
        if codex_modified_files:
            lines.append("")
            lines.append("#### Codex 變更檔案")
            lines.append("")
            for f in codex_modified_files:
                lines.append(f"- `{f}`")
        lines.append("")

        lines.append("### 建置")
        lines.append("")
        lines.append(f"- 是否執行: {build.get('executed', '')}")
        lines.append(f"- 是否通過: {build.get('passed', '')}")
        lines.append(f"- 返回碼: {build.get('returncode', '')}")
        lines.append(f"- 分類: {build.get('classification', '')}")
        lines.append(f"- 摘要: {build.get('summary', '')}")
        if build.get("log_tail"):
            lines.append("")
            lines.append("```text")
            lines.append(build["log_tail"])
            lines.append("```")
        lines.append("")

        lines.append("### 執行驗證")
        lines.append("")
        lines.append(f"- 是否執行: {runtime.get('executed', '')}")
        lines.append(f"- 是否就緒: {runtime.get('ready', '')}")
        lines.append(f"- 是否通過: {runtime.get('passed', '')}")
        lines.append(f"- Port: {runtime.get('port', '')}")
        lines.append(f"- Base URL: {runtime.get('base_url', '')}")
        lines.append(f"- Health URL: {runtime.get('health_url', '')}")
        lines.append(f"- 執行記錄: {runtime.get('runtime_log', '')}")
        lines.append(f"- 截圖: {runtime.get('screenshot', '')}")
        lines.append(f"- Console Log: {runtime.get('console_log', '')}")
        lines.append(f"- 摘要: {runtime.get('summary', '')}")
        if runtime.get("log_tail"):
            lines.append("")
            lines.append("```text")
            lines.append(runtime["log_tail"])
            lines.append("```")
        lines.append("")

        git = attempt.get("git", {})
        if git:
            lines.append("### Git")
            lines.append("")
            lines.append(f"- 是否執行: {git.get('executed', '')}")
            lines.append(f"- 是否通過: {git.get('passed', '')}")
            lines.append(f"- 分支: {git.get('branch', '')}")
            lines.append(f"- Commit 訊息: {git.get('commit_message', '')}")
            lines.append(f"- 摘要: {git.get('summary', '')}")
            if git.get("log_tail"):
                lines.append("")
                lines.append("```text")
                lines.append(git["log_tail"])
                lines.append("```")
            lines.append("")

    redmine = report_data.get("redmine_post_update", {})
    if redmine:
        lines.append("## Redmine 回寫結果")
        lines.append("")
        lines.append(f"- 是否執行: {redmine.get('executed', '')}")
        lines.append(f"- 是否通過: {redmine.get('passed', '')}")
        lines.append(f"- 返回碼: {redmine.get('returncode', '')}")
        lines.append(f"- 錯誤: {redmine.get('error', '')}")
        lines.append("")

    if report_data.get("error"):
        lines.append("## 未完成原因 / 錯誤")
        lines.append("")
        lines.append("```text")
        lines.append(report_data["error"])
        lines.append("```")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_prompt_from_issue(issue: dict) -> str:
    subject = issue.get("summary", "") or issue.get("subject", "")
    raw = issue.get("raw", {})
    description = raw.get("description", "")
    requirements = issue.get("requirements", []) or []
    validation = issue.get("validation", {}) or {}
    attachment_files = issue.get("downloaded_attachments", []) or []
    raw_attachments = raw.get("attachments", []) or []

    requirement_lines = "\n".join(f"- {item}" for item in requirements) if requirements else "- No structured requirements were parsed from the Redmine description."
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
                attachment_lines.append(f"- attachments/{filename} (referenced by Redmine, may require download)")
    if not attachment_lines:
        attachment_lines.append("- No attachments downloaded.")

    return f"""Please read these files first:
- task_context/issue.json
- task_context/prompt.txt

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

Available attachments:
{chr(10).join(attachment_lines)}

Rules:
1. Keep changes minimal and limited to this ticket.
2. Before editing, identify relevant files and logic.
3. Then apply the patch in the repo.
4. Do not commit or push anything.
5. After editing, summarize modified files and suggested verification commands.
6. Do not start long-running servers unless necessary.

CRITICAL EXECUTION RULES:

You MUST apply the change by editing real files in the repository.
Do not stop after analysis.
Do not only describe the change.
Do not propose pseudo-code.
You must actually write and apply the patch.
If no file is modified, the task will be considered FAILED.
""".strip()


def prepare_attachments(issue_id: str, workspace_dir: Path) -> list[str]:
    attachments_dir = workspace_dir / "attachments"
    downloaded = download_issue_attachments(issue_id, attachments_dir)
    return [str(Path(path).resolve()) for path in downloaded]


def prepare_workspace(issue_id: str) -> Path:
    workspace_dir = DEFAULT_WORKSPACE_ROOT / f"issue-{issue_id}"
    (workspace_dir / "attachments").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "repo").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "runtime").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "task_context").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "report").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "report" / "runtime_logs").mkdir(parents=True, exist_ok=True)
    (workspace_dir / "report" / "screenshots").mkdir(parents=True, exist_ok=True)
    return workspace_dir


def prepare_task_context(issue: dict, workspace_dir: Path):
    issue_json_path = workspace_dir / "task_context" / "issue.json"
    prompt_txt_path = workspace_dir / "task_context" / "prompt.txt"

    issue_json_path.write_text(
        json.dumps(issue, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    prompt_txt_path.write_text(build_prompt_from_issue(issue), encoding="utf-8")


def prepare_repo(workspace_dir: Path, issue_id: str) -> dict:
    repo_dir = workspace_dir / "repo"
    branch_name = get_issue_branch(issue_id)
    clone_url = DEFAULT_REPO_SSH or DEFAULT_REPO_GIT_URL
    logs: list[str] = []

    def run_git(args: list[str], cwd: Path | None = None, check: bool = True):
        workdir = cwd or repo_dir
        proc = subprocess.run(
            ["git", *args],
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
        )
        logs.append(f"$ git {' '.join(args)}")
        if proc.stdout:
            logs.append(proc.stdout.strip())
        if proc.stderr:
            logs.append(proc.stderr.strip())
        if check and proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or f"git {' '.join(args)} failed")
        return proc

    try:
        if not clone_url:
            raise RuntimeError("REPO_SSH_URL is not configured")

        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)

        run_git(["clone", clone_url, str(repo_dir)], cwd=workspace_dir)
        run_git(["switch", "develop"])
        run_git(["fetch", "origin"])

        checkout_result = run_git(["checkout", "-b", branch_name], check=False)
        if checkout_result.returncode != 0:
            exists = run_git(["branch", "--list", branch_name], check=False)
            if branch_name in (exists.stdout or ""):
                run_git(["switch", branch_name])
            else:
                raise RuntimeError(checkout_result.stderr or checkout_result.stdout or "git checkout -b failed")

        return {
            "executed": True,
            "passed": True,
            "branch": branch_name,
            "summary": "git clone, switch develop, fetch origin, checkout issue branch completed",
            "log_tail": tail_text("\n".join(logs)),
        }
    except Exception as e:
        return {
            "executed": True,
            "passed": False,
            "branch": branch_name,
            "summary": str(e),
            "log_tail": tail_text("\n".join(logs)),
        }


def detect_project_type(repo_dir: Path) -> str:
    if (repo_dir / "pom.xml").exists():
        return "maven"
    if (
        (repo_dir / "gradlew").exists()
        or (repo_dir / "gradlew.bat").exists()
        or (repo_dir / "build.gradle").exists()
        or (repo_dir / "build.gradle.kts").exists()
    ):
        return "gradle"
    if (repo_dir / "package.json").exists():
        return "node"
    return "unknown"


def http_ok(url: str) -> bool:
    try:
        req = Request(url, headers={"User-Agent": "agent-loop"})
        with urlopen(req, timeout=5) as resp:
            body = resp.read(512).decode("utf-8", errors="ignore").lower()
            if "mongodb over http on the native driver port" in body:
                return False
            return 200 <= resp.status < 400
    except (URLError, HTTPError, TimeoutError):
        return False


def copy_runtime_log_to_report(workspace_dir: Path, log_file: Path) -> str:
    report_log = workspace_dir / "report" / "runtime_logs" / log_file.name
    if log_file.exists():
        shutil.copy2(log_file, report_log)
    return str(report_log)


def collect_modified_files(repo_dir: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []

    modified_files: list[str] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if path:
            modified_files.append(path)
    return modified_files


def ensure_issue_branch(repo_dir: Path, issue_id: str) -> dict:
    branch_name = get_issue_branch(issue_id)
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if (current.stdout or "").strip() == branch_name:
        return {"executed": True, "passed": True, "branch": branch_name, "summary": "already on issue branch"}

    exists = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if branch_name in (exists.stdout or ""):
        proc = subprocess.run(
            ["git", "switch", branch_name],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        return {
            "executed": True,
            "passed": proc.returncode == 0,
            "branch": branch_name,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "summary": "switched to existing issue branch" if proc.returncode == 0 else "failed to switch issue branch",
        }

    proc = subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "executed": True,
        "passed": proc.returncode == 0,
        "branch": branch_name,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "summary": "created issue branch" if proc.returncode == 0 else "failed to create issue branch",
    }


def run_git_phase(workspace_dir: Path, issue_id: str) -> dict:
    repo_dir = workspace_dir / "repo"
    branch_name = get_issue_branch(issue_id)
    commit_message = branch_name
    logs: list[str] = []

    def run_git(args: list[str], check: bool = True):
        proc = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        logs.append(f"$ git {' '.join(args)}")
        if proc.stdout:
            logs.append(proc.stdout.strip())
        if proc.stderr:
            logs.append(proc.stderr.strip())
        if check and proc.returncode != 0:
            raise RuntimeError(proc.stderr or proc.stdout or f"git {' '.join(args)} failed")
        return proc

    try:
        branch_result = ensure_issue_branch(repo_dir, issue_id)
        logs.append(branch_result.get("summary", ""))
        if not branch_result.get("passed", False):
            raise RuntimeError(branch_result.get("stderr") or branch_result.get("summary") or "failed to prepare branch")

        run_git(["add", "."])
        commit_result = run_git(["commit", "-m", commit_message], check=False)
        commit_stdout = (commit_result.stdout or "").lower()
        commit_stderr = (commit_result.stderr or "").lower()
        if commit_result.returncode != 0 and "nothing to commit" not in commit_stdout and "nothing to commit" not in commit_stderr:
            raise RuntimeError(commit_result.stderr or commit_result.stdout or "git commit failed")

        run_git(["fetch", "origin"], check=False)
        rebase_result = run_git(["rebase", "origin/develop"], check=False)
        if rebase_result.returncode != 0:
            raise RuntimeError(
                "git rebase origin/develop failed. Resolve conflicts, then run: git add . ; git rebase --continue"
            )

        run_git(["push", "-u", "origin", branch_name])
        return {
            "executed": True,
            "passed": True,
            "branch": branch_name,
            "commit_message": commit_message,
            "summary": "git add/commit/rebase/push completed",
            "log_tail": tail_text("\n".join(logs)),
        }
    except Exception as e:
        return {
            "executed": True,
            "passed": False,
            "branch": branch_name,
            "commit_message": commit_message,
            "summary": str(e),
            "log_tail": tail_text("\n".join(logs)),
        }


def start_app(workspace_dir: Path) -> dict:
    repo_dir = workspace_dir / "repo"
    project_type = detect_project_type(repo_dir)
    log_file = workspace_dir / "runtime" / "app.log"

    if project_type == "maven":
        cmd = ["mvn", "spring-boot:run"]
    elif project_type == "gradle":
        cmd = gradle_command(repo_dir, "bootRun")
    elif project_type == "node":
        package_json = json.loads((repo_dir / "package.json").read_text(encoding="utf-8"))
        scripts = package_json.get("scripts", {})
        if "dev" in scripts:
            cmd = ["npm", "run", "dev"]
        elif "start" in scripts:
            cmd = ["npm", "start"]
        else:
            return {
                "executed": False,
                "passed": False,
                "summary": "node project has neither dev nor start script",
            }
    else:
        return {
            "executed": False,
            "passed": False,
            "summary": f"unknown project type: {project_type}",
        }

    log_file.parent.mkdir(parents=True, exist_ok=True)
    stdout_handle = log_file.open("w", encoding="utf-8")
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=repo_dir,
            stdout=stdout_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=(os.name != "nt"),
            creationflags=creationflags,
        )
    except Exception as exc:
        stdout_handle.close()
        return {
            "executed": True,
            "passed": False,
            "project_type": project_type,
            "command": " ".join(cmd),
            "pid": "",
            "log_file": str(log_file),
            "returncode": 1,
            "stdout": "",
            "stderr": str(exc),
            "summary": "failed to launch app",
        }
    finally:
        stdout_handle.close()

    return {
        "executed": True,
        "passed": proc.poll() is None,
        "project_type": project_type,
        "command": " ".join(cmd),
        "pid": str(proc.pid),
        "log_file": str(log_file),
        "returncode": 0 if proc.poll() is None else proc.returncode,
        "stdout": "",
        "stderr": "",
        "summary": "app start command launched" if proc.poll() is None else "failed to launch app",
    }


def stop_app(pid: str):
    if not pid:
        return

    try:
        pid_int = int(pid)
    except ValueError:
        return

    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid_int), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return

    try:
        os.killpg(pid_int, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            os.kill(pid_int, signal.SIGTERM)
        except ProcessLookupError:
            pass


def wait_for_app(runtime: dict) -> dict:
    log_file = Path(runtime["log_file"])
    started_at = time.time()
    port = DEFAULT_APP_PORT

    while time.time() - started_at < APP_START_TIMEOUT:
        for path in ("/actuator/health", "/health", "/"):
            url = f"http://localhost:{port}{path}"
            if http_ok(url):
                return {
                    "ready": True,
                    "passed": True,
                    "port": port,
                    "base_url": f"http://localhost:{port}",
                    "health_url": url,
                    "log_file": str(log_file),
                    "summary": f"app ready on port {port}",
                }
        time.sleep(HEALTHCHECK_INTERVAL)

    return {
        "ready": False,
        "passed": False,
        "port": port,
        "base_url": f"http://localhost:{port}" if port else "",
        "health_url": "",
        "log_file": str(log_file),
        "summary": "app did not become ready before timeout",
    }


def run_playwright_capture(workspace_dir: Path, base_url: str) -> dict:
    repo_dir = workspace_dir / "repo"
    output_dir = workspace_dir / "report" / "screenshots"
    storage_state_path = output_dir / "auth-state.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PLAYWRIGHT_TEST_LOGIN_USERNAME"] = DEFAULT_PLAYWRIGHT_TEST_LOGIN_USERNAME
    env["PLAYWRIGHT_TEST_LOGIN_CHINESE_NAME"] = DEFAULT_PLAYWRIGHT_TEST_LOGIN_CHINESE_NAME

    proc = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("playwright_runner.py")),
            base_url,
            str(output_dir),
            str(storage_state_path),
        ],
        cwd=repo_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    result_path = output_dir / "result.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["executed"] = True
        result["returncode"] = proc.returncode
        result["stdout"] = proc.stdout
        result["stderr"] = proc.stderr
        return result

    return {
        "executed": True,
        "passed": False,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "summary": "playwright runner did not generate result.json",
        "actual": proc.stderr or proc.stdout,
        "screenshot": str(output_dir / "screenshot.png"),
        "console_log": str(output_dir / "console.log"),
    }


def run_codex_phase(workspace_dir: Path, issue: dict) -> dict:
    repo_dir = workspace_dir / "repo"
    prompt_path = workspace_dir / "task_context" / "prompt.txt"
    prompt = prompt_path.read_text(encoding="utf-8")
    runner = CodexRunner(
        codex_bin=DEFAULT_CODEX_BIN,
        model=DEFAULT_CODEX_MODEL,
        reasoning_effort=DEFAULT_CODEX_REASONING_EFFORT,
    )
    result = runner.run(prompt, repo_dir)
    summary = "codex phase passed" if result.returncode == 0 else "codex phase failed"
    return {
        "executed": True,
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "command": CodexRunner.shell(result.command),
        "stdout": result.stdout,
        "stderr": result.stderr,
        "log_tail": tail_text((result.stderr or "") + "\n" + (result.stdout or "")),
        "summary": summary,
        "modified_files": result.modified_files,
        "workspace": result.workspace,
        "invocation_style": result.invocation_style,
    }


def run_build_phase(workspace_dir: Path) -> dict:
    repo_dir = workspace_dir / "repo"
    project_type = detect_project_type(repo_dir)

    if project_type == "maven":
        cmd = ["mvn", "clean", "package"]
    elif project_type == "gradle":
        cmd = gradle_command(repo_dir, "clean", "build")
    elif project_type == "node":
        cmd = ["npm", "run", "build"]
    else:
        return {
            "executed": False,
            "passed": False,
            "returncode": 1,
            "classification": "unknown_project_type",
            "summary": f"unknown project type: {project_type}",
        }

    proc = subprocess.run(
        cmd,
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "executed": True,
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "classification": "",
        "project_type": project_type,
        "command": " ".join(cmd),
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "log_tail": tail_text(proc.stderr or proc.stdout),
        "summary": "build phase passed" if proc.returncode == 0 else "build phase failed",
    }


def run_runtime_phase(workspace_dir: Path) -> dict:
    start_result = start_app(workspace_dir)
    if not start_result.get("passed", False):
        return {
            "executed": start_result.get("executed", False),
            "ready": False,
            "passed": False,
            "port": None,
            "base_url": "",
            "summary": start_result.get("summary", "failed to launch app"),
            "stdout": start_result.get("stdout", ""),
            "stderr": start_result.get("stderr", ""),
            "log_tail": tail_text((start_result.get("stderr", "") or "") + "\n" + (start_result.get("stdout", "") or "")),
        }

    pid = start_result.get("pid", "")
    try:
        wait_result = wait_for_app(start_result)
        runtime_log_report_path = copy_runtime_log_to_report(workspace_dir, Path(start_result["log_file"]))

        if not wait_result.get("ready", False):
            wait_result["executed"] = True
            wait_result["runtime_log"] = runtime_log_report_path
            wait_result["project_type"] = start_result.get("project_type", "")
            wait_result["command"] = start_result.get("command", "")
            wait_result["log_tail"] = tail_file(runtime_log_report_path)
            return wait_result

        playwright_result = run_playwright_capture(workspace_dir, wait_result["base_url"])
        return {
            "executed": True,
            "ready": wait_result.get("ready", False),
            "passed": playwright_result.get("passed", False),
            "port": wait_result.get("port"),
            "base_url": wait_result.get("base_url", ""),
            "health_url": wait_result.get("health_url", ""),
            "runtime_log": runtime_log_report_path,
            "project_type": start_result.get("project_type", ""),
            "command": start_result.get("command", ""),
            "screenshot": playwright_result.get("screenshot", ""),
            "console_log": playwright_result.get("console_log", ""),
            "expected": playwright_result.get("expected", ""),
            "actual": playwright_result.get("actual", ""),
            "stdout": playwright_result.get("stdout", ""),
            "stderr": playwright_result.get("stderr", ""),
            "log_tail": tail_file(runtime_log_report_path),
            "summary": playwright_result.get("summary", wait_result.get("summary", "")),
        }
    finally:
        stop_app(pid)


def post_update_redmine(issue_id: str, workspace_dir: Path) -> dict:
    project_root = Path(__file__).resolve().parent
    cmd = [
        sys.executable,
        "-m",
        "scripts.post_execution_redmine_update",
        "--issue-id", str(issue_id),
        "--workspace-dir", str(workspace_dir),
        "--status-id", DEFAULT_REPORT_STATUS_ID,
        "--priority-id", DEFAULT_REPORT_PRIORITY_ID,
    ]

    proc = subprocess.run(
        cmd,
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    return {
        "executed": True,
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "error": "" if proc.returncode == 0 else (proc.stderr or proc.stdout),
    }


def run_agent_for_issue(issue: dict):
    issue = build_issue_payload(get_issue_detail(issue["issue_id"]))
    issue_id = str(issue["issue_id"])
    workspace_dir = prepare_workspace(issue_id)

    report_json_path = workspace_dir / "report" / "agent_report.json"
    report_md_path = workspace_dir / "report" / "agent_report.md"

    report_data = {
        "issue_id": issue_id,
        "mode": "full",
        "attempt_count": 1,
        "final_passed": False,
        "generated_at": now_iso(),
        "current_step": "",
        "current_step_detail": "",
        "attempts": [],
        "redmine_post_update": {},
        "manual_verification_commands": [
            "mvn package",
            "java -jar target/*.jar",
        ],
        "modified_files": [],
    }

    attempt = {
        "codex": {},
        "build": {},
        "runtime": {},
        "git": {},
    }

    try:
        set_step(report_data, "PREPARE_WORKSPACE", str(workspace_dir))
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "FETCH_ISSUE", f"refresh issue #{issue_id} details and attachments")
        issue["downloaded_attachments"] = prepare_attachments(issue_id, workspace_dir)
        report_data["downloaded_attachments"] = issue["downloaded_attachments"]
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "PREPARE_TASK_CONTEXT", "write issue.json and prompt.txt")
        prepare_task_context(issue, workspace_dir)
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "PREPARE_REPO", "prepare repo into workspace/repo")
        repo_result = prepare_repo(workspace_dir, issue_id)
        report_data["repo_prepare"] = repo_result
        if not repo_result.get("passed", False):
            raise RuntimeError(f"repo prepare failed: {repo_result.get('summary', '')}")
        branch_result = ensure_issue_branch(workspace_dir / "repo", issue_id)
        report_data["branch_prepare"] = branch_result
        if not branch_result.get("passed", False):
            raise RuntimeError(f"branch prepare failed: {branch_result.get('summary', '')}")
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "RUN_CODEX", f"issue #{issue_id}")
        attempt["codex"] = run_codex_phase(workspace_dir, issue)
        report_data["modified_files"] = collect_modified_files(workspace_dir / "repo")
        if report_data["modified_files"] and not attempt["codex"].get("modified_files"):
            attempt["codex"]["modified_files"] = report_data["modified_files"]
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "RUN_BUILD", "run build validation")
        attempt["build"] = run_build_phase(workspace_dir)
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "RUN_RUNTIME", "start app, wait for readiness, capture screenshot")
        attempt["runtime"] = run_runtime_phase(workspace_dir)

        report_data["modified_files"] = collect_modified_files(workspace_dir / "repo")
        if report_data["modified_files"]:
            attempt["codex"]["modified_files"] = report_data["modified_files"]
        report_data["attempts"] = [attempt]
        report_data["final_passed"] = (
            attempt["codex"].get("passed", False)
            and attempt["build"].get("passed", False)
            and attempt["runtime"].get("passed", False)
        )
        if report_data["final_passed"]:
            set_step(report_data, "GIT_COMMIT_PUSH", "add commit rebase push")
            git_result = run_git_phase(workspace_dir, issue_id)
            attempt["git"] = git_result
            report_data["git"] = git_result
            report_data["final_passed"] = git_result.get("passed", False)
        else:
            report_data["git"] = {
                "executed": False,
                "passed": False,
                "branch": get_issue_branch(issue_id),
                "commit_message": get_issue_branch(issue_id),
                "summary": "skipped because build/runtime validation did not pass",
            }
        
        set_step(report_data, "WRITE_REPORT", "write final report")
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(
            report_data,
            "UPDATE_REDMINE",
            "upload report and set status=In Progress, priority=Normal",
        )
        redmine_result = post_update_redmine(issue_id, workspace_dir)
        report_data["redmine_post_update"] = redmine_result

        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "DONE", "completed")
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        print(f"[agent] completed issue #{issue_id}", flush=True)
        emit_completion_signal(issue_id)
        return report_data

    except Exception as e:
        report_data["attempts"] = [attempt]
        report_data["final_passed"] = False
        report_data["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)
        print(f"[agent] fatal error while processing issue #{issue_id}: {e}", flush=True)
        raise


def main():
    print("[agent] autonomous queue runner started", flush=True)
    print("[agent] continuous-run mode: keep fetching issues until queue is empty", flush=True)

    while True:
        print("[agent] fetching assigned low-priority issues...", flush=True)
        issue = get_first_low_priority_issue()

        if not issue:
            print("[agent] no assigned low-priority issue found; stopping runner", flush=True)
            return

        print(
            f"[agent] picked issue #{issue['issue_id']} - {issue.get('summary', '') or issue.get('subject', '')}",
            flush=True,
        )
        run_agent_for_issue(issue)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[agent] fatal error: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        raise
