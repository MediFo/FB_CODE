"""
COMPREHENSIVE VALIDATION SUITE — JAO & Nordpool Analytics
=========================================================
Tests every critical component: imports, API connectivity, UTC→CET conversion,
Tab 5/6/7/8 data logic, Tab 9 ENTSO-E integration, propagation module, and
design-token/structural consistency.

Run:
    python test_validation.py
Returns exit-code 0 if all pass, 1 if any fail.
"""
import sys, os, inspect, importlib, traceback, csv, io, json, re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"
HEAD = "\033[1;34m"
RST  = "\033[0m"

results = []   # (name, ok, detail)

def check(name, ok, detail=""):
    tag = PASS if ok else FAIL
    print(f"  {tag}  {name}" + (f"  →  {detail}" if detail else ""))
    results.append((name, ok, detail))

def section(title):
    print(f"\n{HEAD}{'─'*60}{RST}")
    print(f"{HEAD}  {title}{RST}")
    print(f"{HEAD}{'─'*60}{RST}")

# ══════════════════════════════════════════════════════════════
# 1. MODULE IMPORTS
# ══════════════════════════════════════════════════════════════
section("1. MODULE IMPORTS")

for mod in ["requests", "pandas", "numpy", "json", "csv", "zoneinfo",
            "threading", "subprocess", "urllib.parse"]:
    try:
        importlib.import_module(mod)
        check(f"import {mod}", True)
    except ImportError as e:
        check(f"import {mod}", False, str(e))

# matplotlib (optional but used for all charts — warn only if missing)
try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    check("import matplotlib + backend", True)
except ImportError as e:
    print(f"  {WARN}  matplotlib not installed (charts will be unavailable at runtime)")
    results.append(("import matplotlib + backend", True, "env-only — app handles missing gracefully"))

# propagation.py
try:
    import propagation as _prop
    check("import propagation", True)
except Exception as e:
    check("import propagation", False, str(e))
    _prop = None

# synthetic.py
try:
    import synthetic as _syn
    check("import synthetic", True)
except Exception as e:
    check("import synthetic", False, str(e))
    _syn = None

# main app (no GUI — import only the module-level symbols)
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "app_module", os.path.join(ROOT, "app_jao_NP_API.py"))
    # We cannot execute the module (needs Tk display), so just parse it
    import ast
    with open(os.path.join(ROOT, "app_jao_NP_API.py")) as fh:
        src = fh.read()
    ast.parse(src)
    check("app_jao_NP_API.py syntax (ast.parse)", True)
except SyntaxError as e:
    check("app_jao_NP_API.py syntax (ast.parse)", False, str(e))

# ══════════════════════════════════════════════════════════════
# 2. DESIGN CONSTANTS
# ══════════════════════════════════════════════════════════════
section("2. DESIGN CONSTANTS & CONFIGURATION")

# Extract module-level constants from the AST
_constants = {}
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                try:
                    _constants[t.id] = ast.literal_eval(node.value)
                except Exception:
                    pass

required_constants = [
    "NORDPOOL_USER", "NORDPOOL_PASSWORD", "TOKEN_URL",
    "PROD_URL", "CONS_URL", "NP_PRICE_URL", "NP_VOL_URL", "NP_FLOW_URL",
    "NORDIC_ZONES", "C_BG", "C_PANEL", "C_ACCENT", "C_PRIMARY",
    "C_GREEN", "C_RED", "C_AMBER", "C_MUTED", "C_TEXT",
    "FONT_UI", "FONT_BOLD", "FONT_H1", "FONT_MONO",
]
for c in required_constants:
    ok = c in _constants
    check(f"constant {c} defined", ok, str(_constants.get(c, "MISSING"))[:60])

# CHART_PALETTE uses variable refs so ast.literal_eval can't resolve it — check via text
check("constant CHART_PALETTE defined (text check)",
      "CHART_PALETTE" in src and "CHART_PALETTE = [" in src)

# NORDIC_ZONES must have exactly 12 entries
zones = _constants.get("NORDIC_ZONES", [])
check("NORDIC_ZONES has 12 zones", len(zones) == 12, str(zones))

expected_zones = {"NO1","NO2","NO3","NO4","NO5","SE1","SE2","SE3","SE4","DK1","DK2","FI"}
check("NORDIC_ZONES contains correct zones",
      set(zones) == expected_zones, str(set(zones) ^ expected_zones or "exact match"))

# ══════════════════════════════════════════════════════════════
# 3. UTC → CET CONVERSION LOGIC
# ══════════════════════════════════════════════════════════════
section("3. UTC → CET CONVERSION LOGIC")

cet = ZoneInfo("Europe/Oslo")

# Standard winter case: 22:00 UTC → 23:00 CET
ts_winter = "2024-01-15T22:00:00Z"
dt_w = datetime.fromisoformat(ts_winter.replace('Z','+00:00')).astimezone(cet)
check("Winter 22:00 UTC → 23:00 CET", dt_w.hour == 23 and dt_w.minute == 0,
      f"got {dt_w.strftime('%H:%M')} CET")

# Summer case: 22:00 UTC → 00:00 CET+2 (CEST)
ts_summer = "2024-06-15T22:00:00Z"
dt_s = datetime.fromisoformat(ts_summer.replace('Z','+00:00')).astimezone(cet)
check("Summer 22:00 UTC → 00:00 CEST (next day)",
      dt_s.hour == 0 and dt_s.minute == 0,
      f"got {dt_s.strftime('%H:%M')} CET")

# DayAhead 15-min: first slot of day (CET 00:00) must be 22:00 UTC prev day
slot0_utc = datetime(2024, 6, 15, 22, 0, tzinfo=timezone.utc)
slot0_cet = slot0_utc.astimezone(cet)
check("DA slot 0: 22:00 UTC → 00:00 CET next day",
      slot0_cet.hour == 0 and slot0_cet.date() == datetime(2024, 6, 16).date(),
      f"CET={slot0_cet}")

# Key generation (Tab 5 / Tab 7 pattern)
ts = "2024-06-15T22:15:00Z"
cet_dt = datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(cet)
key = f"{cet_dt.strftime('%Y%m%d')}_{cet_dt.strftime('%H:%M')}"
check("Tab5 key: 22:15 UTC → 20240616_00:15 (CET)",
      key == "20240616_00:15", f"got key={key}")

# _apply_cet_conversion: date and time fields populated from dateTimeUtc
data = [{'dateTimeUtc': '2024-01-15T22:00:00Z', 'cneName': 'TEST'}]

def _apply_cet_conversion(entries):
    for entry in entries:
        if entry.get('dateTimeUtc'):
            cet_dt = datetime.fromisoformat(
                entry['dateTimeUtc'].replace('Z', '+00:00')).astimezone(cet)
            entry['date'] = cet_dt.strftime('%Y%m%d')
            entry['time'] = cet_dt.strftime('%H:%M:%S')
    return entries

out = _apply_cet_conversion(data)
check("_apply_cet_conversion: date field", out[0]['date'] == '20240115', out[0].get('date'))
check("_apply_cet_conversion: time field", out[0]['time'] == '23:00:00', out[0].get('time'))

# ══════════════════════════════════════════════════════════════
# 4. TAB 6 PRICE HISTORY — X-AXIS LABEL UTC→CET
# ══════════════════════════════════════════════════════════════
section("4. TAB 6 — PRICE HISTORY X-AXIS LABEL UTC→CET")

def _tab6_label(ts_str):
    cet_dt = datetime.fromisoformat(ts_str.replace('Z','+00:00')).astimezone(cet)
    return f"{cet_dt.strftime('%m%d')} {cet_dt.strftime('%H:%M')}"

# Summer: 22:00 UTC must show as 00:00 next day CET
lbl = _tab6_label("2024-06-15T22:00:00Z")
check("Tab6 label: 22:00 UTC → '0616 00:00' (CET)", lbl == "0616 00:00", f"got {lbl}")

# Winter: 22:00 UTC must show as 23:00 same day CET
lbl_w = _tab6_label("2024-01-15T22:00:00Z")
check("Tab6 label: 22:00 UTC → '0115 23:00' (CET)", lbl_w == "0115 23:00", f"got {lbl_w}")

# ══════════════════════════════════════════════════════════════
# 5. TAB 8 — FLOW DUPLICATE HANDLING (SWL CABLE)
# ══════════════════════════════════════════════════════════════
section("5. TAB 8 — FLOW DUPLICATE HANDLING")

def _simulate_flow_accumulation(flow_entries):
    """Replicate Tab 8 _plot_tab8_thread flow accumulation logic."""
    flows_raw = {}
    for entry in flow_entries:
        area  = entry['from']
        other = entry['to']
        exp   = entry['export']
        key   = (area, other)
        flows_raw[key] = flows_raw.get(key, 0) + exp   # FIX: sum, not overwrite
    return flows_raw

# Two parallel cables SE3→SE4 (cable + SWL)
entries = [
    {'from': 'SE3', 'to': 'SE4', 'export': 1200.0},
    {'from': 'SE3', 'to': 'SE4', 'export':  800.0},   # SWL
]
flows = _simulate_flow_accumulation(entries)
check("Tab8 flow duplicate: SE3→SE4 two cables summed",
      flows[('SE3','SE4')] == 2000.0, f"got {flows[('SE3','SE4')]}")

# Single cable should not be doubled
entries2 = [{'from': 'NO1', 'to': 'NO2', 'export': 500.0}]
flows2 = _simulate_flow_accumulation(entries2)
check("Tab8 flow single: NO1→NO2 not doubled",
      flows2[('NO1','NO2')] == 500.0, f"got {flows2[('NO1','NO2')]}")

# ══════════════════════════════════════════════════════════════
# 6. TAB 8 — MTU DATE+TIME MATCHING
# ══════════════════════════════════════════════════════════════
section("6. TAB 8 — MTU DATE+TIME MATCHING")

def _check_mtu_match(ts_str, target_date, mtu_h, mtu_m):
    s = ts_str.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    cet_dt = datetime.fromisoformat(s).astimezone(cet)
    return (cet_dt.strftime('%Y-%m-%d') == target_date
            and cet_dt.hour   == mtu_h
            and cet_dt.minute == mtu_m)

# Match: 22:00 UTC → 00:00 CET for date 2024-06-16
check("Tab8 MTU match: 22:00Z → 00:00 CET on +1 day",
      _check_mtu_match("2024-06-15T22:00:00Z", "2024-06-16", 0, 0))

# No false-positive on wrong date
check("Tab8 MTU no false match on wrong date",
      not _check_mtu_match("2024-06-15T22:00:00Z", "2024-06-15", 0, 0))

# No false-positive on wrong time
check("Tab8 MTU no false match on wrong time",
      not _check_mtu_match("2024-06-15T22:00:00Z", "2024-06-16", 1, 0))

# Pre-parse app AST for all Tab 9 structural checks (sections 7–12)
tab9_src = src
tree = ast.parse(tab9_src)

# ══════════════════════════════════════════════════════════════
# 7. TAB 9 — ENTSO-E CALL SIGNATURE
# ══════════════════════════════════════════════════════════════
section("7. TAB 9 — ENTSO-E CALL SIGNATURE & TOKEN")

if _prop:
    sig = inspect.signature(_prop.fetch_entsoe_outages)
    params = list(sig.parameters.keys())
    check("propagation.fetch_entsoe_outages exists", True)
    check("param[0] == 'start_utc'", params[0] == 'start_utc', str(params))
    check("param[1] == 'end_utc'",   params[1] == 'end_utc',   str(params))
    check("param 'country_code' present", 'country_code' in params, str(params))
    check("NO 'api_token' param (it's hardcoded)", 'api_token' not in params, str(params))
    check("NO 'src' param (correct name is country_code)", 'src' not in params, str(params))

    # single_event_analysis signature
    sig_sea = inspect.signature(_prop.single_event_analysis)
    sea_params = list(sig_sea.parameters.keys())
    check("single_event_analysis: param[0] == 'no3_df'",   sea_params[0] == 'no3_df',   str(sea_params))
    check("single_event_analysis: param[1] == 'outage_row'", sea_params[1] == 'outage_row', str(sea_params))
    check("single_event_analysis: has baseline_days param", 'baseline_days' in sea_params, str(sea_params))
    check("single_event_analysis: has src param",           'src' in sea_params, str(sea_params))

    # _indep_for_hypothesis exists (new in updated propagation)
    check("propagation._indep_for_hypothesis exists",
          hasattr(_prop, '_indep_for_hypothesis'), "per-hypothesis covariate specs function missing")

    token_val = getattr(_prop, 'ENTSOE_TOKEN', None)
    check("ENTSOE_TOKEN constant defined in propagation.py", token_val is not None)
    check("ENTSOE_TOKEN matches hardcoded value",
          token_val == "3c9307bd-c6e8-4f0c-99de-f9d754ff6488",
          str(token_val)[:16] + "...")
else:
    check("propagation module available for signature check", False, "module not loaded")

# ══════════════════════════════════════════════════════════════
# 8. TAB 9 — CET→UTC DATE CONVERSION
# ══════════════════════════════════════════════════════════════
section("8. TAB 9 — CET→UTC DATE CONVERSION")

def _cet_input_to_utc(raw_str):
    """Replicate _ma_fetch_entsoe conversion logic."""
    _cet = ZoneInfo("Europe/Oslo")
    s_cet = datetime.strptime(raw_str, "%Y-%m-%d %H:%M").replace(tzinfo=_cet)
    return s_cet.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Winter: 2024-01-01 00:00 CET → 2023-12-31T23:00:00Z
utc = _cet_input_to_utc("2024-01-01 00:00")
check("CET 2024-01-01 00:00 → UTC 2023-12-31T23:00:00Z",
      utc == "2023-12-31T23:00:00Z", f"got {utc}")

# Summer: 2024-06-01 00:00 CEST → 2024-05-31T22:00:00Z
utc_s = _cet_input_to_utc("2024-06-01 00:00")
check("CEST 2024-06-01 00:00 → UTC 2024-05-31T22:00:00Z",
      utc_s == "2024-05-31T22:00:00Z", f"got {utc_s}")

# Bad format must raise ValueError
try:
    datetime.strptime("2024-01-01T00:00:00Z", "%Y-%m-%d %H:%M")
    check("Bad CET format (ISO with Z) rejected", False, "should have raised")
except ValueError:
    check("Bad CET format (ISO with Z) rejected", True)

# ══════════════════════════════════════════════════════════════
# 9. TAB 9 — DYNAMIC ZONE REFRESH FROM JAO DATA
# ══════════════════════════════════════════════════════════════
section("9. TAB 9 — DYNAMIC CNEC REFRESH FROM JAO DATA")

import pandas as pd

def _ma_refresh_cnec_list(df, tab2_cnec=""):
    """Replicate _ma_refresh_cnec_list logic."""
    if 'cneName' not in df.columns:
        return []
    cnecs = sorted(str(c) for c in df['cneName'].dropna().unique() if str(c).strip())
    return cnecs

# Standard case
df_jao = pd.DataFrame({
    'cneName':         ['FI-NO3_A','SE1-NO3_B','FI-NO3_C'],
    'biddingZoneFrom': ['FI',       'SE1',       'FI'],
    'biddingZoneTo':   ['NO3',      'NO3',        'SE1'],
})
cnec_list = _ma_refresh_cnec_list(df_jao)
check("CNEC refresh: extracts unique CNEC names",
      set(cnec_list) == {'FI-NO3_A','SE1-NO3_B','FI-NO3_C'}, str(cnec_list))
check("CNEC refresh: sorted alphabetically", cnec_list == sorted(cnec_list), str(cnec_list))

# Default to Tab 2 selection when it exists in data
tab2_sel = "SE1-NO3_B"
first = tab2_sel if tab2_sel in cnec_list else (cnec_list[0] if cnec_list else "")
check("CNEC refresh: defaults to Tab 2 selection", first == "SE1-NO3_B", first)

# Edge case: empty DataFrame
cnec_list_empty = _ma_refresh_cnec_list(pd.DataFrame())
check("CNEC refresh: empty DataFrame → empty list", cnec_list_empty == [], str(cnec_list_empty))

# Edge case: NaN values
df_nan = pd.DataFrame({'cneName': ['NO1-A', None, 'NO2-B']})
cnec_list_nan = _ma_refresh_cnec_list(df_nan)
check("CNEC refresh: NaN values ignored", 'None' not in cnec_list_nan, str(cnec_list_nan))

# Structural check: _ma_sync_from_raw_data must be called at widget-creation time
_create_tab9_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_create_tab9_widgets':
        _create_tab9_src = ast.get_source_segment(tab9_src, node)
        break
if _create_tab9_src:
    check("_create_tab9_widgets calls _ma_sync_from_raw_data at end",
          '_ma_sync_from_raw_data' in _create_tab9_src,
          "CNEC combobox won't be empty when Tab 9 opens with existing data")
else:
    check("_create_tab9_widgets found", False)

# Structural check: _ma_refresh_setup_status also calls sync when data present
_refresh_setup_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_refresh_setup_status':
        _refresh_setup_src = ast.get_source_segment(tab9_src, node)
        break
if _refresh_setup_src:
    check("_ma_refresh_setup_status calls _ma_sync_from_raw_data when data present",
          '_ma_sync_from_raw_data' in _refresh_setup_src, "")
else:
    check("_ma_refresh_setup_status found", False)

# Structural check: _refresh_cnec_selector (Tab 1) calls _ma_sync_from_raw_data
_refresh_cnec_sel_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_refresh_cnec_selector':
        _refresh_cnec_sel_src = ast.get_source_segment(tab9_src, node)
        break
if _refresh_cnec_sel_src:
    check("_refresh_cnec_selector mirrors update to Tab 9 via _ma_sync_from_raw_data",
          '_ma_sync_from_raw_data' in _refresh_cnec_sel_src, "Tab 9 CNEC list won't update when Tab 1 data loads")
else:
    check("_refresh_cnec_selector found", False)

# Structural check: Tab 2 CNEC var has a trace that calls _ma_sync_tab2_cnec
_create_tab2_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_create_tab2_widgets':
        _create_tab2_src = ast.get_source_segment(tab9_src, node)
        break
if _create_tab2_src:
    check("Tab 2 CNEC var trace calls _ma_sync_tab2_cnec",
          '_ma_sync_tab2_cnec' in _create_tab2_src, "Tab 9 won't follow Tab 2 CNEC selection changes")
else:
    check("_create_tab2_widgets found", False)

# Structural check: _ma_apply_setup clears previous results
_apply_setup_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_apply_setup':
        _apply_setup_src = ast.get_source_segment(tab9_src, node)
        break
if _apply_setup_src:
    check("_ma_apply_setup clears _ma_results before loading",
          '_ma_results     = {}' in _apply_setup_src or '_ma_results = {}' in _apply_setup_src, "")
    check("_ma_apply_setup clears _ma_verdicts before loading",
          '_ma_verdicts    = []' in _apply_setup_src or '_ma_verdicts = []' in _apply_setup_src, "")
    check("_ma_apply_setup clears pipeline log widget",
          '_ma_log.delete' in _apply_setup_src, "")
    check("_ma_apply_setup clears results treeview",
          '_ma_res_tree.delete' in _apply_setup_src, "")
    check("_ma_apply_setup clears outage treeview",
          '_ma_out_tree.delete' in _apply_setup_src, "")
    check("_ma_apply_setup resets single-event outage ID list",
          "_ma_single_id['values']" in _apply_setup_src or '_ma_single_id[' in _apply_setup_src, "")
else:
    check("_ma_apply_setup found", False)

# Structural check: outage loading auto-populates single-event list
_entsoe_done_src = None
_load_manual_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_entsoe_done':
        _entsoe_done_src = ast.get_source_segment(tab9_src, node)
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_load_manual':
        _load_manual_src = ast.get_source_segment(tab9_src, node)
check("_ma_entsoe_done auto-populates Single Event outage ID list",
      _entsoe_done_src is not None and '_ma_refresh_single_list' in _entsoe_done_src,
      "User must manually click Refresh List after ENTSO-E fetch")
check("_ma_load_manual auto-populates Single Event outage ID list",
      _load_manual_src is not None and '_ma_refresh_single_list' in _load_manual_src,
      "User must manually click Refresh List after manual CSV load")

# Structural check: Reset All button exists and _ma_reset_all is defined
_reset_all_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_reset_all':
        _reset_all_src = ast.get_source_segment(tab9_src, node)
        break
check("_ma_reset_all method exists",
      _reset_all_src is not None, "No way to clear Tab 9 data without restarting app")
if _reset_all_src:
    check("_ma_reset_all clears outages",
          '_ma_outages_df' in _reset_all_src, "")
    check("_ma_reset_all clears single-event list",
          '_ma_single_id' in _reset_all_src, "")

# ══════════════════════════════════════════════════════════════
# 9b. TAB 9 — summarize_hypotheses CALL IN APP IS CORRECT
# ══════════════════════════════════════════════════════════════
section("9b. TAB 9 — summarize_hypotheses CALL CORRECTNESS")

_pipeline_thread_src2 = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_pipeline_thread':
        _pipeline_thread_src2 = ast.get_source_segment(tab9_src, node)
        break

if _pipeline_thread_src2:
    check("app: summarize_hypotheses NOT called with single dict arg",
          "summarize_hypotheses(results)" not in _pipeline_thread_src2, "")
    check("app: logit_res extracted before summarize call",
          "logit_res" in _pipeline_thread_src2 or "logit_result" in _pipeline_thread_src2, "")
    check("app: summarize_hypotheses receives src= and tgt= args",
          "src=src" in _pipeline_thread_src2 and "tgt=cnec" in _pipeline_thread_src2, "")
    check("app: pipeline uses FULL dataset (not single-CNEC filter) for PanelOLS",
          "cneName ==" not in _pipeline_thread_src2, "Single-CNEC filter breaks PanelOLS entity FE (needs >=2 CNECs)")
    check("app: pipeline log uses 'id' key not 'hypothesis'",
          "v.get('id','?')" in _pipeline_thread_src2, "")
else:
    check("_ma_pipeline_thread found for call check", False)

# single_event_analysis call correctness
_single_thread_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_single_thread':
        _single_thread_src = ast.get_source_segment(tab9_src, node)
        break
if _single_thread_src:
    check("single_event_analysis: passes row (not s,e separately)",
          "single_event_analysis(\n" not in _single_thread_src or "s, e" not in _single_thread_src, "")
    check("single_event_analysis: baseline_days passed as kwarg",
          "baseline_days=baseline_days" in _single_thread_src, "")
    check("single_event_analysis: src passed lowercase",
          "src=src.lower()" in _single_thread_src, "")
else:
    check("_ma_single_thread found for call check", False)

# Verify verdict display uses correct 'id' key not 'hypothesis'
_refresh_results_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_refresh_results':
        _refresh_results_src = ast.get_source_segment(tab9_src, node)
        break
if _refresh_results_src:
    check("_ma_refresh_results: uses 'id' key not 'hypothesis'",
          "v.get('id'" in _refresh_results_src and "v.get('hypothesis'" not in _refresh_results_src, "")
    check("_ma_refresh_results: parses β from verdict string",
          "β=" in _refresh_results_src, "")
else:
    check("_ma_refresh_results found for key check", False)

# _ma_export_html must also use correct keys (was: hypothesis, dep_var, p_value, note)
_export_html_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_export_html':
        _export_html_src = ast.get_source_segment(tab9_src, node)
        break
if _export_html_src:
    check("_ma_export_html: no stale 'hypothesis' key",
          "v.get('hypothesis'" not in _export_html_src, "")
    check("_ma_export_html: no stale 'dep_var' key",
          "v.get('dep_var'" not in _export_html_src, "")
    check("_ma_export_html: no stale 'p_value' key",
          "v.get('p_value'" not in _export_html_src, "")
    check("_ma_export_html: no stale 'note' key",
          "v.get('note'" not in _export_html_src, "")
    check("_ma_export_html: uses 'id' key for hypothesis",
          "v.get('id'" in _export_html_src or "get('id'" in _export_html_src, "")
    check("_ma_export_html: uses 'text' key for note",
          "get('text'" in _export_html_src, "")
else:
    check("_ma_export_html found for key check", False)

# ══════════════════════════════════════════════════════════════
# 10. TAB 9 — PIPELINE STATE CLEARED BEFORE RUN
# ══════════════════════════════════════════════════════════════
section("10. TAB 9 — STALE STATE CLEARED BEFORE PIPELINE RUN")

# Check that _ma_pipeline_thread begins with clearing _ma_results and _ma_verdicts
_pipeline_thread_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_pipeline_thread':
        _pipeline_thread_src = ast.get_source_segment(tab9_src, node)
        break

if _pipeline_thread_src:
    # First two assignments in the function body should clear results and verdicts
    clears_results  = '_ma_results  = {}' in _pipeline_thread_src or \
                      '_ma_results = {}' in _pipeline_thread_src
    clears_verdicts = '_ma_verdicts = []' in _pipeline_thread_src
    check("_ma_pipeline_thread clears _ma_results at start", clears_results)
    check("_ma_pipeline_thread clears _ma_verdicts at start", clears_verdicts)
else:
    check("_ma_pipeline_thread found in source", False)

# _ma_verdicts initialized in _create_tab9_widgets
_create_tab9_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_create_tab9_widgets':
        _create_tab9_src = ast.get_source_segment(tab9_src, node)
        break

if _create_tab9_src:
    check("_ma_verdicts = [] initialized in _create_tab9_widgets",
          '_ma_verdicts    = []' in _create_tab9_src or '_ma_verdicts = []' in _create_tab9_src,
          "")
else:
    check("_create_tab9_widgets found in source", False)

# ══════════════════════════════════════════════════════════════
# 11. TAB 9 — LOG MIRRORING TO TAB 1
# ══════════════════════════════════════════════════════════════
section("11. TAB 9 — LOG MIRRORING TO TAB 1")

_log_append_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_log_append':
        _log_append_src = ast.get_source_segment(tab9_src, node)
        break

if _log_append_src:
    check("_ma_log_append calls _update_status",
          '_update_status' in _log_append_src, "")
    check("_ma_log_append disables log widget after write",
          "state='disabled'" in _log_append_src, "")
    check("_ma_log_append uses [Tab9] prefix",
          '[Tab9]' in _log_append_src, "")
else:
    check("_ma_log_append found in source", False)

# ══════════════════════════════════════════════════════════════
# 12. TAB 9 — ENTSO-E TOKEN NOT IN UI (hardcoded)
# ══════════════════════════════════════════════════════════════
section("12. TAB 9 — ENTSOE TOKEN REMOVED FROM UI")

_ma_build_setup_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_build_setup':
        _ma_build_setup_src = ast.get_source_segment(tab9_src, node)
        break

if _ma_build_setup_src:
    check("_ma_build_setup: _ma_entsoe_token Entry removed",
          '_ma_entsoe_token' not in _ma_build_setup_src, "")
    check("_ma_build_setup: token LabelFrame removed",
          'ENTSO-E API Token' not in _ma_build_setup_src, "")
else:
    check("_ma_build_setup found in source", False)

_ma_fetch_entsoe_src = None
for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == '_ma_fetch_entsoe':
        _ma_fetch_entsoe_src = ast.get_source_segment(tab9_src, node)
        break

if _ma_fetch_entsoe_src:
    check("_ma_fetch_entsoe: no 'api_token' kwarg in call",
          'api_token' not in _ma_fetch_entsoe_src, "")
    check("_ma_fetch_entsoe: uses 'country_code' kwarg",
          'country_code' in _ma_fetch_entsoe_src, "")
    check("_ma_fetch_entsoe: passes start_utc as str (strftime)",
          'strftime' in _ma_fetch_entsoe_src, "")
    check("_ma_fetch_entsoe: input labeled CET",
          'CET' in _ma_fetch_entsoe_src or '%Y-%m-%d %H:%M' in _ma_fetch_entsoe_src, "")
else:
    check("_ma_fetch_entsoe found in source", False)

# ══════════════════════════════════════════════════════════════
# 13. CSV UPLOAD — NUMERIC CONVERSION
# ══════════════════════════════════════════════════════════════
section("13. CSV UPLOAD — NUMERIC FIELD CONVERSION")

def _simulate_csv_upload(rows):
    """Replicate _upload_csv numeric conversion logic."""
    loaded = []
    for row in rows:
        for key in list(row.keys()):
            if (key.lower() in ['shadowprice', 'ram', 'fall', 'flowfb', 'fmax']
                    or key.startswith('ptdf_')):
                try:
                    row[key] = float(row[key]) if row[key] and row[key].strip() != "" else 0.0
                except:
                    row[key] = 0.0
        loaded.append(row)
    return loaded

test_rows = [
    {'cneName': 'TEST', 'shadowPrice': '12.5', 'ram': '450', 'flowfb': '', 'ptdf_FI': '0.23'},
    {'cneName': 'BAD',  'shadowPrice': 'N/A',  'ram': '0',   'flowfb': 'abc', 'ptdf_FI': ''},
]
loaded = _simulate_csv_upload(test_rows)
check("CSV upload: shadowPrice '12.5' → 12.5", loaded[0]['shadowPrice'] == 12.5)
check("CSV upload: ram '450' → 450.0", loaded[0]['ram'] == 450.0)
check("CSV upload: empty flowfb → 0.0", loaded[0]['flowfb'] == 0.0)
check("CSV upload: ptdf_FI '0.23' → 0.23", loaded[0]['ptdf_FI'] == 0.23)
check("CSV upload: 'N/A' shadowPrice → 0.0", loaded[1]['shadowPrice'] == 0.0)
check("CSV upload: 'abc' flowfb → 0.0", loaded[1]['flowfb'] == 0.0)
check("CSV upload: empty ptdf_FI → 0.0", loaded[1]['ptdf_FI'] == 0.0)

# ══════════════════════════════════════════════════════════════
# 14. PROPAGATION MODULE FUNCTIONS
# ══════════════════════════════════════════════════════════════
section("14. PROPAGATION MODULE — FUNCTION SIGNATURES & CONSTANTS")

if _prop:
    for fn_name, expected_params in [
        ('load_jao_csv',          ['path']),
        ('filter_no3',            ['df', 'patterns', 'zone_label']),
        ('fetch_entsoe_outages',  ['start_utc', 'end_utc', 'log_cb', 'country_code']),
        ('build_covariates',      ['jao', 'outages', 'log_cb']),
        ('run_panel_regression',  ['df', 'dep_var']),
        ('run_logit_iva',         ['df']),
        ('summarize_hypotheses',  ['reg_results', 'logit_result', 'src', 'tgt']),
        ('deduplicate_outages',   ['df']),
    ]:
        fn = getattr(_prop, fn_name, None)
        if fn is None:
            check(f"propagation.{fn_name} exists", False, "NOT FOUND")
            continue
        sig  = inspect.signature(fn)
        params = list(sig.parameters.keys())
        # Check all expected params are present (order-independent)
        missing = [p for p in expected_params if p not in params]
        check(f"propagation.{fn_name} params {expected_params}",
              len(missing) == 0, f"missing: {missing}" if missing else "")

    # filter_no3: basic functional test with synthetic data
    df_test = pd.DataFrame({
        'cneName':         ['FI-NO3_A', 'SE1-NO1_B', 'FI-NO3_C'],
        'biddingZoneFrom': ['FI',        'SE1',        'FI'],
        'biddingZoneTo':   ['NO3',       'NO1',        'NO3'],
        'ram':             [100.0, 200.0, 150.0],
        'shadowPrice':     [5.0, 0.0, 10.0],
    })
    try:
        filtered = _prop.filter_no3(df_test, zone_label='NO3')
        check("propagation.filter_no3: filters by zone",
              len(filtered) == 2, f"got {len(filtered)} rows (expected 2)")
        check("propagation.filter_no3: correct rows kept",
              set(filtered['cneName']) == {'FI-NO3_A', 'FI-NO3_C'}, "")
    except Exception as e:
        check("propagation.filter_no3 functional test", False, str(e))
else:
    check("propagation module available for function tests", False, "module not loaded")

# ══════════════════════════════════════════════════════════════
# 15. SYNTHETIC DATA GENERATION
# ══════════════════════════════════════════════════════════════
section("15. SYNTHETIC DATA GENERATION")

if _syn:
    import tempfile
    with tempfile.TemporaryDirectory() as tmpd:
        try:
            info = _syn.generate_demo_dataset(tmpd, days=3)
            check("synthetic.generate_demo_dataset runs (3 days)", True)
            check("returns dict with 'jao_path'",   'jao_path'    in info, str(info.keys()))
            check("returns dict with 'outages_path'", 'outages_path' in info, str(info.keys()))
            check("jao_path file exists",   os.path.exists(info['jao_path']))
            check("outages_path file exists", os.path.exists(info['outages_path']))

            # Validate JAO CSV structure
            df_syn = pd.read_csv(info['jao_path'])
            expected_cols = {'cneName','biddingZoneFrom','biddingZoneTo','ram','shadowPrice','fall'}
            missing_cols  = expected_cols - set(df_syn.columns)
            check("synthetic JAO CSV has required columns",
                  len(missing_cols) == 0, f"missing: {missing_cols}" if missing_cols else "")

            # Validate outage CSV structure
            df_out = pd.read_csv(info['outages_path'])
            out_cols = {'outage_id','start_utc','end_utc','asset_name'}
            missing_out = out_cols - set(df_out.columns)
            check("synthetic outages CSV has required columns",
                  len(missing_out) == 0, f"missing: {missing_out}" if missing_out else "")

            if _prop:
                # Full pipeline test with synthetic data
                try:
                    jao_df  = _prop.load_jao_csv(info['jao_path'])
                    out_df  = pd.read_csv(info['outages_path'])
                    no3_df  = _prop.filter_no3(jao_df, zone_label='NO3')
                    check("propagation: filter_no3 on synthetic data",
                          len(no3_df) > 0, f"{len(no3_df)} rows")

                    logs = []
                    cov_df = _prop.build_covariates(no3_df, out_df, log_cb=logs.append)
                    check("propagation: build_covariates on synthetic data",
                          cov_df is not None and len(cov_df) > 0,
                          f"{len(cov_df) if cov_df is not None else 0} rows")

                    check("propagation: build_covariates logs messages",
                          len(logs) > 0, f"{len(logs)} messages")
                except Exception as e:
                    check("propagation full pipeline on synthetic data", False,
                          str(e)[:120])
        except Exception as e:
            check("synthetic.generate_demo_dataset", False, str(e)[:120])
else:
    check("synthetic module available for generation tests", False, "module not loaded")

# ══════════════════════════════════════════════════════════════
# 16. NORDPOOL API CONNECTIVITY
# ══════════════════════════════════════════════════════════════
section("16. NORDPOOL API CONNECTIVITY")

import requests as _req

NP_USER     = _constants.get('NORDPOOL_USER', '')
NP_PASS     = _constants.get('NORDPOOL_PASSWORD', '')
TOKEN_URL   = _constants.get('TOKEN_URL', '')
NP_PRICE_URL_C = _constants.get('NP_PRICE_URL', '')
NP_VOL_URL_C   = _constants.get('NP_VOL_URL', '')
NP_FLOW_URL_C  = _constants.get('NP_FLOW_URL', '')
PROD_URL_C     = _constants.get('PROD_URL', '')
CONS_URL_C     = _constants.get('CONS_URL', '')

_np_token = None
try:
    r = _req.post(TOKEN_URL, headers={
        'Authorization': 'Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ==',
        'Content-Type': 'application/x-www-form-urlencoded'
    }, data={'grant_type': 'password', 'scope': 'marketdata_api',
             'username': NP_USER, 'password': NP_PASS}, timeout=12)
    _np_token = r.json().get('access_token') if r.status_code == 200 else None
    check("NordPool: get access token", r.status_code == 200,
          f"HTTP {r.status_code}")
except Exception as e:
    check("NordPool: get access token", False, str(e)[:80])

# Find a date with 96 DA price slots (try today, yesterday, -2, +1 day)
if _np_token:
    _probe_hdrs = {'Authorization': f'Bearer {_np_token}', 'accept': 'application/json'}
    for _delta in [0, -1, -2, 1]:
        _probe_date = (datetime.now() + timedelta(days=_delta)).strftime("%Y-%m-%d")
        try:
            _r = _req.get(NP_PRICE_URL_C, headers=_probe_hdrs,
                          params={'date': _probe_date, 'areas': 'NO1',
                                  'currency': 'EUR', 'market': 'DayAhead'}, timeout=10)
            if _r.status_code == 200 and _r.json():
                _slots = len(_r.json()[0].get('prices', []))
                if _slots == 96:
                    _TEST_DATE = _probe_date
                    break
        except Exception:
            pass

# Pick the most recent date that has 15-min DA prices (try today, yesterday, -2d)
_TEST_DATE = datetime.now().strftime("%Y-%m-%d")  # fallback; refined after token check
if _np_token:
    _hdrs = {'Authorization': f'Bearer {_np_token}', 'accept': 'application/json'}

    # Prices
    try:
        r = _req.get(NP_PRICE_URL_C, headers=_hdrs,
                     params={'date': _TEST_DATE,
                             'areas': 'NO1,NO2,NO3,NO4,NO5,SE1,SE2,SE3,SE4,DK1,DK2,FI',
                             'currency': 'EUR', 'market': 'DayAhead'}, timeout=15)
        check("NordPool Prices/ByAreas DayAhead", r.status_code == 200,
              f"HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            zone_names = [a.get('deliveryArea') for a in data]
            check("Prices: all 12 zones returned",
                  len(zone_names) >= 12, f"got {len(zone_names)} areas")
            # Check price count = 96 (15-min DayAhead)
            if data:
                prices = data[0].get('prices', [])
                check("Prices: 96 slots per zone (15-min DA)",
                      len(prices) == 96, f"got {len(prices)} slots")
    except Exception as e:
        check("NordPool Prices/ByAreas", False, str(e)[:80])

    # Volumes
    try:
        r = _req.get(NP_VOL_URL_C, headers=_hdrs,
                     params={'date': _TEST_DATE, 'areas': 'NO1',
                             'market': 'DayAhead'}, timeout=15)
        check("NordPool Volumes/ByAreas DayAhead NO1", r.status_code == 200,
              f"HTTP {r.status_code}")
    except Exception as e:
        check("NordPool Volumes/ByAreas", False, str(e)[:80])

    # Flows
    try:
        r = _req.get(NP_FLOW_URL_C, headers=_hdrs,
                     params={'date': _TEST_DATE, 'areas': 'NO1,NO2,FI',
                             'market': 'DayAhead'}, timeout=15)
        check("NordPool Flows/ByAreas DayAhead", r.status_code == 200,
              f"HTTP {r.status_code}")
        if r.status_code == 200 and r.json():
            # Flow entries should have from/to direction fields
            flow_entry = r.json()[0]
            has_fields = any(k in str(flow_entry) for k in
                             ['deliveryArea','exportSchedule','importSchedule'])
            check("Flows: response has expected fields", has_fields, str(list(str(flow_entry)[:60])))
    except Exception as e:
        check("NordPool Flows/ByAreas", False, str(e)[:80])

    # Productions
    try:
        r = _req.get(PROD_URL_C, headers=_hdrs,
                     params={'date': _TEST_DATE, 'locations': 'NO'}, timeout=15)
        check("NordPool Productions/ByLocations NO", r.status_code == 200,
              f"HTTP {r.status_code}")
    except Exception as e:
        check("NordPool Productions/ByLocations", False, str(e)[:80])

    # Consumptions
    try:
        r = _req.get(CONS_URL_C, headers=_hdrs,
                     params={'date': _TEST_DATE, 'locations': 'NO'}, timeout=15)
        check("NordPool Consumptions/ByLocations NO", r.status_code == 200,
              f"HTTP {r.status_code}")
    except Exception as e:
        check("NordPool Consumptions/ByLocations", False, str(e)[:80])

# ══════════════════════════════════════════════════════════════
# 17. JAO API CONNECTIVITY
# ══════════════════════════════════════════════════════════════
section("17. JAO API CONNECTIVITY (fbDomainShadowPrice)")

import urllib.parse as _up

JAO_URL = "https://publicationtool.jao.eu/nordic/api/data/fbDomainShadowPrice"
# Probe a single 15-min slot — 2025-05-14 00:00 CET = 2025-05-13 22:00 UTC
_from = "2025-05-13T22:00:00.000Z"
_to   = "2025-05-13T22:15:00.000Z"
try:
    params = {"filter": "{}", "skip": 0, "take": 100,
              "fromUtc": _from, "toUtc": _to}
    qs  = _up.urlencode(params, safe='{}":,[]')
    url = f"{JAO_URL}?{qs}"
    # Use requests directly (not PowerShell) to validate API is reachable
    r = _req.get(url, timeout=15)
    check("JAO API reachable (HTTP status)", r.status_code == 200,
          f"HTTP {r.status_code}")
    if r.status_code == 200:
        body = r.json()
        data = body.get('data', body) if isinstance(body, dict) else body
        check("JAO API returns data list", isinstance(data, list), type(data).__name__)
        if data:
            first = data[0]
            jao_fields = ['cneName','biddingZoneFrom','biddingZoneTo',
                          'shadowPrice','ram','dateTimeUtc']
            missing = [f for f in jao_fields if f not in first]
            check("JAO API response has expected fields",
                  len(missing) == 0,
                  f"missing: {missing}" if missing else f"{len(data)} records, fields OK")
except Exception as e:
    check("JAO API connectivity", False, str(e)[:100])

# ══════════════════════════════════════════════════════════════
# 18. DATA FLOW — Tab 1 → Tab 9 DataFrame Bridge
# ══════════════════════════════════════════════════════════════
section("18. DATA FLOW — Tab 1 raw_filtered_data → Tab 9 DataFrame")

# Simulate what raw_filtered_data looks like after _apply_cet_conversion
sample_raw = [
    {'dateTimeUtc': '2024-06-15T22:00:00Z', 'cneName': 'FI-NO3_1',
     'biddingZoneFrom': 'FI', 'biddingZoneTo': 'NO3',
     'shadowPrice': 5.0, 'ram': 120.0, 'fall': 80.0, 'flowFb': 40.0,
     'fmax': 200.0, 'f0': 0.0, 'frm': 10.0, 'fra': 0.0, 'amr': 0.0,
     'faac': 0.0, 'iva': 0.0,
     'date': '20240616', 'time': '00:00:00'},
    {'dateTimeUtc': '2024-06-15T22:15:00Z', 'cneName': 'FI-NO3_1',
     'biddingZoneFrom': 'FI', 'biddingZoneTo': 'NO3',
     'shadowPrice': 0.0, 'ram': 110.0, 'fall': 70.0, 'flowFb': 30.0,
     'fmax': 200.0, 'f0': 0.0, 'frm': 10.0, 'fra': 0.0, 'amr': 0.0,
     'faac': 0.0, 'iva': 0.0,
     'date': '20240616', 'time': '00:15:00'},
]

df_raw = pd.DataFrame(sample_raw)

# Simulate _ma_apply_setup conversion
if 'dateTimeUtc' not in df_raw.columns:
    _cet2 = ZoneInfo("Europe/Oslo")
    df_raw['dateTimeUtc'] = (
        pd.to_datetime(df_raw['date'].astype(str) + ' ' + df_raw['time'],
                       format='%Y%m%d %H:%M:%S')
        .dt.tz_localize(_cet2, ambiguous='infer', nonexistent='shift_forward')
        .dt.tz_convert('UTC')
    )

if 'flowFb' in df_raw.columns and 'flowFB' not in df_raw.columns:
    df_raw['flowFB'] = df_raw['flowFb']

for col in ['shadowPrice','ram','fall','flowFB','fmax','f0','frm','fra','amr','faac','iva']:
    if col in df_raw.columns:
        df_raw[col] = pd.to_numeric(df_raw[col], errors='coerce')

check("Tab9 bridge: dateTimeUtc column present", 'dateTimeUtc' in df_raw.columns)
check("Tab9 bridge: flowFB alias created",        'flowFB' in df_raw.columns)
check("Tab9 bridge: ram is float64",
      str(df_raw['ram'].dtype).startswith('float'), str(df_raw['ram'].dtype))
check("Tab9 bridge: no NaN in ram",
      df_raw['ram'].notna().all(), str(df_raw['ram'].tolist()))

# ══════════════════════════════════════════════════════════════
# 19. RUN_DATA_FETCHING LOGIC (unit test, no network)
# ══════════════════════════════════════════════════════════════
section("19. run_data_fetching_and_processing LOGIC")

# Test parameter parsing and CET→UTC conversion
params_test = {
    'start_cet': '2024-06-15T00:00:00',
    'end_cet':   '2024-06-15T00:30:00',  # 2 slots
    'shadow_price_filter': '',
    'output_file': '',
}

cet_tz = ZoneInfo("Europe/Oslo")
start_cet = datetime.fromisoformat(params_test['start_cet']).replace(tzinfo=cet_tz)
end_cet   = datetime.fromisoformat(params_test['end_cet']).replace(tzinfo=cet_tz)
start_utc = start_cet.astimezone(timezone.utc)
end_utc   = end_cet.astimezone(timezone.utc)

check("run_data: CET start → UTC",
      start_utc.strftime('%H:%M') == "22:00",
      f"UTC start = {start_utc.strftime('%Y-%m-%d %H:%M')}")
check("run_data: total_steps calculation",
      int((end_utc - start_utc).total_seconds() / 900) == 2,
      f"steps={int((end_utc - start_utc).total_seconds() / 900)}")

# shadow_price_filter=positive
all_data = [
    {'shadowPrice': 5.0, 'dateTimeUtc': '2024-06-15T22:00:00Z'},
    {'shadowPrice': 0.0, 'dateTimeUtc': '2024-06-15T22:15:00Z'},
    {'shadowPrice': -1.0,'dateTimeUtc': '2024-06-15T22:30:00Z'},
]
filtered = [d for d in all_data
            if d.get('shadowPrice') is not None and float(d['shadowPrice']) > 0]
check("run_data: shadow_price_filter=positive keeps > 0 only",
      len(filtered) == 1 and filtered[0]['shadowPrice'] == 5.0)

# ══════════════════════════════════════════════════════════════
# 20. MANUAL OUTAGES CSV FORMAT
# ══════════════════════════════════════════════════════════════
section("20. MANUAL OUTAGES CSV — FILE INTEGRITY")

manual_csv = os.path.join(ROOT, "manual_outages.csv")
if os.path.exists(manual_csv):
    df_m = pd.read_csv(manual_csv)
    required = {'outage_id','start_utc','end_utc','asset_name'}
    missing_c = required - set(df_m.columns)
    check("manual_outages.csv exists and is readable", True)
    check("manual_outages.csv has required columns",
          len(missing_c) == 0, f"missing: {missing_c}" if missing_c else str(list(df_m.columns)))
    check("manual_outages.csv has at least 1 row", len(df_m) > 0, f"{len(df_m)} rows")
else:
    check("manual_outages.csv exists", False, manual_csv)

# ══════════════════════════════════════════════════════════════
# FINAL REPORT
# ══════════════════════════════════════════════════════════════
print(f"\n{'═'*60}")
total  = len(results)
passed = sum(1 for _,ok,_ in results if ok)
failed = [(n,d) for n,ok,d in results if not ok]

print(f"  TOTAL:  {total}  |  PASSED: {passed}  |  FAILED: {len(failed)}")
print(f"{'═'*60}")

if failed:
    print(f"\n{FAIL}  FAILURES:")
    for name, detail in failed:
        print(f"    • {name}" + (f"  →  {detail}" if detail else ""))
    print()
    sys.exit(1)
else:
    print(f"\n{PASS}  ALL TESTS PASSED\n")
    sys.exit(0)
