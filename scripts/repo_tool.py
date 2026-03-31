import argparse
import json
import subprocess
from pathlib import Path

from scripts.config import settings
from scripts.git_recovery import git_self_heal_switch_develop
from scripts.redmine_tool import (
    download_issue_attachments,
    get_first_low_priority_issue,
    get_issue_detail,
)
from scripts.task_context_builder import build_context_issue
from scripts.workspace import get_issue_branch, get_issue_workspace


def run_git(args, repo_dir: Path, check=True):
    print(f"$ git {' '.join(args)}")
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        text=True,
        capture_output=True,
    )

    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip())

    if check and result.returncode != 0:
        stderr = result.stderr or ""
        if "Password authentication is not supported" in stderr:
            raise RuntimeError(
                "GitHub HTTPS password auth is not supported. "
                "Please use SSH remote or a Personal Access Token."
            )
        raise RuntimeError(stderr or f"git command failed: {' '.join(args)}")

    return result


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_repo_dir(issue_no: int | str) -> Path:
    # 每張票一個 workspace，repo 放在底下固定資料夾 repo/
    return get_issue_workspace(issue_no) / "repo"


def get_attachments_dir(issue_no: int | str) -> Path:
    return get_issue_workspace(issue_no) / "attachments"


def get_task_context_dir(issue_no: int | str) -> Path:
    return get_issue_workspace(issue_no) / "task_context"


def write_issue_json(issue_no: int | str, issue_detail: dict) -> Path:
    task_context_dir = ensure_dir(get_task_context_dir(issue_no))
    output = task_context_dir / "issue.json"
    issue_detail = build_context_issue(issue_detail)
    output.write_text(
        json.dumps(issue_detail, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


def write_prompt_txt(issue_no: int | str, issue_detail: dict) -> Path:
    task_context_dir = ensure_dir(get_task_context_dir(issue_no))
    attachments_dir = get_attachments_dir(issue_no)
    repo_dir = get_repo_dir(issue_no)
    issue_detail = build_context_issue(issue_detail)

    raw = issue_detail.get("raw", {}) or issue_detail
    issue_id = issue_detail.get("id") or raw.get("id")
    subject = issue_detail.get("summary", "") or raw.get("subject", "")
    description = raw.get("description", "") or ""
    project = (raw.get("project") or {}).get("name", "")
    priority = (raw.get("priority") or {}).get("name", "")
    status = (raw.get("status") or {}).get("name", "")
    attachments = raw.get("attachments", [])
    requirements = issue_detail.get("requirements", []) or []
    validation = issue_detail.get("validation", {}) or {}
    prompt_focus = issue_detail.get("prompt_focus", []) or []
    rewrite_warnings = issue_detail.get("rewrite_warnings", []) or []

    attachment_lines = []
    for att in attachments:
        filename = att.get("filename", "")
        attachment_lines.append(f"- attachments/{filename}")

    if not attachment_lines:
        attachment_lines.append("- (no attachments)")

    prompt = f"""Issue #{issue_id}
Title: {subject}
Project: {project}
Priority: {priority}
Status: {status}

Description:
{description}

Requirements:
{chr(10).join(f"- {item}" for item in requirements) if requirements else "- (no structured requirements)"}

Validation:
- URL: {validation.get("url", "/")}
- Role: {validation.get("role", "") or "(not specified)"}
- Expected: {", ".join(validation.get("expected", [])) or "(none)"}
- Forbidden: {", ".join(validation.get("forbidden", [])) or "(none)"}
- Steps:
{chr(10).join(f"  - {json.dumps(step, ensure_ascii=False)}" for step in validation.get("steps", []) or []) if validation.get("steps") else "  - (no structured steps)"}

Implementation Focus:
{chr(10).join(f"- {item}" for item in prompt_focus) if prompt_focus else "- (none)"}

Rewrite Warnings:
{chr(10).join(f"- {item}" for item in rewrite_warnings) if rewrite_warnings else "- (none)"}

Workspace:
- Repo: {repo_dir}
- Attachments: {attachments_dir}

Please do the following:
1. Inspect the repository code in this workspace.
2. Read issue details from task_context/issue.json.
3. Review any downloaded attachments under attachments/.
4. Implement the requested fix or enhancement.
5. Run relevant tests.
6. Keep changes limited to this ticket.

Attachments:
{chr(10).join(attachment_lines)}
"""

    output = task_context_dir / "prompt.txt"
    output.write_text(prompt, encoding="utf-8")
    return output


def ensure_ssh_remote(repo_dir: Path) -> None:
    ssh_url = settings.repo_ssh_url.strip()
    if not ssh_url:
        return

    current = run_git(["remote", "get-url", "origin"], repo_dir, check=False)
    current_url = (current.stdout or "").strip()

    if current_url != ssh_url:
        print(f"Updating origin remote to SSH: {ssh_url}")
        run_git(["remote", "set-url", "origin", ssh_url], repo_dir)


def prepare_next_issue():
    issue = get_first_low_priority_issue()

    if not issue:
        print("No low priority issue found.")
        return

    issue_no = issue["id"]
    subject = issue["subject"]
    workspace_dir = get_issue_workspace(issue_no)
    repo_dir = get_repo_dir(issue_no)
    attachments_dir = get_attachments_dir(issue_no)
    branch_name = get_issue_branch(issue_no)

    print(f"\nNext Issue → #{issue_no} {subject}")
    print(f"Workspace: {workspace_dir}")
    print(f"Repo dir: {repo_dir}")
    print(f"Preparing branch: {branch_name}\n")

    ensure_dir(workspace_dir)
    ensure_dir(attachments_dir)
    ensure_dir(get_task_context_dir(issue_no))

    git_self_heal_switch_develop(
        settings.repo_ssh_url or settings.repo_url,
        repo_dir,
    )

    ensure_ssh_remote(repo_dir)

    run_git(["fetch", "origin"], repo_dir, check=False)
    run_git(["pull", "origin", "develop"], repo_dir)

    exists = run_git(["branch", "--list", branch_name], repo_dir, check=False)
    if branch_name in (exists.stdout or ""):
        print(f"Branch already exists locally, switching to {branch_name}")
        run_git(["switch", branch_name], repo_dir)
    else:
        run_git(["checkout", "-b", branch_name], repo_dir)

    issue_detail = build_context_issue(get_issue_detail(issue_no))
    downloaded_files = download_issue_attachments(issue_no, attachments_dir)
    issue_json_path = write_issue_json(issue_no, issue_detail)
    prompt_path = write_prompt_txt(issue_no, issue_detail)

    print("\nReady:")
    print(f"- issue: #{issue_no}")
    print(f"- workspace: {workspace_dir}")
    print(f"- repo: {repo_dir}")
    print(f"- branch: {branch_name}")
    print(f"- issue json: {issue_json_path}")
    print(f"- prompt: {prompt_path}")
    print(f"- downloaded attachments: {len(downloaded_files)}")


def repo_status(issue_no: str):
    repo_dir = get_repo_dir(issue_no)
    run_git(["status"], repo_dir)


def finalize_issue(issue_no: str, commit_all: bool = True):
    repo_dir = get_repo_dir(issue_no)
    branch_name = get_issue_branch(issue_no)

    if not repo_dir.exists():
        raise RuntimeError(f"Repo dir does not exist: {repo_dir}")

    if commit_all:
        run_git(["add", "."], repo_dir)

    commit_result = run_git(["commit", "-m", branch_name], repo_dir, check=False)
    if commit_result.returncode != 0:
        stderr = (commit_result.stderr or "").lower()
        stdout = (commit_result.stdout or "").lower()
        if "nothing to commit" not in stderr and "nothing to commit" not in stdout:
            raise RuntimeError(commit_result.stderr or "git commit failed")

    run_git(["fetch", "origin"], repo_dir, check=False)

    rebase_result = run_git(["rebase", "origin/develop"], repo_dir, check=False)
    if rebase_result.returncode != 0:
        print(
            "\nRebase conflict detected.\n"
            f"Please resolve conflicts in: {repo_dir}\n"
            "Then run:\n"
            "  git add .\n"
            "  git rebase --continue\n"
        )
        raise RuntimeError("rebase failed")

    run_git(["push", "-u", "origin", branch_name], repo_dir)
    print(f"\nFinalized and pushed: {branch_name}")
    
def prepare_issue(issue_no: str):
    issue_detail = get_issue_detail(issue_no)
    subject = issue_detail.get("subject", "")
    workspace_dir = get_issue_workspace(issue_no)
    repo_dir = get_repo_dir(issue_no)
    attachments_dir = get_attachments_dir(issue_no)
    branch_name = get_issue_branch(issue_no)

    print(f"\nIssue → #{issue_no} {subject}")
    print(f"Workspace: {workspace_dir}")
    print(f"Repo dir: {repo_dir}")
    print(f"Preparing branch: {branch_name}\n")

    ensure_dir(workspace_dir)
    ensure_dir(attachments_dir)
    ensure_dir(get_task_context_dir(issue_no))

    git_self_heal_switch_develop(
        settings.repo_ssh_url or settings.repo_url,
        repo_dir,
    )

    ensure_ssh_remote(repo_dir)

    run_git(["fetch", "origin"], repo_dir, check=False)
    run_git(["pull", "origin", "develop"], repo_dir)

    exists = run_git(["branch", "--list", branch_name], repo_dir, check=False)
    if branch_name in (exists.stdout or ""):
        print(f"Branch already exists locally, switching to {branch_name}")
        run_git(["switch", branch_name], repo_dir)
    else:
        run_git(["checkout", "-b", branch_name], repo_dir)

    downloaded_files = download_issue_attachments(issue_no, attachments_dir)
    issue_json_path = write_issue_json(issue_no, issue_detail)
    prompt_path = write_prompt_txt(issue_no, issue_detail)

    print("\nReady:")
    print(f"- issue: #{issue_no}")
    print(f"- workspace: {workspace_dir}")
    print(f"- repo: {repo_dir}")
    print(f"- branch: {branch_name}")
    print(f"- issue json: {issue_json_path}")
    print(f"- prompt: {prompt_path}")
    print(f"- downloaded attachments: {len(downloaded_files)}")

def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    subparsers.add_parser("prepare-next-issue")

    p_status = subparsers.add_parser("status")
    p_status.add_argument("issue_no")
    
    p_prepare = subparsers.add_parser("prepare-issue")
    p_prepare.add_argument("issue_no")

    p_finalize = subparsers.add_parser("finalize-issue")
    p_finalize.add_argument("issue_no")

    args = parser.parse_args()

    if args.cmd == "prepare-next-issue":
        prepare_next_issue()
    elif args.cmd == "status":
        repo_status(args.issue_no)
    elif args.cmd == "finalize-issue":
        finalize_issue(args.issue_no)
    elif args.cmd == "prepare-issue":
        prepare_issue(args.issue_no)


if __name__ == "__main__":
    main()
