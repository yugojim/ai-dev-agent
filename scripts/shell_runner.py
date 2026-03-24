from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from scripts.config import settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="+", help="Command to run inside REPO_DIR")
    args = parser.parse_args()
    repo_dir = Path(settings.repo_dir or os.getenv("REPO_DIR", "") or Path.cwd())
    subprocess.run(args.command, cwd=repo_dir, check=True)


if __name__ == "__main__":
    main()
