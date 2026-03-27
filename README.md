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

  這是一份非常紮實的 Agent 環境建置清單。為了讓你能在不同作業系統間無縫切換，我將你的步驟整理成 **Windows (PowerShell)**、**macOS (Zsh)** 以及 **Linux (Ubuntu/WSL)** 三種版本。

---

## 💻 跨平台 環境建置對照表

### ✅ Step 0 - 基礎環境安裝
| 工具 | **Windows (PowerShell)** | **macOS (Homebrew)** | **Linux (Ubuntu)** |
| :--- | :--- | :--- | :--- |
| **Python** | [官網下載](https://www.python.org/) | `brew install python` | `sudo apt install python3 python3-venv python3-pip` |
| **Node.js** | [官網下載](https://nodejs.org/) | `brew install node` | `curl -fsSL https://deb.nodesource.com/...` |
| **Git** | [官網下載](https://git-scm.com/) | `brew install git` | `sudo apt install git` |

---

### ✅ Step 1 ~ 3 - 初始化 Workspace 與 虛擬環境
在各平台打開終端機後執行：

* **Windows:**
    ```powershell
    mkdir ~/ai-dev-agent; cd ~/ai-dev-agent
    python -m venv venv
    .\venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip wheel setuptools
    ```
* **macOS / Linux:**
    ```zsh
    mkdir -p ~/ai-dev-agent && cd ~/ai-dev-agent
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip wheel setuptools
    ```

---

### ✅ Step 4 ~ 5 - Python 套件與 Playwright
這部分的 Python 指令在所有平台**完全相同**（需在虛擬環境內）：

```bash
# 安裝核心套件
pip install requests python-dotenv rich tenacity playwright pydantic gitpython tqdm pyyaml

# 安裝瀏覽器核心
playwright install chromium
```
> **注意：** macOS 不需要額外安裝中文字型（系統內建）；Windows 若有亂碼請確認系統語言設定；Linux 則必須執行 `sudo apt install` 那些 `libnss3` 等依賴項。

---

### ✅ Step 7 ~ 8 - Codex 與 API Key 設定
* **Windows (PowerShell):**
    ```powershell
    npm install -g @openai/codex
    # 永久加入環境變數
    [Environment]::SetEnvironmentVariable("OPENAI_API_KEY", "sk-xxxx", "User")
    # 需重啟終端機生效
    ```
* **macOS / Linux:**
    ```bash
    npm install -g @openai/codex
    echo 'export OPENAI_API_KEY="sk-xxxx"' >> ~/.zshrc # macOS 用 .zshrc, Linux 用 .bashrc
    source ~/.zshrc
    ```

---

### ✅ Step 9 - Git SSH 設定
所有平台指令雷同：
```bash
ssh-keygen -t ed25519
# 查看 Key 並貼到 GitHub
cat ~/.ssh/id_ed25519.pub  # macOS/Linux
type $HOME\.ssh\id_ed25519.pub # Windows
```

---

### ✅ Step 10 - Java & Maven
這是差異較大的部分：

* **Windows:**
    * 建議用 `winget` 或手動下載：
    ```powershell
    winget install Microsoft.OpenJDK.21
    winget install Apache.Maven
    ```
* **macOS:**
    ```zsh
    brew install openjdk@21 maven
    # 需設定 Java Home 指向 brew 路徑
    sudo ln -sfn $(brew --prefix)/opt/openjdk@21/libexec/openjdk.jdk /Library/Java/JavaVirtualMachines/openjdk-21.jdk
    ```
* **Linux (Ubuntu):**
    ```bash
    sudo apt install -y openjdk-21-jdk maven
    ```

---

### ✅ Step 11 ~ 12 - 執行 Agent
一旦環境齊備，執行指令在三平台**一致**：

```bash
# 啟動 Agent
python agent_loop.py --mode codex_only

# 測試專案編譯 (Maven)
mvn clean package -DskipTests
```

---

## 💡 總結建議

1.  **Windows 用戶：** 如果要在 Windows 原生環境跑（非 WSL），強烈建議安裝 [**PowerShell 7**](https://aka.ms/PSWindows) 體驗會更接近 Linux，且 `rm -rf` 等指令會比較好用。
2.  **macOS 用戶：** 遇到 `xcrun: error` 請記得執行 `xcode-select --install`。
3.  **環境變數：** Windows 更改環境變數後，VS Code 的內建終端機通常要**完全關掉重開**才會抓到新變數。


- Use a dedicated test account.
- Review commands before running them.
