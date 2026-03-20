import json
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

base_url = sys.argv[1]
output_dir = Path(sys.argv[2])
storage_state = sys.argv[3] if len(sys.argv) > 3 else None

output_dir.mkdir(parents=True, exist_ok=True)

result = {
    "passed": False,
    "summary": "",
    "expected": "Authenticated target page should load successfully",
    "actual": "",
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

        target_url = f"{base_url}/"
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        final_url = page.url.lower()

        (output_dir / "console.log").write_text("\n".join(console_lines), encoding="utf-8")
        page.screenshot(path=str(output_dir / "screenshot.png"), full_page=True)

        if "amazoncognito.com" in final_url:
            result["summary"] = "Redirected to Cognito login"
            result["actual"] = page.url
        else:
            content = page.content().lower()
            if "whitelabel error page" in content or "exception" in content:
                result["summary"] = "Application page loaded but contains error text"
                result["actual"] = "Detected error content in HTML"
            else:
                result["passed"] = True
                result["summary"] = "Authenticated page loaded successfully"
                result["actual"] = page.url

        browser.close()
except Exception as e:
    result["summary"] = "Playwright execution failed"
    result["actual"] = str(e)

(output_dir / "result.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)