import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


def is_cognito_url(url: str) -> bool:
    url = (url or "").lower()
    return "amazoncognito.com" in url or "/login/oauth2/" in url


def main():
    if len(sys.argv) < 4:
        raise SystemExit("Usage: python -m tests.auth_probe <base_url> <state_file> <output_json>")

    base_url = sys.argv[1]
    state_file = Path(sys.argv[2])
    output_json = Path(sys.argv[3])

    result = {
        "authenticated": False,
        "final_url": "",
        "error": "",
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            if state_file.exists():
                context = browser.new_context(storage_state=str(state_file))
            else:
                context = browser.new_context()

            page = context.new_page()
            page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

            final_url = page.url
            result["final_url"] = final_url
            result["authenticated"] = not is_cognito_url(final_url)

            browser.close()

    except Exception as e:
        result["error"] = str(e)

    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()