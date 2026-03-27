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
2. Refresh the full issue detail and download attachments.
3. Create `issue-<id>` workspace folders.
4. Write `task_context/issue.json` and `task_context/prompt.txt`.
5. Clone the repository into `workspace/repo`.
6. Create or switch to `feat/<issue_id>`.
7. Run Codex CLI against the generated prompt.
8. Run build validation.
9. Start the app locally.
10. Wait for health checks on `http://localhost:<APP_PORT>`.
11. Run Playwright capture, execute `validation.steps`, and save runtime artifacts.
12. If checks pass, commit, rebase, and push.
13. Upload the report back to Redmine.

## 7. Cross-platform behavior

The runtime flow is now designed for both macOS and Windows:

- Workspace paths are built with `pathlib`, not hardcoded slash paths.
- Gradle uses `gradlew.bat` on Windows and `gradlew` on macOS.
- App start/stop uses Python `subprocess` handling instead of `bash`, `nohup`, or `kill`.
- Default workspace resolution uses your current user's home directory on both platforms.

## 8. Codex CLI examples

```bash
codex --help
codex exec -m gpt-5.4 -s danger-full-access -C /path/to/repo "Fix the Redmine issue in task_context/prompt.txt"
```

## 9. Recommended Redmine issue format

The parser currently reads these sections from the issue description:

- `[Requirements]`
- `[Validation]`
- `[Steps]`

For UI text-change tickets, do not rely on screenshots alone. Always include the old text, the new text, and the affected screens in plain text.

Use this template:

```text
[Summary]
將新案申請書與 PI 回覆頁面的附件欄位標題由「備註」改為「說明」

[Problem]
目前附件欄位標題顯示為「備註」，與最新流程文件不一致，使用者容易誤解欄位用途。

[Goal]
相關頁面的附件欄位標題統一顯示為「說明」。

[Requirements]
- 新案申請書頁面的附件欄位標題由「備註」改為「說明」
- PI 回覆頁面的附件欄位標題由「備註」改為「說明」
- 僅修改本票指定畫面的顯示文字，不調整其他流程或欄位

[Validation]
URL: /
Role: reviewer
Expected: 說明
Forbidden: 備註

[Steps]
1. open=新案申請書
2. check=附件欄位標題顯示「說明」
3. open=PI回覆
4. check=附件欄位標題顯示「說明」

[Scope]
- 僅調整附件欄位標題
- 不修改資料結構、API、權限或版面配置

[Notes]
- 若附上截圖，請同步在文字敘述中寫清楚紅框位置與新舊文案
- 若系統使用 i18n/message key，請優先修改對應字串來源，不要直接硬寫在畫面元件內
```

Issue writing rules:

- 每個需求都要能直接轉成程式修改，不要只寫「如附件」
- UI 驗證請盡量提供 `Role` 與 `[Steps]`，讓 Codex / Playwright 可以自動從登入後一路導航到目標頁
- `Steps` 建議使用單一步驟單一動作格式，例如 `open=/feature/list`、`click=text=查詢`、`click=css=.menu-item`、`wait=2000`、`check=text=結果清單`
- Codex 在實作完成後可視需要補強 `task_context/issue.json` 的 `validation.steps`，讓 Playwright 報告顯示實際測試角色、流程與逐步截圖
- 每個畫面都要分開列出，避免模型自行推論影響範圍
- `Expected` / `Forbidden` 要填可比對的字串
- `Steps` 要描述真實操作，不要只寫「進入頁面後確認」
- 若附件是唯一資訊來源，請另外補一段文字摘要，說明紅框位置與預期結果

## 10. Security

- Rotate any secret that was ever pasted into chat.
- Keep secrets only in `.env`.
- Use a dedicated test account.
- Review commands before running them.
