import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

# --- CONFIG ---
NORDPOOL_USER = "API_DATA_MEHDI"
NORDPOOL_PASSWORD = "OsloNordpool@123"
TOKEN_URL = "https://sts.nordpoolgroup.com/connect/token"
NP_FLOW_URL = "https://data-api.nordpoolgroup.com/api/v2/Auction/ScheduledPhysicalFlows/ByAreas"
_CEST = ZoneInfo("Europe/Oslo") # Use CEST for May dates

def get_token():
    r = requests.post(TOKEN_URL, headers={'Authorization': 'Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ=='},
                      data={'grant_type': 'password', 'scope': 'marketdata_api', 'username': NORDPOOL_USER, 'password': NORDPOOL_PASSWORD})
    return r.json().get("access_token")

def validate_results(date_str, target_hour):
    """
    date_str: '2026-05-19'
    target_hour: 12 (for the 12:00-13:00 window)
    """
    token = get_token()
    headers = {'Authorization': f'Bearer {token}', 'accept': 'application/json'}
    params = [('date', date_str), ('market', 'DayAhead')]
    for z in ["FI", "SE1", "SE3", "NO4", "EE"]: # Areas in your Excel
        params.append(('areas', z))

    print(f"--- 1. API RAW DATA FETCH ---")
    response = requests.get(NP_FLOW_URL, headers=headers, params=params)
    raw_data = response.json()
    
    # Storage for the 4 quarters of the hour: {(Area, Other): [val1, val2, val3, val4]}
    hourly_accumulator = defaultdict(list)

    for area_data in raw_data:
        source_area = area_data.get('deliveryArea')
        # Standardize sub-area names (e.g., DK1_VL -> DK1) to match Excel
        source_area = source_area.split('_')[0] 
        
        flow_entries = area_data.get('scheduledFlows') or area_data.get('flows') or []
        
        for entry in flow_entries:
            ts = entry.get('deliveryStart')
            # Convert UTC to local Nordic time (CEST)
            dt_local = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone(_CEST)
            
            # Only process if it falls within our target hour (e.g., 12:00, 12:15, 12:30, 12:45)
            if dt_local.hour == target_hour:
                conns = entry.get('byConnections') or entry.get('connections') or []
                for c in conns:
                    target_area = (c.get('area') or c.get('toArea')).split('_')[0]
                    # We use netPosition from the API: Import is (+), Export is (-)
                    val = float(c.get('netPosition') or 0)
                    hourly_accumulator[(source_area, target_area)].append(val)

    print(f"--- 2. CALCULATION (Average of 15-min intervals for {target_hour}:00-{target_hour+1}:00) ---")
    print(f"{'Connection':<15} | {'Quarterly Values (MW)':<40} | {'Hourly Avg'}")
    print("-" * 75)

    # Focus on FI connections to match your Excel
    for (src, target), vals in sorted(hourly_accumulator.items()):
        if src == "FI" and len(vals) > 0:
            avg = sum(vals) / len(vals)
            vals_str = ", ".join([f"{v:.1f}" for v in vals])
            print(f"{src} - {target:<8} | [{vals_str:<38}] | {avg:>8.1f} MWh")

if __name__ == "__main__":
    # Validating the 12:00-13:00 window from your Excel screenshot
    validate_results("2026-05-19", 12)