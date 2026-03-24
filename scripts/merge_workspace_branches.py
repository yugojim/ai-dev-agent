import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path

DEFAULT_WORKSPACE_ROOT = os.environ.get(
    "WORKSPACE_BASE_DIR",
    str(Path.home() / "ai-workspaces"),
)


def get_issue_branch(issue_no: int | str) -> str:
    return f"feat/{issue_no}"


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    print(f"$ git {' '.join(args)}")
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )

    if proc.stdout:
        print(proc.stdout.strip())
    if proc.stderr:
        print(proc.stderr.strip())

    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"git command failed: {' '.join(args)}")

    return proc


def issue_no_from_workspace(path: Path) -> str | None:
    match = re.fullmatch(r"issue-(\d+)", path.name)
    return match.group(1) if match else None


def iter_workspace_repos(workspace_root: Path) -> list[tuple[str, Path]]:
    repos: list[tuple[str, Path]] = []
    for child in sorted(workspace_root.iterdir()):
        if not child.is_dir():
            continue
        issue_no = issue_no_from_workspace(child)
        if not issue_no:
            continue
        repo_dir = child / "repo"
        if (repo_dir / ".git").exists():
            repos.append((issue_no, repo_dir))
    return repos


def get_current_branch(repo_dir: Path) -> str:
    proc = run_git(["branch", "--show-current"], repo_dir)
    branch = (proc.stdout or "").strip()
    if not branch:
        raise RuntimeError(f"Cannot determine current branch in {repo_dir}")
    return branch


def branch_exists(repo_dir: Path, branch: str) -> bool:
    local = run_git(["show-ref", "--verify", f"refs/heads/{branch}"], repo_dir, check=False)
    if local.returncode == 0:
        return True

    remote = run_git(["ls-remote", "--heads", "origin", branch], repo_dir, check=False)
    return bool((remote.stdout or "").strip())


def origin_url(repo_dir: Path) -> str:
    proc = run_git(["remote", "get-url", "origin"], repo_dir)
    url = (proc.stdout or "").strip()
    if not url:
        raise RuntimeError(f"origin remote not found in {repo_dir}")
    return url


def sanitize_branch_for_dir(branch: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", branch).strip("_") or "merged"


def prepare_output_repo(output_repo: Path, origin: str, base_branch: str, new_branch: str, force: bool) -> None:
    if output_repo.exists() and force:
        shutil.rmtree(output_repo)

    if not output_repo.exists():
        output_repo.parent.mkdir(parents=True, exist_ok=True)
        run_git(["clone", origin, str(output_repo)], output_repo.parent)

    run_git(["fetch", "origin"], output_repo)
    run_git(["checkout", base_branch], output_repo, check=False)
    run_git(["reset", "--hard", f"origin/{base_branch}"], output_repo)
    run_git(["clean", "-fd"], output_repo)
    run_git(["checkout", "-B", new_branch], output_repo)


def merge_branch_into_output(output_repo: Path, source_repo: Path, branch: str) -> None:
    run_git(["fetch", str(source_repo), branch], output_repo)
    run_git(
        ["merge", "--no-ff", "--no-edit", "FETCH_HEAD"],
        output_repo,
    )


def collect_sources(workspace_root: Path, branch_mode: str) -> list[tuple[str, Path, str]]:
    sources: list[tuple[str, Path, str]] = []
    missing: list[str] = []

    for issue_no, repo_dir in iter_workspace_repos(workspace_root):
        branch = get_issue_branch(issue_no) if branch_mode == "issue" else get_current_branch(repo_dir)
        if branch_exists(repo_dir, branch):
            sources.append((issue_no, repo_dir, branch))
        else:
            missing.append(f"issue-{issue_no}: {branch}")

    if missing:
        print("[merge] skipped workspaces with missing branches:")
        for item in missing:
            print(f"- {item}")

    return sources


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge all workspace branches under ai-workspaces into one new branch."
    )
    parser.add_argument(
        "--workspace-root",
        default=DEFAULT_WORKSPACE_ROOT,
        help="Workspace root containing issue-* directories",
    )
    parser.add_argument(
        "--base-branch",
        default="develop",
        help="Base branch for the new integration branch",
    )
    parser.add_argument(
        "--new-branch",
        required=True,
        help="Name of the new integration branch to create",
    )
    parser.add_argument(
        "--output-repo",
        help="Path to the integration repo clone. Default: <workspace-root>/_merge_<branch>",
    )
    parser.add_argument(
        "--branch-mode",
        choices=["issue", "current"],
        default="issue",
        help="Use feat/<issue> or each repo's current branch",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Push the final merged branch to origin",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete and recreate output repo if it already exists",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list the branches that would be merged",
    )
    args = parser.parse_args()

    workspace_root = Path(args.workspace_root).expanduser().resolve()
    if not workspace_root.exists():
        raise RuntimeError(f"Workspace root does not exist: {workspace_root}")

    sources = collect_sources(workspace_root, args.branch_mode)
    if not sources:
        raise RuntimeError(f"No mergeable branches found under {workspace_root}")

    print("[merge] candidate branches:")
    for issue_no, repo_dir, branch in sources:
        print(f"- issue-{issue_no}: {branch} ({repo_dir})")

    if args.dry_run:
        return

    first_origin = origin_url(sources[0][1])
    for issue_no, repo_dir, _branch in sources[1:]:
        repo_origin = origin_url(repo_dir)
        if repo_origin != first_origin:
            raise RuntimeError(
                f"Origin mismatch: issue-{issue_no} uses {repo_origin}, expected {first_origin}"
            )

    output_repo = (
        Path(args.output_repo).expanduser().resolve()
        if args.output_repo
        else (workspace_root / f"_merge_{sanitize_branch_for_dir(args.new_branch)}")
    )

    print(f"[merge] output repo: {output_repo}")
    prepare_output_repo(output_repo, first_origin, args.base_branch, args.new_branch, args.force)

    for issue_no, repo_dir, branch in sources:
        print(f"[merge] merging issue-{issue_no} branch {branch}")
        try:
            merge_branch_into_output(output_repo, repo_dir, branch)
        except Exception as exc:
            print(f"[merge] merge stopped at issue-{issue_no} branch {branch}: {exc}")
            print(f"[merge] resolve conflicts in: {output_repo}")
            raise

    if args.push:
        run_git(["push", "-u", "origin", args.new_branch], output_repo)

    print("[merge] completed")
    print(f"[merge] integration repo: {output_repo}")
    print(f"[merge] integration branch: {args.new_branch}")


if __name__ == "__main__":
    main()
