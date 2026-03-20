import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright


def is_cognito_url(url: str) -> bool:
    url = (url or "").lower()
    return "amazoncognito.com" in url or "/login/oauth2/" in url


def load_issue_context():
    """
    嘗試讀取 ../task_context/issue.json
    若存在且包含 URL / Role / Expected / Forbidden 才使用
    """
    issue_path = Path.cwd().parent / "task_context" / "issue.json"

    if not issue_path.exists():
        return None

    try:
        data = json.loads(issue_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    url = data.get("URL") or data.get("url")
    role = data.get("Role") or data.get("role")
    expected = data.get("Expected") or data.get("expected")
    forbidden = data.get("Forbidden") or data.get("forbidden")

    if url and role and expected is not None and forbidden is not None:
        if isinstance(expected, str):
            expected = [x.strip() for x in expected.split(",") if x.strip()]
        if isinstance(forbidden, str):
            forbidden = [x.strip() for x in forbidden.split(",") if x.strip()]

        return {
            "url": url,
            "role": role,
            "expected": expected,
            "forbidden": forbidden,
        }

    return None
    
def probe_auth_with_state(base_url: str, storage_state_path: str) -> bool:
    from playwright.sync_api import sync_playwright
    import os

    if not os.path.exists(storage_state_path):
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state_path)
        page = context.new_page()
        page.goto(base_url, timeout=30000)

        ok = "login" not in page.url.lower()

        browser.close()
        return ok
        
def bootstrap_login_and_save_state(base_url: str, storage_state_path: str):

    from playwright.sync_api import sync_playwright

    USER = "your_user"
    PASS = "your_password"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(base_url)

        page.fill("input[name='username']", USER)
        page.fill("input[name='password']", PASS)
        page.click("button[type='submit']")

        page.wait_for_load_state("networkidle")

        context.storage_state(path=storage_state_path)

        browser.close()

def main():
    if len(sys.argv) < 3:
        raise SystemExit(
            "Usage: python -m tests.playwright_runner <base_url> <output_dir> [storage_state]"
        )

    base_url = sys.argv[1]
    output_dir = Path(sys.argv[2])
    storage_state = sys.argv[3] if len(sys.argv) > 3 else None

    output_dir.mkdir(parents=True, exist_ok=True)

    issue_ctx = load_issue_context()

    if issue_ctx:
        target_path = issue_ctx["url"]
        expected_texts = issue_ctx["expected"]
        forbidden_texts = issue_ctx["forbidden"]
    else:
        target_path = "/"
        expected_texts = []
        forbidden_texts = []

    if target_path.startswith("http"):
        target_url = target_path
    else:
        target_url = f"{base_url.rstrip('/')}/{target_path.lstrip('/')}"

    result = {
        "passed": False,
        "summary": "",
        "expected": ", ".join(expected_texts) if expected_texts else "",
        "actual": "",
        "target_url": target_url,
        "screenshot": str(output_dir / "screenshot.png"),
        "console_log": str(output_dir / "console.log"),
    }

    console_lines = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            if storage_state:
                context = browser.new_context(storage_state=storage_state)
            else:
                context = browser.new_context()

            page = context.new_page()
            page.on("console", lambda msg: console_lines.append(f"{msg.type}: {msg.text}"))

            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

            final_url = page.url
            content = page.content().lower()

            (output_dir / "console.log").write_text(
                "\n".join(console_lines),
                encoding="utf-8",
            )

            page.screenshot(path=str(output_dir / "screenshot.png"), full_page=True)

            if is_cognito_url(final_url):
                result["summary"] = "Redirected to Cognito login"
                result["actual"] = final_url
            else:
                if "whitelabel error page" in content or "exception" in content:
                    result["summary"] = "Application page contains error text"
                    result["actual"] = "Detected error text"
                else:
                    missing = []
                    for txt in expected_texts:
                        if txt.lower() not in content:
                            missing.append(txt)

                    forbidden_found = []
                    for txt in forbidden_texts:
                        if txt.lower() in content:
                            forbidden_found.append(txt)

                    if missing:
                        result["summary"] = "Expected text missing"
                        result["actual"] = f"Missing: {', '.join(missing)}"
                    elif forbidden_found:
                        result["summary"] = "Forbidden text found"
                        result["actual"] = f"Forbidden present: {', '.join(forbidden_found)}"
                    else:
                        result["passed"] = True
                        result["summary"] = "Page validation passed"
                        result["actual"] = final_url

            browser.close()

    except Exception as e:
        result["summary"] = "Playwright execution failed"
        result["actual"] = str(e)

    (output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    
def run_smoke_test(base_url, storage_state_path, screenshot_dir):
    from playwright.sync_api import sync_playwright
    import time
    import os

    os.makedirs(screenshot_dir, exist_ok=True)
    shot = f"{screenshot_dir}/smoke_{int(time.time())}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state_path)
        page = context.new_page()

        page.goto(base_url)
        page.wait_for_load_state("networkidle")

        page.screenshot(path=shot)

        ok = "login" not in page.url.lower()

        browser.close()

        return ok, shot

if __name__ == "__main__":
    main()