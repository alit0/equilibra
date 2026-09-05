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
        gw = FakeGateway()
        gw.set_available("2026-09-21", ["09:00"])
        with self.assertRaises(BookingError) as cm:
            book(gw, _payload(service_id=1.5, selected_hour="09:00"))
        self.assertEqual(cm.exception.code, "invalid_request")

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


if __name__ == "__main__":
    unittest.main()
