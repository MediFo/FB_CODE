"""
Fetch DayAhead Prices + Flows for one date and dump raw JSON.
Run:  python test_tab8_api.py
"""
import requests, json

TOKEN_URL   = "https://sts.nordpoolgroup.com/connect/token"
NP_USER     = "API_DATA_MEHDI"
NP_PASS     = "OsloNordpool@123"
CLIENT_AUTH = "Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ=="

PRICE_URL   = "https://data-api.nordpoolgroup.com/api/v2/Auction/Prices/ByAreas"
FLOW_URL    = "https://data-api.nordpoolgroup.com/api/v2/Auction/ScheduledPhysicalFlows/ByAreas"

DATE   = "2026-05-14"
ZONES  = "NO1,SE3"
MARKET = "DayAhead"

def get_token():
    r = requests.post(TOKEN_URL, headers={
        'Authorization': CLIENT_AUTH,
        'Content-Type': 'application/x-www-form-urlencoded',
    }, data={'grant_type': 'password', 'scope': 'marketdata_api',
             'username': NP_USER, 'password': NP_PASS}, timeout=10)
    r.raise_for_status()
    return r.json()['access_token']

token   = get_token()
headers = {'Authorization': f'Bearer {token}', 'accept': 'application/json'}

# ── Prices ────────────────────────────────────────────────────────────
rp = requests.get(PRICE_URL, headers=headers,
                  params={'date': DATE, 'areas': ZONES,
                          'currency': 'EUR', 'market': MARKET}, timeout=15)
prices_raw = rp.json() if rp.status_code == 200 else {"error": rp.status_code, "body": rp.text}

# ── Flows ─────────────────────────────────────────────────────────────
rf = requests.get(FLOW_URL, headers=headers,
                  params={'date': DATE, 'areas': ZONES,
                          'market': MARKET}, timeout=15)
flows_raw = rf.json() if rf.status_code == 200 else {"error": rf.status_code, "body": rf.text}

# ── Output ────────────────────────────────────────────────────────────
print(json.dumps({"prices": prices_raw, "flows": flows_raw}, indent=2))
