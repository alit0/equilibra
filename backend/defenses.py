"""Cheap request checks the reservation endpoint runs in front of the nucleus.

EQUILIBRA-014 slice 2. Everything here is a *badén/speed bump*, not a real
security barrier. The one real defence against abuse is the nginx rate limit,
which lands in a later slice; none of these fields can be trusted because a
script that reads the HTML can also fill or fake them.

Two probes live here:
- ``honeypot_filled``: a hidden ``website`` field humans never see. If it has
  content, the request almost certainly came from a script that filled in every
  visible input.
- ``suspicious_fill_time``: ``form_started_at`` is the epoch (ms) when the form
  opened. Filling a form takes more than ~3 s; an instant submit is a bot sign.
  A value in the future (beyond a small client/server clock skew) or absurd
  (negative) is equally suspicious.

Both are written to be PURE and clock-injectable so the absurdity/time cases
can be asserted deterministically in tests, without relying on the wall clock.

For both, the *absence* of a field is NOT treated as suspicious: an old client
that predates this contract simply does not send them and must keep working.
Only a *present and wrong* value trips the probe.
"""

from __future__ import annotations

from typing import Any

# A human cannot realistically fill and submit the form faster than this.
MIN_FILL_MS = 3000


def honeypot_filled(payload: dict[str, Any]) -> bool:
    """True when the hidden honeypot field carries any content.

    Absent / ``None`` / empty / whitespace-only are all *clean*: they are how a
    real browser and a pre-contract client behave. Any other present value makes
    the request suspicious.
    """
    value = payload.get("website")
    if value is None:
        return False
    return bool(str(value).strip())


def suspicious_fill_time(payload: dict[str, Any], now_ms: int) -> bool:
    """True when ``form_started_at`` is present but unusable/implausible.

    A ``None`` (absent) field is NEVER suspicious: an old client that predates
    the contract keeps working. The probe only fires when the value is present
    AND is clearly wrong, and only the *floor* matters — a patient who fills the
    form slowly is never rejected.

    Returns ``True`` when any of these holds:
    - the value is not a time-like number (bool, string) --- broken client;
    - the value is negative --- cannot be when the form opened;
    - the value is in the FUTURE relative to the server's ``now_ms`` --- a real
      form cannot have opened in the future; like a 2099 stamp it marks a fake;
    - less than ``MIN_FILL_MS`` elapsed between opening and submit --- no human
      fills a real form that fast (this is a speed bump, not a barrier).
    """
    value = payload.get("form_started_at")
    if value is None:
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return True
    ms = float(value)
    if ms < 0:
        return True
    if ms > now_ms:
        return True
    return (now_ms - ms) < MIN_FILL_MS
