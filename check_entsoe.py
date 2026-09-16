"""
check_entsoe.py
================
Standalone ENTSO-E connectivity diagnostic, isolated from the rest of this
app -- no tkinter, no matplotlib, no pandas regression stack, and it still
runs its core checks even if propagation.py or entsoe-py can't be imported
at all. Use this to tell "my network/firewall can't reach ENTSO-E" apart
from "the app has a bug" when a Tab 9 ENTSO-E fetch returns 0 events.

Usage:
    python check_entsoe.py                        # last 7 days, FI
    python check_entsoe.py --country SE --days 30
    python check_entsoe.py --start 2026-03-01 --end 2026-04-21 --country FI
    python check_entsoe.py --token YOUR_TOKEN      # override the default

Runs four independent, ordered checks, each printed as PASS/FAIL with a
plain-English diagnosis, so a single blocked layer is easy to spot instead
of getting lost in one big traceback:
  1. DNS + raw TCP connect to the ENTSO-E API host
     -> isolates firewall/network-level blocking from anything HTTP-related
  2. TLS handshake with that host
     -> isolates a TLS-inspecting corporate proxy from a clean network block
  3. A raw HTTPS request to the API with the token
     -> isolates auth/IP-block/proxy-library issues from entsoe-py itself
  4. An actual entsoe-py query -- the same call the app itself makes
Each check only runs if the previous one passed (no point running a higher
layer once a lower one has already failed).
"""
import argparse
import os
import socket
import ssl
import sys
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    requests = None

ENTSOE_HOST = "web-api.tp.entsoe.eu"
ENTSOE_URL = f"https://{ENTSOE_HOST}/api"

# ENTSO-E's bidding-zone EIC codes for the country codes this app supports.
_COUNTRY_EIC = {
    "FI": "10YFI-1--------U",
    "SE": "10YSE-1--------K",
    "NO": "10YNO-0--------C",
    "DK": "10Y1001A1001A65H",
}


def _default_token() -> str:
    """Reads propagation.py's ENTSOE_TOKEN so this always tests the SAME
    credential the app uses, not a stale copy -- but never lets a broken
    propagation.py import (missing pandas/numpy etc.) stop this script's
    own network checks from running."""
    try:
        from propagation import ENTSOE_TOKEN
        return ENTSOE_TOKEN
    except Exception:
        return ""


def _pass(msg): print(f"  [PASS] {msg}")
def _fail(msg): print(f"  [FAIL] {msg}")
def _info(msg): print(f"         {msg}")


def check_tcp_connect(host: str, port: int = 443, timeout: float = 8.0) -> bool:
    print(f"1) DNS + TCP connect to {host}:{port} ...")
    try:
        addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        _fail(f"DNS resolution failed: {e}")
        _info("Likely cause: no internet route, or DNS is blocked/filtered "
              "(corporate DNS, a VPN split-tunnel that excludes this host, "
              "etc.)")
        return False
    _pass(f"DNS resolved to {addrs[0][4][0]}")

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except (socket.timeout, TimeoutError):
        _fail(f"TCP connect to port {port} timed out after {timeout}s")
        _info(f"Likely cause: a firewall is silently dropping outbound "
              f"traffic to {host}:{port} (common on corporate networks). "
              "Ask IT to allowlist this host, or try from a different "
              "network (e.g. a phone hotspot) to confirm.")
        return False
    except OSError as e:
        _fail(f"TCP connect refused/failed: {e}")
        _info("Likely cause: a firewall or proxy is actively rejecting "
              "this connection rather than silently dropping it.")
        return False
    _pass(f"TCP connect to port {port} succeeded")
    return True


def check_tls(host: str, port: int = 443, timeout: float = 8.0) -> bool:
    print(f"2) TLS handshake with {host}:{port} ...")
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls_sock:
                cert = tls_sock.getpeercert()
        issuer = dict(x[0] for x in cert.get('issuer', [])).get('organizationName', '?')
        _pass(f"TLS handshake OK (certificate issuer: {issuer})")
        return True
    except ssl.SSLCertVerificationError as e:
        _fail(f"TLS certificate verification failed: {e}")
        _info("Likely cause: a corporate TLS-inspecting proxy is replacing "
              "the real certificate with its own, and its root CA isn't "
              "trusted by Python. Ask IT for the corporate root CA and "
              "point REQUESTS_CA_BUNDLE / SSL_CERT_FILE at it, or test off "
              "the corporate network to confirm this is the cause.")
        return False
    except Exception as e:
        _fail(f"TLS handshake failed: {e}")
        return False


def check_http_api(token: str, timeout: float = 15.0) -> bool:
    print("3) Raw HTTPS request to the ENTSO-E API itself ...")
    if requests is None:
        _fail("'requests' is not installed -- can't run this check")
        _info("pip install requests")
        return False
    now = datetime.now(timezone.utc)
    params = {
        "securityToken": token,
        "documentType": "A77",
        "biddingZone_Domain": _COUNTRY_EIC["FI"],
        "periodStart": (now - timedelta(days=1)).strftime("%Y%m%d0000"),
        "periodEnd": now.strftime("%Y%m%d0000"),
    }
    try:
        resp = requests.get(ENTSOE_URL, params=params, timeout=timeout)
    except requests.exceptions.ProxyError as e:
        _fail(f"Proxy error: {e}")
        _info("A configured HTTP(S)_PROXY environment variable is "
              "rejecting or failing to reach this request. Check your "
              "proxy settings, or temporarily unset HTTP_PROXY/HTTPS_PROXY "
              "to test a direct connection.")
        return False
    except requests.exceptions.SSLError as e:
        _fail(f"SSL error: {e}")
        return False
    except requests.exceptions.ConnectionError as e:
        _fail(f"Connection error: {e}")
        _info("Same symptom as check #1 above but seen through 'requests' "
              "-- if check #1 passed but this fails, something in "
              "requests' own proxy/SSL configuration differs from a raw "
              "socket (check HTTP_PROXY/HTTPS_PROXY/NO_PROXY env vars).")
        return False
    except requests.exceptions.Timeout:
        _fail(f"Request timed out after {timeout}s")
        return False

    if resp.status_code == 200:
        _pass(f"HTTP 200 OK ({len(resp.content):,} bytes returned)")
        if b"Acknowledgement_MarketDocument" in resp.content:
            _info("Response is an ENTSO-E 'no matching data' acknowledgement "
                  "-- connectivity and the token are fine; this specific "
                  "1-day test window just has no A77 events for FI, which "
                  "is normal and not a sign of a problem.")
        return True
    if resp.status_code == 401:
        _fail("HTTP 401 Unauthorized -- the security token was rejected")
        _info("The token may be invalid, expired, or revoked. Get a fresh "
              "one from your ENTSO-E account (Settings -> Web API Security "
              "Token) and update ENTSOE_TOKEN in propagation.py.")
        return False
    if resp.status_code == 403:
        _fail("HTTP 403 Forbidden")
        _info("ENTSO-E is blocking this specific IP/network from using the "
              "API -- this is a documented, environment-dependent behaviour "
              "(see CLAUDE.md's Known Limitations). Try from a different "
              "network to confirm.")
        return False
    _fail(f"HTTP {resp.status_code}")
    _info(f"Response body (first 500 chars): {resp.text[:500]!r}")
    return False


def check_entsoe_py(token: str, country: str, start: datetime, end: datetime) -> bool:
    print(f"4) Full entsoe-py query (the same call the app makes) for "
          f"{country}, {start:%Y-%m-%d} -> {end:%Y-%m-%d} ...")
    try:
        from entsoe import EntsoePandasClient
    except ImportError:
        _fail("'entsoe-py' is not installed -- can't run this check")
        _info("pip install entsoe-py")
        return False
    import pandas as pd
    client = EntsoePandasClient(api_key=token)
    start_ts = pd.Timestamp(start).tz_localize("UTC") if pd.Timestamp(start).tzinfo is None else pd.Timestamp(start)
    end_ts = pd.Timestamp(end).tz_localize("UTC") if pd.Timestamp(end).tzinfo is None else pd.Timestamp(end)
    try:
        df = client.query_unavailability_of_production_units(
            country_code=country, start=start_ts, end=end_ts, docstatus=None)
    except Exception as e:
        _fail(f"query_unavailability_of_production_units raised: {e}")
        _info("If this says 'File is not a zip file', that's ENTSO-E's own "
              "(documented, expected) way of reporting zero A77 events in "
              "this window for this country -- not a connection problem, "
              "as long as checks 1-3 above all passed.")
        return False
    _pass(f"Returned {len(df)} row(s)")
    if len(df):
        print(df.head(5).to_string())
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--country", default="FI", choices=sorted(_COUNTRY_EIC),
                  help="Country code for check #4 (default: FI)")
    p.add_argument("--days", type=int, default=7,
                  help="Window size in days for check #4 if --start/--end "
                       "aren't given (default: 7)")
    p.add_argument("--start", help="Window start, YYYY-MM-DD (UTC)")
    p.add_argument("--end", help="Window end, YYYY-MM-DD (UTC)")
    p.add_argument("--token", default=None,
                  help="ENTSO-E security token to test (default: read "
                       "ENTSOE_TOKEN from propagation.py)")
    args = p.parse_args()

    token = args.token or _default_token()
    now = datetime.now(timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc) if args.end else now
    start = (datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
             if args.start else end - timedelta(days=args.days))

    print("=" * 70)
    print("ENTSO-E connectivity diagnostic")
    print("=" * 70)
    print(f"Python:  {sys.version.split()[0]} on {sys.platform}")
    print(f"Token:   {token[:8]}...{token[-4:]}" if token else "Token:   (none found -- pass --token)")
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
        if os.environ.get(var):
            print(f"{var:11s} {os.environ[var]}")
    print()

    if not check_tcp_connect(ENTSOE_HOST):
        _print_summary(False, "Blocked at the network layer (DNS/TCP) -- "
                              "this is a firewall/network issue, not an app bug.")
        sys.exit(1)
    print()

    if not check_tls(ENTSOE_HOST):
        _print_summary(False, "TCP connects but TLS fails -- likely a "
                              "TLS-inspecting proxy on this network.")
        sys.exit(1)
    print()

    if not token:
        print("3) Raw HTTPS request to the ENTSO-E API itself ...")
        _fail("No token available -- pass --token or fix propagation.py's import")
        sys.exit(1)
    if not check_http_api(token):
        _print_summary(False, "Network layer is fine, but the API request "
                              "itself failed (see the diagnosis above).")
        sys.exit(1)
    print()

    ok = check_entsoe_py(token, args.country, start, end)
    print()
    _print_summary(ok, "Everything works end-to-end from this machine -- "
                       "if the app still shows 0 events, the issue is in "
                       "the app/GUI layer, not connectivity." if ok else
                       "Network and raw HTTP are fine, but the entsoe-py "
                       "client itself failed (see the diagnosis above).")
    sys.exit(0 if ok else 1)


def _print_summary(ok: bool, note: str):
    print("=" * 70)
    print(("✓ ALL CHECKS PASSED" if ok else "✗ DIAGNOSTIC STOPPED EARLY") + f" — {note}")
    print("=" * 70)


if __name__ == "__main__":
    main()
