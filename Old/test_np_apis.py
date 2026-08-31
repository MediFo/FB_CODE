"""
Test script: fetch raw JSON from NordPool APIs used by Net Position, Gen/Cons, and Price tabs.
Run:  python test_np_apis.py
"""
import requests, json, sys

TOKEN_URL   = "https://sts.nordpoolgroup.com/connect/token"
NP_USER     = "API_DATA_MEHDI"
NP_PASS     = "OsloNordpool@123"
CLIENT_AUTH = "Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ=="

PRICE_URL   = "https://data-api.nordpoolgroup.com/api/v2/Auction/Prices/ByAreas"
VOL_URL     = "https://data-api.nordpoolgroup.com/api/v2/Auction/Volumes/ByAreas"
PROD_URL    = "https://data-api.nordpoolgroup.com/api/v2/PowerSystem/Productions/ByLocations"
CONS_URL    = "https://data-api.nordpoolgroup.com/api/v2/PowerSystem/Consumptions/ByLocations"

DATE    = "2026-05-14"
ZONE    = "NO1"           # bidding zone for price/volumes
COUNTRY = "NO"            # country code for productions/consumptions
MARKET  = "DayAhead"


def get_token():
    r = requests.post(TOKEN_URL, headers={
        'Authorization': CLIENT_AUTH,
        'Content-Type': 'application/x-www-form-urlencoded',
    }, data={'grant_type': 'password', 'scope': 'marketdata_api',
             'username': NP_USER, 'password': NP_PASS}, timeout=10)
    r.raise_for_status()
    return r.json()['access_token']


def fetch(label, url, headers, params):
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"  {label}", file=sys.stderr)
    print(f"  URL:    {url}", file=sys.stderr)
    print(f"  Params: {params}", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    r = requests.get(url, headers=headers, params=params, timeout=15)
    print(f"  HTTP {r.status_code}", file=sys.stderr)
    if r.status_code == 200:
        return r.json()
    return {"error": r.status_code, "body": r.text}


token   = get_token()
headers = {'Authorization': f'Bearer {token}', 'accept': 'application/json'}

# ── 1. Prices (all Nordic zones) ─────────────────────────────────────────────
prices_raw = fetch(
    "PRICES  (Auction/Prices/ByAreas)",
    PRICE_URL, headers,
    {'date': DATE, 'areas': 'NO1,NO2,NO3,NO4,NO5,SE1,SE2,SE3,SE4,DK1,DK2,FI',
     'currency': 'EUR', 'market': MARKET}
)

# ── 2. Volumes / Net Position ─────────────────────────────────────────────────
volumes_raw = fetch(
    "VOLUMES / NET POSITION  (Auction/Volumes/ByAreas)",
    VOL_URL, headers,
    {'date': DATE, 'areas': ZONE, 'market': MARKET}
)

# ── 3. Productions ────────────────────────────────────────────────────────────
prod_raw = fetch(
    "PRODUCTIONS  (PowerSystem/Productions/ByLocations)",
    PROD_URL, headers,
    {'date': DATE, 'locations': COUNTRY}
)

# ── 4. Consumptions ───────────────────────────────────────────────────────────
cons_raw = fetch(
    "CONSUMPTIONS  (PowerSystem/Consumptions/ByLocations)",
    CONS_URL, headers,
    {'date': DATE, 'locations': COUNTRY}
)

# ── Output ────────────────────────────────────────────────────────────────────
print(json.dumps({
    "prices":      prices_raw,
    "volumes":     volumes_raw,
    "productions": prod_raw,
    "consumptions": cons_raw,
}, indent=2))
