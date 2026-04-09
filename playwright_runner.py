import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SUPPORTED_STEP_ACTIONS = {"open", "click", "fill", "wait", "check", "screenshot"}


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


class StepExecutionError(RuntimeError):
    def __init__(self, message: str, results: list[dict]):
        super().__init__(message)
        self.results = results


def parse_list_items(raw_value: str) -> list[str]:
    return [item.strip() for item in (raw_value or "").split(",") if item.strip()]


def parse_testplan_step_line(line: str):
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None

    text = re.sub(r"^\d+\.\s*", "", text)
    text = re.sub(r"^-\s*", "", text)
    if not text or "=" not in text:
        return None

    action, value = text.split("=", 1)
    action = action.strip().lower()
    value = value.strip()

    if action not in SUPPORTED_STEP_ACTIONS or not value:
        return None

    return {action: value}


def load_test_plan(plan_path: Path) -> dict | None:
    if not plan_path.exists():
        return None

    text = plan_path.read_text(encoding="utf-8")
    result = {
        "url": "/",
        "role": "",
        "expected": [],
        "forbidden": [],
        "steps": [],
        "source": str(plan_path),
    }

    current_section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        header_match = re.match(r"^(URL|Role|Expected|Forbidden|Steps|Guide)\s*:\s*(.*)$", line, re.IGNORECASE)
        if header_match:
            key = header_match.group(1).lower()
            value = header_match.group(2).strip()

            if key == "url":
                if value:
                    result["url"] = value
                current_section = ""
                continue
            if key == "role":
                result["role"] = value
                current_section = ""
                continue
            if key in {"expected", "forbidden"}:
                if value:
                    result[key] = parse_list_items(value)
                    current_section = ""
                else:
                    current_section = key
                continue
            if key == "steps":
                current_section = "steps"
                step = parse_testplan_step_line(value)
                if step:
                    result["steps"].append(step)
                continue
            current_section = "guide"
            continue

        if current_section in {"expected", "forbidden"}:
            item = re.sub(r"^-\s*", "", line).strip()
            if item:
                result[current_section].append(item)
            continue

        if current_section == "steps":
            step = parse_testplan_step_line(line)
            if step:
                result["steps"].append(step)

    return result


def load_issue_context() -> dict | None:
    issue_path = Path.cwd().parent / "task_context" / "issue.json"
    if not issue_path.exists():
        return None

    try:
        data = json.loads(issue_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    validation = data.get("validation") or {}
    url = (
        validation.get("url")
        or data.get("URL")
        or data.get("url")
        or "/"
    )
    role = (
        validation.get("role")
        or data.get("Role")
        or data.get("role")
        or ""
    )
    expected = (
        validation.get("expected")
        or data.get("Expected")
        or data.get("expected")
        or []
    )
    forbidden = (
        validation.get("forbidden")
        or data.get("Forbidden")
        or data.get("forbidden")
        or []
    )
    steps = validation.get("steps") or data.get("steps") or []

    if isinstance(expected, str):
        expected = [x.strip() for x in expected.split(",") if x.strip()]
    if isinstance(forbidden, str):
        forbidden = [x.strip() for x in forbidden.split(",") if x.strip()]
    if isinstance(steps, dict):
        steps = [steps]
    if isinstance(steps, str):
        steps = [{"check": steps}]

    return {
        "url": url,
        "role": role,
        "expected": expected,
        "forbidden": forbidden,
        "steps": steps,
    }


def sanitize_filename(name: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", (name or "").strip()).strip("._")
    return value or "step"


def step_action_and_value(step) -> tuple[str, str]:
    if isinstance(step, dict) and step:
        key, value = next(iter(step.items()))
        return str(key).strip().lower(), str(value).strip()
    if isinstance(step, str):
        if "=" in step:
            key, value = step.split("=", 1)
            return key.strip().lower(), value.strip()
        return "check", step.strip()
    return "", ""


def parse_target(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if value.startswith("css="):
        return "css", value[4:].strip()
    if value.startswith("text="):
        return "text", value[5:].strip()
    if value.startswith("role="):
        return "role", value[5:].strip()
    if value.startswith("url="):
        return "url", value[4:].strip()
    if value.startswith("/") or value.startswith("http://") or value.startswith("https://"):
        return "url", value
    return "text", value


def click_target(page, kind: str, target: str, base_url: str | None = None) -> None:
    if kind == "url":
        if not base_url:
            raise RuntimeError("base_url required for url navigation")
        page.goto(build_target_url(base_url, target), wait_until="domcontentloaded", timeout=30000)
        return

    if kind == "css":
        page.locator(target).first.click(timeout=10000)
        return

    if kind == "role":
        parts = [p.strip() for p in target.split("|") if p.strip()]
        role_name = parts[0] if parts else "button"
        accessible_name = parts[1] if len(parts) > 1 else ""
        page.get_by_role(role_name, name=accessible_name or None).first.click(timeout=10000)
        return

    text_target = target or ""
    text_locator = page.get_by_text(text_target, exact=False).first
    text_locator.click(timeout=10000)


def fill_target(page, target: str) -> None:
    if "=>" in target:
        locator_text, value = target.split("=>", 1)
    elif "|" in target:
        locator_text, value = target.split("|", 1)
    else:
        raise RuntimeError("fill step must use `selector=>value` or `selector|value`")

    locator_kind, locator_value = parse_target(locator_text.strip())
    value = value.strip()

    if locator_kind == "css":
        page.locator(locator_value).first.fill(value, timeout=10000)
        return

    if locator_kind == "text":
        locator = page.get_by_label(locator_value, exact=False).first
        locator.fill(value, timeout=10000)
        return

    raise RuntimeError("fill supports css= or label text targets only")


def wait_for_target(page, target: str) -> None:
    value = (target or "").strip()
    if not value:
        page.wait_for_timeout(1000)
        return
    if value.isdigit():
        page.wait_for_timeout(int(value))
        return

    kind, normalized = parse_target(value)
    if kind == "css":
        page.locator(normalized).first.wait_for(state="visible", timeout=10000)
        return

    if kind == "text":
        page.get_by_text(normalized, exact=False).first.wait_for(state="visible", timeout=10000)
        return

    if kind == "url":
        page.wait_for_url(lambda current_url: normalized in current_url, timeout=15000)
        return

    if kind == "role":
        parts = [p.strip() for p in normalized.split("|") if p.strip()]
        role_name = parts[0] if parts else "button"
        accessible_name = parts[1] if len(parts) > 1 else ""
        page.get_by_role(role_name, name=accessible_name or None).first.wait_for(
            state="visible",
            timeout=10000,
        )
        return


def check_target(page, target: str) -> str:
    kind, normalized = parse_target(target)

    if kind == "css":
        locator = page.locator(normalized).first
        locator.wait_for(state="visible", timeout=10000)
        return f"visible selector: {normalized}"

    if kind == "url":
        if normalized not in page.url:
            raise RuntimeError(f"url does not contain `{normalized}`")
        return f"url contains: {normalized}"

    if kind == "role":
        parts = [p.strip() for p in normalized.split("|") if p.strip()]
        role_name = parts[0] if parts else "button"
        accessible_name = parts[1] if len(parts) > 1 else ""
        page.get_by_role(role_name, name=accessible_name or None).first.wait_for(
            state="visible",
            timeout=10000,
        )
        return f"visible role: {normalized}"

    locator = page.get_by_text(normalized, exact=False).first
    locator.wait_for(state="visible", timeout=10000)
    return f"visible text: {normalized}"


def capture_step_screenshot(page, output_dir: Path, index: int, action: str) -> str:
    filename = f"step_{index:02d}_{sanitize_filename(action)}.png"
    path = output_dir / filename
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def execute_steps(page, base_url: str, steps: list, output_dir: Path) -> list[dict]:
    results: list[dict] = []

    for index, step in enumerate(steps, start=1):
        action, value = step_action_and_value(step)
        record = {
            "index": index,
            "action": action,
            "target": value,
            "passed": False,
            "detail": "",
            "screenshot": "",
        }
        if not action:
            record["detail"] = "empty step"
            results.append(record)
            continue

        try:
            print(f"[flow] step {index}: {action}={value}")

            if action == "open":
                target_kind, target_value = parse_target(value)
                click_target(page, target_kind, target_value, base_url=base_url)
                page.wait_for_timeout(1500)
            elif action == "click":
                target_kind, target_value = parse_target(value)
                click_target(page, target_kind, target_value, base_url=base_url)
                page.wait_for_timeout(1200)
            elif action == "fill":
                fill_target(page, value)
            elif action == "wait":
                wait_for_target(page, value)
            elif action == "check":
                record["detail"] = check_target(page, value)
            elif action == "screenshot":
                record["detail"] = "manual screenshot step"
            else:
                raise RuntimeError(f"unsupported step action: {action}")

            try_close_optional_dialogs(page)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass

            if not record["detail"]:
                record["detail"] = "ok"
            record["passed"] = True
            record["screenshot"] = capture_step_screenshot(page, output_dir, index, action)
        except Exception as exc:
            record["detail"] = str(exc)
            try:
                record["screenshot"] = capture_step_screenshot(page, output_dir, index, f"{action}_failed")
            except Exception:
                pass
            results.append(record)
            raise StepExecutionError(
                f"step {index} failed: {action}={value} ({exc})",
                results,
            ) from exc

        results.append(record)

    return results


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


def detect_zh_font_support() -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["fc-list", ":lang=zh", "family"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return False, "fontconfig not installed; cannot verify Chinese font support"

    families = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if families:
        return True, families[0]
    return False, "no Chinese fonts detected; install fonts-noto-cjk for Playwright screenshots"


def main():
    if len(sys.argv) < 3:
        raise SystemExit(
            "Usage: python playwright_runner.py <base_url> <output_dir> [storage_state]"
        )

    base_url = sys.argv[1].strip()
    output_dir = Path(sys.argv[2])
    storage_state = sys.argv[3].strip() if len(sys.argv) > 3 else None

    output_dir.mkdir(parents=True, exist_ok=True)

    testplan_path = Path(
        os.environ.get("PLAYWRIGHT_TESTPLAN_PATH", "testplan.txt")
    ).expanduser()
    if not testplan_path.is_absolute():
        testplan_path = Path.cwd() / testplan_path

    testplan_ctx = load_test_plan(testplan_path)
    issue_ctx = load_issue_context()
    config_ctx = testplan_ctx or issue_ctx

    if config_ctx:
        target_path = config_ctx["url"]
        role = config_ctx["role"]
        expected_texts = config_ctx["expected"]
        forbidden_texts = config_ctx["forbidden"]
        steps = config_ctx["steps"]
    else:
        target_path = "/"
        role = ""
        expected_texts = []
        forbidden_texts = []
        steps = []

    target_url = build_target_url(base_url, target_path)

    result = {
        "passed": False,
        "summary": "",
        "role": role,
        "expected": ", ".join(expected_texts) if expected_texts else "",
        "actual": "",
        "target_url": target_url,
        "final_url": "",
        "screenshot": str(output_dir / "screenshot.png"),
        "console_log": str(output_dir / "console.log"),
        "steps": steps,
        "step_results": [],
        "step_screenshots": [],
        "testplan_path": str(testplan_path) if testplan_ctx else "",
        "config_source": "testplan.txt" if testplan_ctx else ("issue.json" if issue_ctx else ""),
    }

    console_lines = []
    font_ok, font_detail = detect_zh_font_support()

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
            context.set_default_timeout(30000)

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

            if steps:
                try:
                    step_results = execute_steps(page, base_url, steps, output_dir)
                    result["step_results"] = step_results
                    result["step_screenshots"] = [
                        item["screenshot"] for item in step_results if item.get("screenshot")
                    ]
                except StepExecutionError as step_exc:
                    result["step_results"] = step_exc.results
                    result["step_screenshots"] = [
                        item["screenshot"] for item in step_exc.results if item.get("screenshot")
                    ]
                    raise

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
        result["summary"] = str(e) or "Playwright execution failed"
        result["actual"] = str(e)

    result["font_check_passed"] = font_ok
    result["font_check_detail"] = font_detail

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
