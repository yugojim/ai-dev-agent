from __future__ import annotations

import argparse
import subprocess

from scripts.config import settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="+", help="Command to run inside REPO_DIR")
    args = parser.parse_args()
    subprocess.run(args.command, cwd=settings.repo_dir, check=True)


if __name__ == "__main__":
    main()
