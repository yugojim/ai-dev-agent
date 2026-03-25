# scripts/post_execution_redmine_update.py
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from scripts.redmine_writer import (
    attach_and_update_from_workspace,
    RedmineWriterError,
)

load_dotenv()


def parse_args():
    parser = argparse.ArgumentParser(
        description="將 agent 執行產物上傳到 Redmine，並更新議題欄位。"
    )
    parser.add_argument("--issue-id", required=True, help="Redmine 議題編號")
    parser.add_argument("--workspace-dir", required=True, help="工作目錄路徑")
    parser.add_argument("--status-id", required=True, type=int, help="狀態編號")
    parser.add_argument("--priority-id", required=True, type=int, help="優先級編號")
    parser.add_argument("--no-report-json", action="store_true")
    parser.add_argument("--no-report-md", action="store_true")
    parser.add_argument("--no-screenshots", action="store_true")
    parser.add_argument("--attach-runtime-log", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    workspace_dir = Path(args.workspace_dir)

    if not workspace_dir.exists():
        print(f"找不到工作目錄：{workspace_dir}", file=sys.stderr)
        return 2

    try:
        result = attach_and_update_from_workspace(
            issue_id=args.issue_id,
            workspace_dir=workspace_dir,
            status_id=args.status_id,
            priority_id=args.priority_id,
            include_report_json=not args.no_report_json,
            include_report_md=not args.no_report_md,
            include_screenshots=not args.no_screenshots,
            include_latest_runtime_log=args.attach_runtime_log,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except RedmineWriterError as e:
        print(f"Redmine 回寫錯誤：{e}", file=sys.stderr)
        return 3
    except Exception as e:
        print(f"未預期錯誤 {type(e).__name__}：{e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
