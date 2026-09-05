"""Flask application exposing the reservation nucleus.

Only ``POST /api/reservar``. The heavy lifting lives in ``booking.py``; this
module is a thin adapter that parses JSON, injects the concrete
:class:`~ea_client.EaClient` and translates exceptions to HTTP.

Run locally (development only, never deploy from here):
    python -m flask --app backend.app run --port 5001

The container/build wiring, nginx ``location`` and CORS are FOLLOW-UP SLICES
and intentionally absent here.
"""

from __future__ import annotations

import logging
from http import HTTPStatus

from flask import Flask, jsonify, request

from .booking import BookingError, book
from .ea_client import EaUnavailable

log = logging.getLogger("equilibra.reservar")


def make_app(*, gateway=None) -> Flask:
    """Build the Flask app. ``gateway`` is injectable for tests (no network)."""
    from .ea_client import EaClient  # local import keeps creation cheap

    gateway = gateway or EaClient()
    app = Flask(__name__)

    @app.errorhandler(404)
    def not_found(_err):
        return _error(HTTPStatus.NOT_FOUND, "invalid_request", "Ruta no encontrada.")

    @app.post("/api/reservar")
    def reservar():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error(HTTPStatus.BAD_REQUEST, "invalid_request", "El cuerpo debe ser JSON.", [])

        try:
            result = book(gateway, payload)
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

        # Contract is `{"ok": true, "appointment_id": <id>}` only.
        return jsonify({"ok": True, "appointment_id": result.appointment_id}), 201

    return app


# Default instance for `flask --app backend.app run`; tests build their own via
# make_app(gateway=...) so they never open a socket.
app = make_app()


def _error(status: HTTPStatus, code: str, message: str, fields: list[str] | None = None) -> tuple:
    return (
        jsonify({"ok": False, "code": code, "message": message, "fields": fields or []}),
        status.value,
    )

