import shutil
import subprocess
from pathlib import Path


def run(cmd, cwd=None, check=True):
    print(f"$ {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)

    if r.stdout:
        print(r.stdout.strip())
    if r.stderr:
        print(r.stderr.strip())

    if check and r.returncode != 0:
        raise RuntimeError(r.stderr or f"Command failed: {' '.join(cmd)}")

    return r


def is_git_repo(repo_dir: Path) -> bool:
    return (repo_dir / ".git").exists()


def clean_repo_dir(repo_dir: Path):
    if repo_dir.exists():
        print(f"Cleaning repo dir: {repo_dir}")
        shutil.rmtree(repo_dir, ignore_errors=True)


def reclone_repo(repo_url: str, repo_dir: Path):
    if repo_dir.exists():
        raise RuntimeError(f"Repo dir still exists before clone: {repo_dir}")

    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning repo into {repo_dir}")
    run(["git", "clone", repo_url, str(repo_dir)])


def git_self_heal_switch_develop(repo_url: str, repo_dir: Path):
    if not repo_dir.exists():
        reclone_repo(repo_url, repo_dir)
    elif not is_git_repo(repo_dir):
        print("Detected broken repo. Recreating...")
        clean_repo_dir(repo_dir)
        reclone_repo(repo_url, repo_dir)

    r = run(["git", "switch", "develop"], cwd=repo_dir, check=False)

    if r.returncode == 0:
        return

    print("Detected git switch failure → reclone")
    clean_repo_dir(repo_dir)
    reclone_repo(repo_url, repo_dir)
    run(["git", "switch", "develop"], cwd=repo_dir)