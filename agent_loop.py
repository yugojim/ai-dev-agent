from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from scripts.redmine_tool import get_first_low_priority_issue

load_dotenv()

DEFAULT_WORKSPACE_ROOT = Path(
    os.environ.get("WORKSPACE_BASE_DIR", str(Path.home() / "ai-workspaces"))
)

DEFAULT_REPORT_STATUS_ID = os.environ.get("REDMINE_STATUS_ID_IN_PROGRESS", "2").strip()
DEFAULT_REPORT_PRIORITY_ID = os.environ.get("REDMINE_PRIORITY_ID_NORMAL", "4").strip()

DEFAULT_REPO_SSH = os.environ.get("REPO_SSH_URL", "").strip()
DEFAULT_REPO_HTTPS = os.environ.get("REPO_URL", "").strip()
DEFAULT_AGENT_SOURCE_REPO = os.environ.get("AGENT_SOURCE_REPO", "").strip()
DEFAULT_REPO_GIT_URL = (DEFAULT_REPO_SSH or DEFAULT_REPO_HTTPS).strip()
APP_START_TIMEOUT = int(os.environ.get("APP_START_TIMEOUT", "600").strip())
HEALTHCHECK_INTERVAL = int(os.environ.get("HEALTHCHECK_INTERVAL", "3").strip())


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

AGENT_STEPS = [
    "FETCH_ISSUE",
    "PREPARE_WORKSPACE",
    "PREPARE_TASK_CONTEXT",
    "PREPARE_REPO",
    "RUN_CODEX",
    "RUN_BUILD",
    "RUN_RUNTIME",
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
    lines.append("# Agent Report")
    lines.append("")
    lines.append(f"- issue_id: {report_data.get('issue_id', '')}")
    lines.append(f"- mode: {report_data.get('mode', '')}")
    lines.append(f"- attempt_count: {report_data.get('attempt_count', '')}")
    lines.append(f"- final_passed: {report_data.get('final_passed', '')}")
    lines.append(f"- generated_at: {report_data.get('generated_at', '')}")
    lines.append(f"- current_step: {report_data.get('current_step', '')}")
    lines.append(f"- current_step_detail: {report_data.get('current_step_detail', '')}")
    lines.append("")

    attempts = report_data.get("attempts", [])
    for i, attempt in enumerate(attempts, 1):
        lines.append(f"## Attempt {i}")
        lines.append("")

        codex = attempt.get("codex", {})
        build = attempt.get("build", {})
        runtime = attempt.get("runtime", {})

        lines.append("### Codex")
        lines.append("")
        lines.append(f"- executed: {codex.get('executed', '')}")
        lines.append(f"- passed: {codex.get('passed', '')}")
        lines.append(f"- returncode: {codex.get('returncode', '')}")
        lines.append(f"- summary: {codex.get('summary', '')}")
        modified_files = codex.get("modified_files", []) or []
        if modified_files:
            lines.append("")
            lines.append("#### Modified Files")
            lines.append("")
            for f in modified_files:
                lines.append(f"- `{f}`")
        lines.append("")

        lines.append("### Build")
        lines.append("")
        lines.append(f"- executed: {build.get('executed', '')}")
        lines.append(f"- passed: {build.get('passed', '')}")
        lines.append(f"- returncode: {build.get('returncode', '')}")
        lines.append(f"- classification: {build.get('classification', '')}")
        lines.append(f"- summary: {build.get('summary', '')}")
        if build.get("log_tail"):
            lines.append("")
            lines.append("```text")
            lines.append(build["log_tail"])
            lines.append("```")
        lines.append("")

        lines.append("### Runtime")
        lines.append("")
        lines.append(f"- executed: {runtime.get('executed', '')}")
        lines.append(f"- ready: {runtime.get('ready', '')}")
        lines.append(f"- passed: {runtime.get('passed', '')}")
        lines.append(f"- port: {runtime.get('port', '')}")
        lines.append(f"- base_url: {runtime.get('base_url', '')}")
        lines.append(f"- health_url: {runtime.get('health_url', '')}")
        lines.append(f"- runtime_log: {runtime.get('runtime_log', '')}")
        lines.append(f"- screenshot: {runtime.get('screenshot', '')}")
        lines.append(f"- console_log: {runtime.get('console_log', '')}")
        lines.append(f"- summary: {runtime.get('summary', '')}")
        if runtime.get("log_tail"):
            lines.append("")
            lines.append("```text")
            lines.append(runtime["log_tail"])
            lines.append("```")
        lines.append("")

    redmine = report_data.get("redmine_post_update", {})
    if redmine:
        lines.append("## Redmine Post Update")
        lines.append("")
        lines.append(f"- executed: {redmine.get('executed', '')}")
        lines.append(f"- passed: {redmine.get('passed', '')}")
        lines.append(f"- returncode: {redmine.get('returncode', '')}")
        lines.append(f"- error: {redmine.get('error', '')}")
        lines.append("")

    if report_data.get("error"):
        lines.append("## Error")
        lines.append("")
        lines.append("```text")
        lines.append(report_data["error"])
        lines.append("```")
        lines.append("")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_prompt_from_issue(issue: dict) -> str:
    subject = issue.get("subject", "")
    raw = issue.get("raw", {})
    description = raw.get("description", "")

    return f"""Please read these files first:
- task_context/issue.json
- task_context/prompt.txt

Task:
Implement Redmine issue #{issue["issue_id"]}.

Issue subject:
{subject}

Issue description:
{description}

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


def prepare_repo(workspace_dir: Path) -> dict:
    repo_dir = workspace_dir / "repo"

    # 若 repo 已存在且非空，就直接使用
    if repo_dir.exists() and any(repo_dir.iterdir()):
        return {
            "executed": True,
            "passed": True,
            "summary": "existing repo already present in workspace",
        }

    clone_url = DEFAULT_REPO_GIT_URL

    # 優先用本地來源複製；若 AGENT_SOURCE_REPO 是遠端 repo，改走 git clone
    if DEFAULT_AGENT_SOURCE_REPO:
        source_repo = Path(DEFAULT_AGENT_SOURCE_REPO)
        if source_repo.exists():
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            shutil.copytree(source_repo, repo_dir)
            return {
                "executed": True,
                "passed": True,
                "summary": f"copied repo from {source_repo}",
            }

        if "://" in DEFAULT_AGENT_SOURCE_REPO or DEFAULT_AGENT_SOURCE_REPO.startswith("git@"):
            clone_url = DEFAULT_AGENT_SOURCE_REPO
        else:
            return {
                "executed": True,
                "passed": False,
                "summary": f"AGENT_SOURCE_REPO not found: {source_repo}",
            }

    # 否則試著 git clone
    if clone_url:
        proc = subprocess.run(
            ["git", "clone", clone_url, str(repo_dir)],
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
            "summary": "git clone completed" if proc.returncode == 0 else "git clone failed",
        }

    return {
        "executed": True,
        "passed": False,
        "summary": "no repo source configured; set AGENT_SOURCE_REPO or AGENT_REPO_GIT_URL",
    }


def detect_project_type(repo_dir: Path) -> str:
    if (repo_dir / "pom.xml").exists():
        return "maven"
    if (repo_dir / "gradlew").exists() or (repo_dir / "build.gradle").exists() or (repo_dir / "build.gradle.kts").exists():
        return "gradle"
    if (repo_dir / "package.json").exists():
        return "node"
    return "unknown"


def detect_port_from_log(log_file: Path) -> int | None:
    if not log_file.exists():
        return None

    text = log_file.read_text(encoding="utf-8", errors="ignore")
    patterns = [
        r"Tomcat started on port\(s\): (\d+)",
        r"Netty started on port (\d+)",
        r"Local:\s+http://localhost:(\d+)",
        r"localhost:(\d+)",
        r"port[:= ]+(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    return None


def http_ok(url: str) -> bool:
    try:
        req = Request(url, headers={"User-Agent": "agent-loop"})
        with urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 400
    except (URLError, HTTPError, TimeoutError):
        return False


def copy_runtime_log_to_report(workspace_dir: Path, log_file: Path) -> str:
    report_log = workspace_dir / "report" / "runtime_logs" / log_file.name
    if log_file.exists():
        shutil.copy2(log_file, report_log)
    return str(report_log)


def start_app(workspace_dir: Path) -> dict:
    repo_dir = workspace_dir / "repo"
    project_type = detect_project_type(repo_dir)
    log_file = workspace_dir / "runtime" / "app.log"

    if project_type == "maven":
        command = "mvn spring-boot:run"
    elif project_type == "gradle":
        command = "./gradlew bootRun" if (repo_dir / "gradlew").exists() else "gradle bootRun"
    elif project_type == "node":
        package_json = json.loads((repo_dir / "package.json").read_text(encoding="utf-8"))
        scripts = package_json.get("scripts", {})
        if "dev" in scripts:
            command = "npm run dev"
        elif "start" in scripts:
            command = "npm start"
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

    shell_cmd = f"nohup {command} > {shlex.quote(str(log_file))} 2>&1 & echo $!"
    proc = subprocess.run(
        ["bash", "-lc", shell_cmd],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    pid = proc.stdout.strip().splitlines()[-1].strip() if proc.returncode == 0 and proc.stdout.strip() else ""
    return {
        "executed": proc.returncode == 0,
        "passed": proc.returncode == 0 and bool(pid),
        "project_type": project_type,
        "command": command,
        "pid": pid,
        "log_file": str(log_file),
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "summary": "app start command launched" if proc.returncode == 0 and pid else "failed to launch app",
    }


def stop_app(pid: str):
    if not pid:
        return
    subprocess.run(
        ["bash", "-lc", f"kill {shlex.quote(pid)} >/dev/null 2>&1 || true"],
        capture_output=True,
        text=True,
        check=False,
    )


def wait_for_app(runtime: dict) -> dict:
    log_file = Path(runtime["log_file"])
    started_at = time.time()
    port = None

    while time.time() - started_at < APP_START_TIMEOUT:
        port = detect_port_from_log(log_file)
        if port:
            for path in ("/actuator/health", "/health", "/"):
                url = f"http://127.0.0.1:{port}{path}"
                if http_ok(url):
                    return {
                        "ready": True,
                        "passed": True,
                        "port": port,
                        "base_url": f"http://127.0.0.1:{port}",
                        "health_url": url,
                        "log_file": str(log_file),
                        "summary": f"app ready on port {port}",
                    }
        time.sleep(HEALTHCHECK_INTERVAL)

    return {
        "ready": False,
        "passed": False,
        "port": port,
        "base_url": f"http://127.0.0.1:{port}" if port else "",
        "health_url": "",
        "log_file": str(log_file),
        "summary": "app did not become ready before timeout",
    }


def run_playwright_capture(workspace_dir: Path, base_url: str) -> dict:
    repo_dir = workspace_dir / "repo"
    output_dir = workspace_dir / "report" / "screenshots"
    output_dir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("playwright_runner.py")), base_url, str(output_dir)],
        cwd=repo_dir,
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


def run_codex_phase_stub(workspace_dir: Path, issue: dict) -> dict:
    """
    先用 stub 跑通流程。
    之後你把這段替換成真正的 codex_runner 呼叫即可。
    """
    prompt_path = workspace_dir / "task_context" / "prompt.txt"
    return {
        "executed": True,
        "passed": True,
        "returncode": 0,
        "summary": f"stub codex phase; prompt prepared at {prompt_path}",
        "modified_files": [],
    }


def run_build_phase(workspace_dir: Path) -> dict:
    repo_dir = workspace_dir / "repo"
    project_type = detect_project_type(repo_dir)

    if project_type == "maven":
        cmd = ["mvn", "clean", "package"]
    elif project_type == "gradle":
        cmd = ["./gradlew", "clean", "build"] if (repo_dir / "gradlew").exists() else ["gradle", "clean", "build"]
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
    }

    attempt = {
        "codex": {},
        "build": {},
        "runtime": {},
    }

    try:
        set_step(report_data, "PREPARE_WORKSPACE", str(workspace_dir))
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "PREPARE_TASK_CONTEXT", "write issue.json and prompt.txt")
        prepare_task_context(issue, workspace_dir)
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "PREPARE_REPO", "prepare repo into workspace/repo")
        repo_result = prepare_repo(workspace_dir)
        report_data["repo_prepare"] = repo_result
        if not repo_result.get("passed", False):
            raise RuntimeError(f"repo prepare failed: {repo_result.get('summary', '')}")
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "RUN_CODEX", f"issue #{issue_id}")
        attempt["codex"] = run_codex_phase_stub(workspace_dir, issue)
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "RUN_BUILD", "stub build phase")
        attempt["build"] = run_build_phase(workspace_dir)
        write_json(report_json_path, report_data)
        write_md(report_md_path, report_data)

        set_step(report_data, "RUN_RUNTIME", "start app, wait for readiness, capture screenshot")
        attempt["runtime"] = run_runtime_phase(workspace_dir)

        report_data["attempts"] = [attempt]
        report_data["final_passed"] = (
            attempt["codex"].get("passed", False)
            and attempt["build"].get("passed", False)
            and attempt["runtime"].get("passed", False)
        )

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

    print("[agent] fetching assigned low-priority issues...", flush=True)
    issue = get_first_low_priority_issue()

    if not issue:
        print("[agent] no assigned low-priority issue found", flush=True)
        return

    print(f"[agent] picked issue #{issue['issue_id']} - {issue.get('subject', '')}", flush=True)
    run_agent_for_issue(issue)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[agent] fatal error: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        raise
