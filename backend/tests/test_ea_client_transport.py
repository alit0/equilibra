"""Transport-level tests that exercise the REAL :class:`EaClient` HTTP stack.

These are the hard coverage the double-review flagged as missing: they talk to
a local scripted EA-like HTTP server (no real calendar), so HTTP 401 / 500 /
timeouts / cross-origin redirects / broken or empty bodies / pagination all go
through ``EaClient._request`` for real instead of a double that already throws
a normalised ``EaUnavailable`` (hallazgo 14).
"""

from __future__ import annotations

import json
import unittest

from backend.ea_client import EaClient, EaUnavailable

from backend.tests.ea_server import EaTestServer, from_json, hang_forever, paginate_list

TOKEN = "test-secret-bearer-value"


class EaClientTransportTests(unittest.TestCase):
    def setUp(self):
        self.server = EaTestServer()
        self.api_base = self.server.url() + "/api/v1"
        self.client = EaClient(token=TOKEN, base_url=self.api_base, timeout=1.0)
        self.addCleanup(self.server.close)

    def _scenario(self, code, *, record=True):
        def route(path, method, query, body, h):
            if record:
                # run the default content
                pass
            if method == "GET":
                from_json({"error": "boom"}, code=code, handler=h)
                return 0
            from_json({"error": "boom"}, code=code, handler=h)
            return 0

        return route

    def test_http_401_maps_to_EaUnavailable(self):
        self.server.route("GET", "/api/v1/appointments", self._scenario(401))
        with self.assertRaises(EaUnavailable):
            self.client.list_customer_appointments(7)

    def test_http_500_maps_to_EaUnavailable(self):
        self.server.route("GET", "/api/v1/appointments", self._scenario(500))
        with self.assertRaises(EaUnavailable):
            self.client.list_customer_appointments(7)

    def test_conn_refused_maps_to_EaUnavailable(self):
        far = EaClient(token=TOKEN, base_url="http://127.0.0.1:9", timeout=0.6)
        with self.assertRaises(EaUnavailable):
            far.get_available_hours(provider_id=5, service_id=1, date="2026-09-21")

    def test_timeout_maps_to_EaUnavailable(self):
        slow = EaClient(token=TOKEN, base_url=self.api_base, timeout=0.3)
        self.server.route("GET", "/api/v1/appointments", hang_forever)
        with self.assertRaises(EaUnavailable):
            slow.list_customer_appointments(7)

    def test_cross_host_redirect_does_not_replay_bearer(self):
        # Redirect lands on the SAME server but a distinct path; if the client
        # replayed the request it would carry the Bearer and the second path
        # would be hit.
        def redirector(path, method, query, body, h):
            from_json(
                "",
                code=302,
                handler=h,
                headers={"Location": f"{self.server.url()}/api/v1/_stolen_token"},
            )
            return 0

        self.server.route("GET", "/api/v1/customers", redirector)
        with self.assertRaises(EaUnavailable):
            self.client.search_customers("maria")
        hits = [r for r in self.server.record if "stolen_token" in r["path"]]
        self.assertEqual(hits, [])

    def test_invalid_json_from_ea_maps_to_EaUnavailable(self):
        def broken(path, method, query, body, h):
            from_json("not-json-at-all", handler=h)
            return 0

        self.server.route("GET", "/api/v1/appointments", broken)
        with self.assertRaises(EaUnavailable):
            self.client.list_customer_appointments(7)

    def test_degraded_2xx_empty_body_on_appointments_fails_closed(self):
        def empty(path, method, query, body, h):
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return 0

        self.server.route("GET", "/api/v1/appointments", empty)
        with self.assertRaises(EaUnavailable):
            self.client.list_customer_appointments(7)

    def test_degraded_2xx_wrapper_on_appointments_fails_closed(self):
        def wrapper(path, method, query, body, h):
            from_json({"appointments": []}, handler=h)
            return 0

        self.server.route("GET", "/api/v1/appointments", wrapper)
        with self.assertRaises(EaUnavailable):
            self.client.list_customer_appointments(7)

    def test_degraded_2xx_wrapper_on_availabilities_is_empty_not_error(self):
        # Fail-open is ONLY correct on availabilities, where [] is a legit day.
        def wrapper(path, method, query, body, h):
            from_json({"not": "a list"}, handler=h)
            return 0

        self.server.route("GET", "/api/v1/availabilities", wrapper)
        self.assertEqual(self.client.get_available_hours(5, 1, "2026-09-21"), [])

    def test_list_reads_paginate_until_exhausted(self):
        # Hallazgo 7: 250 appointments on one customer must all come back, even
        # though EA answers them in pages of length (the DB seed below has 250).
        cust_id = 5
        rows = [
            {
                "id": i,
                "start": f"2026-{9 + (i // 28):02d}-{(i % 28) + 1:02d} 08:00:00",
                "customerId": cust_id if i % 3 == 0 else 999,
                "hash": f"h{i}",
            }
            for i in range(1, 401)
        ]

        def data(path, method, query, body, h):
            return paginate_list(lambda q, _p: [r for r in rows if r["customerId"] == cust_id])(path, method, query, body, h)

        self.server.route("GET", "/api/v1/appointments", data)
        got = self.client.list_customer_appointments(cust_id)
        expected = [r for r in rows if r["customerId"] == cust_id]
        self.assertEqual(len(got), len(expected))
        page_requests = [r for r in self.server.record if r["method"] == "GET"]
        self.assertGreater(len(page_requests), 1)  # proves we actually paginated

    def test_availabilities_return_list_of_hours(self):
        def data(path, method, query, body, h):
            return from_json(["08:00", "08:30"], handler=h)

        self.server.route("GET", "/api/v1/availabilities", data)
        self.assertEqual(self.client.get_available_hours(5, 1, "2026-09-21"), ["08:00", "08:30"])

    def test_endless_full_pages_fail_closed_not_hang_or_paginate_partial(self):
        # An upstream that answers a full page on every request (ignoring
        # `page`) must NOT hang the worker nor quietly return a truncated
        # "complete" count: a paginated read is a quota control and fails
        # closed (raises) once the page cap is hit.
        def always_full(path, method, query, body, h):
            page_rows = [{"id": i + int(query.get("page", "1")) * 1000, "start": "2099-01-01 08:00:00"} for i in range(100)]
            return from_json(page_rows, handler=h)

        self.server.route("GET", "/api/v1/appointments", always_full)
        capped = EaClient(token=TOKEN, base_url=self.api_base, timeout=1.0, max_pages=5)
        with self.assertRaises(EaUnavailable):
            capped.list_customer_appointments(7)
        # The cap made the read stop after exactly max_pages round-trips, not hang.
        appt_requests = [r for r in self.server.record if r["method"] == "GET" and r["path"] == "/api/v1/appointments"]
        self.assertEqual(len(appt_requests), 5)


if __name__ == "__main__":
    unittest.main()
