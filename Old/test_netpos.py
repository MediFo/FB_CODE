"""
Standalone test for NordPool Auction Volumes API (Net Position).
Run this directly — no UI needed.
"""
import requests
from datetime import datetime

# ── Auth ──────────────────────────────────────────────────────────────
TOKEN_URL = "https://sts.nordpoolgroup.com/connect/token"
NP_USER   = "API_DATA_MEHDI"
NP_PASS   = "OsloNordpool@123"

def get_token():
    r = requests.post(TOKEN_URL, headers={
        'Authorization': 'Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ==',
        'Content-Type':  'application/x-www-form-urlencoded',
    }, data={
        'grant_type': 'password', 'scope': 'marketdata_api',
        'username': NP_USER, 'password': NP_PASS,
    }, timeout=10)
    r.raise_for_status()
    return r.json()['access_token']

# ── Test parameters ───────────────────────────────────────────────────
ZONE = "SE3"
DATE = "2025-05-05"   # change to a recent date you have JAO data for

VOL_URL   = "https://data-api.nordpoolgroup.com/api/v2/Auction/Volumes/ByAreas"
PRICE_URL = "https://data-api.nordpoolgroup.com/api/v2/Auction/Prices/ByAreas"

print(f"\n{'='*60}")
print(f"Testing NordPool Volumes API  zone={ZONE}  date={DATE}")
print('='*60)

token   = get_token()
headers = {'Authorization': f'Bearer {token}', 'accept': 'application/json'}

# ── Volumes ───────────────────────────────────────────────────────────
print("\n--- GET Volumes ---")
rv = requests.get(VOL_URL, headers=headers,
                  params={'market': 'DayAhead', 'areas': ZONE, 'date': DATE},
                  timeout=10)
print(f"Status: {rv.status_code}")

if rv.status_code == 200:
    data = rv.json()
    print(f"Response type: {type(data)}, length: {len(data) if isinstance(data, list) else 'N/A'}")

    # Print top-level keys of first item
    if isinstance(data, list) and data:
        item = data[0]
        print(f"\nTop-level keys: {list(item.keys())}")
        print(f"deliveryArea: {item.get('deliveryArea')}")
        print(f"status:       {item.get('status')}")

        # totalVolume
        tv = item.get('totalVolume', {})
        print(f"totalVolume:  buy={tv.get('buy')}  sell={tv.get('sell')}")
        net_total = (tv.get('sell') or 0) - (tv.get('buy') or 0)
        print(f"  => Net Position (total MWh): {net_total:.1f}")

        # volumes list (per delivery period)
        vols = item.get('volumes', [])
        print(f"\nvolumes list length: {len(vols)}")
        if vols:
            print(f"First volume entry keys: {list(vols[0].keys())}")
            print("\nFirst 5 delivery periods:")
            for v in vols[:5]:
                sell = v.get('sell', 0) or 0
                buy  = v.get('buy',  0) or 0
                print(f"  {v.get('deliveryStart','')}  sell={sell:8.2f}  buy={buy:8.2f}  net={sell-buy:8.2f}")
        else:
            print("  (empty — check market/date/area)")
    elif isinstance(data, dict):
        print("Response is dict (unexpected):", list(data.keys()))
        print(data)
else:
    print("Error body:", rv.text[:500])

# ── Prices ────────────────────────────────────────────────────────────
print("\n--- GET Prices ---")
rp = requests.get(PRICE_URL, headers=headers,
                  params={'date': DATE, 'areas': ZONE, 'currency': 'EUR',
                          'market': 'DayAhead'}, timeout=10)
print(f"Status: {rp.status_code}")
if rp.status_code == 200:
    pdata = rp.json()
    area = next((x for x in pdata if x.get('deliveryArea') == ZONE), None)
    if area:
        prices = area.get('prices', [])
        print(f"Prices count: {len(prices)}")
        if prices:
            print(f"First price entry keys: {list(prices[0].keys())}")
            print("First 3 prices:")
            for p in prices[:3]:
                print(f"  {p.get('deliveryStart','')}  price={p.get('price')}")
    else:
        print(f"Zone {ZONE} not found in response. Available areas:")
        print([x.get('deliveryArea') for x in pdata])
else:
    print("Error body:", rp.text[:300])

print("\nDone.")
