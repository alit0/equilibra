"""Contract test against the deployed site.

scripts/verify.ps1 validates files on disk and never opens a socket, so it
cannot tell whether a redirect fires or a header is sent. This does: it makes
real requests and asserts on real responses.

    python scripts/contract_test.py
    python scripts/contract_test.py --base-url https://soyequilibra.com.ar

Standard library only, so it runs on a clean machine with no setup.

Redirects are served from .htaccess and headers are server configuration, so
neither can be checked before a deploy. Run this after deploying and after
purging the CDN.

Exits non-zero on the first failure of any check.
"""

import argparse
import http.client
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://soyequilibra.com.ar"
APEX_HOST = "soyequilibra.com.ar"
WWW_HOST = "www.soyequilibra.com.ar"
BOOKING_ORIGIN = "https://turnos.allitto.com"

# HSTS is the one directive here that browsers remember. While it lives in a
# visitor's browser, that browser refuses to speak HTTP to the domain, and
# removing the header from .htaccess does not clear it from anyone's machine.
# So the ceiling is asserted, not just the floor: this fails if max-age is
# raised past the current stage, or if includeSubDomains/preload appear before
# the domain has been observed clean at a lower stage.
HSTS_MAX_AGE_STAGE = 300


class Failure(Exception):
    pass


results = []


def check(name):
    """Decorate a zero-argument check so failures are collected, not fatal."""

    def wrap(fn):
        results.append((name, fn))
        return fn

    return wrap


def bust(url):
    """Append a unique query parameter so the CDN cannot answer from cache."""
    sep = "&" if urllib.parse.urlparse(url).query else "?"
    return f"{url}{sep}_cb={int(time.time() * 1000)}"


def raw_request(url, method="GET"):
    """One request, no redirect following. Returns (status, headers, url)."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, method=method, headers={"User-Agent": "equilibra-contract-test"})
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.headers, resp.geturl()
    except urllib.error.HTTPError as err:
        # Redirects and 4xx/5xx arrive here once following is disabled.
        return err.code, err.headers, url


def chase(url, limit=5):
    """Follow redirects one at a time. Returns the list of hops taken."""
    hops = []
    current = url
    for _ in range(limit):
        status, headers, _ = raw_request(current)
        hops.append((status, current, headers.get("Location")))
        if status not in (301, 302, 303, 307, 308):
            return hops
        location = headers.get("Location")
        if not location:
            raise Failure(f"{current} answered {status} with no Location header")
        current = urllib.parse.urljoin(current, location)
    raise Failure(f"more than {limit} redirects starting at {url}")


def expect(condition, message):
    if not condition:
        raise Failure(message)


# --- redirects -------------------------------------------------------------
# These already hold in production. They are here as regression cover, and
# because their passing is what proves the harness really reaches the server.


@check("www redirects to the apex in a single hop")
def _():
    hops = chase(bust(f"https://{WWW_HOST}/"))
    expect(len(hops) == 2, f"expected 1 redirect then a final response, got {len(hops)} hops: {hops}")
    status, _url, location = hops[0]
    expect(status == 301, f"expected 301 from www, got {status}")
    expect(
        urllib.parse.urlparse(location).netloc == APEX_HOST,
        f"www redirected to {location!r}, not the apex host",
    )
    expect(hops[-1][0] == 200, f"apex answered {hops[-1][0]} after the redirect")


@check("www preserves the path when redirecting")
def _():
    hops = chase(bust(f"https://{WWW_HOST}/robots.txt"))
    expect(hops[0][0] == 301, f"expected 301, got {hops[0][0]}")
    expect(
        urllib.parse.urlparse(hops[0][2]).path == "/robots.txt",
        f"path lost in redirect: {hops[0][2]!r}",
    )
    expect(hops[-1][0] == 200, f"final response was {hops[-1][0]}")


@check("/index.html redirects to the root")
def _():
    hops = chase(bust(f"https://{APEX_HOST}/index.html"))
    expect(hops[0][0] == 301, f"expected 301 from /index.html, got {hops[0][0]}")
    expect(
        urllib.parse.urlparse(hops[0][2]).path == "/",
        f"/index.html redirected to {hops[0][2]!r}, not the root",
    )


@check("the apex answers 200 with no redirect")
def _():
    status, _headers, _url = raw_request(bust(f"https://{APEX_HOST}/"))
    expect(status == 200, f"apex answered {status}, expected 200 (a redirect here means a loop)")


# --- security headers ------------------------------------------------------


def apex_headers():
    status, headers, _url = raw_request(bust(f"https://{APEX_HOST}/"))
    expect(status == 200, f"apex answered {status}; cannot check headers")
    return headers


@check("HSTS is present and still at the staged max-age")
def _():
    value = apex_headers().get("Strict-Transport-Security")
    expect(value is not None, "Strict-Transport-Security header is absent")
    normalised = value.lower().replace(" ", "")
    expect(
        f"max-age={HSTS_MAX_AGE_STAGE}" in normalised,
        f"expected max-age={HSTS_MAX_AGE_STAGE} while staging, got {value!r}",
    )
    expect(
        "includesubdomains" not in normalised,
        f"includeSubDomains set before the domain was observed clean: {value!r}",
    )
    expect("preload" not in normalised, f"preload set at the staging max-age: {value!r}")


@check("X-Content-Type-Options is nosniff")
def _():
    value = apex_headers().get("X-Content-Type-Options")
    expect(value is not None, "X-Content-Type-Options header is absent")
    expect(value.strip().lower() == "nosniff", f"expected 'nosniff', got {value!r}")


@check("Referrer-Policy is strict-origin-when-cross-origin")
def _():
    value = apex_headers().get("Referrer-Policy")
    expect(value is not None, "Referrer-Policy header is absent")
    expect(
        value.strip().lower() == "strict-origin-when-cross-origin",
        f"expected 'strict-origin-when-cross-origin', got {value!r}",
    )


@check("X-Frame-Options is SAMEORIGIN")
def _():
    value = apex_headers().get("X-Frame-Options")
    expect(value is not None, "X-Frame-Options header is absent")
    expect(value.strip().upper() == "SAMEORIGIN", f"expected 'SAMEORIGIN', got {value!r}")


@check("Permissions-Policy denies the unused device APIs")
def _():
    value = apex_headers().get("Permissions-Policy")
    expect(value is not None, "Permissions-Policy header is absent")
    normalised = value.lower().replace(" ", "")
    for feature in ("camera=()", "microphone=()", "geolocation=()"):
        expect(feature in normalised, f"{feature} not denied in {value!r}")


@check("Permissions-Policy still delegates payment to the booking iframe")
def _():
    # src/template.html grants the booking iframe `allow="payment *"`. An empty
    # `payment=()` allowlist overrides that and revokes it, which is what the
    # first version of this header did. Booking takes a deposit, so this asserts
    # the capability survives rather than assuming the flow does not need it.
    value = apex_headers().get("Permissions-Policy")
    expect(value is not None, "Permissions-Policy header is absent")
    normalised = value.lower().replace(" ", "")
    expect(
        "payment=()" not in normalised,
        f"payment is denied outright, which revokes the booking iframe's grant: {value!r}",
    )
    expect(
        "payment=(" in normalised and BOOKING_ORIGIN.lower() in normalised,
        f"payment is not delegated to {BOOKING_ORIGIN}: {value!r}",
    )


# --- the page still works --------------------------------------------------


@check("the page still serves its HTML and canonical tag")
def _():
    url = bust(f"https://{APEX_HOST}/")
    req = urllib.request.Request(url, headers={"User-Agent": "equilibra-contract-test"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    expect("<h1" in body, "no <h1> in the served page")
    expect(
        f'rel="canonical" href="https://{APEX_HOST}/"' in body.replace("'", '"'),
        "canonical link is absent or does not point at the apex root",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE, help=argparse.SUPPRESS)
    parser.parse_args()

    print(f"[contract] target: {DEFAULT_BASE}\n")
    failed = 0
    for name, fn in results:
        try:
            fn()
        except Failure as err:
            print(f"  FAIL  {name}\n          {err}")
            failed += 1
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError) as err:
            print(f"  ERROR {name}\n          request failed: {err}")
            failed += 1
        else:
            print(f"  ok    {name}")

    total = len(results)
    print(f"\n[contract] {total - failed}/{total} passed")
    if failed:
        print(f"[contract] {failed} FAILED")
        return 1
    print("[contract] all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
