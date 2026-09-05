"""Unit tests for the in-process ``IdempotencyStore``.

EQUILIBRA-014 slice 2. The store answers the "tres veces confirmar" promise at
the HTTP layer: a second request bearing an already-served ``idempotency_key``
returns the SAME appointment id and does NOT create a second turn.

Guarantees pinned here:
* same key, sequential  -> one creation, same result replayed;
* same key, racing      -> still exactly one creation (the lock serialises);
* different keys        -> independent creations (idempotency is per intent);
* ``key=None`` (old client) -> runs every time, caches nothing;
* a raising ``create``  -> nothing is cached, the error propagates;
* result type is whatever ``create`` returned (an appointment id at runtime).
"""

from __future__ import annotations

import threading
import unittest

from backend.idempotency import IdempotencyStore


def _counter_next(start=1000):
    counter = {"i": start - 1}

    def create():
        counter["i"] += 1
        return counter["i"]

    return create


class IdempotencyStoreTests(unittest.TestCase):
    def test_replays_same_id_for_same_key_without_second_creation(self):
        store = IdempotencyStore()
        counter = {"runs": 0}

        def create():
            counter["runs"] += 1
            return 5001

        self.assertEqual(store.execute_once("key-1", create), 5001)
        self.assertEqual(store.execute_once("key-1", create), 5001)
        self.assertEqual(store.execute_once("key-1", create), 5001)
        self.assertEqual(counter["runs"], 1)  # created exactly once

    def test_different_keys_are_independent(self):
        store = IdempotencyStore()
        next_id = _counter_next(5000)
        self.assertEqual(store.execute_once("a", next_id), 5000)
        self.assertEqual(store.execute_once("b", next_id), 5001)  # a NEW booking
        self.assertEqual(store.execute_once("a", next_id), 5000)  # replay
        self.assertEqual(store.execute_once("b", next_id), 5001)

    def test_exception_is_not_cached_and_propagates(self):
        # A failed intent (e.g. 409) has nothing to replay: the same key must be
        # able to retry later without being stuck on a half answer.
        store = IdempotencyStore()

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            store.execute_once("k", lambda: (_ for _ in ()).throw(Boom("no")))
        # A later retry of the SAME key actually runs create again (it may now
        # succeed and pick a different real slot).
        ran = []
        store.execute_once("k", lambda: ran.append(1) or 777)
        self.assertEqual(ran, [1])
        self.assertEqual(store.execute_once("k", lambda: ran.append(2) or 1), 777)

    def test_None_key_runs_every_time_and_never_caches(self):
        # A pre-contract client sends no key: it always flows through creation.
        store = IdempotencyStore()
        next_id = _counter_next(10)
        self.assertEqual(store.execute_once(None, next_id), 10)
        self.assertEqual(store.execute_once(None, next_id), 11)
        self.assertEqual(store.execute_once(None, next_id), 12)

    def test_racing_threads_same_key_create_once(self):
        # The double-POST race: two incoming requests with the SAME key reach the
        # store "at the same time". Exactly one may create; the other must get
        # the winner's stored id. Uses a barrier so both threads actually overlap
        # on the lock instead of running strictly one-after-another.
        store = IdempotencyStore()
        results: dict[int, int] = {}
        barrier = threading.Barrier(2)
        created_ids: list[int] = []
        lock = threading.Lock()
        create = _counter_next(6000)

        def race(name):
            barrier.wait()
            value = store.execute_once("race-key", create)
            with lock:
                results[name] = value
                created_ids.append(value)

        t1 = threading.Thread(target=race, args=("t1",))
        t2 = threading.Thread(target=race, args=("t2",))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        # Both callers must observe the SAME id from a single creation.
        self.assertEqual(results["t1"], results["t2"])
        self.assertEqual(len(set(created_ids)), 1)


if __name__ == "__main__":
    unittest.main()
