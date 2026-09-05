"""End-to-end HTTP tests of the guards wired into ``POST /api/reservar``.

These exercise the FULL Flask stack against the FakeGateway (no network) and
pin the slice-2 contract behaviour a browser/front actually sees:

* same ``idempotency_key`` sent three times -> ONE turn, the SAME
  ``appointment_id`` each time (the "apreté confirmar tres veces" promise);
* a filled honeypot -> generic 400, indistinguishable from any other bad input,
  and NO turn is created and the calendar stays untouched;
* an implausible ``form_started_at`` -> generic 400, nothing created; a patient
  that took long enough passes;
* a missing key (old client) keeps working; a present-but-unusable key is
  rejected indistinguishably.

A fixed ``now_ms_provider`` makes the fill-time decision deterministic.
"""

from __future__ import annotations

import json
import unittest

from backend.app import make_app
from backend.tests.fake_gateway import FakeGateway

NOW = 1_800_000_000_000  # fixed "server now", ms
UUID = "f47ac10b-58cc-4372-a567-0e02b2c3d479"


def _payload(**overrides) -> dict:
    payload = {
        "service_id": 1,
        "provider_id": 5,
        "selected_date": "2026-09-21",
        "selected_hour": "08:00",
        "service_duration": 30,
        "customer": {
            "first_name": "Ana",
            "last_name": "Ruiz",
            "email": "ana@gmail.com",
            "phone_number": "11 4444 1111",
        },
        "terms_accepted": True,
        "privacy_accepted": True,
        "website": "",
        "form_started_at": NOW - 60_000,  # opened a minute ago: healthy human
        "idempotency_key": UUID,
    }
    payload.update(overrides)
    return payload


def _make_http_client(gw=None):
    gw = gw or FakeGateway()
    client = make_app(gateway=gw, now_ms_provider=lambda: NOW).test_client()
    return gw, client


class IdempotencyHttpTests(unittest.TestCase):
    def setUp(self):
        self.gw, self.client = _make_http_client()
        self.gw.set_available("2026-09-21", ["08:00"])

    def post(self, payload):
        return self.client.post(
            "/api/reservar", data=json.dumps(payload), content_type="application/json"
        )

    def test_triple_confirm_creates_one_turn_same_id(self):
        first = self.post(_payload())
        self.assertEqual(first.status_code, 201)
        first_id = first.get_json()["appointment_id"]

        second = self.post(_payload())
        third = self.post(_payload())

        self.assertEqual(second.status_code, 201)
        self.assertEqual(third.status_code, 201)
        self.assertEqual(second.get_json()["appointment_id"], first_id)
        self.assertEqual(third.get_json()["appointment_id"], first_id)
        # Exactly ONE appointment was ever stored in the calendar.
        self.assertEqual(len(self.gw.appointments), 1)

    def test_retry_reponse_identical_shape(self):
        a = self.post(_payload()).get_json()
        b = self.post(_payload()).get_json()
        self.assertEqual(a, b)


class HoneypotHttpTests(unittest.TestCase):
    def setUp(self):
        self.gw, self.client = _make_http_client()
        self.gw.set_available("2026-09-21", ["08:00"])

    def _post(self, payload):
        return self.client.post(
            "/api/reservar", data=json.dumps(payload), content_type="application/json"
        )

    def test_filled_honeypot_is_generic_rejection_no_creation(self):
        resp = self._post(_payload(website="http://spam.example"))
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        # Indistinguishable from any other invalid_request: no mention of the
        # honeypot, no leaked "website", stable generic code.
        self.assertEqual(body["code"], "invalid_request")
        self.assertNotIn("website", json.dumps(body).lower())
        self.assertNotIn("honeypot", json.dumps(body).lower())
        # And critically it never reached the calendar.
        self.assertEqual(len(self.gw.appointments), 0)
        self.assertEqual(len(self.gw.customers), 0)

    def test_empty_honeypot_does_not_reject(self):
        resp = self._post(_payload())  # website == ""
        self.assertEqual(resp.status_code, 201)

    def test_absent_honeypot_old_client_not_rejected(self):
        body = _payload()
        body.pop("website")
        resp = self._post(body)
        self.assertEqual(resp.status_code, 201)


class FillTimeHttpTests(unittest.TestCase):
    def setUp(self):
        self.gw, self.client = _make_http_client()
        self.gw.set_available("2026-09-21", ["08:00"])

    def _post(self, payload):
        return self.client.post(
            "/api/reservar", data=json.dumps(payload), content_type="application/json"
        )

    def test_instant_submit_is_generic_rejection_no_creation(self):
        resp = self._post(_payload(form_started_at=NOW))  # 0 ms to fill
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertEqual(body["code"], "invalid_request")
        self.assertNotIn("form_started_at", json.dumps(body).lower())
        self.assertEqual(len(self.gw.appointments), 0)

    def test_future_timestamp_is_generic_rejection(self):
        # Client clock (or a bot) claims the form opened in the future.
        resp = self._post(_payload(form_started_at=NOW + 3_600_000_000))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(self.gw.appointments), 0)

    def test_patient_that_took_three_seconds_passes(self):
        resp = self._post(_payload(form_started_at=NOW - 3000))
        self.assertEqual(resp.status_code, 201)

    def test_absent_timestamp_old_client_passes(self):
        body = _payload()
        body.pop("form_started_at")
        resp = self._post(body)
        self.assertEqual(resp.status_code, 201)


class IdempotencyKeyShapeHttpTests(unittest.TestCase):
    def setUp(self):
        self.gw, self.client = _make_http_client()
        self.gw.set_available("2026-09-21", ["08:00"])

    def _post(self, payload):
        return self.client.post(
            "/api/reservar", data=json.dumps(payload), content_type="application/json"
        )

    def test_absent_key_old_client_keeps_working(self):
        body_a = _payload()
        body_a.pop("idempotency_key")
        # Two distinct pre-contract "confirmar" clicks are two full bookings: no
        # idempotency collapses them (the HTTP-level 409 same-day rule still
        # guards the SAME patient on the same day, but that is a business rule,
        # separate; here two different patients both get their turn).
        body_b = _payload(
            selected_hour="09:00",
            customer={**_payload()["customer"], "email": "beto@gmail.com", "first_name": "Beto"},
        )
        body_b.pop("idempotency_key")
        self.gw.set_available("2026-09-21", ["08:00", "09:00"])
        r1 = self._post(body_a)
        r2 = self._post(body_b)
        self.assertEqual(r1.status_code, 201)
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(
            r2.get_json()["appointment_id"], r1.get_json()["appointment_id"]
        )
        self.assertEqual(len(self.gw.appointments), 2)

    def test_present_but_unusable_key_is_generic_rejection_without_creation(self):
        # A key whose shape cannot be a real UUID (number/array/object/null) is
        # present but unusable: honouring a retry for it is impossible, so it is
        # rejected indistinguishably. Never anything is created.
        for bad in (42, ["k"], {"k": 1}):
            body = _payload(idempotency_key=bad)
            resp = self._post(body)
            self.assertEqual(resp.status_code, 400, msg=f"key={bad!r}")
            self.assertEqual(resp.get_json()["code"], "invalid_request")
        self.assertEqual(len(self.gw.appointments), 0)

    def test_blank_or_whitespace_key_acts_like_absent(self):
        # A client that ships idempotency_key as "" is only "half a converter":
        # it is not pretending, so treat it as an absent key and let it book
        # (never break a client over a cosmetic blank). Distinct patients avoid
        # the business same-day rule so both bookings may actually succeed.
        self.gw.set_available("2026-09-21", ["08:00", "09:00"])
        for i, blank in enumerate(("", "   ")):
            email = f"pac{i}@gmail.com"
            body = _payload(
                idempotency_key=blank,
                selected_hour="08:00" if i == 0 else "09:00",
                customer={**_payload()["customer"], "email": email},
            )
            resp = self._post(body)
            self.assertEqual(resp.status_code, 201, msg=f"key={blank!r}")
        self.assertEqual(len(self.gw.appointments), 2)

    def test_null_key_acts_like_absent(self):
        # An explicit JSON null is indistinguishable from "never sent" in many
        # clients, so the safe choice is to treat it as an absent key and book,
        # never breaking a client over a null.
        resp = self._post(_payload(idempotency_key=None))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(self.gw.appointments), 1)


if __name__ == "__main__":
    unittest.main()
