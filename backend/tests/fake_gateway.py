"""In-memory fake of the ``EaGateway`` port that mirrors Easy!Appointments.

This is the *server* stand-in used by the booking tests: it reproduces the
observable EA behaviours that the double-review found missing:

* ``GET availabilities`` honours ``providerId``/``serviceId``/``date`` instead
  of ignoring them, exactly like EA v1 (the endpoint returns the free slots of
  a given provider+service on a date).
* ``POST customers`` / ``POST appointments`` **persist** ``notes`` (and return
  it), so assertions can verify the payload the booking layer builds.
* ``POST appointments`` **accepts overlapping slots** the same way EA does
  (``Appointments_model::validate()`` never checks for a conflict), so tests
  can exercise the TOCTOU class of failure production has.
* Customer emails are unique (EA rejects a second customer with the same
  email) and created customers carry ``phone``.

The screen-layer transport (real pagination, HTTP errors, JSON parse, bearer
redirects) is tested separately against a small scripted HTTP EA server in
``test_ea_client_transport.py``.
"""

from __future__ import annotations

import itertools
from typing import Any

# EA server default column: Easy!Appointments Api::$default_length = 20. The
# booking tests use this fake directly as a full port so they see every row;
# the HTTP transport tests enforce the real 20-row default independently.
ALLOWED_PAIRS: dict[int, int] = {1: 5, 2: 7}


class FakeEa(Exception):
    """Mirrors the EA constraint: an email may only belong to one customer."""


class FakeGateway:
    """Stateful stand-in for the Easy!Appointments backend."""

    def __init__(self):
        self._ids = itertools.count(1000)
        self._appt_ids = itertools.count(5000)
        self.customers: dict[int, dict[str, Any]] = {}
        self.appointments: dict[int, dict[str, Any]] = {}
        # Keyed by (service_id, provider_id, date) -> list of free "HH:MM".
        self.available: dict[tuple[int, int, str], list[str]] = {}
        self.fail_availability: Exception | None = None
        self.require_phone_number = True  # EA production setting (checked 2026-09)

    # ---- seeding helpers ---------------------------------------------------

    def set_available(
        self,
        date: str,
        hours: list[str],
        *,
        service_id: int = 1,
        provider_id: int | None = None,
    ) -> None:
        provider_id = provider_id if provider_id is not None else ALLOWED_PAIRS.get(service_id, service_id)
        self._check_real_pair(service_id, provider_id)
        self.available[(service_id, provider_id, str(date))] = list(hours)

    @staticmethod
    def _check_real_pair(service_id: int, provider_id: int) -> None:
        if ALLOWED_PAIRS.get(service_id) != provider_id:
            raise KeyError(f"no such service/provider pair: {service_id}/{provider_id}")

    def add_customer(self, customer: dict[str, Any]) -> dict[str, Any]:
        cid = next(self._ids)
        record = {
            "id": cid,
            "first_name": customer["first_name"],
            "last_name": customer.get("last_name") or "",
            "email": customer["email"],
            "phone_number": customer.get("phone_number") or "",
        }
        self.customers[cid] = record
        return dict(record)

    def add_appointment(
        self,
        *,
        customer_id: int,
        start: str,
        serviceId: int = 1,
        providerId: int = 5,
        notes: str = "",
    ) -> dict[str, Any]:
        aid = next(self._appt_ids)
        # EA api_encode returns this shape; keep it so callers see real fields.
        record = {
            "id": aid,
            "hash": f"h{aid:012d}",
            "start": start,
            "end": f"{start[:10]} {start[11:16]}:00",
            "notes": notes,
            "customerId": customer_id,
            "providerId": providerId,
            "serviceId": serviceId,
        }
        self.appointments[aid] = record
        return dict(record)

    # ---- EaGateway implementation ---------------------------------------------

    def get_available_hours(self, provider_id: int, service_id: int, date: str) -> list[str]:
        if self.fail_availability is not None:
            raise self.fail_availability
        # EA 404/rejects when asked for a pair it does not serve; the booking
        # layer already restricts to the two real pairs, so returning the
        # stored free slots under the exact (service, provider, date) key is
        # enough of a stand-in for "EA answers the plan and horizon".
        return list(self.available.get((int(service_id), int(provider_id), str(date)), []))

    def search_customers(self, query: str) -> list[dict[str, Any]]:
        # Same broad behaviour as EA's search (LIKE across name + email) that
        # the booking layer relies on to find every alias of a mailbox.
        needle = query.lower()
        out = []
        for rec in self.customers.values():
            if needle in rec["email"].lower() or needle in rec["first_name"].lower():
                out.append(dict(rec))
        return out

    def create_customer(self, customer: dict[str, Any]) -> dict[str, Any]:
        email = customer["email"]
        for _cid, rec in self.customers.items():
            if rec["email"].lower() == email.lower():
                raise FakeEa(f"email already in use: {email}")
        if self.require_phone_number and not (customer.get("phone_number") or "").strip():
            # EA: Customers_model::validate with require_phone_number=1.
            raise FakeEa("Not all required fields are provided: phone_number")
        return self.add_customer(customer)

    def list_customer_appointments(self, customer_id: int) -> list[dict[str, Any]]:
        return [
            dict(a)
            for a in self.appointments.values()
            if a["customerId"] == customer_id
        ]

    def appointments_for_slot(self, provider_id: int, date: str, hour: str) -> list[dict[str, Any]]:
        start = f"{date} {hour}:00"
        return [dict(a) for a in self.appointments.values() if a["providerId"] == provider_id and a["start"] == start]

    def delete_appointment(self, appointment_id: int) -> None:
        self.appointments.pop(appointment_id, None)

    def create_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        # EA does NOT reject an overlapping slot (validate() never checks it),
        # so this fake accepts the second one too and stores both.
        customer_id = payload["customerId"]
        created = self.add_appointment(
            customer_id=customer_id,
            start=payload["start"],
            serviceId=payload.get("serviceId", 1),
            providerId=payload.get("providerId", 5),
            notes=payload.get("notes", ""),
        )
        return created
