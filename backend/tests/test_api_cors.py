"""CORS behaviour of the Flask adapter (EQUILIBRA-014 slice 2).

The reservation endpoint lives on the VPS while the web front lives on
``soyequilibra.com.ar`` (a different host), so a real cross-origin fetch needs
the right preflight and response headers. These tests pin that behaviour down
without any network use (the FakeGateway is in-memory).

The rules under test are the ones the despacho demands:
- only a whitelisted origin gets ``Access-Control-Allow-Origin``;
- a non-whitelisted origin gets NO CORS headers (the browser blocks it);
- ``OPTIONS`` preflight answers correctly;
- credentials are never enabled.
"""

from __future__ import annotations

import json
import unittest

from backend.app import make_app
from backend.tests.fake_gateway import FakeGateway

ALLOWED = "https://soyequilibra.com.ar"
DISALLOWED = "https://evil.example"


def _payload() -> dict:
    return {
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
    }


class CorsTests(unittest.TestCase):
    def setUp(self):
        self.gw = FakeGateway()
        self.gw.set_available("2026-09-21", ["08:00"])
        self.client = make_app(gateway=self.gw, allowed_origins=[ALLOWED]).test_client()

    def test_allowed_origin_gets_header_on_success(self):
        resp = self.client.post(
            "/api/reservar",
            data=json.dumps(_payload()),
            content_type="application/json",
            headers={"Origin": ALLOWED},
        )
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), ALLOWED)
        # Credentials are intentionally never enabled (wider surface).
        self.assertNotIn("Access-Control-Allow-Credentials", resp.headers)

    def test_allowed_origin_gets_header_on_rejection_too(self):
        # A rejected booking still must be readable by the allowed origin so the
        # patient sees a clear error.
        self.gw.set_available("2026-09-21", ["09:00"])  # 08:00 not free
        resp = self.client.post(
            "/api/reservar",
            data=json.dumps(_payload()),
            content_type="application/json",
            headers={"Origin": ALLOWED},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), ALLOWED)

    def test_disallowed_origin_gets_no_cors_headers(self):
        resp = self.client.post(
            "/api/reservar",
            data=json.dumps(_payload()),
            content_type="application/json",
            headers={"Origin": DISALLOWED},
        )
        # Server does not refuse the work (CORS is a browser sandbox, not
        # auth), but it must NEVER hand the cross-origin script the headers,
        # so the browser blocks reading the response.
        self.assertEqual(resp.status_code, 201)
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)
        self.assertNotIn("Access-Control-Allow-Credentials", resp.headers)

    def test_preflight_allowed_origin_answers_ok_with_headers(self):
        resp = self.client.open(
            "/api/reservar",
            method="OPTIONS",
            headers={
                "Origin": ALLOWED,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(resp.status_code, 204)
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), ALLOWED)
        self.assertIn("POST", resp.headers["Access-Control-Allow-Methods"])
        self.assertIn("content-type", resp.headers["Access-Control-Allow-Headers"].lower())
        self.assertNotIn("Access-Control-Allow-Credentials", resp.headers)

    def test_preflight_disallowed_origin_gets_no_headers(self):
        resp = self.client.open(
            "/api/reservar",
            method="OPTIONS",
            headers={
                "Origin": DISALLOWED,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        # No CORS headers -> the fetch is blocked in-browser.
        self.assertNotIn("Access-Control-Allow-Origin", resp.headers)
        self.assertNotIn("Access-Control-Allow-Methods", resp.headers)


if __name__ == "__main__":
    unittest.main()
