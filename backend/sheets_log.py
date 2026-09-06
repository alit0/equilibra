"""Best-effort audit log of reservations to a Google Sheet (EQUILIBRA-014, slice 3).

Purpose
-------
Each successful reservation leaves ONE row at the bottom of a Google Sheet the
owner controls. That row is simultaneously the consent record (the terms /
privacy checkboxes have nowhere to live inside Easy!Appointments), the notebook
Fran and Noe can open from the phone, and the only way to audit afterwards
whether something was scheduled oddly.

THE RULE THAT DOES NOT MOVE
---------------------------
The sheet must NEVER break or slow a reservation. Order of operations is: the
turn is created in EA, the patient is answered, and only THEN is the row
attempted. If the sheet is missing, misconfigured, down or slow, the write is
dropped with an ERROR log (carrying the ``appointment_id`` so an operator can
reconstruct the row by hand) and the request continues. It is a sink, never a
gate. ``app.py`` drives this module only on the post-create success branch and
swallows everything the append raises, so no sheet fault can ever reach the
reservation HTTP response.

Nothing here opens a socket unless a real ``append_row`` writer is provided and
invoked. Tests build and assert against in-memory doubles (see the module-level
``log_reservation``), so the suite stays deterministic and offline.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Any, Protocol

from .booking import BookingResult

_LOG = logging.getLogger("equilibra.sheets_log")

# Clinic operates in Buenos Aires time; the row's ``timestamp`` is human-checked
# from a phone, so it must be the timezone the clinic actually lives in, not UTC.
_CLINIC_TZ_NAME = "America/Argentina/Buenos_Aires"


def _clinic_zone():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(_CLINIC_TZ_NAME)
    except Exception:  # noqa: BLE001 - machine without the IANA database
        return _dt.timezone(_dt.timedelta(hours=-3), name="ART")


_CLINIC_TZ = _clinic_zone()

# The two clinic service/provider pairs Equilibra sells today (booking.ALLOWED_PAIRS)
# and the human-facing sede/professional names that belong to each. Column values
# are exactly what the mobile notebook wants to read, so they are pinned here
# instead of guessed per-request. If a sede is added the mapping grows with it.
_SEDE_BY_SERVICE: dict[int, str] = {1: "Ituzaingó", 2: "Monte Castro"}
_PROFESSIONAL_BY_SERVICE: dict[int, str] = {1: "Francisco Tipitto", 2: "Noelia Pizarro"}

# One attempt, short total bound. The patient already has their answer; a hung
# sheet must not leave a WSGI worker parked indefinitely. No retries here — a
# row that could not be written once is logged at ERROR for a human to replay,
# the same no-endless-retry stance as the TOCTOU rollback in booking.py.
WRITE_TIMEOUT_SECONDS = 5

# Env var the owner sets at deploy to point at a Google service-account JSON the
# Equilibra sheet bot already trusts (it is shared as editor on the owner's
# spreadsheets). Not committed, never printed. Mirrors the bot's own convention
# (GOOGLE_SERVICE_ACCOUNT_JSON + GOOGLE_SHEET_ID) so the same existing credential
# can be reused instead of creating a new one (EQUILIBRA-014 PASO 1).
ENV_SHEET_ID = "EQUILIBRA_SHEETS_ID"
ENV_CREDS = "EQUILIBRA_SHEETS_CREDENTIALS"

# Column order is a decision AND a contract with the owner's spreadsheet: do not
# reorder, insert or delete columns without a matching owner plan upgrade.
HEADERS: list[str] = [
    "timestamp",
    "appointment_id",
    "sede",
    "profesional",
    "fecha_turno",
    "hora_turno",
    "paciente",
    "email",
    "telefono",
    "nota",
    "terminos",
    "privacidad",
    "cliente_ea_id",
    "alias_usado",
]


class SheetWriter(Protocol):
    """Append a single row (a full-width list of cell values) to the sheet."""

    def append_row(self, row: list[str]) -> None: ...


def sheet_id_from_config() -> str | None:
    """The spreadsheet id the reserve log writes to, or ``None`` to disable.

    Read from ``EQUILIBRA_SHEETS_ID``. Empty/absent disables the log entirely:
    the reservation continues exactly as before and writes nothing and prints
    nothing (the default, out-of-the-box state of this slice).
    """
    raw = (os.environ.get(ENV_SHEET_ID) or "").strip()
    return raw or None


def config_writer() -> SheetWriter | None:
    """Live writer built from ``EQUILIBRA_SHEETS_*`` env, or ``None`` (disabled).

    Uses at app-construction the same existing Google service account the
    Equilibra bot already runs with (only the *path* to that JSON is read). When
    no id or no credential is configured the log is off and reserving is
    byte-for-byte untouched.
    """
    return make_writer(sheet_id_from_config(), _creds_path_from_config())


def _creds_path_from_config() -> str | None:
    """Filesystem path to the service-account JSON, or ``None`` when not set.

    The credential value itself is never logged or returned as content; only the
    path is read (absent -> log disabled).
    """
    raw = (os.environ.get(ENV_CREDS) or "").strip()
    return raw or None


def _yes_no(value: Any) -> str:
    return "sí" if value is True else "no"


def build_row(payload: dict[str, Any], result: BookingResult) -> list[str]:
    """Return the 14 cells of the reservation row (contract order in HEADERS).

    ``payload``   - the same validated request body the nucleus booked.
    ``result``    - what ``book`` returned (id, EA customer id + resolved email).

    ``alias_usado`` is the audit trail for the alias logic: when the email EA
    actually stored on the customer differs from the raw the patient typed, an
    alias was (re)used and we record which one; when they match, the plain email
    was used and the cell stays empty.
    """
    customer = payload.get("customer") or {}
    service_id = int(payload.get("service_id"))
    submitted_email = str(customer.get("email") or "").strip()
    resolved_email = result.customer_email.strip()
    alias_used = resolved_email if resolved_email.lower() != submitted_email.lower() else ""

    patient = " ".join(
        part for part in (str(customer.get("first_name") or ""), str(customer.get("last_name") or "")) if part
    ).strip()
    selected_date = str(payload.get("selected_date") or "")
    selected_hour = str(payload.get("selected_hour") or "")

    return [
        _dt.datetime.now(_dt.timezone.utc).astimezone(_CLINIC_TZ).isoformat(),
        str(result.appointment_id),
        _SEDE_BY_SERVICE.get(service_id, ""),
        _PROFESSIONAL_BY_SERVICE.get(service_id, ""),
        selected_date,
        selected_hour,
        patient,
        submitted_email,
        str(customer.get("phone_number") or ""),
        " ".join(str(customer.get("notes") or "").split()),
        _yes_no(payload.get("terms_accepted")),
        _yes_no(payload.get("privacy_accepted")),
        str(result.customer_id),
        alias_used,
    ]


def log_reservation(writer: SheetWriter | None, payload: dict[str, Any], result: BookingResult) -> None:
    """Append the reservation's row through ``writer``, swallowing every failure.

    This is the function the HTTP layer calls right after a *fresh* booking
    succeeded. It never raises: whatever ``writer.append_row`` does, success is
    silent and any exception is logged at ERROR with the ``appointment_id`` so
    the row can be reconstructed by hand. A ``None`` writer (log disabled)
    short-circuits to nothing.
    """
    if writer is None:
        return
    row = build_row(payload, result)
    try:
        writer.append_row(row)
    except Exception as exc:  # noqa: BLE001 - a sheet failure must never
        # propagate into the reservation response (the rule that does not move).
        _LOG.error(
            "could not write reservation log row for appointment %s to sheet "
            "(%s: %s); manual reconstruction required",
            result.appointment_id,
            exc.__class__.__name__,
            exc,
        )


def make_writer(sheet_id: str | None, credentials_path: str | None) -> SheetWriter | None:
    """Build the production writer from configuration, or ``None`` to disable.

    A log only exists when BOTH the spreadsheet id and the service-account JSON
    path are configured. When either is missing the log is off and the
    reservation core is untouched — the exact state this slice ships in.

    The real append is deliberately not exercised by the test suite (no network,
    no real spreadsheet), which injects an in-memory double instead. gspread is
    imported lazily, only at the moment a live append actually runs, so just
    importing this module never requires the dependency.
    """
    if not sheet_id or not credentials_path:
        return None
    return _LiveWriter(sheet_id=sheet_id, credentials_path=credentials_path)


class _LiveWriter:
    """Single-append writer backed by a real Google service account.

    The service account must already be shared as editor on the spreadsheet
    (the Equilibra bot's existing credential is), otherwise the append raises
    and ``log_reservation`` records an ERROR instead of taking the reservation
    down. Never retries; total effort is bounded by ``WRITE_TIMEOUT_SECONDS``.
    """

    def __init__(self, *, sheet_id: str, credentials_path: str) -> None:
        self._sheet_id = sheet_id
        self._credentials_path = credentials_path

    def append_row(self, row: list[str]) -> None:
        try:
            import gspread
        except ImportError as exc:  # pragma: no cover - deploy brings the dep
            raise RuntimeError(
                "google sheets runtime dependencies are not installed (gspread)"
            ) from exc
        client = gspread.service_account(filename=self._credentials_path)
        # Short, hard total bound so a hung sheet can never park a WSGI worker
        # (gspread client timeout covers connect+read of every request it makes).
        client.set_timeout(WRITE_TIMEOUT_SECONDS)
        sheet = client.open_by_key(self._sheet_id).sheet1
        if not sheet.row_values(1):
            sheet.append_row(HEADERS, value_input_option="USER_ENTERED")
        sheet.append_row(row, value_input_option="USER_ENTERED")

