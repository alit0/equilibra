"""Verify EQUILIBRA-014 defect fixes against the live dist server.

Never calls booking/register or book_appointment. Never starts or kills
the Python servers on 8123/8124.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8124"
SHOTS = Path(__file__).resolve().parent / "verify-correcciones-shots"
SHOTS.mkdir(exist_ok=True)
WRITE_HITS: list[str] = []


def launch(p):
    try:
        return p.chromium.launch(headless=True, channel="chrome")
    except Exception:
        return p.chromium.launch(headless=True)


def attach_guards(page):
    def on_request(req):
        url = req.url.lower()
        if "booking/register" in url or "book_appointment" in url:
            WRITE_HITS.append(url)

    page.on("request", on_request)


def go_to_calendar(page):
    page.goto(BASE + "/turnos/", wait_until="networkidle")
    page.locator(".sede").nth(0).click()
    page.get_by_role("button", name="Elegir esta sede").click()


def verify_d1(page) -> dict:
    """Fail-closed calendar: aborted month fetch must leave zero bookable days
    and a real retry button."""
    page.route("**/get_unavailable_dates*", lambda route: route.abort())
    go_to_calendar(page)
    page.wait_for_selector("#day-error", state="visible", timeout=15000)
    page.screenshot(path=str(SHOTS / "d1-fail-closed.png"), full_page=True)
    available = page.locator(".cal-day.is-available").count()
    enabled = page.locator(".cal-day:not([disabled])").count()
    error = page.locator("#day-error").inner_text()
    retry = page.locator("#day-error button")
    retry_count = retry.count()
    result = {
        "available": available,
        "enabled": enabled,
        "error": error,
        "retry_count": retry_count,
    }
    assert available == 0, f"fail-open: {available} days marked available after network abort"
    assert enabled == 0, f"fail-open: {enabled} day buttons still enabled after network abort"
    assert retry_count == 1, f"expected a retry button inside #day-error, got {retry_count}"
    assert "reintent" in retry.inner_text().lower()

    page.unroute("**/get_unavailable_dates*")
    retry.click()
    page.wait_for_selector(".cal-day.is-available", timeout=15000)
    after = page.locator(".cal-day.is-available").count()
    result["available_after_retry"] = after
    assert after >= 1, "retry did not restore real availability"
    page.screenshot(path=str(SHOTS / "d1-after-retry.png"), full_page=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--defect", required=True)
    args = parser.parse_args()
    fn = {"1": verify_d1}.get(args.defect)
    if fn is None:
        print(f"unknown defect {args.defect}", file=sys.stderr)
        return 2

    with sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        attach_guards(page)
        try:
            result = fn(page)
        finally:
            browser.close()

    report = {"defect": args.defect, "write_hits": WRITE_HITS, "result": result}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if WRITE_HITS:
        print("FATAL: production write endpoints were called", file=sys.stderr)
        return 1
    print(f"verify defect {args.defect}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
