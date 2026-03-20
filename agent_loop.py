#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codex_runner import CodexRunner
from playwright_runner import (
    probe_auth_with_state,
    bootstrap_login_and_save_state,
)

MAX_ATTEMPTS = 3
DEFAULT_MODEL = "gpt-5.4"
DEFAULT_CODEX_BIN = "codex"

BUILD_TIMEOUT_SECONDS = 600
RUNTIME_START_TIMEOUT_SECONDS = 120
RUNTIME_HEALTHCHECK_INTERVAL_SECONDS = 3
DEFAULT_RUNTIME_PORT = 8080
MAX_LOG_CHARS = 12000
EARLY_LOG_ERROR_SCAN_CHARS = 20000

EXECUTION_ENFORCEMENT_BLOCK = """

CRITICAL EXECUTION RULES:

You MUST apply the change by editing real files in the repository.
Do not stop after analysis.
Do not only describe the change.
Do not propose pseudo-code.
Do not explain what should be done.

You must actually write and apply the patch.

If no file is modified, the task will be considered FAILED.

Ensure the patch is minimal and limited to the ticket scope.
"""


@dataclass
class PhaseResult:
    executed: bool = False
    passed: bool = False
    command: str = ""
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AttemptResult:
    attempt: int
    codex: Dict[str, Any]
    build: Dict[str, Any]
    auth: Dict[str, Any]
    runtime: Dict[str, Any]
    playwright: Dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)

def run_auth_phase(
    workspace_dir: Path,
    repo_dir: Path,
    runtime_result: Dict[str, Any],
) -> Dict[str, Any]:

    base_url = runtime_result.get("base_url", "http://localhost:8080")
    auth_state_path = workspace_dir / "report" / "auth_state.json"

    try:
        ok = probe_auth_with_state(
            base_url=base_url,
            storage_state_path=str(auth_state_path),
        )

        if ok:
            return {
                "executed": True,
                "passed": True,
                "classification": "",
                "bootstrap_performed": False,
                "state_path": str(auth_state_path),
            }

        bootstrap_login_and_save_state(
            base_url=base_url,
            storage_state_path=str(auth_state_path),
        )

        return {
            "executed": True,
            "passed": True,
            "classification": "",
            "bootstrap_performed": True,
            "state_path": str(auth_state_path),
        }

    except Exception as e:
        return {
            "executed": True,
            "passed": False,
            "classification": "auth_failed",
            "error": str(e),
            "bootstrap_performed": False,
            "state_path": str(auth_state_path),
        }
        
def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_issue_json(issue_json_path: Path) -> dict:
    return json.loads(read_text(issue_json_path))


def trim_output(text: str, limit: int = MAX_LOG_CHARS) -> str:
    if not text:
        return ""
    return text[-limit:]


def normalize_path(path_text: str) -> str:
    return path_text.replace("\\", "/").strip()


def detect_modified_files(repo_dir: Path) -> List[str]:
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0:
            return []

        files: List[str] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            file_part = line[3:].strip()
            if file_part:
                files.append(normalize_path(file_part))
        return files
    except Exception:
        return []


def extract_error_files(build_output: str) -> List[str]:
    if not build_output:
        return []

    patterns = [
        r"([A-Za-z0-9_./\\:-]+\.java)",
        r"([A-Za-z0-9_./\\:-]+\.kt)",
        r"([A-Za-z0-9_./\\:-]+\.groovy)",
        r"([A-Za-z0-9_./\\:-]+\.properties)",
        r"([A-Za-z0-9_./\\:-]+\.json)",
        r"([A-Za-z0-9_./\\:-]+\.xml)",
        r"([A-Za-z0-9_./\\:-]+\.yml)",
        r"([A-Za-z0-9_./\\:-]+\.yaml)",
    ]

    found: List[str] = []
    for pattern in patterns:
        for match in re.findall(pattern, build_output):
            cleaned = normalize_path(match)
            if cleaned not in found:
                found.append(cleaned)
    return found


def classify_build_failure(
    returncode: Optional[int],
    stdout: str,
    stderr: str,
    modified_files: List[str],
    timed_out: bool = False,
) -> str:
    if timed_out:
        return "build_timeout"

    combined = f"{stdout}\n{stderr}"
    combined_lower = combined.lower()

    error_files = extract_error_files(combined)
    normalized_modified = {normalize_path(f) for f in modified_files}
    modified_basenames = {Path(f).name for f in normalized_modified}

    matched_modified_file = False
    for err_file in error_files:
        normalized_err = normalize_path(err_file)
        err_name = Path(normalized_err).name
        if normalized_err in normalized_modified or err_name in modified_basenames:
            matched_modified_file = True
            break

    has_compile_error = any(
        key in combined_lower
        for key in [
            "compilation failure",
            "compilation error",
            "cannot find symbol",
            "package does not exist",
            "failed to execute goal",
            "symbol:",
            "location:",
            "error:",
        ]
    )

    if has_compile_error and not matched_modified_file:
        return "pre_existing_compile_errors"

    if returncode is None:
        return "ticket_related_build_failure"

    if returncode != 0:
        return "ticket_related_build_failure"

    if has_compile_error:
        return "ticket_related_build_failure"

    return ""


def build_codex_prompt(
    issue_json_path: Path,
    prompt_txt_path: Path,
    attachments_dir: Path,
    retry_feedback: str = "",
) -> str:
    issue_obj = json.loads(issue_json_path.read_text(encoding="utf-8"))
    issue_id = issue_obj.get("id", "UNKNOWN")

    base = f"""Please read these files first:
- {issue_json_path}
- {prompt_txt_path}

Also inspect files under:
- {attachments_dir}

Task:
Implement Redmine issue #{issue_id}.

Rules:
1. Keep changes minimal and limited to this ticket.
2. Before editing, identify the relevant files and logic.
3. Then edit the code to implement the requested behavior.
4. Do not commit or push anything.
5. After editing, summarize modified files and suggest exact startup/test commands.
6. Do not start long-running local servers unless strictly necessary.
7. Prefer build-safe changes that can later be verified with:
   - mvn package
8. You MUST actually edit files in the repo when a code/resource change is required.
9. If the workspace is not writable, state that clearly and stop immediately.
"""

    if retry_feedback.strip():
        base += f"""

Retry feedback from previous attempt:
{retry_feedback}
"""

    base += EXECUTION_ENFORCEMENT_BLOCK
    return base.strip()


def run_build_phase(repo_dir: Path, modified_files: Optional[List[str]] = None) -> Dict[str, Any]:
    modified_files = modified_files or []
    cmd = ["mvn", "package"]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=BUILD_TIMEOUT_SECONDS,
        )

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = f"{stdout}\n{stderr}".lower()

        has_compile_error_text = any(
            key in combined
            for key in [
                "cannot find symbol",
                "compilation failure",
                "compilation error",
                "package does not exist",
                "failed to execute goal",
                "error:",
            ]
        )

        passed = (proc.returncode == 0) and not has_compile_error_text

        classification = ""
        if not passed:
            classification = classify_build_failure(
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                modified_files=modified_files,
                timed_out=False,
            )

        return {
            "executed": True,
            "passed": passed,
            "command": " ".join(cmd),
            "returncode": proc.returncode,
            "stdout": trim_output(stdout),
            "stderr": trim_output(stderr),
            "error": "",
            "classification": classification,
            "timeout_seconds": BUILD_TIMEOUT_SECONDS,
            "error_files": extract_error_files(f"{stdout}\n{stderr}"),
        }

    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""

        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

        return {
            "executed": True,
            "passed": False,
            "command": " ".join(cmd),
            "returncode": None,
            "stdout": trim_output(stdout),
            "stderr": trim_output(stderr),
            "error": f"Build timed out after {BUILD_TIMEOUT_SECONDS} seconds",
            "classification": "build_timeout",
            "timeout_seconds": BUILD_TIMEOUT_SECONDS,
            "error_files": extract_error_files(f"{stdout}\n{stderr}"),
        }

    except Exception as exc:
        return {
            "executed": True,
            "passed": False,
            "command": " ".join(cmd),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": f"{type(exc).__name__}: {exc}",
            "classification": "ticket_related_build_failure",
            "timeout_seconds": BUILD_TIMEOUT_SECONDS,
            "error_files": [],
        }


def find_packaged_jar(repo_dir: Path) -> Optional[Path]:
    target_dir = repo_dir / "target"
    if not target_dir.exists():
        return None

    jar_candidates = [
        p for p in target_dir.glob("*.jar")
        if p.is_file()
        and not p.name.endswith(".original")
        and not p.name.endswith("-sources.jar")
        and not p.name.endswith("-javadoc.jar")
        and "plain" not in p.name
    ]

    if not jar_candidates:
        return None

    jar_candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return jar_candidates[0]


def is_port_open(host: str, port: int, timeout_seconds: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except OSError:
        return False


def detect_listening_pids(port: int) -> List[int]:
    commands = [
        ["bash", "-lc", f"ss -ltnp '( sport = :{port} )' || true"],
        ["bash", "-lc", f"lsof -tiTCP:{port} -sTCP:LISTEN || true"],
    ]

    found: List[int] = []
    pid_pattern = re.compile(r"pid=(\d+)")
    plain_pid_pattern = re.compile(r"^\d+$")

    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output = f"{proc.stdout}\n{proc.stderr}"
            for pid_text in pid_pattern.findall(output):
                pid = int(pid_text)
                if pid not in found:
                    found.append(pid)
            for line in output.splitlines():
                value = line.strip()
                if plain_pid_pattern.fullmatch(value):
                    pid = int(value)
                    if pid not in found:
                        found.append(pid)
        except Exception:
            continue

    return found


def http_probe(host: str, port: int, path: str, timeout_seconds: float = 3.0) -> Tuple[bool, str]:
    try:
        import urllib.request

        url = f"http://{host}:{port}{path}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "agent-loop-runtime-probe"},
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read(1000).decode("utf-8", errors="replace")
            return 200 <= status < 500, f"{status} {body[:200]}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def read_log_tail(log_path: Path, limit: int = MAX_LOG_CHARS) -> str:
    if not log_path.exists():
        return ""
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        return trim_output(content, limit=limit)
    except Exception:
        return ""


def read_log_head_and_tail(log_path: Path, limit: int = EARLY_LOG_ERROR_SCAN_CHARS) -> str:
    if not log_path.exists():
        return ""
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        if len(content) <= limit:
            return content
        half = limit // 2
        return content[:half] + "\n...\n" + content[-half:]
    except Exception:
        return ""


def detect_runtime_failure_pattern(log_text: str) -> str:
    lower = (log_text or "").lower()

    patterns = [
        ("runtime_port_conflict", [
            "address already in use",
            "port 8080 was already in use",
            "failed to start component [connector",
            "bindexception",
        ]),
        ("runtime_db_connection_failure", [
            "failed to configure a datasource",
            "jdbc",
            "hikari",
            "datasource",
            "unable to acquire jdbc connection",
        ]),
        ("runtime_config_failure", [
            "could not resolve placeholder",
            "failed to bind properties",
            "invalid config",
            "configurationpropertiesbindexception",
        ]),
        ("runtime_classpath_failure", [
            "classnotfoundexception",
            "noclassdeffounderror",
            "nosuchmethoderror",
            "beancreationexception",
        ]),
        ("runtime_web_start_failure", [
            "application run failed",
            "web server failed to start",
            "unable to start web server",
            "failed to start bean",
        ]),
    ]

    for classification, keywords in patterns:
        if any(keyword in lower for keyword in keywords):
            return classification

    return ""


def terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass

    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.5)

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def classify_runtime_failure(
    *,
    timed_out: bool,
    process_returncode: Optional[int],
    port_open: bool,
    probe_hits: List[Dict[str, str]],
    log_text: str,
    preexisting_port_conflict: bool,
) -> str:
    if preexisting_port_conflict:
        return "runtime_port_conflict"

    pattern_class = detect_runtime_failure_pattern(log_text)
    if pattern_class:
        return pattern_class

    if timed_out:
        if port_open and probe_hits:
            return "runtime_healthcheck_not_ready"
        if port_open:
            return "runtime_port_open_but_unhealthy"
        return "runtime_start_timeout"

    if process_returncode is not None and process_returncode != 0:
        return "runtime_process_exited"

    if port_open:
        return "runtime_healthcheck_not_ready"

    return "runtime_not_reachable"


def run_runtime_phase(
    repo_dir: Path,
    workspace_dir: Path,
    build_soft_failed: bool = False,
) -> Dict[str, Any]:
    jar_path = find_packaged_jar(repo_dir)
    logs_dir = workspace_dir / "report" / "runtime_logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / f"runtime_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    base_url = f"http://127.0.0.1:{DEFAULT_RUNTIME_PORT}"
    health_url = f"{base_url}/actuator/health"

    if jar_path is None:
        return {
            "executed": True,
            "ready": False,
            "passed": False,
            "build_soft_failed": build_soft_failed,
            "port": DEFAULT_RUNTIME_PORT,
            "base_url": base_url,
            "health_url": health_url,
            "command": "",
            "jar_path": "",
            "log_file": str(log_file),
            "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
            "process_id": None,
            "returncode": None,
            "classification": "runtime_jar_not_found",
            "probe_hits": [],
            "preexisting_port_conflict": False,
            "preexisting_listening_pids": [],
            "stdout": "",
            "stderr": "",
            "error": "Packaged jar not found under target/",
        }

    preexisting_pids = detect_listening_pids(DEFAULT_RUNTIME_PORT)
    preexisting_port_conflict = len(preexisting_pids) > 0 or is_port_open("127.0.0.1", DEFAULT_RUNTIME_PORT)

    if preexisting_port_conflict:
        return {
            "executed": True,
            "ready": False,
            "passed": False,
            "build_soft_failed": build_soft_failed,
            "port": DEFAULT_RUNTIME_PORT,
            "base_url": base_url,
            "health_url": health_url,
            "command": f"java -jar {jar_path}",
            "jar_path": str(jar_path),
            "log_file": str(log_file),
            "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
            "process_id": None,
            "returncode": None,
            "classification": "runtime_port_conflict",
            "probe_hits": [],
            "preexisting_port_conflict": True,
            "preexisting_listening_pids": preexisting_pids,
            "stdout": "",
            "stderr": "",
            "error": f"Port {DEFAULT_RUNTIME_PORT} is already in use before runtime start",
        }

    cmd = ["java", "-jar", str(jar_path)]
    health_candidates = [
        "/actuator/health",
        "/login",
        "/",
    ]

    log_handle = None
    proc: Optional[subprocess.Popen] = None

    try:
        log_handle = open(log_file, "w", encoding="utf-8", buffering=1)

        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_dir),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        deadline = time.time() + RUNTIME_START_TIMEOUT_SECONDS
        probe_hits: List[Dict[str, str]] = []

        while time.time() < deadline:
            current_returncode = proc.poll()
            log_handle.flush()
            log_text = read_log_head_and_tail(log_file)

            failure_pattern = detect_runtime_failure_pattern(log_text)
            if failure_pattern:
                terminate_process_tree(proc)
                return {
                    "executed": True,
                    "ready": False,
                    "passed": False,
                    "build_soft_failed": build_soft_failed,
                    "port": DEFAULT_RUNTIME_PORT,
                    "base_url": base_url,
                    "health_url": health_url,
                    "command": " ".join(cmd),
                    "jar_path": str(jar_path),
                    "log_file": str(log_file),
                    "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
                    "process_id": proc.pid,
                    "returncode": proc.poll(),
                    "classification": failure_pattern,
                    "probe_hits": probe_hits,
                    "preexisting_port_conflict": False,
                    "preexisting_listening_pids": [],
                    "stdout": "",
                    "stderr": read_log_tail(log_file),
                    "error": f"Runtime failed early: {failure_pattern}",
                }

            if current_returncode is not None:
                terminate_process_tree(proc)
                return {
                    "executed": True,
                    "ready": False,
                    "passed": False,
                    "build_soft_failed": build_soft_failed,
                    "port": DEFAULT_RUNTIME_PORT,
                    "base_url": base_url,
                    "health_url": health_url,
                    "command": " ".join(cmd),
                    "jar_path": str(jar_path),
                    "log_file": str(log_file),
                    "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
                    "process_id": proc.pid,
                    "returncode": current_returncode,
                    "classification": classify_runtime_failure(
                        timed_out=False,
                        process_returncode=current_returncode,
                        port_open=False,
                        probe_hits=probe_hits,
                        log_text=log_text,
                        preexisting_port_conflict=False,
                    ),
                    "probe_hits": probe_hits,
                    "preexisting_port_conflict": False,
                    "preexisting_listening_pids": [],
                    "stdout": "",
                    "stderr": read_log_tail(log_file),
                    "error": f"Runtime process exited before becoming ready (returncode={current_returncode})",
                }

            port_open = is_port_open("127.0.0.1", DEFAULT_RUNTIME_PORT)
            if port_open:
                for path in health_candidates:
                    ok, detail = http_probe("127.0.0.1", DEFAULT_RUNTIME_PORT, path)
                    probe_hits.append(
                        {
                            "path": path,
                            "ok": str(ok).lower(),
                            "detail": detail,
                        }
                    )
                    if ok:
                        log_handle.flush()
                        stdout_tail = read_log_tail(log_file)
                        terminate_process_tree(proc)
                        final_returncode = proc.poll()

                        return {
                            "executed": True,
                            "ready": True,
                            "passed": True,
                            "build_soft_failed": build_soft_failed,
                            "port": DEFAULT_RUNTIME_PORT,
                            "base_url": base_url,
                            "health_url": health_url,
                            "command": " ".join(cmd),
                            "jar_path": str(jar_path),
                            "log_file": str(log_file),
                            "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
                            "process_id": proc.pid,
                            "returncode": final_returncode,
                            "classification": "",
                            "probe_hits": probe_hits,
                            "preexisting_port_conflict": False,
                            "preexisting_listening_pids": [],
                            "stdout": stdout_tail,
                            "stderr": "",
                            "error": "",
                        }

            time.sleep(RUNTIME_HEALTHCHECK_INTERVAL_SECONDS)

        log_handle.flush()
        log_text = read_log_head_and_tail(log_file)
        stderr_tail = read_log_tail(log_file)
        port_open = is_port_open("127.0.0.1", DEFAULT_RUNTIME_PORT)
        terminate_process_tree(proc)

        return {
            "executed": True,
            "ready": False,
            "passed": False,
            "build_soft_failed": build_soft_failed,
            "port": DEFAULT_RUNTIME_PORT,
            "base_url": base_url,
            "health_url": health_url,
            "command": " ".join(cmd),
            "jar_path": str(jar_path),
            "log_file": str(log_file),
            "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
            "process_id": proc.pid,
            "returncode": proc.poll(),
            "classification": classify_runtime_failure(
                timed_out=True,
                process_returncode=proc.poll(),
                port_open=port_open,
                probe_hits=probe_hits,
                log_text=log_text,
                preexisting_port_conflict=False,
            ),
            "probe_hits": probe_hits,
            "preexisting_port_conflict": False,
            "preexisting_listening_pids": [],
            "stdout": "",
            "stderr": stderr_tail,
            "error": f"Runtime not ready after {RUNTIME_START_TIMEOUT_SECONDS} seconds",
        }

    except Exception as exc:
        if proc is not None:
            try:
                terminate_process_tree(proc)
            except Exception:
                pass

        return {
            "executed": True,
            "ready": False,
            "passed": False,
            "build_soft_failed": build_soft_failed,
            "port": DEFAULT_RUNTIME_PORT,
            "base_url": base_url,
            "health_url": health_url,
            "command": " ".join(cmd),
            "jar_path": str(jar_path),
            "log_file": str(log_file),
            "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
            "process_id": proc.pid if proc is not None else None,
            "returncode": proc.poll() if proc is not None else None,
            "classification": "runtime_exception",
            "probe_hits": [],
            "preexisting_port_conflict": False,
            "preexisting_listening_pids": [],
            "stdout": "",
            "stderr": read_log_tail(log_file),
            "error": f"{type(exc).__name__}: {exc}",
        }

    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass


def make_empty_auth_result() -> dict:
    return {
        "authenticated": False,
        "bootstrap_performed": False,
        "state_file": str(Path.home() / "ai-dev-agent/.auth/cognito_auth_state.json"),
        "final_url": "",
        "error": "",
    }


def make_empty_runtime_result() -> dict:
    return {
        "executed": False,
        "ready": False,
        "passed": False,
        "build_soft_failed": False,
        "port": None,
        "base_url": "",
        "health_url": "",
        "command": "",
        "jar_path": "",
        "log_file": "",
        "timeout_seconds": RUNTIME_START_TIMEOUT_SECONDS,
        "process_id": None,
        "returncode": None,
        "classification": "",
        "probe_hits": [],
        "preexisting_port_conflict": False,
        "preexisting_listening_pids": [],
        "stdout": "",
        "stderr": "",
        "error": "",
    }


def make_empty_playwright_result() -> dict:
    return {
        "passed": False,
        "summary": "Not executed",
        "expected": "",
        "actual": "",
        "screenshot": "",
        "console_log": "",
    }


def classify_codex_error(returncode: int, stdout: str, stderr: str, modified_files: List[str]) -> str:
    combined = f"{stdout}\n{stderr}".lower()

    if "unexpected argument '--approval-mode'" in combined:
        return "UNSUPPORTED_APPROVAL_MODE"

    if "unexpected argument '--sandbox'" in combined:
        return "UNSUPPORTED_SANDBOX_OPTION"

    if "unexpected argument 'exec'" in combined or "unrecognized subcommand 'exec'" in combined:
        return "UNSUPPORTED_EXEC_SUBCOMMAND"

    if "read-only filesystem" in combined or "sandbox: read-only" in combined:
        return "READ_ONLY_SANDBOX"

    if returncode != 0:
        return "CODEX_NONZERO_EXIT"

    if not modified_files:
        return "NO_FILE_CHANGES"

    return ""


def build_retry_feedback(agent_error_class: str) -> str:
    mapping = {
        "UNSUPPORTED_APPROVAL_MODE": (
            "Previous attempt failed because the Codex CLI does not support "
            "--approval-mode. Remove that option."
        ),
        "UNSUPPORTED_SANDBOX_OPTION": (
            "Previous attempt failed because the Codex CLI does not support "
            "--sandbox. Use CLI-compatible invocation."
        ),
        "UNSUPPORTED_EXEC_SUBCOMMAND": (
            "Previous attempt failed because this Codex CLI does not support "
            "the exec subcommand. Use direct prompt invocation."
        ),
        "READ_ONLY_SANDBOX": (
            "Previous attempt failed because Codex was in a read-only environment. "
            "Use writable workspace mode."
        ),
        "NO_FILE_CHANGES": (
            "Previous attempt produced no actual file edits. Apply the required "
            "patch to the repository."
        ),
        "CODEX_NONZERO_EXIT": (
            "Previous attempt failed during Codex execution. Re-check CLI invocation "
            "and ensure the patch is actually applied."
        ),
    }
    return mapping.get(
        agent_error_class,
        "Previous attempt did not pass. Re-check implementation and keep it minimal.",
    )


def should_break_early(agent_error_class: str) -> bool:
    return agent_error_class in {
        "UNSUPPORTED_APPROVAL_MODE",
        "UNSUPPORTED_SANDBOX_OPTION",
    }


def run_codex_phase(
    repo_dir: Path,
    issue_json_path: Path,
    prompt_txt_path: Path,
    attachments_dir: Path,
    retry_feedback: str = "",
) -> Dict[str, Any]:
    prompt = build_codex_prompt(
        issue_json_path=issue_json_path,
        prompt_txt_path=prompt_txt_path,
        attachments_dir=attachments_dir,
        retry_feedback=retry_feedback,
    )

    runner = CodexRunner(
        codex_bin=DEFAULT_CODEX_BIN,
        model=DEFAULT_MODEL,
        reasoning_effort="medium",
    )

    result = runner.run(
        prompt=prompt,
        repo_dir=repo_dir,
    )

    codex_result = result.to_dict()
    codex_result["shell_command"] = runner.shell(result.command)
    codex_result["sandbox_mode"] = "workspace-write"
    codex_result["writable_probe_passed"] = True
    codex_result["writable_probe_error"] = ""
    codex_result["agent_error_class"] = classify_codex_error(
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        modified_files=result.modified_files,
    )
    return codex_result

def run_playwright_phase(
    workspace_dir: Path,
    runtime_result: Dict[str, Any],
    auth_result: Dict[str, Any],
) -> Dict[str, Any]:

    from playwright_runner import run_smoke_test

    base_url = runtime_result.get("base_url")
    state = auth_result.get("state_path")

    try:
        ok, screenshot = run_smoke_test(
            base_url=base_url,
            storage_state_path=state,
            screenshot_dir=str(workspace_dir / "report"),
        )

        return {
            "executed": True,
            "passed": ok,
            "classification": "" if ok else "smoke_failed",
            "screenshot": screenshot,
        }

    except Exception as e:
        return {
            "executed": True,
            "passed": False,
            "classification": "playwright_exception",
            "error": str(e),
        }
        
def run_attempt(
    attempt_no: int,
    workspace_dir: Path,
    repo_dir: Path,
    issue_json_path: Path,
    prompt_txt_path: Path,
    attachments_dir: Path,
    mode: str,
    retry_feedback: str = "",
) -> AttemptResult:

    if mode == "build_only":
        codex_result = {
            "command": [],
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "sandbox_mode": "workspace-write",
            "workspace": str(repo_dir),
            "writable_probe_passed": True,
            "writable_probe_error": "",
            "invocation_style": "skipped_for_build_only",
            "modified_files": detect_modified_files(repo_dir),
            "shell_command": "",
            "agent_error_class": "",
        }
    else:
        codex_result = run_codex_phase(
            repo_dir=repo_dir,
            issue_json_path=issue_json_path,
            prompt_txt_path=prompt_txt_path,
            attachments_dir=attachments_dir,
            retry_feedback=retry_feedback,
        )

    # -----------------
    # Build phase
    # -----------------
    if mode in ("build_only", "full"):
        build_result = run_build_phase(
            repo_dir=repo_dir,
            modified_files=codex_result.get("modified_files", []),
        )
    else:
        build_result = PhaseResult(executed=False, passed=False).to_dict()

    build_passed = build_result.get("passed", False)
    build_class = build_result.get("classification", "")
    build_soft_failed = (
        not build_passed and build_class == "pre_existing_compile_errors"
    )

    allow_runtime = build_passed or build_soft_failed

    # -----------------
    # Runtime phase
    # -----------------
    if mode == "full" and allow_runtime:
        runtime_result = run_runtime_phase(
            repo_dir=repo_dir,
            workspace_dir=workspace_dir,
            build_soft_failed=build_soft_failed,
        )
    else:
        runtime_result = make_empty_runtime_result()

    # -----------------
    # Auth phase
    # -----------------
    if mode == "full" and runtime_result.get("passed", False):
        auth_result = run_auth_phase(
            workspace_dir=workspace_dir,
            repo_dir=repo_dir,
            runtime_result=runtime_result,
        )
    else:
        auth_result = make_empty_auth_result()

    # -----------------
    # Playwright phase
    # -----------------
    if mode == "full" and auth_result.get("passed", False):
        playwright_result = run_playwright_phase(
            workspace_dir=workspace_dir,
            runtime_result=runtime_result,
            auth_result=auth_result,
        )
    else:
        playwright_result = make_empty_playwright_result()

    return AttemptResult(
        attempt=attempt_no,
        codex=codex_result,
        build=build_result,
        auth=auth_result,
        runtime=runtime_result,
        playwright=playwright_result,
    )


def render_markdown_report(report: dict) -> str:
    lines: List[str] = []

    lines.append("# Agent Report")
    lines.append("")
    lines.append(f"- issue_id: {report.get('issue_id')}")
    lines.append(f"- mode: {report.get('mode')}")
    lines.append(f"- attempt_count: {report.get('attempt_count')}")
    lines.append(f"- final_passed: {report.get('final_passed')}")
    lines.append(f"- generated_at: {report.get('generated_at')}")
    lines.append("")
    lines.append("## Manual Verification Commands")
    lines.append("")
    for cmd in report.get("manual_verification_commands", []):
        lines.append(f"- `{cmd}`")
    lines.append("")

    for attempt in report.get("attempts", []):
        lines.append(f"## Attempt {attempt['attempt']}")
        lines.append("")

        codex = attempt["codex"]
        build = attempt["build"]
        runtime = attempt["runtime"]

        lines.append("### Codex")
        lines.append("")
        lines.append(f"- returncode: {codex.get('returncode')}")
        lines.append(f"- sandbox_mode: {codex.get('sandbox_mode')}")
        lines.append(f"- invocation_style: {codex.get('invocation_style')}")
        lines.append(f"- writable_probe_passed: {codex.get('writable_probe_passed')}")
        lines.append(f"- agent_error_class: {codex.get('agent_error_class')}")
        lines.append("")

        lines.append("#### Modified Files")
        lines.append("")
        modified = codex.get("modified_files", [])
        if modified:
            for f in modified:
                lines.append(f"- `{f}`")
        else:
            lines.append("- none")
        lines.append("")

        lines.append("#### Command")
        lines.append("")
        lines.append("```bash")
        lines.append(codex.get("shell_command", ""))
        lines.append("```")
        lines.append("")

        lines.append("#### Stdout")
        lines.append("")
        lines.append("```text")
        lines.append(trim_output(codex.get("stdout", "")))
        lines.append("```")
        lines.append("")

        lines.append("#### Stderr")
        lines.append("")
        lines.append("```text")
        lines.append(trim_output(codex.get("stderr", "")))
        lines.append("```")
        lines.append("")

        lines.append("### Build")
        lines.append("")
        lines.append(f"- executed: {build.get('executed')}")
        lines.append(f"- passed: {build.get('passed')}")
        lines.append(f"- returncode: {build.get('returncode')}")
        lines.append(f"- classification: {build.get('classification', '')}")
        lines.append(f"- timeout_seconds: {build.get('timeout_seconds', '')}")
        lines.append(f"- error: {build.get('error', '')}")
        lines.append("")

        lines.append("#### Build Command")
        lines.append("")
        lines.append("```bash")
        lines.append(build.get("command", ""))
        lines.append("```")
        lines.append("")

        lines.append("#### Build Error Files")
        lines.append("")
        error_files = build.get("error_files", [])
        if error_files:
            for f in error_files:
                lines.append(f"- `{f}`")
        else:
            lines.append("- none")
        lines.append("")

        lines.append("#### Build Stdout")
        lines.append("")
        lines.append("```text")
        lines.append(trim_output(build.get("stdout", "")))
        lines.append("```")
        lines.append("")

        lines.append("#### Build Stderr")
        lines.append("")
        lines.append("```text")
        lines.append(trim_output(build.get("stderr", "")))
        lines.append("```")
        lines.append("")

        lines.append("### Runtime")
        lines.append("")
        lines.append(f"- executed: {runtime.get('executed')}")
        lines.append(f"- ready: {runtime.get('ready')}")
        lines.append(f"- passed: {runtime.get('passed')}")
        lines.append(f"- build_soft_failed: {runtime.get('build_soft_failed')}")
        lines.append(f"- port: {runtime.get('port')}")
        lines.append(f"- base_url: {runtime.get('base_url')}")
        lines.append(f"- health_url: {runtime.get('health_url')}")
        lines.append(f"- process_id: {runtime.get('process_id')}")
        lines.append(f"- returncode: {runtime.get('returncode')}")
        lines.append(f"- classification: {runtime.get('classification')}")
        lines.append(f"- timeout_seconds: {runtime.get('timeout_seconds')}")
        lines.append(f"- preexisting_port_conflict: {runtime.get('preexisting_port_conflict')}")
        lines.append(f"- preexisting_listening_pids: {runtime.get('preexisting_listening_pids')}")
        lines.append(f"- log_file: {runtime.get('log_file')}")
        lines.append(f"- error: {runtime.get('error')}")
        lines.append("")

        lines.append("#### Runtime Command")
        lines.append("")
        lines.append("```bash")
        lines.append(runtime.get("command", ""))
        lines.append("```")
        lines.append("")

        lines.append("#### Runtime Probe Hits")
        lines.append("")
        probe_hits = runtime.get("probe_hits", [])
        if probe_hits:
            for hit in probe_hits:
                lines.append(
                    f"- `{hit.get('path')}` ok={hit.get('ok')} detail={hit.get('detail')}"
                )
        else:
            lines.append("- none")
        lines.append("")

        lines.append("#### Runtime Stdout")
        lines.append("")
        lines.append("```text")
        lines.append(trim_output(runtime.get("stdout", "")))
        lines.append("```")
        lines.append("")

        lines.append("#### Runtime Stderr")
        lines.append("")
        lines.append("```text")
        lines.append(trim_output(runtime.get("stderr", "")))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python agent_loop.py <workspace_dir> [mode]", file=sys.stderr)
        return 2

    workspace_dir = Path(sys.argv[1]).resolve()
    mode = sys.argv[2] if len(sys.argv) >= 3 else "codex_only"

    repo_dir = workspace_dir / "repo"
    task_context_dir = workspace_dir / "task_context"
    attachments_dir = workspace_dir / "attachments"
    report_dir = workspace_dir / "report"

    issue_json_path = task_context_dir / "issue.json"
    prompt_txt_path = task_context_dir / "prompt.txt"
    report_json_path = report_dir / "agent_report.json"
    report_md_path = report_dir / "agent_report.md"

    if not repo_dir.exists():
        print(f"Repo dir not found: {repo_dir}", file=sys.stderr)
        return 2
    if not issue_json_path.exists():
        print(f"issue.json not found: {issue_json_path}", file=sys.stderr)
        return 2
    if not prompt_txt_path.exists():
        print(f"prompt.txt not found: {prompt_txt_path}", file=sys.stderr)
        return 2

    issue = load_issue_json(issue_json_path)

    attempts: List[dict] = []
    final_passed = False
    retry_feedback = ""

    for i in range(1, MAX_ATTEMPTS + 1):
        try:
            attempt = run_attempt(
                attempt_no=i,
                workspace_dir=workspace_dir,
                repo_dir=repo_dir,
                issue_json_path=issue_json_path,
                prompt_txt_path=prompt_txt_path,
                attachments_dir=attachments_dir,
                mode=mode,
                retry_feedback=retry_feedback,
            )
            attempts.append(attempt.to_dict())

            codex_result = attempt.codex
            build_result = attempt.build
            runtime_result = attempt.runtime

            agent_error_class = codex_result.get("agent_error_class", "")
            modified_files = codex_result.get("modified_files", [])
            returncode = codex_result.get("returncode", 1)

            if should_break_early(agent_error_class):
                break

            codex_ok = (returncode == 0 and bool(modified_files)) if mode != "build_only" else True
            build_ok = build_result.get("passed", False)
            build_soft_failed = build_result.get("classification", "") == "pre_existing_compile_errors"
            runtime_ok = runtime_result.get("passed", False)

            if mode == "codex_only":
                if codex_ok:
                    final_passed = True
                    break

            elif mode == "build_only":
                if build_ok or build_soft_failed:
                    final_passed = True
                    break

            elif mode == "full":
                if codex_ok and (build_ok or build_soft_failed) and runtime_ok:
                    final_passed = True
                    break

            else:
                retry_feedback = f"Unknown mode: {mode}"
                break

            retry_feedback = build_retry_feedback(agent_error_class)

        except Exception as exc:
            attempts.append(
                AttemptResult(
                    attempt=i,
                    codex={
                        "command": [],
                        "returncode": 1,
                        "stdout": "",
                        "stderr": f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
                        "sandbox_mode": "workspace-write",
                        "workspace": str(repo_dir),
                        "writable_probe_passed": False,
                        "writable_probe_error": str(exc),
                        "invocation_style": "",
                        "modified_files": [],
                        "shell_command": "",
                        "agent_error_class": "UNHANDLED_EXCEPTION",
                    },
                    build=PhaseResult(executed=False, passed=False).to_dict(),
                    auth=make_empty_auth_result(),
                    runtime=make_empty_runtime_result(),
                    playwright=make_empty_playwright_result(),
                ).to_dict()
            )
            break

    report = {
        "issue_id": str(issue.get("id", "")),
        "mode": mode,
        "attempt_count": len(attempts),
        "final_passed": final_passed,
        "manual_verification_commands": [
            "mvn package",
            "java -jar target/*.jar",
        ],
        "generated_at": now_iso(),
        "attempts": attempts,
    }

    write_json(report_json_path, report)
    write_markdown(report_md_path, render_markdown_report(report))

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if final_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())