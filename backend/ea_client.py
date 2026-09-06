"""Concrete ``EaGateway`` implemented with the standard library only.

Talks to Easy!Appointments over HTTPS with a Bearer token read from the
environment variable ``EQUILIBRA_EA_API_TOKEN``. Nothing else touches the
token, and no message, log or raised error includes it.

Kinds of errors are collapsed deliberately:

* any transport failure (DNS, connect, read, timeout) and any non-2xx HTTP
  answer become :class:`EaUnavailable`; the web layer maps it to 502 without
  exposing EA's internals to the patient. The same happens when EA answers a
  2xx that we cannot parse as JSON or that is not a list where a *list*
  contract is required (customers, appointments).

Deliberate fail-open/fail-closed split (hallazgo 10):

* ``GET availabilities`` answering ``[]`` is a *legitimate* answer meaning "no
  slot that day"; but a broken body that is not a list is treated as ``[]``
  (no slot) — never as a network error, matching the front-end's empty-day.
* ``GET customers`` / ``GET appointments`` are **quota checks that must fail
  closed**: if the body is not a list we treat it as an upstream error, NOT as
  an empty answer, otherwise a degraded EA would let a 4th appointment slip.

Every collection read is paginated until exhausted: EA v1 caps each response at
``length`` (Api::$default_length = 20) unless a larger ``length`` is sent, so a
single un-paginated call would silently only see the first 20 rows and break
the same-day / mailbox rules (hallazgo 7).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Root exposed by nginx; already proven not to swallow the Authorization
# header (EQUILIBRA-014 sonda de token, camino A).
EA_BASE_URL = os.environ.get("EQUILIBRA_EA_BASE_URL", "https://turnos.allitto.com/index.php/api/v1")

# Page size we ask EA for per round-trip. EA honours `length` as a plain SQL
# LIMIT, so this can be comfortably large; a page still caps what a single
# response may contain, so reads still loop until a short page is returned.
_PAGE_SIZE = 100

# Hard cap on how many pages a collection read may fetch. A sane EA terminates
# on a short page quickly, so this is an orders-of-magnitude safety net against
# an upstream that ignores `page` and answers a full page forever. Reaching the
# cap is NOT "no more rows": the read is a quota control and must fail closed
# (raise) rather than silently hand back a truncated, under-counted list.
_MAX_PAGES = 1000

# Timezone we hand to EA when *we* create a patient. It must be the exact
# string EA stores for the provider and the iframe customers (checked against
# `ea_users`), NOT the longer IANA alias: EA compares these strings textually
# in Notifications to decide whether to convert a mail, so a different spelling
# would keep the patient "in another zone" and mail would flip back to UTC.
CUSTOMER_TIMEZONE = "America/Buenos_Aires"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    urllib's default handler would rebuild the request *and keep the
    ``Authorization`` header* when following a 302 to another host (hallazgo
    9). By returning ``None`` here the redirect is turned into an
    ``HTTPError`` that none of the list/unavailability wrappers follow — the
    Bearer token never leaves the origin.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class EaUnavailable(Exception):
    """EA could not fulfil the request in a safe, patient-blaming way."""


class EaClient:
    """Standard-library HTTP adapter over the Easy!Appointments v1 REST API."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float = 12.0,
        max_pages: int = _MAX_PAGES,
    ) -> None:
        self._token = (token if token is not None else os.environ.get("EQUILIBRA_EA_API_TOKEN", "")).strip()
        self._base_url = (base_url or EA_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_pages = max_pages
        # One opener per client that never follows redirects.
        self._opener = urllib.request.build_opener(_NoRedirect)

    # ---- the single private transport --------------------------------------

    def _request(self, method: str, path: str, query: dict[str, str] | None = None, body: Any = None) -> Any:
        url = f"{self._base_url}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        data = None
        headers: dict[str, str] = {"Accept": "application/json"}
        if not self._token:
            raise EaUnavailable("missing EA API token")
        headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as err:
            # 4xx/5xx (incl. any redirect, which we never follow) are EA being
            # alive but refusing; keep the detail internal.
            raise EaUnavailable(f"EA answered HTTP {err.code} on {method} {path}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise EaUnavailable(f"EA unreachable on {method} {path}: {err.__class__.__name__}") from None

        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as err:
            # Invalid JSON from a *2xx* upstream is still an upstream problem
            # (hallazgo 13), never a generic 500 coming from the Flask layer.
            raise EaUnavailable(f"EA returned invalid JSON on {method} {path}") from None

    # ---- collection reads --------------------------------------------------

    def _iter_pages(self, path: str, base_query: dict[str, str]) -> list[Any]:
        """Fetch every page of a list resource until a short page is returned.

        EA v1 answers ``(page-1)*length``…``page*length`` rows; a page smaller
        than ``length`` (or empty) means we reached the end.
        """
        all_rows: list[Any] = []
        page = 1
        while True:
            query = dict(base_query)
            query["length"] = str(_PAGE_SIZE)
            query["page"] = str(page)
            result = self._request("GET", path, query)
            page_rows = result if isinstance(result, list) else None
            if page_rows is None:
                raise EaUnavailable(f"EA answered a non-list on GET {path} (degraded 2xx)")
            all_rows.extend(page_rows)
            if len(page_rows) < _PAGE_SIZE:
                return all_rows
            if page >= self._max_pages:
                # Still a full page at the cap: EA is not advancing. This is an
                # incomplete count, never "no more rows" — the quota read fails
                # closed so an under-count can't let a 4th turn slip through.
                raise EaUnavailable(
                    f"EA never returned a short page on GET {path} "
                    f"(failed closed after {self._max_pages} pages)"
                )
            page += 1

    # ---- domain-facing helpers --------------------------------------------

    @staticmethod
    def _customer_from_api(c: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(c, dict) or "id" not in c:
            raise EaUnavailable("EA returned a malformed customer row")
        return {
            "id": int(c["id"]),
            "first_name": c.get("firstName") or "",
            "last_name": c.get("lastName") or "",
            "email": (c.get("email") or "").strip(),
            "phone_number": c.get("phone") or "",
        }

    def _require_list(self, path: str, query: dict[str, str]) -> list[Any]:
        # Fail-closed: a collection endpoint must reply with a list; anything
        # else (empty body, wrapper, HTML from a proxy error page) is treated as
        # an upstream failure, NOT as "no rows" — see module docstring.
        return self._iter_pages(path, query)

    # ---- EaGateway implementation ------------------------------------------

    def get_available_hours(self, provider_id: int, service_id: int, date: str) -> list[str]:
        result = self._request(
            "GET",
            "availabilities",
            {"providerId": str(provider_id), "serviceId": str(service_id), "date": date},
        )
        if result is None:
            return []
        # EA answers `[]` when the day is out of the provider's plan, already
        # past or beyond the horizon. `[]` is a *legitimate* "nothing that day",
        # never an error. Broken shapes are read as empty (no slot) too.
        hours = result if isinstance(result, list) else []
        return [str(h) for h in hours]

    def search_customers(self, query: str) -> list[dict[str, Any]]:
        rows = self._require_list("customers", {"q": query})
        return [self._customer_from_api(c) for c in rows]

    def create_customer(self, customer: dict[str, Any]) -> dict[str, Any]:
        created = self._request(
            "POST",
            "customers",
            body={
                "firstName": customer["first_name"],
                "lastName": customer.get("last_name") or "",
                "email": customer["email"].strip(),
                "phone": customer.get("phone_number") or "",
                "notes": customer.get("notes") or "",
                # Without an explicit timezone EA falls back to the column
                # default (UTC), so the patient lands in a different zone than
                # the provider and every confirmation mail gets shifted +3h.
                "timezone": CUSTOMER_TIMEZONE,
            },
        )
        return self._customer_from_api(created)

    def list_customer_appointments(self, customer_id: int) -> list[dict[str, Any]]:
        rows = self._require_list("appointments", {"customerId": str(customer_id)})
        return list(rows)  # appointment rows keep EA's camelCase (start, id, ...)

    def appointments_for_slot(self, provider_id: int, date: str, hour: str) -> list[dict[str, Any]]:
        rows = self._require_list("appointments", {"providerId": str(provider_id), "date": date})
        wanted_start = f"{date} {hour}:00"
        return [
            r
            for r in rows
            if isinstance(r, dict)
            and (r.get("start") or "")[:16] in (f"{date} {hour}", wanted_start[:16])
        ]

    def create_appointment(self, payload: dict[str, Any]) -> dict[str, Any]:
        created = self._request("POST", "appointments", body=payload)
        if not isinstance(created, dict) or "id" not in created:
            raise EaUnavailable("EA returned a malformed appointment on POST")
        return dict(created)

    def delete_appointment(self, appointment_id: int) -> None:
        self._request("DELETE", f"appointments/{appointment_id}")
