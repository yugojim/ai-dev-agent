# AI Dev Agent Starter Kit

A minimal local agent that:

- reads Redmine issues via REST API
- clones / updates a Git repo
- asks you to use ChatGPT Web for reasoning
- runs shell commands locally
- runs Playwright smoke tests
- writes results back to Redmine

## 1. Prerequisites

- Python 3.11+
- Node.js 20+
- Git
- Playwright browsers
- Optional: Codex CLI

## 2. Setup

### Windows PowerShell

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install
copy .env.example .env
```

### macOS / Linux

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
bash scripts/install_playwright_fonts.sh
playwright install
cp .env.example .env
```

If Playwright screenshots show Chinese as tofu boxes, verify the runtime fonts:

```bash
bash scripts/check_zh_fonts.sh
```

## 3. Configure `.env`

Fill in your own secrets in `.env`.
Never commit `.env`.

## 4. Common commands

### Fetch one Redmine issue

```bash
python scripts/redmine_tool.py --next
```

### Clone or pull repo

```bash
python scripts/repo_tool.py sync
```

### Create working branch

```bash
python scripts/repo_tool.py branch 12345-fix-login
```

### Run smoke tests

```bash
python tests/playwright_smoke.py
```

### Run the guided local loop

```bash
python agent_loop.py
```

## 5. Suggested workflow

1. `python agent_loop.py`
2. The tool fetches one Redmine issue.
3. Paste the issue into ChatGPT Web and get a fix plan.
4. Use Codex CLI or edit code manually.
5. Run build/tests.
6. Run Playwright smoke test.
7. The script can post a Redmine note and optionally move the issue status.

## 6. Codex CLI examples

```bash
codex --help
codex edit "Fix issue #12345 based on the Redmine description and failing test output"
```

## 7. Security

- Rotate any secret that was ever pasted into chat.
- Keep secrets only in `.env`.
- Use a dedicated test account.
- Review commands before running them.
