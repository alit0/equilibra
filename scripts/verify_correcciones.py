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


def wait_held(page, held, n, timeout=10):
    deadline = time.time() + timeout
    while len(held) < n and time.time() < deadline:
        page.wait_for_timeout(50)
    assert len(held) >= n, f"expected {n} held requests, got {len(held)}"


def selected_date_of(route) -> str:
    url = route.request.url
    if "selected_date=" in url:
        return url.split("selected_date=")[1].split("&")[0]
    return route.request.post_data or ""


def fulfill_json(route, body):
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def verify_d2(page) -> dict:
    """Stale month and hour responses must not overwrite the latest request."""
    held = []
    page.route("**/get_unavailable_dates*", lambda route: held.append(route))
    page.goto(BASE + "/turnos/", wait_until="domcontentloaded")
    page.locator(".sede").nth(0).click()
    page.get_by_role("button", name="Elegir esta sede").click()
    wait_held(page, held, 1)
    page.locator("#cal-next").click()
    page.locator("#cal-next").click()
    wait_held(page, held, 3)

    by_month = {}
    for route in held:
        date = selected_date_of(route)
        by_month[date[:7]] = route

    assert "2026-09" in by_month and "2026-11" in by_month, list(by_month.keys())
    leftover = [r for r in held if r not in (by_month["2026-09"], by_month["2026-11"])]
    for route in leftover:
        fulfill_json(route, [])
    fulfill_json(by_month["2026-11"], [])
    fulfill_json(by_month["2026-09"], {"is_month_unavailable": True})

    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "d2-stale-month.png"), full_page=True)
    label = page.locator("#cal-month").inner_text()
    available = page.locator(".cal-day.is-available").count()
    result = {"month_label": label, "available_after_stale": available}
    assert "Noviembre 2026" in label, f"stale month won: {label}"
    assert available >= 1, "stale September unavailable-month overwrote November"

    page.unroute("**/get_unavailable_dates*")

    hours_held = []
    page.route("**/get_available_hours*", lambda route: hours_held.append(route))
    page.get_by_role("button", name="Reservar este día").click()
    wait_held(page, hours_held, 1)
    page.get_by_role("button", name="Volver atrás").click()
    page.wait_for_selector(".cal-day.is-available")
    days = page.locator(".cal-day.is-available")
    days.nth(min(1, days.count() - 1)).click()
    page.get_by_role("button", name="Reservar este día").click()
    wait_held(page, hours_held, 2)

    fulfill_json(hours_held[1], ["18:00"])
    fulfill_json(hours_held[0], ["08:00", "08:30", "09:00"])
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "d2-stale-hours.png"), full_page=True)
    shown = page.locator(".hour").all_inner_texts()
    result["hours_shown"] = shown
    assert shown == ["18:00"], f"stale hours won: {shown}"
    return result


def go_to_datos(page):
    go_to_calendar(page)
    page.wait_for_selector(".cal-day.is-available", timeout=15000)
    page.get_by_role("button", name="Reservar este día").click()
    page.wait_for_selector(".hour", timeout=15000)
    page.get_by_role("button", name="Elegir este horario").click()
    page.wait_for_selector("#first-name")


def go_to_confirm(page):
    go_to_datos(page)
    page.fill("#first-name", "Maria")
    page.fill("#last-name", "Lopez")
    page.fill("#email", "maria.lopez@gmail.com")
    page.fill("#phone", "11 5555 5555")
    page.get_by_role("button", name="Ir a confirmar").click()
    page.wait_for_selector("#title-5")


def verify_d7(page) -> dict:
    """Email example has a single space; phone letter-error is one shared sentence."""
    go_to_datos(page)
    page.fill("#email", "sinarroba")
    page.locator("#phone").click()
    page.wait_for_timeout(150)
    email_err = page.locator("#email-error").text_content()
    page.fill("#phone", "abcdefghi")
    page.wait_for_timeout(150)
    phone_err = page.locator("#phone-error").text_content()
    page.screenshot(path=str(SHOTS / "d7-minors.png"), full_page=True)
    result = {"email_err": email_err, "phone_err": phone_err}
    assert "Ejemplo:  " not in email_err
    assert "Ejemplo: maria.lopez@gmail.com" in email_err
    assert phone_err == "El celular no puede tener letras. Ejemplo: 11 5555 5555"
    return result


def verify_d6(page) -> dict:
    """Legal buttons live outside the checkbox label, have a 44px target, dialogs are named."""
    go_to_confirm(page)
    nested = page.locator("label button").count()
    terms_box = page.locator("[data-open='terms-dialog']").bounding_box()
    privacy_box = page.locator("[data-open='privacy-dialog']").bounding_box()
    terms_checked_before = page.locator("#terms").is_checked()
    page.locator("[data-open='terms-dialog']").click()
    page.screenshot(path=str(SHOTS / "d6-terms-dialog.png"), full_page=True)
    terms_checked_after = page.locator("#terms").is_checked()
    terms_dialog = page.locator("#terms-dialog")
    terms_labelled = terms_dialog.get_attribute("aria-labelledby")
    page.locator("#terms-dialog button").click()
    page.locator("[data-open='privacy-dialog']").click()
    privacy_dialog = page.locator("#privacy-dialog")
    privacy_labelled = privacy_dialog.get_attribute("aria-labelledby")
    page.screenshot(path=str(SHOTS / "d6-privacy-dialog.png"), full_page=True)
    result = {
        "nested_label_buttons": nested,
        "terms_height": terms_box["height"] if terms_box else None,
        "privacy_height": privacy_box["height"] if privacy_box else None,
        "terms_toggled": terms_checked_before != terms_checked_after,
        "terms_labelledby": terms_labelled,
        "privacy_labelledby": privacy_labelled,
    }
    assert nested == 0, "legal button is still nested inside a label"
    assert terms_box and terms_box["height"] >= 44, terms_box
    assert privacy_box and privacy_box["height"] >= 44, privacy_box
    assert not result["terms_toggled"], "opening terms toggled the checkbox"
    assert terms_labelled and page.locator("#" + terms_labelled).count() == 1
    assert privacy_labelled and page.locator("#" + privacy_labelled).count() == 1
    return result


def verify_d5(page) -> dict:
    """Blurring one field must not validate untouched fields. Submit still validates all."""
    go_to_datos(page)
    page.fill("#first-name", "Maria")
    page.locator("#last-name").click()
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / "d5-blur-name.png"), full_page=True)
    last_shown = page.locator("#last-name-error").is_visible()
    email_shown = page.locator("#email-error").is_visible()
    phone_shown = page.locator("#phone-error").is_visible()
    first_shown = page.locator("#first-name-error").is_visible()
    result = {
        "after_name_blur": {
            "first": first_shown,
            "last": last_shown,
            "email": email_shown,
            "phone": phone_shown,
        }
    }
    assert not last_shown, "blur on name showed last-name error"
    assert not email_shown, "blur on name showed email error"
    assert not phone_shown, "blur on name showed phone error"
    assert not first_shown

    page.get_by_role("button", name="Ir a confirmar").click()
    page.screenshot(path=str(SHOTS / "d5-submit-all.png"), full_page=True)
    result["submit_last"] = page.locator("#last-name-error").inner_text()
    result["submit_email"] = page.locator("#email-error").inner_text()
    result["submit_phone"] = page.locator("#phone-error").inner_text()
    assert page.locator("#last-name-error").is_visible()
    assert page.locator("#email-error").is_visible()
    assert page.locator("#phone-error").is_visible()
    return result


def verify_d4(page) -> dict:
    """Empty hours copy is for patients; a hours-fetch failure must not render empty-state."""
    page.route("**/get_available_hours*", lambda route: fulfill_json(route, []))
    go_to_calendar(page)
    page.wait_for_selector(".cal-day.is-available", timeout=15000)
    page.get_by_role("button", name="Reservar este día").click()
    page.wait_for_selector("#hours-empty", timeout=15000)
    page.screenshot(path=str(SHOTS / "d4-empty-hours.png"), full_page=True)
    empty = page.locator("#hours-empty").inner_text()
    hint = page.locator("#hour-hint").inner_text()
    body = page.locator("body").inner_text()
    result = {"empty": empty, "hint": hint}
    assert "error de red" not in body.lower()
    assert "elegí otro día" in empty.lower() or "elegi otro dia" in empty.lower()
    page.unroute("**/get_available_hours*")

    page.get_by_role("button", name="Volver atrás").click()
    page.route("**/get_available_hours*", lambda route: route.abort())
    page.get_by_role("button", name="Reservar este día").click()
    page.wait_for_selector("#hour-error", state="visible", timeout=15000)
    page.screenshot(path=str(SHOTS / "d4-hours-error.png"), full_page=True)
    hours_text = page.locator("#hours").inner_text()
    hour_error = page.locator("#hour-error").inner_text()
    result["hours_on_error"] = hours_text
    result["hour_error"] = hour_error
    assert page.locator("#hours-empty").count() == 0
    assert "este día no tiene horarios" not in hours_text.lower()
    assert "error de red" not in page.locator("body").inner_text().lower()
    assert "cargar" in hour_error.lower()
    return result


def box_gap(above, below) -> float:
    a = above.bounding_box()
    b = below.bounding_box()
    return b["y"] - (a["y"] + a["height"])


def verify_a7(page) -> dict:
    """Notes field must not sit flush against the confirm CTA. Use panel 16px stack gap."""
    go_to_datos(page)
    page.set_viewport_size({"width": 390, "height": 844})
    notes = page.locator("#notes")
    cta = page.locator("#cta-datos")
    phone = page.locator("#phone")
    notes_field = page.locator("#field-notes")
    gap_notes_cta = box_gap(notes, cta)
    gap_phone_notes = box_gap(phone, notes_field)
    page.screenshot(path=str(SHOTS / "a7-notes-gap-390.png"), full_page=True)
    result = {
        "gap_notes_cta": round(gap_notes_cta, 1),
        "gap_phone_notes": round(gap_phone_notes, 1),
    }
    assert gap_notes_cta >= 15, f"notes still flush against CTA: {gap_notes_cta}px"
    assert abs(gap_notes_cta - gap_phone_notes) <= 2, result
    return result


def verify_d3(page) -> dict:
    """A hung fetch must surface the calendar error after ~10-12s, not hang."""
    page.route("**/get_unavailable_dates*", lambda route: None)
    go_to_calendar(page)
    started = time.time()
    page.wait_for_selector("#day-error", state="visible", timeout=16000)
    elapsed = time.time() - started
    page.screenshot(path=str(SHOTS / "d3-timeout.png"), full_page=True)
    available = page.locator(".cal-day.is-available").count()
    error = page.locator("#day-error").inner_text()
    result = {"elapsed_s": round(elapsed, 2), "available": available, "error": error}
    assert 9 <= elapsed <= 14, f"timeout was {elapsed:.2f}s, expected 10-12s"
    assert available == 0
    assert "cargar" in error.lower()
    return result


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
    fn = {
        "1": verify_d1, "2": verify_d2, "3": verify_d3,
        "4": verify_d4, "5": verify_d5, "6": verify_d6, "7": verify_d7,
        "a7": verify_a7,
    }.get(args.defect)
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
