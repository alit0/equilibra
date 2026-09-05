"""Unit tests for the pure anti-abuse probes in ``backend.defenses``.

These pin the exact decisions of EQUILIBRA-014 slice 2:

* ``website`` (honeypot): *absent* means the same as *empty* and is fine; only a
  present, non-whitespace value is suspicious (a bot that filled every input).
* ``form_started_at`` (map/speed bump): *absent* is fine (old client). Present
  and wrong (negative, future of any size, non-numeric, or submitted in <
  ``MIN_FILL_MS``) is suspicious. Only the floor matters: a slow patient is never
  penalised.

Clock values are injected so absurd/future cases are deterministic.
"""

from __future__ import annotations

import unittest

from backend.defenses import (
    MIN_FILL_MS,
    honeypot_filled,
    suspicious_fill_time,
)

NOW = 1_800_000_000_000  # any fixed moment, ms


class HoneypotTests(unittest.TestCase):
    def test_absent_website_is_clean(self):
        self.assertFalse(honeypot_filled({}))
        self.assertFalse(honeypot_filled({"customer": {}}))

    def test_empty_and_whitespace_website_are_clean(self):
        # A real browser submits the contract's empty value. Whitespace-only and
        # None are equivalent to empty, not to "filled".
        self.assertFalse(honeypot_filled({"website": ""}))
        self.assertFalse(honeypot_filled({"website": "   "}))
        self.assertFalse(honeypot_filled({"website": None}))

    def test_content_of_any_kind_is_suspicious(self):
        self.assertTrue(honeypot_filled({"website": "http://x"}))
        self.assertTrue(honeypot_filled({"website": "a"}))
        self.assertTrue(honeypot_filled({"website": 123}))
        self.assertTrue(honeypot_filled({"website": True}))


class FillTimeTests(unittest.TestCase):
    def test_absent_field_is_clean(self):
        self.assertFalse(suspicious_fill_time({}, NOW))
        self.assertFalse(suspicious_fill_time({"customer": {}}, NOW))

    def test_instant_submit_is_suspicious(self):
        # Form opened "now" and submitted ~1 ms later: a script, not a human.
        self.assertTrue(suspicious_fill_time({"form_started_at": NOW}, NOW))
        self.assertTrue(
            suspicious_fill_time({"form_started_at": NOW - 100}, NOW)
        )

    def test_under_min_fill_is_suspicious(self):
        self.assertTrue(
            suspicious_fill_time({"form_started_at": NOW - (MIN_FILL_MS - 1)}, NOW)
        )

    def test_slow_patient_never_suspicious(self):
        # FAR past = a patient who left the tab open for an hour (or a day).
        # Only the floor matters; there is no ceiling.
        self.assertFalse(
            suspicious_fill_time({"form_started_at": NOW - 3_600_000}, NOW)
        )
        self.assertFalse(
            suspicious_fill_time({"form_started_at": NOW - 86_400_000}, NOW)
        )

    def test_less_than_min_fill_threshold_is_clean(self):
        # Exactly at the floor is a plausible human (3 s), so it must pass.
        self.assertFalse(
            suspicious_fill_time({"form_started_at": NOW - MIN_FILL_MS}, NOW)
        )

    def test_future_of_any_kind_is_suspicious(self):
        # A real form cannot have opened in the future. 1 s ahead or "2099"
        # alike, it is as suspicious as a too-recent stamp.
        self.assertTrue(suspicious_fill_time({"form_started_at": NOW + 1}, NOW))
        self.assertTrue(
            suspicious_fill_time({"form_started_at": NOW + 3_600_000_000}, NOW)
        )

    def test_negative_is_suspicious(self):
        self.assertTrue(suspicious_fill_time({"form_started_at": -1}, NOW))

    def test_non_numeric_is_suspicious(self):
        self.assertTrue(suspicious_fill_time({"form_started_at": "yesterday"}, NOW))
        self.assertTrue(suspicious_fill_time({"form_started_at": True}, NOW))


if __name__ == "__main__":
    unittest.main()
