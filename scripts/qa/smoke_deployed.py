"""End-to-end smoke test against the deployed canopy-web.

Authenticates by attaching a Personal Access Token (PAT) as a
Bearer header on every page fetch — Playwright's `extra_http_headers`
makes this trivial. The token is resolved upstream by
`apps.tokens.middleware.BearerTokenAuthMiddleware` into a real Django
user; the rest of the request stack treats it identically to a
session-cookie login.

Run:
    CANOPY_PAT=<raw-token> uv run python scripts/qa/smoke_deployed.py

Env:
    CANOPY_PAT   required — raw Personal Access Token. Mint one with
                 `uv run python manage.py create_token --email X --label Y`
                 (or via POST /api/tokens/ once you have a session).
    CANOPY_URL   default https://canopy-web-ujpz2cuyxq-uc.a.run.app

Output: screenshots/ directory + printed pass/fail summary.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from playwright.sync_api import Browser, Page, sync_playwright

URL = os.environ.get("CANOPY_URL", "https://canopy-web-ujpz2cuyxq-uc.a.run.app")
PAT = os.environ.get("CANOPY_PAT", "")
SCREENSHOTS = Path(__file__).parent / "screenshots"


def check_page(
    browser: Browser,
    pat: str,
    path: str,
    *,
    expect_text: list[str] | None = None,
    forbid_text: list[str] | None = None,
    name: str,
) -> tuple[bool, str]:
    """Navigate to URL+path with Bearer PAT, assert text expectations, screenshot."""
    context = browser.new_context(
        extra_http_headers={"Authorization": f"Bearer {pat}",
                            "X-Canopy-Client": "smoke_deployed"},
    )
    page: Page = context.new_page()
    console_errors: list[str] = []
    page.on(
        "console",
        lambda m: console_errors.append(f"{m.type}: {m.text[:200]}") if m.type == "error" else None,
    )
    try:
        response = page.goto(f"{URL}{path}", wait_until="networkidle", timeout=30000)
    except Exception as e:
        context.close()
        return False, f"goto failed: {e}"
    status = response.status if response else "unknown"
    if status >= 400:
        context.close()
        return False, f"HTTP {status}"

    SCREENSHOTS.mkdir(exist_ok=True)
    screenshot_path = SCREENSHOTS / f"{name}.png"
    page.screenshot(path=str(screenshot_path), full_page=True)

    body_text = page.inner_text("body").lower()
    missing = [t for t in (expect_text or []) if t.lower() not in body_text]
    forbidden = [t for t in (forbid_text or []) if t.lower() in body_text]
    context.close()

    issues = []
    if missing:
        issues.append(f"missing={missing}")
    if forbidden:
        issues.append(f"FORBIDDEN={forbidden}")
    if console_errors:
        issues.append(f"console_errors={console_errors[:3]}")
    if issues:
        return False, f"{'; '.join(issues)}; screenshot={screenshot_path}"
    return True, f"OK; screenshot={screenshot_path}; status={status}"


PAGES = [
    # (path, screenshot_name, must-contain substrings, must-NOT-contain substrings)
    ("/", "dashboard", ["projects"], ["failed to load"]),
    ("/insights", "insights", ["insights"], ["failed to load"]),
    ("/skills", "skills", ["skills"], ["failed to load"]),
    ("/workspaces", "workspaces", ["workspace"], ["failed to load"]),
    ("/new", "new-collection", ["collection"], ["failed to load"]),
    ("/leaderboard", "leaderboard", ["leaderboard"], ["failed to load"]),
    ("/guide", "guide", ["guide"], ["failed to load"]),
    ("/settings", "settings", ["ai backend"], ["failed to load"]),
    ("/walkthroughs", "walkthroughs", ["walkthroughs"], ["failed to load"]),
]


def main() -> int:
    if not PAT:
        raise SystemExit(
            "CANOPY_PAT must be set. Mint one with:\n"
            "  uv run python manage.py create_token --email ace@dimagi-ai.com --label smoke-script"
        )
    print(f"Smoke target: {URL}")
    print(f"Auth: PAT ({PAT[:12]}…)")
    print()

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for path, name, expects, forbidden in PAGES:
            ok, msg = check_page(
                browser, PAT, path,
                expect_text=expects, forbid_text=forbidden, name=name,
            )
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {path:25s} → {msg}")
            results.append((path, ok, msg))
        browser.close()

    print()
    failed = [r for r in results if not r[1]]
    print(f"{len(results) - len(failed)}/{len(results)} pages passed")
    if failed:
        print()
        print("Failures:")
        for path, _, msg in failed:
            print(f"  {path}: {msg}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
