from __future__ import annotations

import datetime as _dt
import unittest

from backend.booking import (
    BookingError,
    build_alias,
    build_notes,
    mailbox_key,
    normalise_name,
    book,
)
from backend.tests.fake_gateway import FakeEa, FakeGateway


def _payload(**overrides) -> dict:
    payload = {
        "service_id": 1,
        "provider_id": 5,
        "selected_date": "2026-09-21",
        "selected_hour": "08:00",
        "service_duration": 30,
        "customer": {
            "first_name": "Juan",
            "last_name": "López",
            "email": "maria@gmail.com",
            "phone_number": "11 5555 2222",
            "notes": "Dolor en el talón\ndel pie izquierdo.",
        },
        "terms_accepted": True,
        "privacy_accepted": True,
    }
    payload.update(overrides)
    return payload


def _future_date() -> str:
    """A plausible provider-5 (Monday) date that is always ahead of today."""
    today = _dt.date.today().isoformat()
    base = _dt.date.fromisoformat(today)
    monday = base + _dt.timedelta(days=(7 - base.weekday()) % 7 or 7)
    return monday.isoformat()


class NotesTests(unittest.TestCase):
    def test_notes_with_note_collapses_whitespace(self):
        self.assertEqual(
            build_notes("Juan", "López", "Dolor  en\nel talón\tizquierdo."),
            "Paciente: Juan López\nNota: Dolor en el talón izquierdo.",
        )

    def test_notes_without_note_has_no_nota_line(self):
        self.assertEqual(build_notes("Ana", "Perez", ""), "Paciente: Ana Perez")

    def test_notes_missing_last_name_still_formats(self):
        self.assertEqual(build_notes("Ana", "", "fiebre"), "Paciente: Ana\nNota: fiebre")


class AliasTests(unittest.TestCase):
    def test_deterministic_documented_alias(self):
        self.assertEqual(build_alias("maria@gmail.com", "Juan López"), "maria+juanlopez@gmail.com")

    def test_alias_ignores_existing_plus_tag(self):
        # Only the pre-+ part belongs to the real mailbox.
        self.assertEqual(build_alias("maria+other@gmail.com", "Juan López"), "maria+juanlopez@gmail.com")

    def test_alias_normalises_accents_and_case(self):
        self.assertEqual(build_alias("Pedro@hotmail.com", "José María García Pérez"), "pedro+josemariagarciaperez@hotmail.com")

    def test_alias_stays_deterministic(self):
        a = build_alias("x@gmail.com", "María Ruiz")
        b = build_alias("x@gmail.com", "maria      ruiz ")
        self.assertEqual(a, b)

    def test_alias_sharing_slug_prefix_not_truncation_collision(self):
        # Two patients whose slugs share their entire *truncatable* prefix and
        # differ only after the RFC 5321 cap would collapse under a naive
        # `[:62]` truncation (hallazgo 8 / despacho segundo filtro). Build the
        # alias over a long local-part so the name barely fits, then assert the
        # hash-based alias still keeps them apart.
        long_email = "m" * 57 + ".@gmail.com"
        a = build_alias(long_email, "Ana Perez")
        b = build_alias(long_email, "Ana Gomez")
        self.assertNotEqual(a, b)


class NormaliseAndMailboxTests(unittest.TestCase):
    def test_mailbox_key_strips_plus_tag(self):
        self.assertEqual(mailbox_key("maria+abc@gmail.com"), "maria@gmail.com")
        self.assertEqual(mailbox_key("maria@gmail.com"), "maria@gmail.com")

    def test_normalise_name(self):
        self.assertEqual(normalise_name("  José María  López "), "josemarialopez")


class ValidatePayloadTests(unittest.TestCase):
    def assert_code(self, payload, code):
        gw = FakeGateway()
        with self.assertRaises(BookingError) as cm:
            book(gw, payload)
        self.assertEqual(cm.exception.code, code)

    def test_valid_pair_accepts_and_missing_whitespace_name_rejected(self):
        # The (1,5) pair is the documented valid one.
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        # a valid request proceeds to the availability check (hour present) and succeeds
        result = book(gw, _payload())
        self.assertIsInstance(result.appointment_id, int)

    def test_rejects_wrong_pair(self):
        self.assert_code(_payload(service_id=1, provider_id=7), "invalid_request")

    def test_bad_date(self):
        self.assert_code(_payload(selected_date="07-21-2026"), "invalid_request")

    def test_bad_time(self):
        self.assert_code(_payload(selected_hour="25:00"), "invalid_request")

    def test_bad_email(self):
        self.assert_code(_payload(customer={**_payload()["customer"], "email": "noesunmail"}), "invalid_request")

    def test_short_phone(self):
        self.assert_code(
            _payload(customer={**_payload()["customer"], "phone_number": "11"}), "invalid_request"
        )

    def test_missing_name(self):
        cust = {**_payload()["customer"], "first_name": " ", "last_name": " "}
        self.assert_code(_payload(customer=cust), "invalid_request")

    def test_terms_not_checked(self):
        self.assert_code(_payload(terms_accepted=False), "invalid_request")

    def test_privacy_payload_only_true_when_bool_checked(self):
        self.assert_code(_payload(privacy_accepted="false"), "invalid_request")


class SlotRuleTests(unittest.TestCase):
    def test_occupied_slot_rejected_and_nothing_created(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["09:00", "10:00"])  # 08:00 already taken
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(selected_hour="08:00"))
        self.assertEqual(cm.exception.code, "requested_hour_is_unavailable")
        self.assertEqual(gw.appointments, {})
        self.assertEqual(gw.customers, {})


class CustomerMatchTests(unittest.TestCase):
    def test_same_email_same_name_reuses_customer(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        book(gw, _payload())
        first_count = len(gw.customers)

        # A second, separate date booking to keep the single-turn/day free.
        gw.set_available("2026-09-28", ["08:00"])
        book(gw, _payload(selected_date="2026-09-28"))

        self.assertEqual(len(gw.customers), first_count)
        ids = {c["id"] for c in gw.customers.values()}
        self.assertEqual(len(ids), 1)

    def test_same_email_different_name_creates_alias_customer(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        # Mother books for herself first (root email, no alias).
        book(gw, _payload(customer={**_payload()["customer"], "first_name": "María", "last_name": "López"}))
        root = next(c for c in gw.customers.values() if c["email"] == "maria@gmail.com")

        # Now the same email books a *child* on a different day -> alias.
        gw.set_available("2026-10-12", ["08:00"])
        book(
            gw,
            _payload(
                selected_date="2026-10-12",
                customer={**_payload()["customer"], "first_name": "Juan", "last_name": "López"},
            ),
        )
        emails = {c["email"].lower() for c in gw.customers.values()}
        self.assertIn("maria@gmail.com", emails)
        self.assertIn("maria+juanlopez@gmail.com", emails)


class DayRuleTests(unittest.TestCase):
    def test_second_turn_same_patient_same_day_rejected(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00", "09:00", "10:00"])
        book(gw, _payload(selected_hour="08:00"))
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(selected_hour="09:00"))
        self.assertEqual(cm.exception.code, "patient_already_booked_that_day")
        # Only the first appointment exists.
        self.assertEqual(len(gw.appointments), 1)


class FutureQuotaTests(unittest.TestCase):
    def test_fourth_future_turn_on_same_mailbox_rejected(self):
        gw = FakeGateway()
        date = _future_date()
        # Seed three future bookings for the mailbox across customers aliases
        # (the very scenario hard rule 3 exists for).
        root = gw.add_customer({"first_name": "María", "last_name": "López", "email": "maria@gmail.com"})
        alias = gw.add_customer(
            {"first_name": "Juan", "last_name": "López", "email": "maria+juanlopez@gmail.com"}
        )
        from datetime import timedelta

        base = _dt.date.fromisoformat(date)
        gw.add_appointment(customer_id=root["id"], start=f"{(base + timedelta(days=7)).isoformat()} 09:00:00")
        gw.add_appointment(customer_id=alias["id"], start=f"{(base + timedelta(days=14)).isoformat()} 09:00:00")
        gw.add_appointment(customer_id=alias["id"], start=f"{(base + timedelta(days=21)).isoformat()} 09:00:00")

        gw.set_available(date, ["08:00"])
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(selected_date=date, selected_hour="08:00"))
        self.assertEqual(cm.exception.code, "too_many_future_appointments")
        self.assertEqual(len([a for a in gw.appointments.values()]), 3)


class EmptyAvailabilityTests(unittest.TestCase):
    def test_empty_avail_list_is_not_an_error(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", [])  # "no hay nada ese día"
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(selected_hour="08:00"))
        self.assertEqual(cm.exception.code, "requested_hour_is_unavailable")
        self.assertEqual(gw.appointments, {})


class RegressionPostExploitationTests(unittest.TestCase):
    """One failing-then-passing test per production fix (despacho equi-014)."""

    # ---- hallazgo 4: phone carried onto the EA customer --------------------
    def test_created_customer_keeps_phone(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        book(gw, _payload(customer={**_payload()["customer"], "email": "nuevo@gmail.com"}))
        created = next(iter(gw.customers.values()))
        self.assertEqual(created["phone_number"], "11 5555 2222")

    def test_rejected_reservation_creates_no_customer(self):
        # hallazgo 11/12/5 umbrella: an invalid request leaves EA untouched.
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["09:00"])
        with self.assertRaises(BookingError):
            book(gw, _payload(selected_hour="08:00"))  # not free
        self.assertEqual(gw.customers, {})
        self.assertEqual(gw.appointments, {})

    # ---- hallazgo 2: casing + Gmail dots cannot evade the mailbox quota ----
    def test_casing_does_not_evade_mailbox_quota(self):
        gw = FakeGateway()
        date = _future_date()
        self._seed_three_future(gw, date)
        gw.set_available(date, ["08:00"])
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(customer={**_payload()["customer"], "email": "MARIA@Gmail.com"}, selected_date=date))
        self.assertEqual(cm.exception.code, "too_many_future_appointments")

    def test_gmail_dots_do_not_evade_mailbox_quota(self):
        gw = FakeGateway()
        date = _future_date()
        self._seed_three_future(gw, date)
        gw.set_available(date, ["08:00"])
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(customer={**_payload()["customer"], "email": "m.aria@gmail.com"}, selected_date=date))
        self.assertEqual(cm.exception.code, "too_many_future_appointments")

    def test_mailbox_key_normalises_case_and_dots(self):
        from backend.booking import mailbox_key

        self.assertEqual(mailbox_key("Maria@GMail.com"), mailbox_key("maria@gmail.com"))
        self.assertEqual(mailbox_key("m.aria@gmail.com"), mailbox_key("maria+tag@gmail.com"))

    @staticmethod
    def _seed_three_future(gw, date):
        from datetime import timedelta

        root = gw.add_customer({"first_name": "María", "last_name": "López", "email": "maria@gmail.com"})
        alias = gw.add_customer({"first_name": "Juan", "last_name": "López", "email": "maria+juan@gmail.com"})
        base = _dt.date.fromisoformat(date)
        gw.add_appointment(customer_id=root["id"], start=f"{(base + timedelta(days=14)).isoformat()} 09:00:00")
        gw.add_appointment(customer_id=alias["id"], start=f"{(base + timedelta(days=7)).isoformat()} 09:00:00")
        gw.add_appointment(customer_id=alias["id"], start=f"{(base + timedelta(days=21)).isoformat()} 09:00:00")

    # ---- hallazgos 3 & 5: same-day *future* slots count; zone = clinic -----
    def test_turn_later_today_counts_as_future(self):
        gw = FakeGateway()
        art = _dt.timezone(_dt.timedelta(hours=-3))
        now = _dt.datetime(2026, 9, 7, 8, 0, 0, tzinfo=art)  # Monday 08:00 clinic
        monday = "2026-09-07"
        # Three already-reserved slots LATER today for the same real mailbox.
        root = gw.add_customer({"first_name": "Ana", "last_name": "Lopez", "email": "ana@gmail.com"})
        c2 = gw.add_customer({"first_name": "Pepe", "last_name": "Lopez", "email": "ana+jpopez@gmail.com"})
        gw.add_appointment(customer_id=root["id"], start="2026-09-07 10:00:00")
        gw.add_appointment(customer_id=c2["id"], start="2026-09-07 11:00:00")
        gw.add_appointment(customer_id=c2["id"], start="2026-09-07 12:00:00")
        gw.set_available(monday, ["13:00"])
        book_payload = _payload(
            selected_date=monday,
            selected_hour="13:00",
            customer={**_payload()["customer"], "email": "ana@gmail.com"},
        )
        with self.assertRaises(BookingError) as cm:
            book(gw, book_payload, now=now)
        self.assertEqual(cm.exception.code, "too_many_future_appointments")

    def test_clinic_date_used_not_utc(self):
        # 2026-09-07T01:00Z == Sunday 23:00 in Buenos Aires; that Sunday's slot
        # is a FUTURE one only from the clinic's wall clock, but here we just
        # assert the correct local "today" is picked so quotas align with the
        # clinic.
        from backend.booking import _as_clinic_naive

        art_midnight = _dt.datetime(2026, 9, 6, 20, 0, tzinfo=_dt.timezone.utc)
        self.assertEqual(_as_clinic_naive(art_midnight).date().isoformat(), "2026-09-06")

    def test_book_uses_clinic_zone_for_quota_not_utc_rollover(self):
        # Regression for the second review: app.py/booking must treat "now" in
        # the *clinic's* wall clock. Feed a UTC instant that is Tuesday 01:00
        # UTC == Monday 22:00 in Buenos Aires. A naive-UTC clock would think it
        # is Tuesday and would no longer treat the three *Monday-evening* turns
        # (22:30/22:45/23:00 ART) as future, letting a 4th turn through. With the
        # clinic conversion those Monday turns still count toward the cap of 3,
        # so this booking must be rejected.
        gw = FakeGateway()
        monday = "2026-09-07"
        now_utc = _dt.datetime(2026, 9, 8, 1, 0, 0, tzinfo=_dt.timezone.utc)  # Monday 22:00 ART
        from datetime import timedelta

        # Each +tag folds to the real inbox ana@gmail.com (Gmail-equivalent dots
        # would be stripped differently, so aliases use +tags).
        seed = [
            ("ana@gmail.com", "Ana", "22:30"),
            ("ana+juan@gmail.com", "Juan", "22:45"),
            ("ana+maria@gmail.com", "Maria", "23:00"),
        ]
        for email, name, hour in seed:
            cid = gw.add_customer({"first_name": name, "last_name": "Familia", "email": email})["id"]
            # Three future turns later ON that Monday, in clinic wall time
            # (2026-09-07 22:30/22:45/23:00 == 2026-09-08 01:30/01:45/02:00 UTC).
            gw.add_appointment(customer_id=cid, start=f"{monday} {hour}:00")
        # A 4th booking for the same real mailbox (Rosa, brand-new alias) on a
        # later clinic date would push the mailbox past 3.
        later = _dt.date(2026, 9, 14).isoformat()
        gw.set_available(later, ["08:00"])
        book_payload = _payload(
            selected_date=later,
            selected_hour="08:00",
            customer={**_payload()["customer"], "email": "ana@gmail.com", "first_name": "Rosa", "last_name": "Gomez"},
        )
        customers_before = {k for k in gw.customers}
        with self.assertRaises(BookingError) as cm:
            book(gw, book_payload, now=now_utc)
        self.assertEqual(cm.exception.code, "too_many_future_appointments")
        # None of the mailbox turns may already exist for Rosa, and no orphan
        # customer was created because the quota check precedes any create.
        self.assertEqual({k for k in gw.customers}, customers_before)

    # ---- hallazgo 1: TOCTOU pre-create re-check + post rollback ------------
    def test_slot_that_disappears_after_check_is_rejected_before_post(self):
        # Availability lists 08:00 on the first read, but the slot is gone on
        # the second (the pre-create re-validation) — production must NOT POST.
        from datetime import timedelta

        available = {"calls": 0}

        class VanishingGateway(FakeGateway):
            def get_available_hours(self, provider_id, service_id, date):
                available["calls"] += 1
                return [] if available["calls"] >= 2 else ["08:00"]

        gw = VanishingGateway()
        gw.set_available("2026-09-21", ["08:00", "09:00"])
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload())
        self.assertEqual(cm.exception.code, "requested_hour_is_unavailable")
        self.assertEqual(gw.appointments, {})

    def test_double_book_same_slot_disallowed_even_though_ea_accepts(self):
        # Two DIFFERENT patients of the same real mailbox request the very same
        # slot. EA (and this fake) never refuse an overlap, so the post-create
        # reconciliation must drop the second one and report a real conflict
        # instead of silently double-booking.
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00", "09:00", "10:00"])
        base_cust = _payload()["customer"]
        mother = {
            **_payload(),
            "customer": {
                **base_cust,
                "email": "maria@gmail.com",
                "first_name": "María",
                "last_name": "López",
            },
        }
        book(gw, mother)
        # A child of the mailbox (deterministic +alias) asks for the exact same slot.
        child = {
            **_payload(),
            "customer": {
                **base_cust,
                "email": "maria@gmail.com",
                "first_name": "Juan",
                "last_name": "López",
            },
        }
        with self.assertRaises(BookingError) as cm:
            book(gw, child)
        self.assertEqual(cm.exception.code, "requested_hour_is_unavailable")
        self.assertEqual(len(gw.appointments), 1)

    # ---- hallazgo 1 (segundo filtro) / A: rollback DELETE that fails ---------
    def test_rollback_delete_failing_still_fails_closed_and_logs_id(self):
        # A competing appointment is detected right after our POST and we try to
        # roll our own row back, but the DELETE call throws (e.g. EA unreachable
        # on that request). The booking must NOT report success, must raise a
        # controlled BookingError (not a raw transport exception), and must log
        # the appointment_id an operator needs to reconcile the two rows by hand.
        from backend.ea_client import EaUnavailable

        deletes: list[int] = []

        class FlakyDelete(FakeGateway):
            def delete_appointment(self, appointment_id):
                deletes.append(int(appointment_id))
                raise EaUnavailable("EA DELETE unreachable on test")

        gw = FlakyDelete()
        gw.set_available("2026-09-21", ["08:00", "09:00", "10:00"])
        base_cust = _payload()["customer"]
        mother = {
            **_payload(),
            "customer": {
                **base_cust,
                "email": "maria@gmail.com",
                "first_name": "María",
                "last_name": "López",
            },
        }
        book(gw, mother)
        child = {
            **_payload(),
            "customer": {
                **base_cust,
                "email": "maria@gmail.com",
                "first_name": "Juan",
                "last_name": "López",
            },
        }
        with self.assertLogs("backend.booking", level="ERROR") as logs:
            with self.assertRaises(BookingError) as cm:
                book(gw, child)
        self.assertEqual(cm.exception.code, "requested_hour_is_unavailable")
        # The delete was *attempted* but the orphan could not be removed, so both
        # rows persist; the leftover must be recorded, never silently ignored.
        self.assertEqual(deletes, [5001])
        self.assertEqual(len(gw.appointments), 2)
        self.assertTrue(any("5001" in line for line in logs.output))
        self.assertTrue(any("manual cleanup required" in line for line in logs.output))

    # ---- hallazgo 5: no orphan customer when the day rule rejects ----------
    def test_second_turn_same_day_rejects_without_new_customer(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00", "09:00"])
        first = book(gw, _payload(selected_hour="08:00"))
        customer_ids_before = {c for c in gw.customers}
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(selected_hour="09:00"))
        self.assertEqual(cm.exception.code, "patient_already_booked_that_day")
        self.assertEqual({c for c in gw.customers}, customer_ids_before)

    # ---- hallazgo 5 (segundo filtro) / E: new alias + full quota -> no create
    def test_new_alias_full_mailbox_quota_creates_no_customer(self):
        # The order guarantee (quota BEFORE creating any customer, despacho
        # equilibra-014 re-review #5) bites precisely when the caller is a
        # patient that mailbox has NEVER seen: EA has 3 future turns already, and
        # resolving this brand-new person would create a fresh +alias customer.
        # That customer must NOT exist after the rejection.
        gw = FakeGateway()
        date = _future_date()

        # Seed a full mailbox: root + two +alias customers, 3 future turns total.
        for email, name in (
            ("maria@gmail.com", "María"),
            ("maria+juan@gmail.com", "Juan"),
            ("maria+pepe@gmail.com", "Pepe"),
        ):
            rec = gw.add_customer({"first_name": name, "last_name": "Lopez", "email": email})
            gw.add_appointment(customer_id=rec["id"], start="2099-01-05 09:00:00")
        before = {k for k in gw.customers}
        gw.set_available(date, ["08:00"])

        # A NEW name on that mailbox: resolve would mint maria+rosagomez@gmail.com.
        book_payload = _payload(
            selected_date=date,
            customer={**_payload()["customer"], "email": "maria@gmail.com", "first_name": "Rosa", "last_name": "Gomez"},
        )
        with self.assertRaises(BookingError) as cm:
            book(gw, book_payload)
        self.assertEqual(cm.exception.code, "too_many_future_appointments")
        # The orphan that a create-before-check order would leave must not appear.
        self.assertEqual({k for k in gw.customers}, before)
        self.assertEqual(len(gw.appointments), 3)

    # ---- hallazgo 6: name cannot inject an extra notes line ----------------
    def test_notes_collapse_whitespace_in_name(self):
        self.assertEqual(
            build_notes("Juan\nPaciente: Otro", "Lopez", "me duele"),
            "Paciente: Juan Paciente: Otro Lopez\nNota: me duele",
        )

    # ---- hallazgo 8: alias never collides when truncated --------------------
    def test_long_base_alias_never_equals_root_and_is_hash_unique(self):
        long_email = "nombrepersonamuyperolarguisimo.palabrasaltamente.complejas@gmail.com"
        root_local = long_email.split("@")[0]
        a = build_alias(long_email, "Maximiliano Perez Gomez")
        b = build_alias(long_email, "María Rodriguez Fernandez")
        self.assertNotEqual(a.split("@")[0], root_local)  # never the root email
        self.assertNotEqual(a, b)

    # ---- hallazgo 11: type enforcement --------------------------------------
    def test_non_string_email_400(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        cust = {**_payload()["customer"], "email": 123, "phone_number": "11 5555 2222"}
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(customer=cust))
        self.assertEqual(cm.exception.code, "invalid_request")

    def test_service_id_float_not_coerced(self):
        # 1.0 would be a *valid* pair (service 1 / provider 5) if the code
        # coerced floats with int(): 1.5 is ambiguous because ALLOWED_PAIRS.get
        # rejects it even without coercion. Using 1.0 proves the validator
        # rejects the *type*, and that coercing would actually have booked.
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        cust = {**_payload()["customer"], "phone_number": "11 5555 2222"}
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(service_id=1.0, customer=cust, selected_hour="08:00"))
        self.assertEqual(cm.exception.code, "invalid_request")
        self.assertEqual(cm.exception.fields, ["service_id"])
        # A type-coercion bug would have progressed to create the appointment;
        # assert the calendar and contacts stayed untouched.
        self.assertEqual(gw.appointments, {})
        self.assertEqual(gw.customers, {})

    def test_phone_must_be_string(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        cust = {**_payload()["customer"], "phone_number": 1155552222}
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(customer=cust))
        self.assertEqual(cm.exception.code, "invalid_request")

    # ---- hallazgo 12: non-existent calendar date ---------------------------
    def test_leap_invalid_date_400_not_409(self):
        gw = FakeGateway()
        gw.set_available("2026-02-31", ["08:00"])
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(selected_date="2026-02-31"))
        self.assertEqual(cm.exception.code, "invalid_request")


class HappyPathTests(unittest.TestCase):
    def test_books_and_returns_id(self):
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        result = book(gw, _payload())
        self.assertEqual(result.appointment_id, next(iter(gw.appointments)))
        self.assertIsNotNone(result.appointment_hash)
        appt = next(iter(gw.appointments.values()))
        self.assertEqual(appt["start"], "2026-09-21 08:00:00")
        # Hallazgo 4/6: the stored notes persist exactly what we built and the
        # free note is collapsed to a single parseable line.
        self.assertEqual(
            appt["notes"],
            "Paciente: Juan López\nNota: Dolor en el talón del pie izquierdo.",
        )
        # The customer keeps the phone (require_phone_number lives in EA).
        created_customer = next(iter(gw.customers.values()))
        self.assertEqual(created_customer["phone_number"], "11 5555 2222")

    def test_appointment_start_stays_clinic_naive_not_converted(self):
        # EQUILIBRA-014 guard: the root cause was a *missing timezone on the
        # customer*, NOT a timezone bug on the slot. The `start` we hand EA must
        # keep the naive clinic string "{date} {hour}:00" ground on clinic-local
        # time (provider zone); converting it to UTC here would corrupt cupo,
        # Google Calendar and the real clinic-clock hour. Pinning it: 08:00 must
        # be stored as 08:00 no matter the argument's own zone tricks.
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["08:00"])
        book(gw, _payload(selected_date="2026-09-21", selected_hour="08:00"))
        appt = next(iter(gw.appointments.values()))
        self.assertEqual(appt["start"], "2026-09-21 08:00:00")


if __name__ == "__main__":
    unittest.main()
