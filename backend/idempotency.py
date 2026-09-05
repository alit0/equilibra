"""In-process response idempotency for the reservation endpoint.

``POST /api/reservar`` must create ONE appointment even when the patient hits
"confirmar" three times, when a retry fires after a network timeout, or when two
POSTs with the same ``idempotency_key`` race each other. The second request with
an already-seen key must return the SAME ``appointment_id`` and create nothing.

This is deliberately the simplest correct thing for a SINGLE process:

- The map ``key -> appointment_id`` lives in this object's memory. Flask runs
  the app with one of these shared across all threads, so within one process the
  guard is exact.
- Creation happens UNDER the object's own lock. That is what makes the
  double-POST race safe: whichever thread grabs the lock first runs ``book()``,
  records the id, and the second thread finds the key already answered and
  returns the stored id without ever calling ``book()`` again. There is no
  sub-process window where two requests with the same key both create.

What this does NOT guarantee (said out loud, mirroring the TOCTOU honesty in
``booking.py``):

- It is NOT durable. If the process stops between a successful create and the
  reply, the map is gone; a client that retries after that would create again,
  because nothing survived. This endpoint has no shared store, and wiring a real
  one (Redis/DB/nginx) is not in this slice.
- It does NOT span processes/workers. Two independent workers each hold their own
  map, so two racing POSTs routed to DIFFERENT workers could both create. In
  today's deployment (a single Flask process behind nginx) that cannot happen;
  if a later slice adds more than one worker, idempotency must move to a shared
  store first. It is a correctness limit, not a secret.
- ``key=None`` (an old client that does not send the field at all) means "no
  idempotency": the request always runs ``book()`` and nothing is cached.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


class IdempotencyStore:
    """Maps an ``idempotency_key`` to the appointment id it already produced."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._map: dict[str, int] = {}

    def execute_once(self, key: str | None, create: Callable[[], Any]) -> Any:
        """Run ``create()`` at most once per ``key`` and replay its result.

        * When ``key`` is ``None``, ``create`` runs every time and nothing is
          stored (a pre-contract client keeps working).
        * When ``key`` is present and already served, returns the stored value
          WITHOUT calling ``create``.
        * When ``key`` is new, ``create`` runs under the lock and its result is
          recorded. If ``create`` raises, nothing is recorded (a failed intent is
          not idempotent) and the exception propagates untouched.
        """
        if key is None:
            return create()
        with self._lock:
            if key in self._map:
                return self._map[key]
            result = create()
            self._map[key] = result
            return result
