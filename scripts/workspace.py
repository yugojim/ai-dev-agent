from pathlib import Path
from scripts.config import settings


def get_workspace_root() -> Path:
    return Path(settings.workspace_base_dir).expanduser().resolve(strict=False)


def get_issue_workspace(issue_no: int | str) -> Path:
    return get_workspace_root() / f"issue-{issue_no}"


def get_issue_branch(issue_no: int | str) -> str:
    return f"feat/{issue_no}"
