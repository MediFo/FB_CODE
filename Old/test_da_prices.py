"""
Fetch DayAhead 15-min prices for all Nordic zones and print a table
matching the NordPool website format (Delivery period CET | zone prices).
Run:  python test_da_prices.py
"""
import requests, json
from datetime import datetime
from zoneinfo import ZoneInfo

TOKEN_URL   = "https://sts.nordpoolgroup.com/connect/token"
NP_USER     = "API_DATA_MEHDI"
NP_PASS     = "OsloNordpool@123"
CLIENT_AUTH = "Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ=="
PRICE_URL   = "https://data-api.nordpoolgroup.com/api/v2/Auction/Prices/ByAreas"

DATE  = "2026-05-14"
ZONES = ["NO1", "NO2", "NO3", "NO4", "NO5", "SE1", "SE2", "SE3", "SE4", "DK1", "DK2", "FI"]

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
cet_tz  = ZoneInfo("Europe/Oslo")

r = requests.get(PRICE_URL, headers=headers,
    params={'date': DATE, 'areas': ','.join(ZONES),
            'currency': 'EUR', 'market': 'DayAhead'}, timeout=20)
r.raise_for_status()
data = r.json()

# Build slot → {zone: price} map
slots = {}
for area in data:
    zone = area.get('deliveryArea', '')
    if zone not in ZONES:
        continue
    for p in area.get('prices', []):
        ts = p.get('deliveryStart', '')
        if not ts:
            continue
        cet_dt  = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(cet_tz)
        cet_end = cet_dt.strftime('%H:%M')
        # deliveryEnd
        ts_e = p.get('deliveryEnd', '')
        if ts_e:
            end_dt  = datetime.fromisoformat(ts_e.replace('Z', '+00:00')).astimezone(cet_tz)
            cet_end = end_dt.strftime('%H:%M')
        period = f"{cet_dt.strftime('%H:%M')} - {cet_end}"
        slots.setdefault(period, {})['sort_key'] = cet_dt
        slots[period][zone] = p.get('price')

# Print table
col_w = 12
header = f"{'Delivery period (CET)':<22}" + "".join(f"{z:>{col_w}}" for z in ZONES)
print(f"\nDate: {DATE}  |  Market: DayAhead  |  Currency: EUR")
print("-" * len(header))
print(header)
print("-" * len(header))

for period, vals in sorted(slots.items(), key=lambda x: x[1].get('sort_key', datetime.min)):
    row = f"{period:<22}"
    for z in ZONES:
        v = vals.get(z)
        row += f"{v:>{col_w}.2f}" if v is not None else f"{'N/A':>{col_w}}"
    print(row)
