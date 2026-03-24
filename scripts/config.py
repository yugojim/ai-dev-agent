from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def resolve_workspace_base_dir(raw_path: str) -> Path:
    base_dir = Path(raw_path).expanduser() if raw_path else (Path.home() / "ai-workspaces")
    return base_dir.resolve(strict=False)


@dataclass
class Settings:
    repo_url: str
    repo_ssh_url: str
    workspace_base_dir: str
    repo_dir: str
    redmine_base_url: str
    redmine_api_key: str
    openai_api_key: str = ""
    test_username: str = ""
    test_password: str = ""


settings = Settings(
    repo_url=os.getenv("REPO_URL", ""),
    repo_ssh_url=os.getenv("REPO_SSH_URL", ""),
    workspace_base_dir=str(resolve_workspace_base_dir(os.getenv("WORKSPACE_BASE_DIR", ""))),
    repo_dir=os.getenv("REPO_DIR", ""),
    redmine_base_url=os.getenv("REDMINE_BASE_URL", ""),
    redmine_api_key=os.getenv("REDMINE_API_KEY", ""),
    openai_api_key=os.getenv("OPENAI_API_KEY", ""),
    test_username=os.getenv("TEST_USERNAME", ""),
    test_password=os.getenv("TEST_PASSWORD", ""),
)
