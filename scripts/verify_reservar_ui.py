"""Walk the 5-step form against the local fake booking server.

Blocks every request to turnos.allitto.com and production booking.
Saves mobile 390 and desktop screenshots of the three booking outcomes.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = Path(r"C:\Users\Ale\Proyectos\_MEMORIA\.despachos\equilibra-014-slice2-frontend-shots")
BASE = "http://127.0.0.1:8787"
FAKE = BASE

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_reservar  # noqa: E402


FORBIDDEN_HOSTS = (
    "turnos.allitto.com",
    "soyequilibra.com.ar",
    "www.soyequilibra.com.ar",
)
WRITE_MARKERS = ("booking/register", "book_appointment")


def set_outcome(name: str) -> None:
    urllib.request.urlopen(f"{FAKE}/_fake/outcome?set={name}", timeout=5).read()


def last_payload() -> dict:
    raw = urllib.request.urlopen(f"{FAKE}/_fake/last", timeout=5).read()
    return json.loads(raw.decode("utf-8"))


def launch(p):
    try:
        return p.chromium.launch(headless=True, channel="chrome")
    except Exception:
        return p.chromium.launch(headless=True)


def attach_guards(page, hits: dict) -> None:
    def on_request(req):
        url = req.url.lower()
        hits["all"].append(req.url)
        if any(marker in url for marker in WRITE_MARKERS):
            hits["writes"].append(req.url)
        if any(host in url for host in FORBIDDEN_HOSTS):
            hits["forbidden"].append(req.url)

    def handle_forbidden(route):
        url = route.request.url
        hits["blocked"].append(url)
        if "get_unavailable_dates" in url:
            route.fulfill(status=200, content_type="application/json", body="[]")
            return
        if "get_available_hours" in url:
            route.fulfill(
                status=200,
                content_type="application/json",
                body='["08:00","08:30","09:00","09:30"]',
            )
            return
        route.fulfill(status=599, body="blocked-by-verify")

    page.on("request", on_request)
    page.route("https://turnos.allitto.com/**", handle_forbidden)
    page.route("http://turnos.allitto.com/**", handle_forbidden)
    page.route("https://soyequilibra.com.ar/**", handle_forbidden)
    page.route("https://www.soyequilibra.com.ar/**", handle_forbidden)
    page.route("http://soyequilibra.com.ar/**", handle_forbidden)
    page.route("http://www.soyequilibra.com.ar/**", handle_forbidden)


def fill_patient(page) -> None:
    page.fill("#first-name", "Ana")
    page.fill("#last-name", "Prueba")
    page.fill("#email", "ana.prueba@example.com")
    page.fill("#email-confirm", "ana.prueba@example.com")
    page.fill("#phone", "1155552222")


def reach_confirm(page) -> None:
    page.goto(BASE + "/turnos/", wait_until="domcontentloaded")
    page.wait_for_selector("h1")
    page.locator(".sede").nth(0).click()
    page.get_by_role("button", name="Elegir esta sede").click()
    page.wait_for_selector(".cal-day.is-available", timeout=15000)
    page.locator(".cal-day.is-available").first.click()
    page.get_by_role("button", name="Reservar este día").click()
    page.wait_for_selector(".hour", timeout=15000)
    page.locator(".hour").first.click()
    page.get_by_role("button", name="Elegir este horario").click()
    page.wait_for_selector("#first-name")
    fill_patient(page)
    page.get_by_role("button", name="Ir a confirmar").click()
    page.wait_for_selector("#cta-confirm")
    page.locator("#terms").check()
    page.locator("#privacy").check()


def shot(page, name: str) -> None:
    page.screenshot(path=str(SHOTS / name), full_page=True)


def honeypot_ok(page) -> dict:
    info = page.evaluate(
        """() => {
          const input = document.getElementById("website");
          const wrap = input ? input.closest(".hp") : null;
          const cs = wrap ? getComputedStyle(wrap) : null;
          const rect = input ? input.getBoundingClientRect() : null;
          return {
            exists: !!input,
            value: input ? input.value : null,
            tabindex: input ? input.getAttribute("tabindex") : null,
            ariaHidden: wrap ? wrap.getAttribute("aria-hidden") : null,
            clip: cs ? cs.clip : null,
            width: rect ? rect.width : null,
            height: rect ? rect.height : null,
            labels: input ? input.labels.length : 0
          };
        }"""
    )
    return info


def run_viewport(page, width: int, tag: str, hits: dict, report: dict) -> None:
    page.set_viewport_size({"width": width, "height": 844 if width <= 400 else 900})

    set_outcome("success")
    reach_confirm(page)
    report[f"{tag}-honeypot"] = honeypot_ok(page)
    shot(page, f"{tag}-paso5-antes.png")
    page.get_by_role("button", name="Pedir la evaluación").click()
    page.wait_for_selector("#cta-confirm[aria-busy='true'], #booking-result:not([hidden])", timeout=5000)
    if page.locator("#cta-confirm[aria-busy='true']").count():
        shot(page, f"{tag}-reservando.png")
    page.wait_for_selector("#booking-result:not([hidden])", timeout=10000)
    page.wait_for_function("() => document.getElementById('title-5').textContent.includes('reservado')")
    shot(page, f"{tag}-exito.png")
    report[f"{tag}-success"] = {
        "title": page.locator("#title-5").inner_text(),
        "banner": page.locator("#booking-result").inner_text(),
        "cta_hidden": page.locator("#cta-confirm").is_hidden(),
        "checks_hidden": page.locator(".checks").is_hidden(),
        "payload": last_payload(),
    }

    set_outcome("conflict")
    reach_confirm(page)
    page.get_by_role("button", name="Pedir la evaluación").click()
    page.wait_for_selector("#confirm-error:not([hidden])", timeout=10000)
    shot(page, f"{tag}-ocupado.png")
    report[f"{tag}-conflict"] = {
        "error": page.locator("#confirm-error").inner_text(),
        "cta_enabled": page.locator("#cta-confirm").is_enabled(),
    }

    set_outcome("down")
    reach_confirm(page)
    page.get_by_role("button", name="Pedir la evaluación").click()
    page.wait_for_selector("#confirm-error:not([hidden])", timeout=10000)
    shot(page, f"{tag}-caido.png")
    err = page.locator("#confirm-error")
    report[f"{tag}-down"] = {
        "error": err.inner_text(),
        "whatsapp": err.locator("a[href*='wa.me']").count(),
        "cta_enabled": page.locator("#cta-confirm").is_enabled(),
    }

    first = last_payload()
    key1 = (first.get("last") or {}).get("idempotency_key")
    page.get_by_role("button", name="Pedir la evaluación").click()
    page.wait_for_timeout(700)
    second = last_payload()
    key2 = (second.get("last") or {}).get("idempotency_key")
    report[f"{tag}-idempotent-retry"] = {"first": key1, "second": key2, "same": key1 == key2}

    page.locator('.change[data-goto="3"]').click()
    page.wait_for_selector(".hour")
    page.locator(".hour").nth(1).click()
    page.get_by_role("button", name="Elegir este horario").click()
    page.wait_for_selector("#first-name")
    page.get_by_role("button", name="Ir a confirmar").click()
    page.wait_for_selector("#cta-confirm")
    page.locator("#terms").check()
    page.locator("#privacy").check()
    page.get_by_role("button", name="Pedir la evaluación").click()
    page.wait_for_timeout(700)
    third = last_payload()
    key3 = (third.get("last") or {}).get("idempotency_key")
    report[f"{tag}-idempotent-new-slot"] = {"previous": key2, "new": key3, "rotated": key3 != key2}


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((fake_reservar.HOST, fake_reservar.PORT), fake_reservar.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    hits = {"all": [], "writes": [], "forbidden": [], "blocked": []}
    report = {}
    try:
        with sync_playwright() as p:
            browser = launch(p)
            page = browser.new_page()
            attach_guards(page, hits)
            run_viewport(page, 390, "m390", hits, report)
            run_viewport(page, 1440, "d1440", hits, report)
            browser.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    writes = hits["writes"]
    real_forbidden = [u for u in hits["forbidden"] if u not in hits["blocked"] and "turnos.allitto.com" in u]
    # Intercepted EA reads are listed in forbidden AND blocked; that is expected.
    leaked = [u for u in hits["forbidden"] if not any(u.startswith(prefix) for prefix in (
        "https://turnos.allitto.com/",
        "http://turnos.allitto.com/",
    )) or any(m in u.lower() for m in WRITE_MARKERS)]
    leaked_writes = writes
    report["guards"] = {
        "write_attempts": leaked_writes,
        "blocked_ea": len(hits["blocked"]),
        "forbidden_seen": len(hits["forbidden"]),
    }

    out = SHOTS / "verify-report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    failures = []
    if leaked_writes:
        failures.append("write to production agenda: " + ", ".join(leaked_writes))
    success = report.get("m390-success", {})
    if "Turno reservado" not in (success.get("title") or ""):
        failures.append("mobile success title missing")
    if "9001" not in (success.get("banner") or ""):
        failures.append("mobile success missing appointment id")
    payload = ((success.get("payload") or {}).get("last") or {})
    if payload.get("website") not in ("", None):
        failures.append("honeypot was not empty")
    if not isinstance(payload.get("form_started_at"), int):
        failures.append("form_started_at missing")
    if not payload.get("idempotency_key"):
        failures.append("idempotency_key missing")
    conflict = (report.get("m390-conflict", {}).get("error") or "").lower()
    if "ocupar" not in conflict:
        failures.append("mobile conflict copy missing")
    if not report.get("m390-success", {}).get("cta_hidden"):
        failures.append("mobile success still shows the confirm button")
    if not report.get("m390-success", {}).get("checks_hidden", True):
        failures.append("mobile success still shows the legal checks")
    down = report.get("m390-down", {})
    if down.get("whatsapp", 0) < 1:
        failures.append("mobile 502 missing WhatsApp")
    if not down.get("cta_enabled"):
        failures.append("mobile 502 left the button disabled")
    if not report.get("m390-idempotent-retry", {}).get("same"):
        failures.append("idempotency key was not reused on retry")
    if not report.get("m390-idempotent-new-slot", {}).get("rotated"):
        failures.append("idempotency key did not rotate after hour change")
    hp = report.get("m390-honeypot") or {}
    if hp.get("ariaHidden") != "true" or hp.get("tabindex") != "-1" or hp.get("labels"):
        failures.append("honeypot a11y attrs wrong: " + json.dumps(hp))
    desktop_ok = "Turno reservado" in (report.get("d1440-success", {}).get("title") or "")
    if not desktop_ok:
        failures.append("desktop success title missing")

    print(json.dumps({"failures": failures, "shots": sorted(p.name for p in SHOTS.glob("*.png"))}, indent=2))
    if failures:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
