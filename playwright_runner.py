import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


def is_cognito_url(url: str) -> bool:
    url = (url or "").lower()
    return (
        "amazoncognito.com" in url
        or "/oauth2/authorize" in url
        or "/oauth2/idpresponse" in url
        or ("cognito" in url and "login" in url)
    )


def normalize_target_path(target_path: str) -> str:
    target_path = (target_path or "/").strip()
    if not target_path:
        return "/"
    if target_path.startswith("http://") or target_path.startswith("https://"):
        return target_path
    if not target_path.startswith("/"):
        return f"/{target_path}"
    return target_path


def build_target_url(base_url: str, target_path: str) -> str:
    target_path = normalize_target_path(target_path)
    if target_path.startswith("http://") or target_path.startswith("https://"):
        return target_path
    return f"{base_url.rstrip('/')}/{target_path.lstrip('/')}"


def app_origin(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def app_host(base_url: str) -> str:
    return urlparse(base_url).netloc


def load_issue_context() -> dict | None:
    issue_path = Path.cwd().parent / "task_context" / "issue.json"
    if not issue_path.exists():
        return None

    try:
        data = json.loads(issue_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    url = data.get("URL") or data.get("url") or "/"
    role = data.get("Role") or data.get("role") or ""
    expected = data.get("Expected") or data.get("expected") or []
    forbidden = data.get("Forbidden") or data.get("forbidden") or []

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


def first_visible_selector(page, selectors: list[str], timeout: int = 2500) -> str | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            return selector
        except Exception:
            continue
    return None


def click_first_visible(page, selectors: list[str], timeout: int = 2500) -> bool:
    selector = first_visible_selector(page, selectors, timeout=timeout)
    if not selector:
        return False
    page.click(selector)
    return True


def save_storage_state(context, storage_state_path: str | None) -> None:
    if not storage_state_path:
        return
    path = Path(storage_state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))
    print(f"[auth] storage state saved: {path}")


def try_close_optional_dialogs(page) -> None:
    possible_buttons = [
        'button:has-text("Skip")',
        'button:has-text("略過")',
        'button:has-text("Cancel")',
        'button:has-text("取消")',
        'button:has-text("Close")',
        'button:has-text("關閉")',
    ]
    for selector in possible_buttons:
        try:
            locator = page.locator(selector).first
            if locator.is_visible(timeout=800):
                locator.click(timeout=1500)
                page.wait_for_timeout(800)
        except Exception:
            continue


def wait_until_back_to_app(page, base_url: str, timeout_ms: int = 40000) -> bool:
    host = app_host(base_url)
    origin = app_origin(base_url)

    try:
        page.wait_for_url(lambda url: host in url, timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass
        print(f"[auth] redirected back to app: {page.url}")
        return True
    except PlaywrightTimeoutError:
        pass

    try:
        page.goto(origin, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(2500)
        if not is_cognito_url(page.url):
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            print(f"[auth] reached app after manual origin goto: {page.url}")
            return True
    except Exception:
        pass

    return False


def probe_auth_with_state(browser, base_url: str, storage_state_path: str):
    state_path = Path(storage_state_path)
    if not state_path.exists():
        return None

    context = None
    try:
        context = browser.new_context(storage_state=str(state_path))
        page = context.new_page()
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2500)

        if is_cognito_url(page.url):
            print(f"[auth] existing storage_state invalid, redirected to Cognito: {page.url}")
            page.close()
            context.close()
            return None

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass

        print(f"[auth] reuse storage_state success: {state_path}")
        page.close()
        return context
    except Exception as e:
        print(f"[auth] probe storage_state failed: {e}")
        if context:
            try:
                context.close()
            except Exception:
                pass
        return None


def cognito_form_login(context, base_url: str, username: str, password: str, output_dir: Path) -> bool:
    page = context.new_page()

    try:
        print("[auth] open app and trigger Cognito login")
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        if not is_cognito_url(page.url):
            print("[auth] already authenticated without Cognito form")
            page.close()
            return True

        username_selectors = [
            'input[name="username"]',
            'input[id="username"]',
            'input[data-testid="username"]',
            'input[autocomplete="username"]',
            'input[type="email"]',
            'input[placeholder*="Username"]',
            'input[placeholder*="username"]',
            'input[placeholder*="帳號"]',
            'input[placeholder*="使用者"]',
            'input[type="text"]',
        ]
        password_selectors = [
            'input[name="password"]',
            'input[id="password"]',
            'input[data-testid="password"]',
            'input[autocomplete="current-password"]',
            'input[type="password"]',
            'input[placeholder*="Password"]',
            'input[placeholder*="password"]',
            'input[placeholder*="密碼"]',
        ]
        submit_selectors = [
            'button[data-testid="sign-in"]',
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Sign in")',
            'button:has-text("Login")',
            'button:has-text("登入")',
            'button:has-text("登 入")',
            'button:has-text("下一步")',
            'button:has-text("繼續")',
            'button:has-text("Continue")',
            'button:has-text("Next")',
        ]

        username_selector = first_visible_selector(page, username_selectors, timeout=7000)
        password_selector = first_visible_selector(page, password_selectors, timeout=1500)

        if not username_selector and not password_selector:
            shot = output_dir / "cognito_login_form_not_found.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[auth] Cognito form fields not found, screenshot: {shot}")
            page.close()
            return False

        if username_selector:
            print(f"[auth] username selector: {username_selector}")
            page.fill(username_selector, username)

            if not password_selector:
                if click_first_visible(page, submit_selectors, timeout=2500):
                    print("[auth] submitted username step")
                else:
                    print("[auth] username step submit button not found, fallback to Enter")
                    page.press(username_selector, "Enter")

                page.wait_for_timeout(2500)
                password_selector = first_visible_selector(page, password_selectors, timeout=7000)

        if not password_selector:
            shot = output_dir / "cognito_password_form_not_found.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[auth] password field not found after username step, screenshot: {shot}")
            page.close()
            return False

        print(f"[auth] password selector: {password_selector}")
        page.fill(password_selector, password)

        if click_first_visible(page, submit_selectors, timeout=2500):
            print("[auth] submitted password step")
        else:
            print("[auth] password step submit button not found, fallback to Enter")
            page.press(password_selector, "Enter")

        page.wait_for_timeout(2000)

        if not wait_until_back_to_app(page, base_url, timeout_ms=45000):
            shot = output_dir / "cognito_login_failed.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[auth] still not back to app after submit: {page.url}")
            print(f"[auth] screenshot saved: {shot}")
            page.close()
            return False

        try_close_optional_dialogs(page)

        if is_cognito_url(page.url):
            shot = output_dir / "still_on_cognito.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[auth] still on Cognito after dialog handling: {page.url}")
            page.close()
            return False

        print(f"[auth] Cognito login success: {page.url}")
        page.close()
        return True

    except Exception as e:
        print(f"[auth] Cognito login exception: {e}")
        try:
            shot = output_dir / "cognito_login_exception.png"
            page.screenshot(path=str(shot), full_page=True)
            print(f"[auth] exception screenshot saved: {shot}")
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass
        return False


def ensure_authenticated_context(browser, base_url: str, storage_state_path: str | None, output_dir: Path):
    if storage_state_path:
        reused = probe_auth_with_state(browser, base_url, storage_state_path)
        if reused:
            return reused

    username = (
        os.environ.get("TEST_USERNAME", "").strip()
        or os.environ.get("PLAYWRIGHT_USERNAME", "").strip()
        or os.environ.get("COGNITO_USERNAME", "").strip()
    )
    password = (
        os.environ.get("TEST_PASSWORD", "").strip()
        or os.environ.get("PLAYWRIGHT_PASSWORD", "").strip()
        or os.environ.get("COGNITO_PASSWORD", "").strip()
    )

    if not username or not password:
        raise RuntimeError("TEST_USERNAME / TEST_PASSWORD not set")

    context = browser.new_context()

    if not cognito_form_login(context, base_url, username, password, output_dir):
        context.close()
        raise RuntimeError("Cognito form login failed")

    save_storage_state(context, storage_state_path)
    return context


def validate_page(page, expected_texts: list[str], forbidden_texts: list[str]) -> tuple[bool, str, str]:
    final_url = page.url
    content = page.content().lower()

    if is_cognito_url(final_url):
        return False, "Redirected to Cognito login", final_url

    if "whitelabel error page" in content or "exception" in content:
        return False, "Application page contains error text", "Detected error text"

    missing = [txt for txt in expected_texts if txt.lower() not in content]
    forbidden_found = [txt for txt in forbidden_texts if txt.lower() in content]

    if missing:
        return False, "Expected text missing", f"Missing: {', '.join(missing)}"

    if forbidden_found:
        return False, "Forbidden text found", f"Forbidden present: {', '.join(forbidden_found)}"

    return True, "Page validation passed", final_url


def main():
    if len(sys.argv) < 3:
        raise SystemExit(
            "Usage: python playwright_runner.py <base_url> <output_dir> [storage_state]"
        )

    base_url = sys.argv[1].strip()
    output_dir = Path(sys.argv[2])
    storage_state = sys.argv[3].strip() if len(sys.argv) > 3 else None

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

    target_url = build_target_url(base_url, target_path)

    result = {
        "passed": False,
        "summary": "",
        "expected": ", ".join(expected_texts) if expected_texts else "",
        "actual": "",
        "target_url": target_url,
        "final_url": "",
        "screenshot": str(output_dir / "screenshot.png"),
        "console_log": str(output_dir / "console.log"),
    }

    console_lines = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )

            context = ensure_authenticated_context(
                browser=browser,
                base_url=base_url,
                storage_state_path=storage_state,
                output_dir=output_dir,
            )

            page = context.new_page()
            page.on("console", lambda msg: console_lines.append(f"{msg.type}: {msg.text}"))

            print(f"[test] goto target url: {target_url}")
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeoutError:
                pass

            try_close_optional_dialogs(page)

            result["final_url"] = page.url

            ok, summary, actual = validate_page(page, expected_texts, forbidden_texts)
            result["passed"] = ok
            result["summary"] = summary
            result["actual"] = actual

            (output_dir / "console.log").write_text(
                "\n".join(console_lines),
                encoding="utf-8",
            )

            page.screenshot(path=str(output_dir / "screenshot.png"), full_page=True)

            context.close()
            browser.close()

    except Exception as e:
        result["summary"] = "Playwright execution failed"
        result["actual"] = str(e)

    (output_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def run_smoke_test(base_url, storage_state_path, screenshot_dir):
    os.makedirs(screenshot_dir, exist_ok=True)
    shot = f"{screenshot_dir}/smoke_{int(time.time())}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state_path)
        page = context.new_page()

        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PlaywrightTimeoutError:
            pass

        page.screenshot(path=shot, full_page=True)
        ok = not is_cognito_url(page.url)

        context.close()
        browser.close()

        return ok, shot


if __name__ == "__main__":
    main()  
