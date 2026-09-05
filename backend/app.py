"""Flask application exposing the reservation nucleus.

``POST /api/reservar``. The heavy lifting lives in ``booking.py``; this module
is a thin adapter that parses JSON, injects the concrete
:class:`~ea_client.EaClient` and translates exceptions to HTTP. Since
EQUILIBRA-014 slice 2 it also owns the *transport* defences that sit in front
of the nucleus but are NOT business rules: CORS, a honeypot, a minimum-fill
speed bump and response idempotency (see ``defenses.py``).

Run locally (development only, never deploy from here):
    python -m flask --app backend.app run --port 5001

CORS: the front lives on ``soyequilibra.com.ar`` while this backend runs on the
VPS, so an origin is only ever echoed back (``Access-Control-Allow-Origin``)
when it appears in ``EQUILIBRA_ALLOWED_ORIGINS`` (comma separated, defaults to
the production origin). A non-whitelisted origin never receives CORS headers,
so a browser blocks it. Credentials are never enabled.
"""

from __future__ import annotations

import logging
import os
from http import HTTPStatus

from flask import Flask, jsonify, request

from .booking import BookingError, book
from .defenses import honeypot_filled, suspicious_fill_time
from .ea_client import EaUnavailable
from .idempotency import IdempotencyStore

log = logging.getLogger("equilibra.reservar")

# Default origin served by the production site; configurable via env (csv).
DEFAULT_ALLOWED_ORIGINS = ("https://soyequilibra.com.ar",)

# Generic, indistinguishable rejection used by every anti-abuse guard. We never
# tell a bot WHICH probe tripped, or it would just avoid it next time.
_SUSPICIOUS_REJECT = {
    "code": "invalid_request",
    "message": "No pudimos procesar la solicitud. Verificá e intentá de nuevo.",
    "fields": [],
}


def _load_allowed_origins(override: list[str] | None = None) -> tuple[str, ...]:
    """Whitelist of origins that receive CORS headers.

    Explicit argument wins (tests inject it); otherwise the ``EQUILIBRA_ALLOWED_ORIGINS``
    env is a comma-separated list; otherwise the production default. Never ``*`` — this
    endpoint writes to a medical calendar.
    """
    if override is not None:
        return tuple(o.strip() for o in override if o.strip())
    raw = os.environ.get("EQUILIBRA_ALLOWED_ORIGINS")
    if raw:
        parsed = tuple(o.strip() for o in raw.split(",") if o.strip())
        if parsed:
            return parsed
    return DEFAULT_ALLOWED_ORIGINS


def make_app(*, gateway=None, allowed_origins=None, idempotency_store=None, now_ms_provider=None) -> Flask:
    """Build the Flask app. Dependencies are injectable for tests (no network).

    ``gateway``            - the Easy!Appointments transport (default real).
    ``allowed_origins``    - CORS whitelist override (production default from env).
    ``idempotency_store``  - the key -> appointment map (default in-process).
    ``now_ms_provider``    - injects the request-time clock for tests; ``None``
                             uses the real wall clock.
    """
    from .ea_client import EaClient  # local import keeps creation cheap

    gateway = gateway or EaClient()
    store = idempotency_store or IdempotencyStore()
    allowed = _load_allowed_origins(allowed_origins)
    now_ms = now_ms_provider or (lambda: _real_now_ms())
    app = Flask(__name__)

    @app.errorhandler(404)
    def not_found(_err):
        return _error(HTTPStatus.NOT_FOUND, "invalid_request", "Ruta no encontrada.")

    @app.after_request
    def _attach_cors_headers(response):
        # Only echo back an origin that belongs to the whitelist. A browser
        # blocks reading the response when this header is absent.
        origin = request.headers.get("Origin")
        if origin and origin in allowed:
            response.headers.setdefault("Access-Control-Allow-Origin", origin)
            response.headers.setdefault("Vary", "Origin")
        # Allow-Credentials is deliberately never set.
        return response

    @app.before_request
    def _answer_preflight():
        # A real preflight is an OPTIONS that carries the method the browser
        # intends to use. Same-site or spurious OPTIONS are left alone.
        if request.method != "OPTIONS":
            return None
        if not request.headers.get("Access-Control-Request-Method"):
            return None
        origin = request.headers.get("Origin")
        if not (origin and origin in allowed):
            # Non-whitelisted preflight: final answer without CORS headers so
            # the browser refuses the actual request.
            return ("", HTTPStatus.NO_CONTENT.value)
        resp = app.response_class("", status=HTTPStatus.NO_CONTENT.value, mimetype="text/plain")
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        req_headers = request.headers.get("Access-Control-Request-Headers")
        if req_headers:
            resp.headers["Access-Control-Allow-Headers"] = req_headers
        resp.headers["Access-Control-Max-Age"] = "600"
        return resp

    def _suspicious_response() -> tuple:
        # One generic, indistinguishable rejection for every anti-abuse probe.
        # It deliberately reuses the existing error shape (code/message/fields)
        # so a bot cannot tell which bait caught it.
        return _error(
            HTTPStatus.BAD_REQUEST,
            _SUSPICIOUS_REJECT["code"],
            _SUSPICIOUS_REJECT["message"],
            _SUSPICIOUS_REJECT["fields"],
        )

    def _reservation_response(appointment_id: int) -> tuple:
        # Contract is `{"ok": true, "appointment_id": <id>}` only.
        return jsonify({"ok": True, "appointment_id": appointment_id}), 201

    @app.post("/api/reservar")
    def reservar():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error(HTTPStatus.BAD_REQUEST, "invalid_request", "El cuerpo debe ser JSON.", [])

        # Two cheap probes IN FRONT of the nucleus. Logged as suspicion so the
        # usefulness of each can be measured, but never surfaced to the client.
        if honeypot_filled(payload):
            log.warning("suspected bot: honeypot field not empty (dropping)")
            return _suspicious_response()
        if suspicious_fill_time(payload, now_ms()):
            log.warning("suspected bot: fill time implausible/instant (dropping)")
            return _suspicious_response()

        # Idempotency runs BEFORE book() so a retry of a key we have already
        # served never re-enters the nucleus (and never creates a second turn).
        key = payload.get("idempotency_key")
        if isinstance(key, str):
            key = key.strip() or None  # empty/blank behaves exactly like absent
        elif key is None:
            key = None  # old client: no idempotency, still works
        else:
            # Present but not a string (number/array/object): cannot honour a
            # retry for it, so reject indistinguishably rather than guess.
            log.warning("reservation rejected: unusable idempotency_key")
            return _suspicious_response()

        try:
            served_id = store.execute_once(key, lambda: book(gateway, payload).appointment_id)
        except BookingError as err:
            status = {
                "requested_hour_is_unavailable": HTTPStatus.CONFLICT,
                "patient_already_booked_that_day": HTTPStatus.CONFLICT,
                "too_many_future_appointments": HTTPStatus.CONFLICT,
                "invalid_request": HTTPStatus.BAD_REQUEST,
            }.get(err.code, HTTPStatus.BAD_REQUEST)
            log.warning("reservation rejected: code=%s", err.code)
            return _error(status, err.code, err.message, err.fields)
        except EaUnavailable as err:
            # Never echo EA internals to the patient; keep the log token-free.
            log.warning("upstream EA unavailable: %s", err)
            return _error(
                HTTPStatus.BAD_GATEWAY,
                "upstream_unavailable",
                "No pudimos reservar el turno en este momento. Volvé a intentar en unos minutos.",
                [],
            )

        return _reservation_response(served_id)

    return app


def _real_now_ms() -> int:
    """Wall-clock milliseconds used by the fill-time probe by default."""
    import time as _time

    return int(_time.time() * 1000)


def _error(status: HTTPStatus, code: str, message: str, fields: list[str] | None = None) -> tuple:
    return (
        jsonify({"ok": False, "code": code, "message": message, "fields": fields or []}),
        status.value,
    )


# Default instance for `flask --app backend.app run`; tests build their own via
# make_app(gateway=...) so they never open a socket.
app = make_app()

