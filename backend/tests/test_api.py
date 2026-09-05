from __future__ import annotations

import json
import unittest

from backend.app import make_app
from backend.ea_client import EaUnavailable
from backend.tests.fake_gateway import FakeGateway


def _json_payload(hour="08:00", **overrides) -> dict:
    payload = {
        "service_id": 1,
        "provider_id": 5,
        "selected_date": "2026-09-21",
        "selected_hour": hour,
        "service_duration": 30,
        "customer": {
            "first_name": "Ana",
            "last_name": "Ruiz",
            "email": "ana@gmail.com",
            "phone_number": "11 4444 1111",
        },
        "terms_accepted": True,
        "privacy_accepted": True,
    }
    payload.update(overrides)
    return payload


class HttpSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.client = make_app(gateway=self.gw).test_client()

    def post(self, payload):
        return self.client.post("/api/reservar", data=json.dumps(payload), content_type="application/json")

    def test_happy_path_201(self):
        self.gw.set_available("2026-09-21", ["08:00"])
        resp = self.post(_json_payload())
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["appointment_id"], int)
        # Hallazgo 15: the contract is `{"ok": true, "appointment_id": <id>}`,
        # no leak of the internal `appointment_hash`.
        self.assertNotIn("appointment_hash", body)
        self.assertEqual(set(body), {"ok", "appointment_id"})

    def test_invalid_calendar_date_400(self):
        self.gw.set_available("2026-02-31", ["08:00"])
        resp = self.post(_json_payload(selected_date="2026-02-31"))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "invalid_request")

    def test_typed_email_400_not_500(self):
        resp = self.post(_json_payload(customer={**_json_payload()["customer"], "email": 123}))
        self.assertEqual(resp.status_code, 400)

    def test_slot_taken_409_no_creation(self):
        self.gw.set_available("2026-09-21", ["09:00"])
        resp = self.post(_json_payload(hour="08:00"))
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.get_json()["code"], "requested_hour_is_unavailable")
        self.assertEqual(len(self.gw.appointments), 0)

    def test_bad_pair_400(self):
        self.gw.set_available("2026-09-21", ["08:00"])
        resp = self.post(_json_payload(service_id=1, provider_id=7))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "invalid_request")

    def test_terms_false_400(self):
        self.gw.set_available("2026-09-21", ["08:00"])
        resp = self.post(_json_payload(privacy_accepted=False))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()["code"], "invalid_request")

    def test_non_json_body_400(self):
        resp = self.client.post("/api/reservar", data="not json", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_nonexistent_route_404(self):
        resp = self.client.post("/api/reservarX", data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 404)


class UpstreamFailureTests(unittest.TestCase):
    """EA is down / 401 / timeout -> controlled 502, no internals leaked."""

    def setUp(self):
        class Broken:
            def get_available_hours(self, *a, **k):
                raise EaUnavailable("EA answered HTTP 502 on GET availabilities")

        self.client = make_app(gateway=Broken()).test_client()

    def test_ea_failure_maps_to_502_and_hides_detail(self):
        resp = self.client.post(
            "/api/reservar",
            data=json.dumps(_json_payload()),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 502)
        body = resp.get_json()
        self.assertEqual(body["code"], "upstream_unavailable")
        self.assertNotIn("EA", json.dumps(body))
        self.assertNotIn("502", json.dumps(body))
        self.assertNotIn("Bearer", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
