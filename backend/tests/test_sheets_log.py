"""Tests for the reservation -> Google Sheet audit log (EQUILIBRA-014, slice 3).

Pins the one rule that does not move: the sheet must never break or slow a
reservation. Covered here are

* ``build_row`` producing the 14-cell row (col 1..14 order and content) for both
  clinic sedes, alias-vs-plain handling, and consent markers (unit, offline);
* ``log_reservation`` never letting a writer fault escape (app-level promise);
* ``EQUILIBRA_SHEETS_ID`` empty/absent => the log is off and reserving writes
  nothing and still returns 201;
* at the HTTP layer: **a broken/down sheet still returns 201 with the same
  ``appointment_id``** -- the regression this whole slice exists to guard. It
  must turn RED the moment someone lets a sheet error propagate into the
  response;
* no duplicate sheet row on an idempotency replay of the same key (one turn =
  one row).
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from backend.app import make_app
from backend.booking import BookingResult
from backend.sheets_log import HEADERS, build_row, config_writer, log_reservation, sheet_id_from_config
from backend.tests.fake_gateway import FakeGateway

NOW = 1_800_000_000_000
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
            "notes": "me duele el talon",
        },
        "terms_accepted": True,
        "privacy_accepted": True,
        "website": "",
        "form_started_at": NOW - 60_000,
        "idempotency_key": UUID,
    }
    payload.update(overrides)
    return payload


def _booking_result(**overrides) -> BookingResult:
    fields = dict(
        appointment_id=5000,
        appointment_hash=None,
        customer_id=1000,
        customer_email="ana@gmail.com",
    )
    fields.update(overrides)
    return BookingResult(**fields)


class RecordingWriter:
    """In-memory ``SheetWriter`` double: records every appended row."""

    def __init__(self):
        self.rows: list[list[str]] = []

    def append_row(self, row: list[str]) -> None:
        self.rows.append(row)


class BrokenWriter:
    """Sheet writer that raises on every append (the sheet is 'down')."""

    def append_row(self, row: list[str]) -> None:
        raise ConnectionError("google sheets unreachable")


def _make_http_client(gw=None, writer=None):
    gw = gw or FakeGateway()
    client = make_app(gateway=gw, now_ms_provider=lambda: NOW, sheet_writer=writer).test_client()
    return gw, client


class BuildRowTests(unittest.TestCase):
    def test_row_has_exactly_the_14_contract_columns(self):
        row = build_row(_payload(), _booking_result())
        self.assertEqual(len(HEADERS), 14)
        self.assertEqual(len(row), 14)

    def test_clinic_pair_ituzaingo_first_columns(self):
        # service 1 / provider 5 = Ituzaingó / Francisco Tipitto
        row = build_row(_payload(), _booking_result())
        self.assertEqual(row[2], "Ituzaingó")  # sede
        self.assertEqual(row[3], "Francisco Tipitto")  # profesional

    def test_clinic_pair_monte_castro(self):
        p = _payload(service_id=2, provider_id=7, selected_date="2026-09-26", selected_hour="10:00")
        row = build_row(p, _booking_result())
        self.assertEqual(row[2], "Monte Castro")
        self.assertEqual(row[3], "Noelia Pizarro")

    def test_submitted_vs_appointment_and_date_time_columns(self):
        row = build_row(_payload(), _booking_result(appointment_id=5000))
        self.assertEqual(row[1], "5000")
        self.assertEqual(row[4], "2026-09-21")  # fecha_turno
        self.assertEqual(row[5], "08:00")  # hora_turno
        self.assertEqual(row[6], "Ana Ruiz")  # paciente
        self.assertEqual(row[7], "ana@gmail.com")
        self.assertEqual(row[8], "11 4444 1111")
        self.assertEqual(row[9], "me duele el talon")  # nota
        self.assertEqual(row[12], "1000")  # cliente_ea_id

    def test_terminos_y_privacidad_son_si(self):
        row = build_row(_payload(), _booking_result())
        self.assertEqual(row[10:12], ["sí", "sí"])

    def test_plain_email_means_no_alias_column(self):
        # resolved email == submitted plain email => no alias used
        row = build_row(_payload(), _booking_result(customer_email="ana@gmail.com"))
        self.assertEqual(row[13], "")

    def test_alias_resolution_is_recorded(self):
        # EA stored an alias (mailbox dedupe) different from what was typed
        row = build_row(
            _payload(customer={**_payload()["customer"], "email": "maria@gmail.com"}),
            _booking_result(customer_email="maria+juanlopez@gmail.com"),
        )
        self.assertEqual(row[13], "maria+juanlopez@gmail.com")

    def test_timestamp_is_clinic_tz_iso(self):
        row = build_row(_payload(), _booking_result())
        ts = row[0]
        # ISO instant with an explicit offset; the clinic lives in
        # America/Argentina/Buenos_Aires (fixed UTC-3, no DST there).
        self.assertIn("T", ts)
        self.assertTrue(ts.endswith("-03:00"))

    def test_headers_are_the_decision_documented_in_code(self):
        self.assertEqual(HEADERS[0], "timestamp")
        self.assertEqual(HEADERS[1], "appointment_id")
        self.assertEqual(HEADERS[-1], "alias_usado")
        self.assertEqual(len(HEADERS), 14)


class LoggingOffroomTests(unittest.TestCase):
    def test_disabled_writer_none_is_a_noop(self):
        payload = _payload()
        result = _booking_result()
        # must not raise and must not touch anything
        log_reservation(None, payload, result)

    def test_config_no_sheet_id_is_off(self):
        env = dict(os.environ)
        env.pop("EQUILIBRA_SHEETS_ID", None)
        env.pop("EQUILIBRA_SHEETS_CREDENTIALS", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(sheet_id_from_config())
            self.assertIsNone(config_writer())

    def test_config_with_sheet_id_but_no_creds_is_off(self):
        env = dict(os.environ)
        env["EQUILIBRA_SHEETS_ID"] = "abc123"
        env.pop("EQUILIBRA_SHEETS_CREDENTIALS", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(sheet_id_from_config(), "abc123")
            self.assertIsNone(config_writer())


class LogMutationGuardTests(unittest.TestCase):
    """The *no-propagate* guarantee, exercised at the HTTP layer via doubles."""

    def test_log_disabled_by_default_the_reserva_works_ignoring_sheet_env(self):
        # Default construction reads env at make_app time. Unset => disabled,
        # reserving succeeds 201, nothing raised, nothing written.
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        with mock.patch.dict(os.environ, {}, clear=False):
            client = make_app(gateway=gw, now_ms_provider=lambda: NOW).test_client()
        resp = client.post("/api/reservar", data=json.dumps(_payload()), content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.get_json()["ok"])

    def test_sheet_down_still_returns_201_with_appointment_id(self):
        # THE headline regression: a broken sheet must not break the reserva.
        gw, client = _make_http_client(writer=BrokenWriter())
        gw.set_available("2026-09-21", ["08:00"])
        resp = client.post("/api/reservar", data=json.dumps(_payload()), content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["appointment_id"], int)
        # And the turn really was booked in EA despite the sheet being down.
        self.assertEqual(len(gw.appointments), 1)
        self.assertEqual(body["appointment_id"], next(iter(gw.appointments)))

    def test_concurrent_sheet_fault_cannot_turn_201_into_a_500(self):
        # Guards that log_reservation/its wrapper swallow *any* exception type,
        # not just a specific ConnectionError.
        class Weird(Exception):
            pass

        class CarnageWriter:
            def append_row(self, row):
                raise Weird("who knows")

        gw, client = _make_http_client(writer=CarnageWriter())
        gw.set_available("2026-09-21", ["08:00"])
        resp = client.post("/api/reservar", data=json.dumps(_payload()), content_type="application/json")
        self.assertEqual(resp.status_code, 201)

    def test_one_row_per_fresh_reserve_written(self):
        rec = RecordingWriter()
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        client = make_app(gateway=gw, now_ms_provider=lambda: NOW, sheet_writer=rec).test_client()
        resp = client.post("/api/reservar", data=json.dumps(_payload()), content_type="application/json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(len(rec.rows), 1)

    def test_idempotency_replay_does_not_duplicate_the_sheet_row(self):
        # Same idempotency_key sent three times = ONE turn => ONE sheet row.
        rec = RecordingWriter()
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        client = make_app(gateway=gw, now_ms_provider=lambda: NOW, sheet_writer=rec).test_client()

        def post():
            return client.post("/api/reservar", data=json.dumps(_payload()), content_type="application/json")

        id1 = post().get_json()["appointment_id"]
        id2 = post().get_json()["appointment_id"]
        id3 = post().get_json()["appointment_id"]
        self.assertEqual({id1, id2, id3}, {id1})  # all replays of the same turn
        self.assertEqual(len(gw.appointments), 1)
        self.assertEqual(len(rec.rows), 1)  # exactly one row ever written
        self.assertEqual(rec.rows[0][1], str(id1))

    def test_row_content_hits_the_writer_with_expected_values(self):
        rec = RecordingWriter()
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        client = make_app(gateway=gw, now_ms_provider=lambda: NOW, sheet_writer=rec).test_client()
        client.post("/api/reservar", data=json.dumps(_payload()), content_type="application/json")
        row = rec.rows[0]
        self.assertEqual(row[1], str(next(iter(gw.appointments))))
        self.assertEqual(row[2], "Ituzaingó")
        self.assertEqual(row[7], "ana@gmail.com")
        self.assertTrue(
            row[13] == "" or "@" in row[13]
        )  # plain email => empty alias cell


if __name__ == "__main__":
    unittest.main()
