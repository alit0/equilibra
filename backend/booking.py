"""Core booking logic for POST /api/reservar (framework-free, no HTTP).

This module holds every business rule but no network call and no web
framework. Side effects live behind the ``EaGateway`` port; the concrete
transport is injected at the app boundary (``ea_client.py``), and the tests
inject an in-memory fake so they run with zero network access.

Alias email format (deterministic, documented, tested)
------------------------------------------------------
.. code-block:: text

    <base>+<slug>@<domain>

* ``<base>``  - the incoming local-part before its first ``+``;
* ``<slug>``  - the full name normalised (lowercased, accents and spaces
  removed);
* ``<domain>``- kept as sent.

Example: ``maria@gmail.com`` + "Juan López" -> ``maria+juanlopez@gmail.com``.
Repeatedly reserving for the same person on the same mailbox always re-creates
(or reuses) the exact same alias, so Easy!Appointments keeps one customer per
patient even though the REST API enforces a unique email per customer.

The RFC 5321 local-part cap (64 chars) applies to ``<base>+<slug>``, but
silently cutting characters off ``<slug>`` lets *two different patients*
collapse onto one alias (or one alias equal to the root email). When the slug
does not fit we keep a readable fragment of the base plus a 10-char SHA-1 of
the *full* slug, so distinct names never collide while the alias is just as
deterministic.

Mailbox identity for quotas
---------------------------
Google (and most providers) deliver ``a@x`` and ``a+tag@x`` to the same inbox,
and ignore dots in Gmail local-parts. Hard rule 3 caps active future
appointments per *real mailbox*, so the count must span every customer whose
email maps to the same canonical mailbox, not just the customer we resolved.

Notes format (goes into ``ea_appointments.notes``)
--------------------------------------------------
.. code-block:: text

    Paciente: <first> <last>
    Nota: <single free-text line>

The ``Nota`` line is omitted when the note is empty. Every whitespace run and
line break inside the free note *and* inside the name is collapsed to single
spaces, so neither field can forge an extra parseable header line.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

_LOG = logging.getLogger(__name__)

# The clinic keeps its calendar (and EA stores appointment times) in
# America/Argentina/Buenos_Aires. "Today"/future comparisons always use this
# zone, never UTC.
CLINIC_TZ_NAME = "America/Argentina/Buenos_Aires"

# Argentina abolished daylight-saving in 2015 and has stayed on UTC−3. Prefer
# the real IANA database when the platform has tzdata; on machines without it
# (stock Windows, no third-party timestamps required) fall back to fixed UTC−3,
# which is observationally identical for this clinic.
def _clinic_zone():
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        return ZoneInfo(CLINIC_TZ_NAME)
    except Exception:  # noqa: BLE001 - environment lacks the IANA database
        return _dt.timezone(_dt.timedelta(hours=-3), name="ART")


_CLINIC_TZ = _clinic_zone()

# Providers that fold dots in the local-part into the same real mailbox.
_DOT_INSENSITIVE_DOMAINS = {"gmail.com", "googlemail.com"}

# The only (service_id, provider_id) pairs Equilibra sells today.
ALLOWED_PAIRS: dict[int, int] = {1: 5, 2: 7}

VALID_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VALID_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# RFC 5321 caps the local-part at 64 chars; we budget the alias local-part.
_MAX_ALIAS_LOCAL = 62
_MAX_FUTURE_ACTIVE_PER_MAILBOX = 3


class BookingError(Exception):
    """A controlled, user-safe error with a stable machine ``code``."""

    def __init__(self, code: str, message: str, fields: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.fields = fields or []


@dataclass(frozen=True)
class BookingResult:
    appointment_id: int
    appointment_hash: str | None


class EaGateway(Protocol):
    """Port into Easy!Appointments that booking.py depends on."""

    def get_available_hours(self, provider_id: int, service_id: int, date: str) -> list[str]:
        ...

    def search_customers(self, query: str) -> list[dict[str, Any]]:
        ...

    def create_customer(self, customer: dict[str, Any]) -> dict[str, Any]:
        ...

    def list_customer_appointments(self, customer_id: int) -> list[dict[str, Any]]:
        ...

    def appointments_for_slot(self, provider_id: int, date: str, hour: str) -> list[dict[str, Any]]:
        """Appointments already stored on a (provider, date, hour) slot. EA does
        not refuse overlaps, so this is how a caller can detect one."""
        ...

    def create_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...

    def delete_appointment(self, appointment_id: int) -> None:
        """Remove a just-created appointment (rollback after a detected overlap)."""
        ...


def _as_clinic_naive(moment: _dt.datetime) -> _dt.datetime:
    """Convert an aware (or naive-as-UTC) moment to clinic-local naive."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_CLINIC_TZ).replace(tzinfo=None)


def mailbox_key(email: str) -> str:
    """Normalise to the canonical key that identifies the *real* inbox.

    ``.lower()`` the whole address, drop a ``+tag`` from the local-part, and —
    for dot-folding providers (Gmail family) — strip dots from the local-part.
    ``Maria@gmail.com``, ``m.aria@gmail.com`` and ``maria+tag@Gmail.com`` all
    collapse to ``maria@gmail.com``.
    """
    local, _, domain = email.rpartition("@")
    base = local.split("+", 1)[0].lower()
    domain = domain.strip().lower()
    if domain in _DOT_INSENSITIVE_DOMAINS:
        base = base.replace(".", "")
    return f"{base}@{domain}" if domain else base


def normalise_name(full_name: str) -> str:
    """Lowered, de-accented, alphanumeric-only slug used by aliases and matches."""
    decomposed = unicodedata.normalize("NFD", full_name.strip())
    ascii_only = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


def build_alias(email: str, full_name: str) -> str:
    """Deterministic, truncation-resistant alias for a same-mailbox patient.

    ``maria@gmail.com`` + "Juan López" -> ``maria+juanlopez@gmail.com``.

    If the full local-part would exceed RFC 5321's 64-char cap, truncating the
    slug could merge two different patients onto one customer. Then we keep a
    readable prefix of the base and append a short hash of the *whole* slug, so
    the alias is deterministic, never equals the plain root email, and distinct
    names never collapse.
    """
    local, _, domain = email.rpartition("@")
    base = local.split("+", 1)[0].lower()
    slug = normalise_name(full_name)
    domain = domain.strip().lower()

    if len(base) + 1 + len(slug) <= _MAX_ALIAS_LOCAL:
        return f"{base}+{slug}@{domain}"

    digest = hashlib.sha1(slug.encode("ascii", "ignore")).hexdigest()[:10]
    budget = _MAX_ALIAS_LOCAL - 1 - len(digest)  # characters left for the base
    kept = base[: max(budget, 0)]
    return f"{kept}+{digest}@{domain}"


def build_notes(first_name: str, last_name: str, note: str) -> str:
    """Return the exact parseable text to store in ``ea_appointments.notes``.

    The patient's name and the free note are each collapsed to a single line,
    so a value like ``"Ana\\nNota: falsa"`` or ``"Ana\\nPaciente: Otro"`` can
    never forge an extra header (hallazgo 6).
    """
    patient = " ".join(
        (" ".join((first_name or "").split()), " ".join((last_name or "").split()))
    ).strip()
    note = " ".join((note or "").split())
    header = f"Paciente: {patient}" if patient else "Paciente: "
    if note:
        return f"{header}\nNota: {note}"
    return header.rstrip()


def _person_of(cust: dict[str, Any]) -> str:
    parts = [cust.get("first_name") or "", cust.get("last_name") or ""]
    return " ".join(p for p in parts if p).strip()


def _same_person(cust: dict[str, Any], first_name: str, last_name: str) -> bool:
    if not (first_name.strip() or last_name.strip()):
        return False
    stored = normalise_name(_person_of(cust))
    incoming = normalise_name(f"{first_name} {last_name}")
    return bool(stored) and stored == incoming


def _customer_email(cust: dict[str, Any]) -> str:
    return (cust.get("email") or cust.get("Email") or "").strip()


def _real_date(value: str) -> _dt.date | None:
    """``datetime.date`` if ``value`` is a real calendar date (fixes 2026-02-31)."""
    try:
        return _dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def validate_payload(payload: dict[str, Any]) -> None:
    """Raise BookingError(code="invalid_request") for malformed client payloads.

    Enforces *real* types and *real* calendar dates so public input can never
    reach a ``.strip()``/``int()`` that blows up into an HTTP 500 (hallazgo 11
    and 12).
    """
    if not isinstance(payload, dict):
        raise BookingError("invalid_request", "El cuerpo debe ser un objeto JSON.", [])

    service_id = payload.get("service_id")
    provider_id = payload.get("provider_id")

    def _int_of(name: str, value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    if _int_of("service_id", service_id) is None:
        raise BookingError("invalid_request", "Servicio inválido.", ["service_id"])
    if _int_of("provider_id", provider_id) is None:
        raise BookingError("invalid_request", "Profesional inválido.", ["provider_id"])

    if ALLOWED_PAIRS.get(service_id) != provider_id:
        raise BookingError(
            "invalid_request",
            "No encontramos esa sede con ese profesional. Verificá y volvé a intentar.",
            ["service_id", "provider_id"],
        )

    selected_date = payload.get("selected_date")
    selected_hour = payload.get("selected_hour")
    if not (isinstance(selected_date, str) and VALID_DATE_RE.match(selected_date)):
        raise BookingError("invalid_request", "La fecha es inválida.", ["selected_date"])
    if _real_date(selected_date) is None:
        raise BookingError("invalid_request", "La fecha es inválida.", ["selected_date"])
    if not (isinstance(selected_hour, str) and VALID_TIME_RE.match(selected_hour)):
        raise BookingError("invalid_request", "El horario es inválido.", ["selected_hour"])

    sd = payload.get("service_duration")
    if sd is not None:
        if isinstance(sd, bool) or not isinstance(sd, int) or sd <= 0:
            raise BookingError("invalid_request", "La duración es inválida.", ["service_duration"])

    customer = payload.get("customer")
    if not isinstance(customer, dict):
        raise BookingError("invalid_request", "Faltan los datos de contacto.", ["customer"])

    email = (customer.get("email") or "")
    if not isinstance(email, str) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$", email.strip()):
        raise BookingError("invalid_request", "El email no es válido.", ["customer.email"])

    phone = customer.get("phone_number") or ""
    if not isinstance(phone, str) or len(re.sub(r"\D", "", phone)) < 8:
        raise BookingError(
            "invalid_request",
            "El teléfono debe tener al menos 8 dígitos.",
            ["customer.phone_number"],
        )

    first_name = customer.get("first_name")
    last_name = customer.get("last_name")
    if not isinstance(first_name, str) or not isinstance(last_name, str):
        raise BookingError("invalid_request", "Falta el nombre del paciente.", ["customer.first_name"])
    if not (first_name.strip() or last_name.strip()):
        raise BookingError("invalid_request", "Falta el nombre del paciente.", ["customer.first_name"])

    for flag, field in (("terms_accepted", "terms_accepted"), ("privacy_accepted", "privacy_accepted")):
        if not isinstance(payload.get(flag), bool) or not payload[flag]:
            raise BookingError("invalid_request", "Hay que aceptar todos los avisos legales.", [field])


# ---- Easy!Appointments customer resolution --------------------------------


def _exact_email(gateway: EaGateway, email: str) -> dict[str, Any] | None:
    """Return the customer whose email is *exactly* ``email`` (EA search is LIKE)."""
    candidates = gateway.search_customers(email)
    for c in candidates:
        if _customer_email(c).lower() == email.lower():
            return c
    return None


def resolve_customer(
    gateway: EaGateway,
    email: str,
    first_name: str,
    last_name: str,
    phone_number: str | None = None,
) -> dict[str, Any]:
    """Return the EA customer an appointment attaches to (creating if needed).

    * same mailbox + same normalised name  -> reuse the existing customer;
    * same mailbox + different name        -> reuse/create a ``+alias`` customer;
    * mailbox not seen yet                 -> create a plain customer (no alias).

    A freshly created customer always carries ``phone_number`` — EA runs with
    ``require_phone_number=1`` and an empty phone makes the whole create fail.
    A found alias whose stored name does NOT match the requested person is never
    reused (truncation collisions from the legacy slice must not mislabel an
    appointment); it is treated as an identity conflict.
    """
    full_name = f"{first_name} {last_name}".strip()
    if not (first_name.strip() or last_name.strip()):
        raise BookingError("invalid_request", "Falta el nombre del paciente.", ["customer.first_name"])

    existing = _exact_email(gateway, email)
    if existing is not None and _same_person(existing, first_name, last_name):
        return existing

    alias_email = build_alias(email, full_name)

    if existing is None:
        # Brand-new mailbox: adopt the existing matching alias (same name) if
        # one already exists, otherwise create the root customer.
        alias_exists = _exact_email(gateway, alias_email)
        if alias_exists is not None:
            if _same_person(alias_exists, first_name, last_name):
                return alias_exists
            raise BookingError(
                "invalid_request",
                "No pudimos identificar este paciente. Volvé a intentar en unos minutos.",
                ["customer.first_name"],
            )
        return _new_customer(gateway, first_name, last_name, email, phone_number)

    # Same mailbox, different person: reuse the alias only when its name also
    # matches; never attach a booking to another patient's record.
    alias = _exact_email(gateway, alias_email)
    if alias is not None:
        if _same_person(alias, first_name, last_name):
            return alias
        raise BookingError(
            "invalid_request",
            "No pudimos identificar este paciente. Volvé a intentar en unos minutos.",
            ["customer.first_name"],
        )
    return _new_customer(gateway, first_name, last_name, alias_email, phone_number)


def _new_customer(
    gateway: EaGateway, first_name: str, last_name: str, email: str, phone_number: str | None
) -> dict[str, Any]:
    return gateway.create_customer(
        {
            "first_name": first_name,
            "last_name": last_name,
            "email": email,
            "phone_number": phone_number or "",
        }
    )


# ---- orchestration ---------------------------------------------------------


def _start_dt(appt: dict[str, Any]) -> _dt.datetime:
    raw = appt.get("start") or appt.get("start_datetime") or ""
    try:
        return _dt.datetime.fromisoformat(str(raw)[:19])
    except ValueError:
        return _dt.datetime.min


def _is_future(appt: dict[str, Any], now_eci: _dt.datetime) -> bool:
    """True if the appointment's clinic-local start is still ahead of ``now``.

    A slot *later today* (18:00 while it is 08:00) IS future: compare the
    datetime instant, not a date-only value.
    """
    if not (appt.get("start") or appt.get("start_datetime")):
        return False
    return _start_dt(appt) > now_eci


def _same_day_appointments(gateway: EaGateway, customer_id: int, date: str) -> list[dict[str, Any]]:
    return [a for a in gateway.list_customer_appointments(customer_id) if _start_dt(a).date().isoformat() == date]


def _future_over_mailbox(gateway: EaGateway, mailbox: str, now_eci: _dt.datetime) -> int:
    """Count future appointments over every customer sharing the real mailbox.

    Returns ``_MAX_FUTURE_ACTIVE_PER_MAILBOX`` as soon as the quota is reached,
    so a busy mailbox is not scanned fully.
    """
    count = 0
    seen: set[int] = set()
    base_local, _, _ = mailbox.rpartition("@")
    for cust in gateway.search_customers(base_local):
        if mailbox_key(_customer_email(cust)) != mailbox:
            continue
        cid = int(cust["id"])
        if cid in seen:
            continue
        seen.add(cid)
        for appt in gateway.list_customer_appointments(cid):
            if _is_future(appt, now_eci):
                count += 1
                if count >= _MAX_FUTURE_ACTIVE_PER_MAILBOX:
                    return count
    return count


def _resolve_target_id(gateway: EaGateway, email: str, first_name: str, last_name: str) -> int | None:
    """Customer id(*) this booking would land on IF it already exists — without
    creating anything. Used to pre-check the same-day rule before any write.

    (*) A new patient (no customer/alias yet) cannot have booked twice, by
    definition, so it returns None and skips the day check.
    """
    full_name = f"{first_name} {last_name}".strip()
    if not (first_name.strip() or last_name.strip()):
        return None
    existing = _exact_email(gateway, email)
    if existing is not None and _same_person(existing, first_name, last_name):
        return int(existing["id"])
    alias = _exact_email(gateway, build_alias(email, full_name))
    if alias is not None and _same_person(alias, first_name, last_name):
        return int(alias["id"])
    return None


def book(
    gateway: EaGateway,
    payload: dict[str, Any],
    *,
    now: _dt.datetime | None = None,
) -> BookingResult:
    """Create the appointment, enforcing the four hard rules.

    Order guarantees no orphan writes: the day rule and the future quota are
    checked *before* ``resolve_customer`` creates anything, so a rejected
    reservation leaves the calendar (and the EA contacts) untouched.

    Concurrency guarantee (TOCTOU): availability (hard rule 1) is re-run
    immediately before ``create_appointment``, shrinking the race to the single
    POST round-trip. Immediately after the create we list the slot again: if a
    slot we just booked actually overlaps one on the same provider+hour that a
    concurrent process stored first, we roll our own row back (delete) and
    return ``requested_hour_is_unavailable`` — EA stays consistent and the
    patient sees a real conflict. This is best-effort, not a lock: EA exposes
    no row to serialise on, so the sub-millisecond window on a true simultaneous
    double-POST cannot be fully removed until EA validates slots server-side or
    an out-of-band lock/idempotency key is added (decided separately).

    Guarantee of that rollback: when the post-create reconciliation finds an
    overlap we always reply ``requested_hour_is_unavailable`` — the caller is
    never told the booking succeeded. If ``delete_appointment`` succeeds, our own
    row is gone and EA keeps the concurrent one (consistent, one turn). If the
    DELETE *fails* (e.g. EA is unreachable on that call) the two turns both stay
    on the calendar; that is NOT silently ignored — we log an ERROR carrying the
    ``appointment_id`` we failed to remove so an operator can reconcile, and we
    still return a controlled error, never a raw transport exception. What EA
    gives no transaction for, we cannot fix: a failed DELETE leaves an orphan a
    human must delete.
    """
    validate_payload(payload)

    now_utc = now if now is not None else _dt.datetime.now(_dt.timezone.utc)
    now_eci = _as_clinic_naive(now_utc)  # clinic-local naive instant

    service_id = int(payload["service_id"])
    provider_id = int(payload["provider_id"])
    selected_date: str = payload["selected_date"]
    selected_hour: str = payload["selected_hour"]
    customer = payload["customer"]
    email = (customer["email"] or "").strip()
    first_name = str(customer.get("first_name") or "").strip()
    last_name = str(customer.get("last_name") or "").strip()
    notes = str(customer.get("notes") or "")
    phone_number = str(customer.get("phone_number") or "").strip()

    def _available_now() -> bool:
        available = gateway.get_available_hours(provider_id, service_id, selected_date)
        return selected_hour in (available or [])

    def _reject_slot() -> BookingError:
        return BookingError(
            "requested_hour_is_unavailable",
            "Ese horario ya no está disponible. Elegí otro horario.",
            ["selected_hour"],
        )

    # Hard rule 1 — first availability check. Keep this BEFORE the mailbox
    # reads: an EA outage must fail closed (502) fast, before any further work.
    if not _available_now():
        raise _reject_slot()

    # Hard rule 3 — BEFORE creating anything, so a rejected reservation leaves
    # no orphan customer (hallazgo 5). EA only ever lists future hours as
    # available, so any choice this layer books is necessarily a not-yet-started
    # slot and would raise the mailbox past the cap.
    mailbox = mailbox_key(email)
    if _future_over_mailbox(gateway, mailbox, now_eci) >= _MAX_FUTURE_ACTIVE_PER_MAILBOX:
        raise BookingError(
            "too_many_future_appointments",
            "Ese email ya tiene el máximo de turnos futuros activos.",
            ["email"],
        )

    # Hard rule 2 — pre-check on the *existing* patient/alias (no writes).
    target_id = _resolve_target_id(gateway, email, first_name, last_name)
    if target_id is not None and _same_day_appointments(gateway, target_id, selected_date):
        raise BookingError(
            "patient_already_booked_that_day",
            "Ese paciente ya tiene un turno esa fecha. Elegí otro día.",
            ["selected_date"],
        )

    # Customer match rule — the only place we create anything.
    ea_customer = resolve_customer(gateway, email, first_name, last_name, phone_number)
    customer_id = int(ea_customer["id"])

    # Day rule again, now that we know the real resolved id (other writes may
    # have landed here while we were resolving).
    if _same_day_appointments(gateway, customer_id, selected_date):
        raise BookingError(
            "patient_already_booked_that_day",
            "Ese paciente ya tiene un turno esa fecha. Elegí otro día.",
            ["selected_date"],
        )

    # Hard rule 1 again — immediate pre-create re-validation (TOCTOU fix).
    if not _available_now():
        raise _reject_slot()

    created = gateway.create_appointment(
        {
            "start": f"{selected_date} {selected_hour}:00",
            "serviceId": service_id,
            "providerId": provider_id,
            "customerId": customer_id,
            "notes": build_notes(first_name, last_name, notes),
        }
    )
    created_id = int(created["id"])

    # Post-create reconciliation: if a concurrent process stored a competing
    # slot on the same (provider, date, hour) just before our POST, EA kept
    # both. Roll ours back and fail closed instead of double-booking.
    others = [
        a
        for a in gateway.appointments_for_slot(provider_id, selected_date, selected_hour)
        if (a.get("id") is not None and int(a["id"]) != created_id)
    ]
    if others:
        try:
            gateway.delete_appointment(created_id)
        except Exception as exc:  # noqa: BLE001 - a failed rollback must still
            # never report success, and must leave a trace an operator can act on.
            _LOG.error(
                "rollback of overlapping appointment %s after POST failed (%s: %s); "
                "manual cleanup required - two turns may share the same slot",
                created_id,
                exc.__class__.__name__,
                exc,
            )
        raise BookingError(
            "requested_hour_is_unavailable",
            "Ese horario ya no está disponible. Elegí otro horario.",
            ["selected_hour"],
        )

    return BookingResult(
        appointment_id=created_id,
        appointment_hash=created.get("hash"),
    )
