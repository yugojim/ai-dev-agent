# AI Dev Agent Starter Kit

A minimal local agent that:

- reads Redmine issues via REST API
- prepares a per-issue workspace
- clones or repairs a Git repo into that workspace
- runs build and runtime checks locally
- captures Playwright screenshots
- writes results back to Redmine

## 1. Prerequisites

- Python 3.11+
- Node.js 20+
- Git
- Playwright browsers
- Optional: Codex CLI
- For Java projects: Maven or Gradle

## 2. Setup

### Windows PowerShell

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install
New-Item -Path .env -ItemType File
```

### macOS / Linux

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install
touch .env
```

If Playwright screenshots show missing Chinese fonts on macOS/Linux, you can run:

```bash
bash scripts/check_zh_fonts.sh
```

## 3. Configure `.env`

Fill in your own secrets in `.env`. Never commit `.env`.

Required variables:

```env
REDMINE_BASE_URL=https://your-redmine.example.com
REDMINE_API_KEY=your_api_key
REPO_URL=https://your.git/repo.git
REPO_SSH_URL=git@your.git:repo.git
```

Useful optional variables:

```env
WORKSPACE_BASE_DIR=~/ai-workspaces
APP_PORT=8080
APP_START_TIMEOUT=600
HEALTHCHECK_INTERVAL=3
PLAYWRIGHT_TEST_LOGIN_USERNAME=tester
PLAYWRIGHT_TEST_LOGIN_CHINESE_NAME=測試使用者
REDMINE_STATUS_ID_IN_PROGRESS=2
REDMINE_PRIORITY_ID_NORMAL=4
REDMINE_VERIFY_SSL=true
REPO_DIR=
```

Notes:

- `WORKSPACE_BASE_DIR` now uses a cross-platform path resolved by Python `pathlib`.
- On Windows you can also set `WORKSPACE_BASE_DIR` to a native path such as `C:\ai-workspaces`.
- If `WORKSPACE_BASE_DIR` is not set, the default is `<home>/ai-workspaces`.
- `REPO_DIR` is only needed if you use `scripts/shell_runner.py`.

## 4. Workspace layout

Each issue gets its own workspace:

```text
<WORKSPACE_BASE_DIR>/
  issue-12345/
    attachments/
    repo/
    runtime/
    task_context/
    report/
```

## 5. Common commands

### Fetch the next assigned low-priority issue from Redmine

```bash
python scripts/redmine_tool.py
```

### Prepare the next issue workspace automatically

```bash
python scripts/repo_tool.py prepare-next-issue
```

### Prepare a specific issue workspace

```bash
python scripts/repo_tool.py prepare-issue 12345
```

### Check Git status for an issue workspace

```bash
python scripts/repo_tool.py status 12345
```

### Finalize and push an issue branch

```bash
python scripts/repo_tool.py finalize-issue 12345
```

### Run the guided local loop

```bash
python agent_loop.py
```

## 6. What `agent_loop.py` does

The loop currently performs this flow:

1. Fetch one issue from Redmine.
2. Create `issue-<id>` workspace folders.
3. Write `task_context/issue.json` and `task_context/prompt.txt`.
4. Clone the repository into `workspace/repo`.
5. Create or switch to `feat/<issue_id>`.
6. Run the current Codex phase stub.
7. Run build validation.
8. Start the app locally.
9. Wait for health checks on `http://localhost:<APP_PORT>`.
10. Run Playwright capture and save runtime artifacts.
11. If checks pass, commit, rebase, and push.
12. Upload the report back to Redmine.

## 7. Cross-platform behavior

The runtime flow is now designed for both macOS and Windows:

- Workspace paths are built with `pathlib`, not hardcoded slash paths.
- Gradle uses `gradlew.bat` on Windows and `gradlew` on macOS.
- App start/stop uses Python `subprocess` handling instead of `bash`, `nohup`, or `kill`.
- Default workspace resolution uses your current user's home directory on both platforms.

## 8. Codex CLI examples

```bash
codex --help
codex exec -m gpt-5.4 -s workspace-write -C /path/to/repo "Fix the Redmine issue in task_context/prompt.txt"
```

## 9. Security

- Rotate any secret that was ever pasted into chat.
- Keep secrets only in `.env`.
- Use a dedicated test account.
- Review commands before running them.
