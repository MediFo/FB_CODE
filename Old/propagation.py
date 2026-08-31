"""
fi_no3_propagation.py
=====================
Analytical pipeline for validating propagation of Finnish (FI) maintenance
outages to NO3 CNEC parameters in the Nordic Day-Ahead Flow-Based domain.

Functions exposed:
    load_jao_csv(path) -> DataFrame
    filter_no3(df, patterns, zone_label='NO3') -> DataFrame
    fetch_entsoe_outages(start, end, log_cb) -> DataFrame
    load_manual_outages(path) -> DataFrame
    deduplicate_outages(df) -> DataFrame
    build_covariates(jao, outages, log_cb) -> DataFrame
    run_panel_regression(df, dep_var, ...) -> dict
    run_logit_iva(df) -> dict
    decompose_delta_ram(df, cnec, t_start, t_end, baseline_h=168) -> DataFrame
    summarize_hypotheses(results) -> list[dict]
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import numpy as np
import pandas as pd

try:
    import requests
except ImportError:
    requests = None

try:
    import statsmodels.api as sm
    from statsmodels.stats.diagnostic import het_breuschpagan
    from statsmodels.stats.outliers_influence import variance_inflation_factor
    from statsmodels.stats.stattools import durbin_watson
except (ImportError, AttributeError, Exception):
    # AttributeError covers statsmodels<0.14 using removed np.MachAr on NumPy 2.x
    sm = None

try:
    from linearmodels.panel import PanelOLS
except (ImportError, AttributeError, Exception):
    PanelOLS = None

try:
    from entsoe import EntsoePandasClient
except (ImportError, AttributeError, Exception):
    EntsoePandasClient = None


LogCallback = Callable[[str], None]
def _noop(msg: str) -> None: ...

# ---------------------------------------------------------------------------
# 1. JAO CSV loader
# ---------------------------------------------------------------------------
EXPECTED_NUMERIC = [
    "shadowPrice", "ram", "fall", "flowFB", "fmax", "fref", "f0",
    "frm", "fra", "amr", "faac", "iva",
]
EXPECTED_PTDF_PREFIXES = ("ptdf_",)

def load_jao_csv(path: str) -> pd.DataFrame:
    """Load a JAO CSV exported from the user's existing tool.
    Robust to missing columns - they are filled with NaN."""
    df = pd.read_csv(path, low_memory=False)
    df.columns = [c.strip() for c in df.columns]

    # Find datetime column under any common name
    dt_col = None
    for c in ("dateTimeUtc", "datetime_utc", "dt_utc", "timestampUtc"):
        if c in df.columns:
            dt_col = c
            break
    if dt_col is None:
        # try to construct from date + time
        if "date" in df.columns and "time" in df.columns:
            df["dateTimeUtc"] = pd.to_datetime(
                df["date"].astype(str) + " " + df["time"].astype(str),
                errors="coerce", utc=False
            ).dt.tz_localize("Europe/Oslo", ambiguous="infer", nonexistent="shift_forward")
            df["dateTimeUtc"] = df["dateTimeUtc"].dt.tz_convert("UTC")
        else:
            raise ValueError("JAO CSV has no recognizable datetime column")
    else:
        df["dateTimeUtc"] = pd.to_datetime(df[dt_col], utc=True, errors="coerce")

    df = df.dropna(subset=["dateTimeUtc"]).copy()

    # JAO Nordic publication tool publishes flow-related fields under several
    # name variants. Map common aliases to our canonical names.
    JAO_ALIASES = {
        "f0":          ["f0", "F0"],
        "fref":        ["fref", "Fref", "fRef"],
        "fmax":        ["fmax", "Fmax", "fMax"],
        "frm":         ["frm", "FRM"],
        # fnrao = Flow from Non-costly RAs and Other adjustments = FRA term
        # This is REQUIRED for the correct RAM formula (missing = 300-400 MW error)
        "fnrao":       ["fnrao", "fra", "FRA", "fNrao"],
        "amr":         ["amr", "AMR"],
        # aac = Already Allocated Capacity (FAAC in CCM notation)
        "faac":        ["faac", "aac", "AAC", "alreadyAllocated"],
        "iva":         ["iva", "IVA"],
        "ram":         ["ram", "RAM"],
        "fall":        ["fall", "fAll"],
        "flowFB":      ["flowFB", "flowFb", "flow_FB"],
        "shadowPrice": ["shadowPrice", "shadow_price", "shadowprice"],
        "cneName":     ["cneName", "cne_name", "cnecName"],
    }
    for canonical, aliases in JAO_ALIASES.items():
        if canonical in df.columns:
            continue
        for alt in aliases:
            if alt in df.columns and alt != canonical:
                df[canonical] = df[alt]
                break

    for col in EXPECTED_NUMERIC:
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # fnrao (FRA) is critical for RAM formula — warn if missing
    if "fnrao" not in df.columns or df["fnrao"].isna().all():
        df["fnrao"] = 0.0

    # Ensure a few common PTDF columns exist
    for z in ("FI", "FI_FS", "FI_EL", "SE3_FS", "NO3", "NO4", "SE1", "SE2", "SE3"):
        col = f"ptdf_{z}"
        if col not in df.columns:
            df[col] = np.nan
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "cneName" not in df.columns:
        raise ValueError(
            "JAO CSV missing cneName column. Available columns: "
            + ", ".join(df.columns[:30].tolist())
        )

    # Correct Nordic RAM formula (JAO Nordic Publication Handbook v1.5):
    # RAM = Fmax - FRM - Fref + fnrao + AMR - AAC - IVA
    # Note: fref = f0 in JAO export (both represent reference flow at CGMA NP)
    if df["f0"].isna().all() and df["fref"].notna().any():
        df["f0"] = df["fref"]

    return df.sort_values(["cneName", "dateTimeUtc"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2. NO3 filter
# ---------------------------------------------------------------------------
_ZONE_PATTERNS: dict = {
    # ── Nordic bidding zones ──────────────────────────────────────────────────
    "NO3": (r"klæbu.*surna", r"klaebu.*surna",
            r"klæbu.*orkdal", r"klaebu.*orkdal",
            r"refsdal.*modalen", r"aurland",
            r"tunnsjødal", r"tunnsjodal",
            r"viklandet", r"namsos", r"verdal"),
    "NO1": (r"hasle", r"flesaker", r"tegneby", r"furuset", r"oslo.*fjord"),
    "NO2": (r"kvilldal", r"tonstad", r"kristiansand", r"feda"),
    "NO4": (r"ofoten", r"kvandal", r"balsfjord", r"lyfjord", r"nea"),
    "NO5": (r"haugaland", r"sauda", r"fimreite"),
    "SE1": (r"nässjö.*1", r"hagby", r"stornorrfors", r"luleå"),
    "SE2": (r"svartnäs", r"sundsvall", r"midskog"),
    "SE3": (r"hallsberg", r"borgvik", r"midnordic", r"nässjö.*3"),
    "SE4": (r"hurva", r"sege", r"trelleborg", r"barsebäck"),
    "DK1": (r"kassø", r"tjele", r"fraugde", r"revsing"),
    "DK2": (r"sjælland", r"zealand", r"amager", r"ishøj"),
    "FI":  (r"fenno.*skan", r"kymi", r"olkiluoto", r"vuosaari"),
    # ── Baltic bidding zones (part of Nordic FB coupling) ────────────────────
    "EE":  (r"estlink", r"harku", r"kiisa", r"balti"),
    "LV":  (r"kurzeme", r"riga", r"augstceltne"),
    "LT":  (r"kruonis", r"vilnius", r"ignalina"),
}
DEFAULT_NO3_PATTERNS: tuple = _ZONE_PATTERNS["NO3"]


def zone_patterns(target_zone: str) -> tuple:
    """Return CNEC name regex patterns for a given bidding zone code."""
    return _ZONE_PATTERNS.get(target_zone.upper(), ())


# ---------- ENTSO-E A78 cross-border query configuration --------------------
#
# IMPORTANT: two different code formats are used depending on the query type:
#
#   A77 production unavailability  →  country code   "FI", "NO", "SE" …
#   A78 transmission unavailability → bidding-zone codes  "FI", "SE_1", "NO_2", "DK_1" …
#
# Keys below are country codes (what the user selects, matches A77).
# Values are lists of (from, to) bidding-zone pairs (what entsoe-py needs for A78).
# Norway example: user selects "NO" → we query A78 for every NO_x ↔ neighbour pair.
#
# Bidding-zone codes used by entsoe-py (matches Area enum in the library):
#   FI        Finland
#   SE_1..4   Sweden North, North-Mid, South-Mid, South
#   NO_1..5   Norway East/West/Mid/North/Far-North
#   DK_1..2   Denmark West, East
#   EE/LV/LT  Estonia, Latvia, Lithuania
#   DE        Germany (single zone for cross-border purposes)
#   PL        Poland
#   NL        Netherlands
#   GB        Great Britain
#
_KNOWN_BORDERS: dict = {
    # ── Finland ──────────────────────────────────────────────────────────────
    # FI ↔ SE1 (AC), FI ↔ SE3 (Fenno-Skan HVDC), FI ↔ EE (Estlink HVDC),
    # FI ↔ NO4 (AC — Rana–Tana 300 kV)
    "FI":  [("FI","SE_1"),("SE_1","FI"),
            ("FI","SE_3"),("SE_3","FI"),
            ("FI","EE"),  ("EE","FI"),
            ("FI","NO_4"),("NO_4","FI")],

    # ── Norway ───────────────────────────────────────────────────────────────
    # NO2 ↔ DE (NordLink HVDC), NO2 ↔ NL (NorNed HVDC),
    # NO2 ↔ DK1 (Skagerrak HVDC), NO2 ↔ GB (North Sea Link HVDC),
    # NO1 ↔ SE3 (AC), NO3 ↔ SE1 (AC), NO4 ↔ SE1 (AC), NO4 ↔ SE2 (AC),
    # NO5 ↔ SE3 (AC)
    "NO":  [("NO_2","DE"),  ("DE","NO_2"),
            ("NO_2","NL"),  ("NL","NO_2"),
            ("NO_2","DK_1"),("DK_1","NO_2"),
            ("NO_2","GB"),  ("GB","NO_2"),
            ("NO_1","SE_3"),("SE_3","NO_1"),
            ("NO_3","SE_1"),("SE_1","NO_3"),
            ("NO_4","SE_1"),("SE_1","NO_4"),
            ("NO_4","SE_2"),("SE_2","NO_4"),
            ("NO_5","SE_3"),("SE_3","NO_5")],

    # ── Sweden ───────────────────────────────────────────────────────────────
    # SE1 ↔ FI (AC), SE3 ↔ FI (Fenno-Skan HVDC),
    # SE4 ↔ LT (SwePol/NordBalt HVDC), SE4 ↔ DE (Baltic Cable HVDC),
    # SE4 ↔ PL (SwePol HVDC), SE3 ↔ DK1 (AC), SE4 ↔ DK2 (AC+HVDC)
    "SE":  [("SE_1","FI"),  ("FI","SE_1"),
            ("SE_3","FI"),  ("FI","SE_3"),
            ("SE_4","LT"),  ("LT","SE_4"),
            ("SE_4","DE"),  ("DE","SE_4"),
            ("SE_4","PL"),  ("PL","SE_4"),
            ("SE_3","DK_1"),("DK_1","SE_3"),
            ("SE_4","DK_2"),("DK_2","SE_4")],

    # ── Denmark ──────────────────────────────────────────────────────────────
    # DK1 ↔ NO2 (Skagerrak HVDC), DK1 ↔ SE3 (AC),
    # DK1 ↔ DE (AC+HVDC Kontek), DK2 ↔ DE (AC),
    # DK2 ↔ SE4 (AC+HVDC), DK2 ↔ NL (Cobra Cable HVDC)
    "DK":  [("DK_1","NO_2"),("NO_2","DK_1"),
            ("DK_1","SE_3"),("SE_3","DK_1"),
            ("DK_1","DE"),  ("DE","DK_1"),
            ("DK_2","DE"),  ("DE","DK_2"),
            ("DK_2","SE_4"),("SE_4","DK_2"),
            ("DK_2","NL"),  ("NL","DK_2")],

    # ── Baltic states ─────────────────────────────────────────────────────────
    "EE":  [("EE","FI"),("FI","EE"),
            ("EE","LV"),("LV","EE")],
    "LV":  [("LV","EE"),("EE","LV"),
            ("LV","LT"),("LT","LV")],
    "LT":  [("LT","LV"),("LV","LT"),
            ("LT","SE_4"),("SE_4","LT"),
            ("LT","PL"),  ("PL","LT")],
}
_HVDC_PAIRS: set = {
    # Pairs where the A78 transmission type is HVDC (not AC)
    frozenset({"FI",    "EE"}),      # Estlink 1 & 2
    frozenset({"FI",    "SE_3"}),    # Fenno-Skan 1 & 2
    frozenset({"SE_4",  "LT"}),      # NordBalt / LitPol
    frozenset({"SE_4",  "DE"}),      # Baltic Cable
    frozenset({"SE_4",  "PL"}),      # SwePol
    frozenset({"NO_2",  "DE"}),      # NordLink
    frozenset({"NO_2",  "NL"}),      # NorNed
    frozenset({"NO_2",  "DK_1"}),    # Skagerrak
    frozenset({"NO_2",  "GB"}),      # North Sea Link
    frozenset({"DK_2",  "NL"}),      # Cobra Cable
}


def _country_borders(country_code: str) -> list:
    """Return ENTSO-E A78 (from, to) bidding-zone pairs for a source country code.

    The country_code is the two-letter code used for A77 queries (e.g. "FI", "NO").
    The returned pairs use bidding-zone codes required by entsoe-py for A78 queries
    (e.g. "NO_2", "SE_1") — these are NOT the same as the A77 country codes.
    """
    cc = country_code.upper()
    if cc in _KNOWN_BORDERS:
        return _KNOWN_BORDERS[cc]
    return []


def _outage_cols(src: str) -> tuple:
    """Return (BIN_COLS, MW_COLS) tuples using the given source-country prefix."""
    p = src.lower()
    bin_cols = (f"{p}_planned_outage_active", f"{p}_forced_outage_active",
                f"{p}_hvdc_outage_active",    f"{p}_ac_line_outage_active")
    mw_cols  = (f"{p}_gen_outage_mw_lost", f"{p}_hvdc_outage_mw_lost",
                f"{p}_ac_outage_mw_lost")
    return bin_cols, mw_cols


def _default_indep(src: str = "fi") -> list:
    """Default independent variable names for panel regression.

    IMPORTANT — COLLINEARITY NOTE:
    With a small outage portfolio (e.g. one HVDC + one AC + one generator event),
    this 7-covariate spec has rank 3 — four covariates are linearly dependent.
    PanelOLS with drop_absorbed=True silently removes redundant columns.
    For hypothesis testing, use per-hypothesis specs (see _indep_for_hypothesis)
    which include only the treatment variable + orthogonal controls.
    """
    p = src.lower()
    return [f"{p}_planned_outage_active", f"{p}_forced_outage_active",
            f"{p}_hvdc_outage_active",    f"{p}_ac_line_outage_active",
            f"{p}_gen_outage_mw_lost",    f"{p}_hvdc_outage_mw_lost",
            f"{p}_ac_outage_mw_lost"]


def _indep_for_hypothesis(hid: str, src: str = "fi") -> list:
    """Return the correct covariate spec for each hypothesis.

    Each list contains the PRIMARY treatment variable first, followed by
    CONTROL variables of distinct type. Redundant combinations (e.g.
    fi_hvdc_outage_active when only forced outages are HVDC) are omitted
    to prevent rank deficiency and silent covariate absorption.

    Physical rationale:
      H1 (fall_signed):  HVDC outage changes power flow via NP reallocation.
                         Control for AC and generator separately.
      H2 (|PTDF|):       Only AC topology changes move PTDFs.
                         HVDC/gen are controls (should have β≈0).
      H3 (RAM):          All outage types affect RAM via fall or fnrao.
      H4 (shadow price): Market-clearing effect; all types relevant.
      H6 (FRM placebo):  FRM is structural; treatment type does not matter.
    """
    p = src.lower()
    specs = {
        # H1: HVDC outage → fall_signed shift via NP reallocation.
        #     Use fi_hvdc_outage_active (binary) as primary.
        #     fi_hvdc_outage_mw_lost omitted: it is constant×binary = proportional.
        #     AC line and generator are controls of different physical type.
        "H1": [f"{p}_hvdc_outage_active",
               f"{p}_ac_line_outage_active",  f"{p}_gen_outage_mw_lost"],
        # H2: AC line outage → |PTDF| shift via admittance matrix change.
        #     Use fi_ac_line_outage_active (binary) as primary.
        #     fi_ac_outage_mw_lost omitted for same proportionality reason.
        "H2": [f"{p}_ac_line_outage_active",
               f"{p}_hvdc_outage_active",      f"{p}_gen_outage_mw_lost"],
        # H3/H4: all outage types affect RAM/shadow price.
        #     With non-overlapping events, {forced, planned, hvdc_mw, ac_mw, gen_mw}
        #     has rank 3: forced=hvdc_mw/const and planned=f(ac_mw,gen_mw).
        #     Use {forced, ac_mw, gen_mw} — independent spanning set — as basis.
        #     forced captures the HVDC window; ac_mw and gen_mw capture planned windows
        #     with dose information.
        "H3": [f"{p}_forced_outage_active",
               f"{p}_ac_outage_mw_lost",  f"{p}_gen_outage_mw_lost"],
        "H4": [f"{p}_forced_outage_active",
               f"{p}_ac_outage_mw_lost",  f"{p}_gen_outage_mw_lost"],
        # H6 placebo: FRM should not respond to any outage type.
        "H6": [f"{p}_forced_outage_active",   f"{p}_planned_outage_active"],
    }
    return specs.get(hid, _default_indep(src))


def filter_no3(df: pd.DataFrame, patterns: Sequence[str] = DEFAULT_NO3_PATTERNS,
               zone_label: str = "NO3") -> pd.DataFrame:
    pat = re.compile("|".join(patterns), flags=re.IGNORECASE) if patterns else None
    is_zone = pd.Series(False, index=df.index)
    if "biddingZoneFrom" in df.columns:
        is_zone |= (df["biddingZoneFrom"] == zone_label)
    if "biddingZoneTo" in df.columns:
        is_zone |= (df["biddingZoneTo"] == zone_label)
    has_substr = df["cneName"].astype(str).str.contains(pat, na=False) if pat else pd.Series(False, index=df.index)
    return df.loc[is_zone | has_substr].copy()


# ---------------------------------------------------------------------------
# 3. Outage data sources
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ENTSO-E API token (hard-coded)
# ---------------------------------------------------------------------------
ENTSOE_TOKEN = "3c9307bd-c6e8-4f0c-99de-f9d754ff6488"


def fetch_entsoe_outages(start_utc: str, end_utc: str,
                         log_cb: LogCallback = _noop,
                         country_code: str = "FI") -> pd.DataFrame:
    """Fetch ENTSO-E A77 (production) + A78 (transmission) outages.
    country_code: ENTSO-E two-letter country code, e.g. "FI", "NO", "SE", "DK".
    Uses the hard-coded ENTSOE_TOKEN constant."""
    if EntsoePandasClient is None:
        log_cb("ENTSO-E: 'entsoe-py' library NOT INSTALLED.")
        log_cb("           Run:  pip install entsoe-py")
        log_cb("           Then restart the dashboard.")
        return pd.DataFrame()
    log_cb(f"ENTSO-E: fetching with token {ENTSOE_TOKEN[:8]}...{ENTSOE_TOKEN[-4:]}")

    client = EntsoePandasClient(api_key=ENTSOE_TOKEN)
    # Use the same safe helper once it is defined below.
    # start_utc / end_utc come from user input (ISO strings) so they may be naive.
    _s_ts = pd.Timestamp(start_utc)
    _e_ts = pd.Timestamp(end_utc)
    start = _s_ts.tz_localize("UTC") if _s_ts.tzinfo is None else _s_ts.tz_convert("UTC")
    end   = _e_ts.tz_localize("UTC") if _e_ts.tzinfo is None else _e_ts.tz_convert("UTC")
    def _ts_utc(val) -> pd.Timestamp:
        """Convert any timestamp value to UTC-aware Timestamp.
        Handles: UTC-aware, any-tz-aware (converts), and naive (assumes UTC).
        Safe against tz_convert() raising TypeError on naive inputs."""
        ts = pd.Timestamp(val)
        if ts.tzinfo is None:
            return ts.tz_localize("UTC")   # naive → assume UTC (ENTSO-E API default)
        return ts.tz_convert("UTC")

    events = []

    # ----- A77: production unavailability -----
    try:
        log_cb(f"ENTSO-E: query_unavailability_of_production_units ({country_code})")
        df = client.query_unavailability_of_production_units(
            country_code=country_code, start=start, end=end, docstatus=None,
        )
        for i, row in df.reset_index().iterrows():
            try:
                nominal = float(row.get("nominal_power", 0) or 0)
                avail   = float(row.get("avail_qty", 0) or 0)
                cap_lost = max(0.0, nominal - avail)
                if cap_lost <= 0: continue
                btype = str(row.get("businesstype", "")).strip()
                p_or_f = "forced" if btype == "A54" else "planned"
                # Skip implausibly long records (>30 days = status updates)
                try:
                    dur_h = (pd.Timestamp(row["end"]) - pd.Timestamp(row["start"])
                             ).total_seconds() / 3600
                    if dur_h > 30 * 24:
                        log_cb(f"  A77 skipped: {row.get('production_resource_name','')} "
                               f"duration {dur_h/24:.0f} d (likely status update)")
                        continue
                except Exception:
                    pass
                events.append({
                    "outage_id": f"entsoe_a77:{row.get('mrid', i)}:{row.get('start')}",
                    "start_utc": _ts_utc(row["start"]).isoformat(),
                    "end_utc":   _ts_utc(row["end"]).isoformat(),
                    "asset_id":  row.get("production_resource_id"),
                    "asset_name": row.get("production_resource_name"),
                    "asset_type": "generator",
                    "voltage_kv": None,
                    "capacity_mw": cap_lost,
                    "planned_or_forced": p_or_f,
                    "bidding_zone": country_code,
                    "control_area": country_code,
                    "source": "entsoe_a77",
                    "raw_payload": json.dumps({k: str(v) for k, v in row.items()}, default=str)[:2000],
                })
            except Exception as e:
                log_cb(f"  A77 row error: {e}")
    except Exception as e:
        log_cb(f"ENTSO-E A77 failed: {e}")

    # ----- A78: transmission unavailability -----
    borders = _country_borders(country_code)
    for fr, to in borders:
        try:
            log_cb(f"ENTSO-E: A78 {fr} -> {to}")
            df = client.query_unavailability_transmission(
                country_code_from=fr, country_code_to=to,
                start=start, end=end, docstatus=None)
            if df is None or len(df) == 0:
                continue
            for i, row in df.reset_index().iterrows():
                try:
                    cap = float(row.get("avail_qty", 0) or 0)
                    is_hvdc = frozenset({fr, to}) in _HVDC_PAIRS
                    # Skip implausibly long records (>30 days = ENTSO-E status
                    # updates stored as new events, not real outages)
                    try:
                        dur_h = (pd.Timestamp(row["end"]) - pd.Timestamp(row["start"])
                                 ).total_seconds() / 3600
                        if dur_h > 30 * 24:
                            log_cb(f"  A78 skipped: {fr}->{to} duration "
                                   f"{dur_h/24:.0f} d (likely status update)")
                            continue
                    except Exception:
                        pass
                    events.append({
                        "outage_id": f"entsoe_a78:{fr}-{to}:{row.get('mrid', i)}:{row.get('start')}",
                        "start_utc": _ts_utc(row["start"]).isoformat(),
                        "end_utc":   _ts_utc(row["end"]).isoformat(),
                        "asset_id":  None,
                        "asset_name": f"{fr}->{to}",
                        "asset_type": "hvdc" if is_hvdc else "ac_line",
                        "voltage_kv": None,
                        "capacity_mw": cap,
                        "planned_or_forced": "forced",  # entsoe-py issue #137
                        "bidding_zone": country_code,
                        "control_area": country_code,
                        "source": "entsoe_a78",
                        "raw_payload": json.dumps({"from": fr, "to": to}),
                    })
                except Exception as e:
                    log_cb(f"  A78 row err: {e}")
        except Exception as e:
            err_str = str(e)
            if "400" in err_str or "No matching data found" in err_str:
                # 400 = border not published in ENTSO-E TP (normal for many FI borders)
                log_cb(f"  A78 {fr}->{to}: no data in ENTSO-E TP (skipped)")
            else:
                log_cb(f"ENTSO-E A78 {fr}->{to} failed: {e}")

    log_cb(f"ENTSO-E events recorded: {len(events)}")
    return pd.DataFrame(events)


def load_manual_outages(path: str, log_cb: LogCallback = _noop) -> pd.DataFrame:
    """Load a hand-curated outage CSV. Creates a template if missing."""
    p = Path(path)
    if not p.exists():
        log_cb(f"Manual outage CSV {p} missing; creating template")
        p.parent.mkdir(parents=True, exist_ok=True)
        template = pd.DataFrame([{
            "outage_id": "manual:fennoskan_2024-11-29",
            "start_utc": "2024-11-29T00:00:00Z",
            "end_utc":   "2024-12-01T23:00:00Z",
            "asset_id":  "",
            "asset_name": "Fenno-Skan PTC (planned, IVA-handled)",
            "asset_type": "hvdc",
            "voltage_kv": 400.0,
            "capacity_mw": 800.0,
            "planned_or_forced": "planned",
            "bidding_zone": "FI",
            "control_area": "FI",
            "source": "manual",
            "raw_payload": "{}",
        }])
        template.to_csv(p, index=False)
        return template
    df = pd.read_csv(p)
    if "raw_payload" not in df.columns:
        df["raw_payload"] = "{}"
    log_cb(f"Manual outage rows loaded: {len(df)}")
    return df


def deduplicate_outages(df: pd.DataFrame) -> pd.DataFrame:
    """Merge events from different sources by (asset, time-window) similarity.
    Priority: entsoe_a78 > entsoe_a77 > entsoe_a80 > fingrid > manual.

    Only removes a lower-priority event when it overlaps in time with a
    higher-priority event for the same asset. Non-overlapping outage periods
    for the same asset (e.g., two Fenno-Skan outages months apart) are kept.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    for c in ("start_utc", "end_utc"):
        df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
    df = df.dropna(subset=["start_utc", "end_utc"]).reset_index(drop=True)

    pri = {"entsoe_a78": 4, "entsoe_a77": 3, "entsoe_a80": 2, "fingrid": 1, "manual": 0}
    df["_pri"] = df["source"].map(pri).fillna(-1)
    df["_asset_key"] = (
        df["asset_id"].fillna("").astype(str) + "|" +
        df["asset_name"].fillna("").astype(str).str.lower().str.strip() + "|" +
        df["asset_type"].fillna("").astype(str)
    )
    # Sort: same-asset rows together, highest priority first
    df = df.sort_values(["_asset_key", "_pri", "start_utc"],
                        ascending=[True, False, True]).reset_index(drop=True)

    keep = np.ones(len(df), dtype=bool)
    for _, grp in df.groupby("_asset_key", sort=False):
        idxs = grp.index.tolist()
        if len(idxs) == 1:
            continue
        # Within this asset group, suppress lower-priority rows that overlap
        # with any already-kept higher-priority row.
        for i in range(len(idxs)):
            if not keep[idxs[i]]:
                continue
            si, ei = df.loc[idxs[i], "start_utc"], df.loc[idxs[i], "end_utc"]
            for j in range(i + 1, len(idxs)):
                if not keep[idxs[j]]:
                    continue
                sj, ej = df.loc[idxs[j], "start_utc"], df.loc[idxs[j], "end_utc"]
                # Two intervals overlap iff neither ends before the other starts
                if si < ej and sj < ei:
                    keep[idxs[j]] = False  # j has lower or equal priority → drop

    return df.loc[keep].drop(columns=["_asset_key", "_pri"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 4. Outage cache (sqlite)
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS outage_events (
    outage_id           TEXT PRIMARY KEY,
    start_utc           TEXT NOT NULL,
    end_utc             TEXT NOT NULL,
    asset_id            TEXT,
    asset_name          TEXT,
    asset_type          TEXT,
    voltage_kv          REAL,
    capacity_mw         REAL,
    planned_or_forced   TEXT,
    bidding_zone        TEXT,
    control_area        TEXT,
    source              TEXT,
    raw_payload         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outage_window ON outage_events(start_utc, end_utc);
CREATE INDEX IF NOT EXISTS idx_outage_type ON outage_events(asset_type);
"""

def open_cache(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA_SQL)
    return con


def upsert_events(con: sqlite3.Connection, rows: list[dict]) -> int:
    cols = ("outage_id","start_utc","end_utc","asset_id","asset_name",
            "asset_type","voltage_kv","capacity_mw","planned_or_forced",
            "bidding_zone","control_area","source","raw_payload")
    placeholders = ",".join("?" * len(cols))
    sql = f"INSERT OR REPLACE INTO outage_events ({','.join(cols)}) VALUES ({placeholders})"
    pay = []
    for r in rows:
        pay.append(tuple(
            (r.get(c).isoformat() if isinstance(r.get(c), pd.Timestamp) else r.get(c))
            for c in cols))
    con.executemany(sql, pay)
    con.commit()
    return len(pay)


def load_cached_outages(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM outage_events", con)


# ---------------------------------------------------------------------------
# 5. Covariate construction
# ---------------------------------------------------------------------------
OUTAGE_BIN_COLS, OUTAGE_MW_COLS = _outage_cols("fi")  # default; overridden per-call

def _interval_active(times: np.ndarray, starts: np.ndarray,
                     ends: np.ndarray) -> np.ndarray:
    """Vectorised: for each time t, return 1 if any (start <= t < end)."""
    if len(starts) == 0:
        return np.zeros(len(times), dtype=int)
    out = np.zeros(len(times), dtype=int)
    # Sort intervals by start
    order = np.argsort(starts)
    s = starts[order]; e = ends[order]
    for i, t in enumerate(times):
        idx = np.searchsorted(s, t, side="right")
        # Any interval with s<=t and e>t in s[:idx]
        if idx > 0 and np.any(e[:idx] > t):
            out[i] = 1
    return out


def _interval_mw(times: np.ndarray, starts: np.ndarray, ends: np.ndarray,
                 caps: np.ndarray) -> np.ndarray:
    if len(starts) == 0:
        return np.zeros(len(times), dtype=float)
    out = np.zeros(len(times), dtype=float)
    for i, t in enumerate(times):
        mask = (starts <= t) & (ends > t)
        out[i] = float(caps[mask].sum())
    return out


def build_covariates(jao: pd.DataFrame, outages: pd.DataFrame,
                     log_cb: LogCallback = _noop,
                     src: str = "fi") -> pd.DataFrame:
    """Build outage covariate columns.

    src: column-name prefix ('fi' → fi_hvdc_outage_active, etc.)

    Treatment windows use the physical [start_utc, end_utc) interval only.
    FRM within-CNEC variation is checked as a placebo validity diagnostic.
    """
    p = src.lower()
    bin_cols, mw_cols = _outage_cols(p)
    jao = jao.copy()
    if outages is None or outages.empty:
        log_cb("No outages provided; covariates set to zero")
        for c in bin_cols + mw_cols:
            jao[c] = 0.0
    else:
        # ensure datetime
        outg = outages.copy()
        for c in ("start_utc", "end_utc"):
            outg[c] = pd.to_datetime(outg[c], utc=True, errors="coerce")
        outg = outg.dropna(subset=["start_utc", "end_utc"])
        outg["capacity_mw"] = pd.to_numeric(outg["capacity_mw"],
                                            errors="coerce").fillna(0.0)

        # ── Filter to source-country outages only ────────────────────────────
        # bidding_zone in outage rows is the 2-letter country code (e.g. "FI",
        # "NO") or a bidding-zone code starting with those letters (e.g. "NO_2",
        # "NO3").  Keep only outages whose zone starts with src.upper() so that
        # FI events don't pollute NO covariates and vice versa.
        src_prefix = p.upper()  # "FI", "NO", "SE", …
        if "bidding_zone" in outg.columns:
            bz = outg["bidding_zone"].fillna("").str.upper()
            n_before = len(outg)
            outg = outg[bz.str.startswith(src_prefix)]
            n_after = len(outg)
            if n_after < n_before:
                log_cb(f"  Filtered outages to {src_prefix} source: "
                       f"{n_after}/{n_before} events kept "
                       f"({n_before - n_after} non-{src_prefix} events excluded from covariates)")
            if outg.empty:
                log_cb(f"  ⚠ No {src_prefix} outage events in dataset — "
                       f"all {src_prefix} covariate columns will be zero. "
                       f"Fetch outages with source country = {src_prefix} for meaningful results.")

        ts = jao["dateTimeUtc"].values.astype("datetime64[ns]")

        def subset_ndarrays(df: pd.DataFrame):
            if df.empty:
                return (np.array([], dtype="datetime64[ns]"),
                        np.array([], dtype="datetime64[ns]"),
                        np.array([], dtype=float))
            return (df["start_utc"].values.astype("datetime64[ns]"),
                    df["end_utc"].values.astype("datetime64[ns]"),
                    df["capacity_mw"].values.astype(float))

        log_cb(f"Building covariates for {len(jao)} JAO rows (src={p.upper()})...")
        s,e,c = subset_ndarrays(outg[outg["planned_or_forced"]=="planned"])
        jao[f"{p}_planned_outage_active"] = _interval_active(ts, s, e)
        s,e,c = subset_ndarrays(outg[outg["planned_or_forced"]=="forced"])
        jao[f"{p}_forced_outage_active"] = _interval_active(ts, s, e)
        s,e,c = subset_ndarrays(outg[outg["asset_type"]=="hvdc"])
        jao[f"{p}_hvdc_outage_active"]  = _interval_active(ts, s, e)
        jao[f"{p}_hvdc_outage_mw_lost"] = _interval_mw(ts, s, e, c)
        s,e,c = subset_ndarrays(outg[outg["asset_type"]=="ac_line"])
        jao[f"{p}_ac_line_outage_active"] = _interval_active(ts, s, e)
        jao[f"{p}_ac_outage_mw_lost"]     = _interval_mw(ts, s, e, c)
        s,e,c = subset_ndarrays(outg[outg["asset_type"]=="generator"])
        jao[f"{p}_gen_outage_mw_lost"] = _interval_mw(ts, s, e, c)

    # Lagged versions
    jao = jao.sort_values(["cneName", "dateTimeUtc"])
    for col in bin_cols + (f"{p}_gen_outage_mw_lost",):
        jao[f"{col}_lag1h"]  = jao.groupby("cneName")[col].shift(4).fillna(0.0)
        jao[f"{col}_lag24h"] = jao.groupby("cneName")[col].shift(96).fillna(0.0)

    # Time fixed-effect columns
    jao["hour"]  = jao["dateTimeUtc"].dt.hour
    jao["dow"]   = jao["dateTimeUtc"].dt.dayofweek
    jao["month"] = jao["dateTimeUtc"].dt.month
    jao["date"]  = jao["dateTimeUtc"].dt.date.astype(str)

    # ── CORRECT RAM FORMULA VERIFICATION ────────────────────────────────────
    # Verified on real JAO data: RAM = Fmax - FRM + fnrao - AAC - fall  (R²=1.000)
    # 'fall' (F_allReference) is the reference flow entering the formula.
    # 'fref'/'f0' in JAO = flow at CGMA NP ≈ fall + PTDF*NP_CGMA; NOT in RAM formula.
    ram_check = (jao["fmax"].fillna(0) - jao["frm"].fillna(0)
                 + jao["fnrao"].fillna(0) - jao["faac"].fillna(0)
                 - jao["fall"].fillna(0))
    if "ram" in jao.columns and jao["ram"].notna().any():
        diff = (jao["ram"] - ram_check).abs()
        pct_ok = (diff < 1.0).mean()
        if pct_ok < 0.95:
            log_cb(f"⚠  RAM formula check: only {pct_ok*100:.1f}% rows balance within 1 MW "
                   f"(expected ~100%). Columns may differ from JAO Nordic v1.5 schema.")
        else:
            log_cb(f"✓ RAM formula verified: {pct_ok*100:.1f}% of rows balance within 1 MW")

    # ── DEPENDENT VARIABLE CONSTRUCTION ─────────────────────────────────────
    # H1: use 'fall' (F_allReference) — the actual Fref in the RAM formula.
    #     Sign-normalise per CNEC so positive always = "loading in congested direction".
    #     Within-CNEC entity FE handles the sign, but sign-normalisation makes the
    #     pooled coefficient interpretable and avoids cross-CNEC cancellation.
    if "fall" in jao.columns and jao["fall"].notna().any():
        cnec_fall_sign = (jao.groupby("cneName")["fall"]
                          .median()
                          .apply(lambda x: 1.0 if x >= 0 else -1.0)
                          .rename("fall_sign"))
        jao = jao.join(cnec_fall_sign, on="cneName")
        jao["fall_signed"] = jao["fall"] * jao["fall_sign"]
        jao = jao.drop(columns=["fall_sign"])
    else:
        jao["fall_signed"] = np.nan

    # H2: |PTDF_{SRC}| — topology shift is sign-agnostic; AC outage changes the
    #     admittance matrix in magnitude, not necessarily direction.
    # Column is named ptdf_{SRC}_abs (e.g. ptdf_FI_abs, ptdf_NO_abs).
    # ptdf_FI_abs is kept as an alias so downstream code that hardcodes "ptdf_FI_abs"
    # still works when src != "fi".
    ptdf_src_col     = f"ptdf_{src.upper()}"
    ptdf_src_abs_col = f"ptdf_{src.upper()}_abs"   # e.g. "ptdf_NO_abs"
    if ptdf_src_col in jao.columns:
        jao[ptdf_src_abs_col] = jao[ptdf_src_col].abs()
    elif "ptdf_FI" in jao.columns:
        jao[ptdf_src_abs_col] = jao["ptdf_FI"].abs()
    else:
        jao[ptdf_src_abs_col] = np.nan
    if ptdf_src_abs_col != "ptdf_FI_abs":          # backward-compat alias
        jao["ptdf_FI_abs"] = jao[ptdf_src_abs_col]

    # Shadow price cleaning: JAO encodes non-binding as 1e-8 (not truly 0).
    # Treat anything < 1e-6 as non-binding (shadow price = 0).
    if "shadowPrice" in jao.columns:
        jao["shadowPrice_clean"] = jao["shadowPrice"].where(
            jao["shadowPrice"].fillna(0) > 1e-6, 0.0)
        n_zero = (jao["shadowPrice_clean"] == 0).sum()
        n_bind = (jao["shadowPrice_clean"] > 0).sum()
        pct_bind = n_bind / max(len(jao), 1) * 100
        log_cb(f"  Shadow price: {n_bind:,} binding ({pct_bind:.1f}%), "
               f"{n_zero:,} non-binding (SP<1e-6 treated as 0)")
        if pct_bind > 95:
            log_cb("  ⚠  >95% of rows are binding. The JAO CSV was likely fetched "
                   "with a SP>0 filter. Shadow price regression is conditioned on "
                   "binding CNECs only (selection bias for population inference). "
                   "Re-fetch without the filter for unbiased estimates.")

    # ── FRM within-CNEC variation check (H6 placebo validity) ───────────────
    # FRM should be constant within a calibration period (set annually/quarterly).
    # If within-CNEC FRM std > 5 MW, FRM varies with contingency scenario changes
    # and the H6 placebo can false-fire from coincidental timing with outages.
    if "frm" in jao.columns and jao["frm"].notna().any():
        frm_within_std = jao.groupby("cneName")["frm"].std().dropna()
        n_variable_cnecs = (frm_within_std > 5.0).sum()
        if n_variable_cnecs > 0:
            max_std = frm_within_std.max()
            log_cb(
                f"  ⚠  H6 PLACEBO CAVEAT: FRM varies within {n_variable_cnecs} of "
                f"{len(frm_within_std)} CNECs (max within-CNEC std = {max_std:.1f} MW). "
                f"FRM variation is due to contingency scenario changes, not annual recalibration. "
                f"A significant H6 result may reflect contingency timing, not outage propagation. "
                f"Treat H6 as informational rather than definitive in this window.")
        else:
            log_cb("  ✓ FRM constant within all CNECs — H6 placebo test is valid")

    return jao.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6. Regressions
# ---------------------------------------------------------------------------
DEFAULT_INDEP = _default_indep("fi")


def run_panel_regression(df: pd.DataFrame, dep_var: str,
                         indep: Sequence[str] | None = None,
                         add_time_fe: bool = True,
                         cluster: str = "time",   # "time" | "entity" | "two_way"
                         log_cb: LogCallback = _noop,
                         src: str = "fi") -> dict:
    """
    Panel OLS with entity fixed effects and configurable clustered SE.

    cluster="time"    — cluster by calendar date (correct for outage designs where
                        treatment hits all CNECs simultaneously; ~365 clusters
                        for a 1-year window). This is the methodologically correct
                        choice for the FI outage → NO3 CNEC analysis.
    cluster="entity"  — cluster by CNEC (traditional but underestimates SE here).
    cluster="two_way" — attempt (CNEC × date); falls back to time if it fails.
    """
    if PanelOLS is None or sm is None:
        log_cb("linearmodels/statsmodels missing; skip")
        return {}
    indep = list(indep) if indep else _default_indep(src)
    keep = ["dateTimeUtc","cneName",dep_var] + indep + ["hour","dow","month","date"]
    keep = [c for c in keep if c in df.columns]
    if dep_var not in keep:
        log_cb(f"Column {dep_var} not found in data")
        return {}
    sub = df[keep].dropna()
    if sub.empty or sub[dep_var].nunique() < 2:
        log_cb(f"Insufficient data for {dep_var}")
        return {}
    if sub["cneName"].nunique() < 2:
        log_cb(f"Need >=2 CNECs for entity FE on {dep_var}")
        return {}

    sub = sub.set_index(["cneName", "dateTimeUtc"])
    y = pd.to_numeric(sub[dep_var], errors="coerce").astype(float)
    X = sub[indep].apply(pd.to_numeric, errors="coerce").astype(float).copy()
    if add_time_fe:
        # Hour FE only if window covers >1 day; dow only if covers >1 week;
        # month only if >1 month. This avoids rank deficiency on short samples.
        date_span = (sub.index.get_level_values(1).max() -
                     sub.index.get_level_values(1).min()).total_seconds()
        if date_span > 86400:  # > 1 day
            d = pd.get_dummies(sub["hour"], prefix="h", drop_first=True).astype(float)
            X = pd.concat([X, d], axis=1)
        if date_span > 7*86400:
            d = pd.get_dummies(sub["dow"], prefix="d", drop_first=True).astype(float)
            X = pd.concat([X, d], axis=1)
        if date_span > 31*86400 and sub["month"].nunique() > 1:
            d = pd.get_dummies(sub["month"], prefix="m", drop_first=True).astype(float)
            X = pd.concat([X, d], axis=1)
    # Force everything to float (defensive: some pandas/numpy combos can
    # leave object-dtype columns after concat with dummies).
    X = X.apply(pd.to_numeric, errors="coerce").astype(float)
    # Drop rows where any conversion produced NaN; align y
    valid = X.notna().all(axis=1) & y.notna()
    X = X.loc[valid]; y = y.loc[valid]
    # Drop columns with zero variance (constant dummies that didn't activate)
    nz = X.std(axis=0, skipna=True) > 1e-12
    X = X.loc[:, nz]
    X = sm.add_constant(X, has_constant="add")

    # Build cluster arrays aligned to the model index
    # Time-clustering: group all CNECs on the same calendar date into one cluster.
    # This is the correct design for outage studies where treatment is assigned
    # at the event (time) level, not the CNEC level.
    cluster_series_entity = pd.Series(
        pd.Categorical(sub.index.get_level_values(0)).codes.astype(np.int64),
        index=sub.index)
    cluster_series_time = pd.Series(
        pd.Categorical(sub["date"]).codes.astype(np.int64),
        index=sub.index)
    cluster_series_entity = cluster_series_entity.loc[y.index]
    cluster_series_time   = cluster_series_time.loc[y.index]

    cluster_codes_twoway = np.column_stack([cluster_series_entity.values,
                                             cluster_series_time.values])
    cluster_codes_time   = cluster_series_time.values.reshape(-1, 1)

    res = None
    for attempt_label, kwargs in [
        ("time-clustered",
         {"cov_type": "clustered",
          "clusters": cluster_codes_time,
          "group_debias": True}
         if cluster in ("time", "two_way") else
         {"cov_type": "clustered",
          "clusters": cluster_codes_twoway,
          "group_debias": True}),
        ("entity-clustered", {"cov_type": "clustered", "cluster_entity": True}),
        ("robust",           {"cov_type": "robust"}),
        ("unadjusted",       {"cov_type": "unadjusted"}),
    ]:
        try:
            mod = PanelOLS(y, X, entity_effects=True, drop_absorbed=True,
                           check_rank=False)
            res = mod.fit(**kwargs)
            break
        except Exception as e:
            if attempt_label == "time-clustered":
                pass  # silent fallback
            else:
                log_cb(f"  {attempt_label} failed: {e}")

    if res is None:
        return {}

    # Diagnostics
    try:
        resid = res.resids.values
        bp_stat, bp_p, _, _ = het_breuschpagan(resid, X.values)
    except Exception:
        bp_stat, bp_p = float("nan"), float("nan")
    try:
        dw = float(durbin_watson(res.resids.values))
    except Exception:
        dw = float("nan")
    vifs = {}
    try:
        Xv = sub[indep].astype(float).dropna().values
        for i, c in enumerate(indep):
            if Xv.shape[0] > Xv.shape[1] + 5:
                vifs[c] = float(variance_inflation_factor(Xv, i))
    except Exception:
        pass

    coefs = pd.DataFrame({
        "param": res.params.index,
        "coef":  res.params.values,
        "std_err": res.std_errors.values,
        "t":     res.tstats.values,
        "p":     res.pvalues.values,
    })

    # res.summary can crash on singular F-stat covariance (small samples).
    # We don't actually need the full summary string for the dashboard; coefs
    # is the important thing. Build a minimal summary if the full one fails.
    try:
        summary_text = str(res.summary)
    except Exception as e:
        summary_text = (f"[Full summary unavailable: {e}]\n"
                        f"n_obs = {int(res.nobs)}, "
                        f"R^2 = {float(res.rsquared):.4f}, "
                        f"R^2 within = {float(getattr(res,'rsquared_within',float('nan'))):.4f}\n"
                        + coefs.to_string(index=False))

    return {
        "dep": dep_var,
        "n_obs": int(res.nobs),
        "n_entities": int(sub.index.get_level_values(0).nunique()),
        "rsquared": float(res.rsquared),
        "rsquared_within": float(getattr(res, "rsquared_within", float("nan"))),
        "summary_text": summary_text,
        "coefs": coefs,
        "bp_stat": float(bp_stat) if pd.notna(bp_stat) else None,
        "bp_p":    float(bp_p)    if pd.notna(bp_p)    else None,
        "durbin_watson": dw,
        "vif": vifs,
    }


def run_logit_iva(df: pd.DataFrame, log_cb: LogCallback = _noop,
                  src: str = "fi") -> dict:
    if sm is None:
        return {}
    sub = df.copy()
    if "iva" not in sub.columns:
        log_cb("No iva column; skip logit")
        return {}
    sub["iva_active"] = (sub["iva"].fillna(0).abs() > 1e-6).astype(int)
    n_pos = int(sub["iva_active"].sum())
    if n_pos < 5:
        log_cb(f"H5 logit skipped: only {n_pos} IVA-active rows in target zone window. "
               f"Need a longer JAO window or curated outages overlapping IVA events.")
        return {}
    p = src.lower()
    feat = [f"{p}_planned_outage_active", f"{p}_forced_outage_active",
            f"{p}_hvdc_outage_active",    f"{p}_ac_line_outage_active"]
    feat = [f for f in feat if f in sub.columns]
    if not feat:
        return {}
    keep = ["iva_active","cneName","hour","dow","month"] + feat
    sub = sub[keep].dropna()
    if sub.empty: return {}

    X = pd.concat([sub[feat].astype(float),
                   pd.get_dummies(sub["hour"], prefix="h", drop_first=True).astype(float),
                   pd.get_dummies(sub["dow"], prefix="d", drop_first=True).astype(float),
                   pd.get_dummies(sub["month"], prefix="m", drop_first=True).astype(float)],
                  axis=1)
    X = X.apply(pd.to_numeric, errors="coerce").astype(float)
    valid = X.notna().all(axis=1)
    X = X.loc[valid]
    y_logit = sub["iva_active"].loc[valid]
    nz = X.std(axis=0, skipna=True) > 1e-12
    X = X.loc[:, nz]
    X = sm.add_constant(X, has_constant="add")
    try:
        res = sm.Logit(y_logit, X).fit(method="lbfgs", maxiter=200, disp=False)
    except Exception as e:
        log_cb(f"Logit failed: {e}")
        return {}
    coefs = pd.DataFrame({"param": res.params.index, "coef": res.params.values,
                          "std_err": res.bse.values, "z": res.tvalues.values,
                          "p": res.pvalues.values})
    return {"summary_text": str(res.summary()), "pseudo_r2": float(res.prsquared),
            "coefs": coefs, "n_obs": int(res.nobs),
            "n_positive": int(sub["iva_active"].sum())}


# ---------------------------------------------------------------------------
# 7. ΔRAM decomposition
# ---------------------------------------------------------------------------
def decompose_delta_ram(df: pd.DataFrame, cnec: str,
                        outage_start: pd.Timestamp,
                        outage_end: pd.Timestamp,
                        baseline_h: int = 168) -> pd.DataFrame:
    """For one CNEC and outage window, decompose ΔRAM into per-parameter
    contributions relative to the previous baseline_h hours.

    Verified Nordic JAO RAM formula (R² = 1.000 on real data):
        RAM = Fmax - FRM + fnrao - AAC - fall

    where:
        fall   = F_allReference (reference flow; negative = anti-congestion)
        fnrao  = Non-costly RA and other adjustments (positive = adds capacity)
        AAC    = Already Allocated Capacity (aac column in JAO)

    Note: fref/f0 are NOT in this formula (they represent a different quantity).
    Note: AMR and IVA are zero for NO3 CNECs in the studied period (Apr-May 2026).
    """
    s = pd.Timestamp(outage_start, tz="UTC") if outage_start.tzinfo is None else outage_start
    e = pd.Timestamp(outage_end,   tz="UTC") if outage_end.tzinfo   is None else outage_end
    sub = df[df["cneName"] == cnec]
    pre    = sub[(sub["dateTimeUtc"] < s) &
                 (sub["dateTimeUtc"] >= s - pd.Timedelta(hours=baseline_h))]
    during = sub[(sub["dateTimeUtc"] >= s) & (sub["dateTimeUtc"] < e)]
    if pre.empty or during.empty:
        return pd.DataFrame()

    # Verified columns
    formula_cols = ["fmax", "frm", "fnrao", "faac", "fall", "ram"]
    cols = [c for c in formula_cols if c in sub.columns]
    means_pre = pre[cols].mean()
    means_dur = during[cols].mean()
    delta = means_dur - means_pre

    contrib = {}
    if "fmax"  in delta: contrib["+ Δfmax"]  = float( delta["fmax"])
    if "frm"   in delta: contrib["- Δfrm"]   = float(-delta["frm"])
    if "fnrao" in delta: contrib["+ Δfnrao"] = float( delta["fnrao"])   # RA channel
    if "faac"  in delta: contrib["- Δaac"]   = float(-delta["faac"])
    if "fall"  in delta: contrib["- Δfall"]  = float(-delta["fall"])    # reference flow channel

    sigma = float(sum(contrib.values()))
    obs   = float(delta.get("ram", float("nan")))
    residual = obs - sigma  # should be ~0 if formula is correctly specified

    contrib["= Σ contribs"] = sigma
    contrib["Δram observed"] = obs
    if not np.isnan(residual) and abs(residual) > 1.0:
        contrib["(residual — formula mismatch)"] = residual

    return pd.DataFrame(
        {"component": list(contrib.keys()), "MW": list(contrib.values())}
    )


# ---------------------------------------------------------------------------
# 8. Hypothesis verdicts
# ---------------------------------------------------------------------------
def _build_hypotheses(src: str = "fi", tgt: str = "NO3") -> list:
    """Build the hypothesis list for a given source country and target zone.

    All variable names and display text are derived from src/tgt so that the
    same analytical pipeline works for any bidding-zone pair.
    """
    p   = src.lower()
    SRC = src.upper()
    return [
        # H1: SRC HVDC outage → F_allReference shifts on TGT CNECs
        {"id": "H1", "dep": "fall_signed",
         "var": f"{p}_hvdc_outage_active",
         "expected_sign": None,
         "text": (f"fall (sign-normalised F_allReference) shifts during {SRC} HVDC outages "
                  f"on {tgt} CNECs (sign depends on HVDC normal flow direction)")},

        # H2: SRC AC line outage → |PTDF_SRC| shifts on TGT CNECs
        {"id": "H2", "dep": f"ptdf_{SRC}_abs",
         "var": f"{p}_ac_line_outage_active",
         "expected_sign": None,
         "text": (f"|PTDF_{SRC}| on {tgt} CNECs shifts during {SRC} AC line outages "
                  f"(only AC topology changes can move PTDFs — not gen/HVDC outages)")},

        # H3: SRC outage → RAM changes on TGT CNECs
        {"id": "H3", "dep": "ram",
         "var": f"{p}_forced_outage_active",
         "expected_sign": None,
         "text": (f"RAM on {tgt} CNECs changes significantly during forced {SRC} outages "
                  f"(direction depends on outage type: HVDC often increases RAM)")},

        # H4: SRC outage → shadow price changes on TGT CNECs (binding MTUs)
        {"id": "H4", "dep": "shadowPrice",
         "var": f"{p}_forced_outage_active",
         "expected_sign": None,
         "text": (f"Shadow price on binding {tgt} CNECs changes during {SRC} outages "
                  f"(sign depends on whether outage relieves or adds congestion)")},

        # H5: IVA more frequent under forced than planned SRC outages
        {"id": "H5", "dep": None, "var": None,
         "expected_sign": None,
         "text": (f"IVA more frequent on {tgt} CNECs under forced than planned {SRC} outages "
                  f"(forced outages absent from D-2 IGM → TSO must apply IVA correction)")},

        # H6 placebo: FRM must NOT move with individual SRC outage events
        {"id": "H6", "dep": "frm",
         "var": f"{p}_forced_outage_active",
         "expected_sign": 0,
         "text": (f"FRM is structural (annual calibration); no MTU-level {SRC}-outage "
                  f"propagation [PLACEBO — significant result = model misspecification]")},
    ]


# Backward-compatible constant — used by code that still references HYPOTHESES directly
HYPOTHESES = _build_hypotheses("fi", "NO3")


def _holm_bonferroni(p_values: list[float | None], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni step-down procedure for multiple testing correction.
    Returns a list of booleans: True = reject H0 at family-wise alpha.
    None p-values are treated as non-rejectable."""
    m = len(p_values)
    valid = [(i, p) for i, p in enumerate(p_values) if p is not None]
    valid.sort(key=lambda x: x[1])
    reject = [False] * m
    for rank, (i, p) in enumerate(valid):
        threshold = alpha / (m - rank)
        if p <= threshold:
            reject[i] = True
        else:
            break  # once we fail to reject, all remaining are kept
    return reject


def summarize_hypotheses(reg_results: dict, logit_result: dict,
                          src: str = "fi", tgt: str = "NO3") -> list[dict]:
    """Summarise verdicts for the given source country and target zone.
    When the primary test variable is absorbed by fixed effects, falls back to
    alternative channel variables and reports why the original test wasn't possible."""
    p = src.lower()
    fallback_vars = {
        "H1": [f"{p}_hvdc_outage_active", f"{p}_forced_outage_active",
               f"{p}_hvdc_outage_mw_lost", f"{p}_planned_outage_active"],
        "H2": [f"{p}_ac_line_outage_active", f"{p}_ac_outage_mw_lost",
               f"{p}_planned_outage_active"],
        "H3": [f"{p}_forced_outage_active", f"{p}_planned_outage_active",
               f"{p}_hvdc_outage_active"],
        "H4": [f"{p}_forced_outage_active", f"{p}_planned_outage_active",
               f"{p}_hvdc_outage_active"],
    }
    out = []
    for h in _build_hypotheses(src, tgt):
        verdict = "n/a"
        if h["id"] == "H5":
            if logit_result and "coefs" in logit_result:
                cf = logit_result["coefs"].set_index("param")
                try:
                    bp = cf.loc[f"{p}_planned_outage_active"]
                    bf = cf.loc[f"{p}_forced_outage_active"]
                    verdict = (f"forced β={bf['coef']:.3g} (p={bf['p']:.3g}) | "
                               f"planned β={bp['coef']:.3g} (p={bp['p']:.3g}) | "
                               f"{'SUPPORTED' if bf['coef']>bp['coef'] and bf['p']<0.10 else 'NOT supported'}")
                except KeyError:
                    verdict = "vars absent in logit (likely no IVA-active rows)"
            else:
                verdict = "logit not run (need IVA-active rows in NO3 window)"
        elif h["id"] == "H6":
            r = reg_results.get(h["dep"])
            if r and "coefs" in r:
                cf = r["coefs"].set_index("param")
                if h["var"] in cf.index:
                    beta = cf.loc[h["var"], "coef"]; p = cf.loc[h["var"], "p"]
                    verdict = (f"β={beta:.3g}, p={p:.3g} → "
                               f"{'CONSISTENT (no MTU effect)' if p>=0.05 else 'inconsistent (MTU effect detected)'}")
                else:
                    verdict = f"{h['var']} absorbed by FE; placebo inconclusive"
        else:
            r = reg_results.get(h["dep"])
            if not r or "coefs" not in r:
                verdict = f"regression for {h['dep']} did not converge"
            else:
                cf = r["coefs"].set_index("param")
                tried = []
                for var in [h["var"]] + fallback_vars.get(h["id"], []):
                    if var in cf.index:
                        beta = cf.loc[var, "coef"]; p = cf.loc[var, "p"]
                        used_label = ("" if var == h["var"] else f" [via {var}]")

                        if h["expected_sign"] is None:
                            # Direction is physically ambiguous — any significant
                            # result is a finding; neither direction is "wrong"
                            if p < 0.05:
                                direction = "positive" if beta > 0 else "negative"
                                verdict = (f"β={beta:.3g}, p={p:.3g} → "
                                           f"SIGNIFICANT ({direction} direction){used_label}")
                            else:
                                verdict = (f"β={beta:.3g}, p={p:.3g} → "
                                           f"inconclusive (p≥0.05){used_label}")
                        else:
                            sign_ok = (np.sign(beta) == np.sign(h["expected_sign"]))
                            if sign_ok and p < 0.05:
                                verdict = f"β={beta:.3g}, p={p:.3g} → SUPPORTED{used_label}"
                            elif p < 0.05:
                                verdict = (f"β={beta:.3g}, p={p:.3g} → "
                                           f"SIGNIFICANT but opposite direction{used_label}")
                            else:
                                verdict = (f"β={beta:.3g}, p={p:.3g} → "
                                           f"inconclusive (p≥0.05){used_label}")
                        break
                    tried.append(var)
                else:
                    verdict = f"all candidate vars absorbed by FE: {tried}"
        out.append({"id": h["id"], "text": h["text"], "verdict": verdict})

    # ── Multiple-testing correction (H1–H4 form the testable family) ─────────
    # H5 is a logit (different distributional family); H6 is explicitly a
    # placebo — significant H6 is already flagged as misspecification, not a
    # finding, so it doesn't belong in the FWER pool.
    fwer_pool = {"H1", "H2", "H3", "H4"}
    p_vals: list[float | None] = []
    pool_out = [h for h in out if h["id"] in fwer_pool]
    for h in pool_out:
        m = re.search(r",\s*p=([0-9.eE+\-]+)", h["verdict"])
        p_vals.append(float(m.group(1)) if m else None)

    if any(p is not None for p in p_vals):
        reject = _holm_bonferroni(p_vals, alpha=0.05)
        for h, rej, p in zip(pool_out, reject, p_vals):
            if p is None:
                continue
            if "SIGNIFICANT" in h["verdict"] and not rej:
                h["verdict"] += (
                    "  ⚠ Holm–Bonferroni: p does NOT survive FWER correction "
                    f"(raw p={p:.3g} > corrected threshold)")
            elif "inconclusive" not in h["verdict"] and rej and p >= 0.05:
                pass  # would be a contradiction — ignore

    return out


# ---------------------------------------------------------------------------
# 9. HTML report
# ---------------------------------------------------------------------------
def render_html_report(out_dir: str, ctx: dict) -> str:
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    html_parts = []
    a = html_parts.append
    a("<!doctype html><html><head><meta charset='utf-8'>")
    a("<title>FI -> NO3 FB Propagation Report</title>")
    a("<style>")
    a("body{font-family:Georgia,serif;max-width:1100px;margin:32px auto;padding:0 24px;color:#1a1a1a;line-height:1.55}")
    a("h1,h2{font-family:'Helvetica Neue',sans-serif;letter-spacing:-0.02em}")
    a("h1{border-bottom:3px solid #c0392b;padding-bottom:8px}")
    a("h2{margin-top:36px;border-bottom:1px solid #888;color:#2c3e50}")
    a("table{border-collapse:collapse;margin:10px 0;font-size:0.92em}")
    a("th,td{border:1px solid #aaa;padding:5px 9px}")
    a("th{background:#f4f4f4}")
    a("pre{background:#f7f7f4;padding:12px;overflow-x:auto;font-size:0.78em;border-left:3px solid #2c3e50}")
    a(".caveat{background:#fff8dc;border-left:4px solid #d4a017;padding:10px 14px;font-size:0.93em}")
    a(".v-supported{color:#27ae60;font-weight:600}.v-no{color:#c0392b;font-weight:600}")
    a("img{max-width:100%;margin:12px 0;border:1px solid #ccc}")
    a("</style></head><body>")
    src_lbl = ctx.get("source_country", "FI")
    tgt_lbl = ctx.get("target_zone", "NO3")
    a(f"<h1>{src_lbl} → {tgt_lbl} Flow-Based Propagation: Validation Report</h1>")
    a(f"<p><em>Generated {ctx.get('ts','')}.</em> "
      f"JAO rows: {ctx.get('n_jao',0)} | NO3 rows analysed: {ctx.get('n_no3',0)} | "
      f"outage events: {ctx.get('n_outages',0)}</p>")

    a("<h2>Hypotheses</h2><table><tr><th>ID</th><th>Hypothesis</th><th>Verdict</th></tr>")
    for h in ctx.get("hypotheses", []):
        cls = "v-supported" if "SUPPORTED" in h["verdict"] or "CONSISTENT" in h["verdict"] else "v-no"
        a(f"<tr><td>{h['id']}</td><td>{h['text']}</td>"
          f"<td class='{cls}'>{h['verdict']}</td></tr>")
    a("</table>")

    for label, key in [("fall_signed / F_allReference regression (H1)","f0"),
                       ("PTDF_FI regression (H2)","ptdf_FI"),
                       ("RAM regression (H3)","ram"),
                       ("Shadow price regression (H4)","shadowPrice"),
                       ("FRM regression — placebo (H6)","frm"),
                       ("Logit on IVA active (H5)","logit")]:
        text = ctx.get(f"summary_{key}", "n/a")
        a(f"<h2>{label}</h2><pre>{text}</pre>")

    figs = ctx.get("figures", [])
    if figs:
        a("<h2>Diagnostic plots</h2>")
        for fp in figs:
            a(f"<img src='{fp}'>")

    a("<h2>Caveats</h2><div class='caveat'>")
    a(ctx.get("caveats",""))
    a("</div></body></html>")

    out_file = out / "report.html"
    out_file.write_text("\n".join(html_parts), encoding="utf-8")
    return str(out_file)


# ---------------------------------------------------------------------------
# 10. End-to-end orchestrator (simple, called by dashboard)
# ---------------------------------------------------------------------------
def main_cli() -> None:
    """pip console-script entry point for fi-no3-analyse.
    Locates run_analysis.py relative to this file and delegates to its main()."""
    import importlib.util
    import sys as _sys
    _here = Path(__file__).resolve().parent
    _candidates = [
        _here.parent.parent / "scripts" / "run_analysis.py",  # src/fi_no3/ layout
        _here.parent / "scripts" / "run_analysis.py",
        _here / "run_analysis.py",
        _here.parent / "run_analysis.py",
    ]
    for _script in _candidates:
        if _script.exists():
            spec = importlib.util.spec_from_file_location("run_analysis", _script)
            mod  = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.main()
            return
    print("ERROR: run_analysis.py not found relative to propagation.py.", file=_sys.stderr)
    _sys.exit(1)


@dataclass
class PipelineConfig:
    jao_csv:        str   = ""
    out_dir:        str   = "./fb_output"
    cache_db:       str   = "./fb_output/outage_cache.sqlite"
    manual_csv:     str   = "./manual_outages.csv"
    start_utc:      str   = "2024-10-29T00:00:00Z"
    end_utc:        str   = "2026-05-06T00:00:00Z"
    use_entsoe:     bool  = True
    use_manual:     bool  = True
    source_country: str   = "FI"    # ENTSO-E country code for the outage source
    target_zone:    str   = "NO3"   # Bidding zone to analyse (CNEC filter)
    no3_patterns:   tuple = DEFAULT_NO3_PATTERNS  # auto-updated in __post_init__

    def __post_init__(self):
        # If no3_patterns is the default, auto-populate from target_zone
        if self.no3_patterns is DEFAULT_NO3_PATTERNS:
            pats = zone_patterns(self.target_zone)
            if pats:
                self.no3_patterns = pats


def run_pipeline(cfg: PipelineConfig, jao_df: pd.DataFrame | None = None,
                 outages_df: pd.DataFrame | None = None,
                 log_cb: LogCallback = _noop) -> dict:
    Path(cfg.out_dir).mkdir(parents=True, exist_ok=True)

    # 1. JAO
    if jao_df is None:
        log_cb(f"Loading JAO CSV: {cfg.jao_csv}")
        jao = load_jao_csv(cfg.jao_csv)
    else:
        jao = jao_df.copy()
    log_cb(f"  JAO rows: {len(jao)}, CNECs: {jao['cneName'].nunique()}")
    no3 = filter_no3(jao, cfg.no3_patterns, zone_label=cfg.target_zone)
    log_cb(f"  {cfg.target_zone} rows: {len(no3)}, {cfg.target_zone} CNECs: {no3['cneName'].nunique()}")

    if no3.empty:
        # Produce a helpful diagnostic before aborting
        zones_in_file: list = []
        for col in ("biddingZoneFrom", "biddingZoneTo"):
            if col in jao.columns:
                zones_in_file.extend(jao[col].dropna().unique().tolist())
        zone_hint = ", ".join(dict.fromkeys(zones_in_file))  # dedup, preserve order
        raise ValueError(
            f"No CNECs found for target zone '{cfg.target_zone}' in the loaded JAO data.\n"
            f"Check that your JAO CSV contains {cfg.target_zone} CNECs.\n"
            f"Bidding zones detected in this file: {zone_hint or '(none detected)'}\n"
            f"CNEC name patterns tried: {cfg.no3_patterns}"
        )

    jao_start = no3["dateTimeUtc"].min()
    jao_end   = no3["dateTimeUtc"].max()
    log_cb(f"  JAO window: {jao_start.strftime('%Y-%m-%d %H:%M')} UTC "
           f"→ {jao_end.strftime('%Y-%m-%d %H:%M')} UTC")

    # 2. Outages
    if outages_df is None:
        con = open_cache(cfg.cache_db)
        all_events = []
        if cfg.use_entsoe:
            df_es = fetch_entsoe_outages(cfg.start_utc, cfg.end_utc,
                                         log_cb=log_cb,
                                         country_code=cfg.source_country)
            if not df_es.empty:
                upsert_events(con, df_es.to_dict("records"))
                all_events.append(df_es)
        if cfg.use_manual:
            df_m = load_manual_outages(cfg.manual_csv, log_cb=log_cb)
            if not df_m.empty:
                upsert_events(con, df_m.to_dict("records"))
                all_events.append(df_m)
        outages = load_cached_outages(con)
        outages = deduplicate_outages(outages)
        con.close()
    else:
        outages = deduplicate_outages(outages_df)
    log_cb(f"  Unified outage events (deduped): {len(outages)}")

    # --- Overlap check: warn if no outages fall inside the JAO window ---
    if not outages.empty:
        out_ts = outages.copy()
        for c in ("start_utc", "end_utc"):
            out_ts[c] = pd.to_datetime(out_ts[c], utc=True, errors="coerce")
        overlapping = out_ts[
            (out_ts["start_utc"] <= jao_end) &
            (out_ts["end_utc"]   >= jao_start)
        ]
        n_overlap = len(overlapping)
        if n_overlap == 0:
            log_cb("⚠  WARNING: NONE of the outage events overlap the JAO data window!")
            log_cb(f"   Outage dates span: {out_ts['start_utc'].min().strftime('%Y-%m-%d')} "
                   f"→ {out_ts['end_utc'].max().strftime('%Y-%m-%d')}")
            log_cb(f"   JAO window:        {jao_start.strftime('%Y-%m-%d')} "
                   f"→ {jao_end.strftime('%Y-%m-%d')}")
            log_cb("   RESULT: all outage covariates will be zero → "
                   "regressions will produce no signal.")
            log_cb("   FIX OPTIONS:")
            log_cb("   1. Load a longer JAO CSV covering the outage dates above.")
            log_cb("   2. Set the Outage source window to match your JAO dates "
                   f"({jao_start.strftime('%Y-%m-%dT%H:%M:%SZ')} → "
                   f"{jao_end.strftime('%Y-%m-%dT%H:%M:%SZ')}) and re-fetch.")
            log_cb("   3. Add a manual outage row with a date inside the JAO window.")
        else:
            log_cb(f"  ✓ {n_overlap} outage event(s) overlap the JAO window "
                   f"({len(outages) - n_overlap} outside and excluded from signal).")
            for _, r in overlapping.iterrows():
                log_cb(f"    - {r['asset_type']} | {r['planned_or_forced']} | "
                       f"{str(r['asset_name'])[:40]} | "
                       f"{r['start_utc'].strftime('%Y-%m-%d')} → "
                       f"{r['end_utc'].strftime('%Y-%m-%d')}")

    # 3. Covariates (includes RAM formula check, fall_signed, SP cleaning)
    _src = cfg.source_country.lower()
    no3_cov = build_covariates(no3, outages, log_cb=log_cb, src=_src)
    no3_cov.to_csv(Path(cfg.out_dir) / "no3_with_outage_covariates.csv", index=False)

    # 4. Regressions
    # ── Collinearity diagnostic ───────────────────────────────────────────────
    # With a small or homogeneous outage portfolio (e.g., all forced outages are
    # HVDC), the 7-covariate default spec is rank-deficient. PanelOLS silently
    # drops absorbed columns, which can hide the primary treatment variable.
    # We use per-hypothesis covariate specs (_indep_for_hypothesis) that put the
    # primary treatment variable FIRST — it is absorbed last if at all.
    _all_indep = _default_indep(_src)
    _indep_present = [c for c in _all_indep if c in no3_cov.columns]
    _X_diag = no3_cov[_indep_present].astype(float).dropna()
    _rank = int(np.linalg.matrix_rank(_X_diag.values)) if len(_X_diag) > 0 else 0
    _redundant = len(_indep_present) - _rank
    if _redundant > 0:
        log_cb(f"  Covariate matrix rank={_rank} of {len(_indep_present)} variables "
               f"({_redundant} are collinear — likely because outage types do not co-occur). "
               f"Using per-hypothesis specs to keep primary treatment variable identified.")
    else:
        log_cb(f"  Covariate matrix full rank ({_rank}) — all treatment variables identified independently.")

    # ── H1: fall_signed with HVDC-first spec ─────────────────────────────────
    log_cb(f"Running fall (sign-normalised) regression [H1] (src={_src.upper()})...")
    h1_dep  = "fall_signed" if "fall_signed" in no3_cov.columns else "fall"
    h1_spec = _indep_for_hypothesis("H1", _src)
    res_fall = run_panel_regression(
        no3_cov, h1_dep, indep=h1_spec, log_cb=log_cb, cluster="time", src=_src)

    # ── H2: |PTDF| with AC-first spec ────────────────────────────────────────
    log_cb(f"Running |PTDF_{_src.upper()}| regression [H2]...")
    _ptdf_abs_col = f"ptdf_{_src.upper()}_abs"
    h2_dep  = (_ptdf_abs_col if _ptdf_abs_col in no3_cov.columns
               else "ptdf_FI_abs" if "ptdf_FI_abs" in no3_cov.columns
               else "ptdf_FI")
    h2_spec = _indep_for_hypothesis("H2", _src)
    res_ptdf = run_panel_regression(
        no3_cov, h2_dep, indep=h2_spec, log_cb=log_cb, cluster="time", src=_src)

    # ── H3: RAM ──────────────────────────────────────────────────────────────
    log_cb("Running RAM regression [H3]...")
    res_ram = run_panel_regression(
        no3_cov, "ram", indep=_indep_for_hypothesis("H3", _src),
        log_cb=log_cb, cluster="time", src=_src)

    # ── H4: shadow price (binding MTUs only) ─────────────────────────────────
    # Use shadowPrice_clean (1e-8 → 0) then restrict to truly binding rows.
    log_cb("Running shadow price regression [H4] — binding MTUs only...")
    sp_col  = "shadowPrice_clean" if "shadowPrice_clean" in no3_cov.columns else "shadowPrice"
    sp_mask = no3_cov[sp_col].fillna(0) > 0
    no3_binding = no3_cov[sp_mask].copy()
    log_cb(f"  Truly binding (SP > 1e-6): {sp_mask.sum():,} / {len(no3_cov):,} "
           f"({sp_mask.mean():.1%})")
    if sp_mask.sum() < 50:
        log_cb("  Too few truly-binding MTUs; skipping shadow price regression")
        res_sp = {}
    else:
        res_sp = run_panel_regression(
            no3_binding, sp_col,
            indep=_indep_for_hypothesis("H4", _src),
            log_cb=log_cb, cluster="time", src=_src)

    # ── H6 placebo: FRM ──────────────────────────────────────────────────────
    log_cb("Running FRM placebo regression [H6]...")
    res_frm = run_panel_regression(
        no3_cov, "frm",
        indep=_indep_for_hypothesis("H6", _src),
        log_cb=log_cb, cluster="time", src=_src)

    # ── Bonus: fnrao (RA trigger channel) ────────────────────────────────────
    log_cb("Running fnrao (RA trigger) regression [bonus]...")
    res_fnrao = run_panel_regression(
        no3_cov, "fnrao", indep=_indep_for_hypothesis("H3", _src),
        log_cb=log_cb, cluster="time", src=_src) \
        if "fnrao" in no3_cov.columns and no3_cov["fnrao"].notna().any() else {}

    # ── H5: IVA logit ────────────────────────────────────────────────────────
    log_cb("Running IVA logit [H5]...")
    res_logit = run_logit_iva(no3_cov, log_cb=log_cb, src=_src)

    reg_results = {
        "fall_signed":  res_fall,
        "fall":         res_fall,    # backward compatibility
        "fref_signed":  res_fall,    # backward compatibility (was wrong variable)
        "f0":           res_fall,    # backward compatibility
        _ptdf_abs_col:  res_ptdf,    # e.g. "ptdf_NO_abs" or "ptdf_FI_abs"
        "ptdf_FI_abs":  res_ptdf,    # backward-compat alias
        "ptdf_FI":      res_ptdf,
        "ram":          res_ram,
        "shadowPrice":  res_sp,
        "shadowPrice_clean": res_sp,
        "frm":          res_frm,
        "fnrao":        res_fnrao,
    }
    hypotheses = summarize_hypotheses(reg_results, res_logit,
                                      src=_src, tgt=cfg.target_zone)

    # 5. Save outages
    outages.to_csv(Path(cfg.out_dir) / "outages_unified.csv", index=False)

    return {
        "no3": no3_cov, "outages": outages,
        "regressions": reg_results, "logit": res_logit,
        "hypotheses": hypotheses,
        "out_dir": cfg.out_dir,
        "source_country": cfg.source_country,
        "target_zone": cfg.target_zone,
        "overlapping_outages": overlapping if not outages.empty else pd.DataFrame(),
    }


# ===========================================================================
# EVENT STUDY ANALYSIS
# ===========================================================================

def build_event_time_dummies(df: pd.DataFrame, outages: pd.DataFrame,
                              leads: int = 12, lags: int = 48,
                              step_hours: int = 1) -> pd.DataFrame:
    """
    For each JAO row, compute the event-time index k in HOURS relative to
    the nearest overlapping outage start.

    k = -leads ... -1 : pre-event hours (pre-trend test)
    k = 0             : first hour of the outage
    k = 1 ... lags    : hours after outage start

    Uses hourly (not 15-min MTU) resolution because:
    - Outage starts are typically rounded to the hour by ENTSO-E
    - 15-min data has AC=0.63 → inflates degrees of freedom 4x
    - Meaningful recovery dynamics occur over hours, not MTUs

    Rows not within (leads+lags+1) hours of any outage get event_k = NaN.
    Dummy columns: D_km{leads} ... D_k{lags} (omit k=-1 as reference).
    """
    df = df.copy()
    if outages is None or outages.empty:
        df["event_k"] = np.nan
        return df

    out = outages.copy()
    for c in ("start_utc", "end_utc"):
        out[c] = pd.to_datetime(out[c], utc=True, errors="coerce")
    out = out.dropna(subset=["start_utc"])

    step = pd.Timedelta(hours=step_hours)
    ts   = df["dateTimeUtc"].values

    event_k = np.full(len(df), np.nan)
    for _, row in out.iterrows():
        s = row["start_utc"]
        for k in range(-leads, lags + 1):
            target = s + k * step
            # Match any row within ±step/2 of the target hour
            half = step.total_seconds() * 1e9 / 2
            mask = np.abs((ts - target.to_datetime64()).astype("int64")) < half
            # Only assign if not yet assigned or if this is closer
            event_k[mask] = k

    df["event_k"] = event_k

    # Dummy columns
    for k in range(-leads, lags + 1):
        col = f"D_k{'m' if k < 0 else ''}{abs(k)}"
        df[col] = (df["event_k"] == k).astype(float)

    return df


def run_event_study(df: pd.DataFrame, dep_var: str,
                    leads: int = 4, lags: int = 8,
                    log_cb: LogCallback = _noop) -> dict:
    """
    Event study regression: estimate β_k for k = -leads ... lags.
    Returns a DataFrame with columns [k, beta, se, ci_lo, ci_hi, p].
    k < 0 are pre-trend; k >= 0 are post-event.
    Omits k = -1 as the reference period.
    """
    if PanelOLS is None or sm is None:
        log_cb("linearmodels/statsmodels missing; skip event study")
        return {}

    dummy_cols = []
    for k in range(-leads, lags + 1):
        if k == -1:
            continue  # reference period
        col = f"D_k{'m' if k < 0 else ''}{abs(k)}"
        if col in df.columns:
            dummy_cols.append((k, col))

    if not dummy_cols:
        log_cb("No event-time dummy columns found; run build_event_time_dummies first")
        return {}

    keep = ["dateTimeUtc", "cneName", dep_var, "hour", "dow", "month"] \
           + [c for _, c in dummy_cols]
    keep = [c for c in keep if c in df.columns]
    sub = df[keep].dropna(subset=[dep_var, "event_k"] if "event_k" in df.columns
                                  else [dep_var])
    # Keep only rows that are in event windows or in baseline
    sub = sub[sub[[c for _, c in dummy_cols]].sum(axis=1) >= 0]  # all rows
    if sub.empty or sub[dep_var].nunique() < 2 or sub["cneName"].nunique() < 2:
        log_cb(f"Insufficient data for event study on {dep_var}")
        return {}

    sub = sub.set_index(["cneName", "dateTimeUtc"])
    y = pd.to_numeric(sub[dep_var], errors="coerce").astype(float)

    X = pd.DataFrame(index=sub.index)
    for _, col in dummy_cols:
        X[col] = sub[col].astype(float)

    # Add hour FE
    date_span = (sub.index.get_level_values(1).max()
                 - sub.index.get_level_values(1).min()).total_seconds()
    if date_span > 86400:
        X = pd.concat([X, pd.get_dummies(sub["hour"], prefix="h",
                                          drop_first=True).astype(float)], axis=1)

    X = X.apply(pd.to_numeric, errors="coerce").astype(float)
    valid = X.notna().all(axis=1) & y.notna()
    X = X.loc[valid]; y = y.loc[valid]
    nz = X.std(axis=0, skipna=True) > 1e-12
    X = X.loc[:, nz]
    X = sm.add_constant(X, has_constant="add")

    try:
        mod = PanelOLS(y, X, entity_effects=True, drop_absorbed=True,
                       check_rank=False)
        res = mod.fit(cov_type="clustered", cluster_entity=True)
    except Exception as e:
        log_cb(f"Event study fit failed for {dep_var}: {e}")
        return {}

    rows = []
    for k, col in dummy_cols:
        if col in res.params.index:
            b  = float(res.params[col])
            se = float(res.std_errors[col])
            p  = float(res.pvalues[col])
            rows.append({"k": k, "beta": b, "se": se,
                         "ci_lo": b - 1.96 * se,
                         "ci_hi": b + 1.96 * se,
                         "p": p})

    coef_df = pd.DataFrame(rows).sort_values("k").reset_index(drop=True)

    # Pre-trend test: are betas for k < 0 jointly zero?
    pre = coef_df[coef_df["k"] < 0]
    pre_trend_ok = (pre["p"] >= 0.05).all() if len(pre) else True

    return {
        "dep": dep_var,
        "coefs": coef_df,
        "n_obs": int(res.nobs),
        "pre_trend_ok": bool(pre_trend_ok),
        "pre_trend_detail": pre[["k", "beta", "p"]].to_dict("records"),
        "rsquared_within": float(getattr(res, "rsquared_within", np.nan)),
    }


# ===========================================================================
# SINGLE EVENT ANALYSIS
# ===========================================================================


# ===========================================================================
# ITS COUNTERFACTUAL MODELS
# Each function receives the same pre-period DataFrame and the full window
# DataFrame, and returns a projected Series indexed like all_agg.
# All models obey the fundamental rule: fit on PRE-PERIOD DATA ONLY,
# project into during and post without ever touching those observations.
# ===========================================================================

def _its_seasonal_naive(pre_agg: pd.DataFrame, all_agg: pd.DataFrame,
                        col: str) -> pd.Series:
    """
    Model 1 — Seasonal naive (current default).

    Counterfactual Y(0)_t = mean(actual_{h,dow} | t in pre-period)

    Strengths:
      - Zero estimation variance → reliable with as few as 4-7 days pre-data
      - Perfectly captures the repeating diurnal + weekly pattern
      - Simple to explain and audit
    Weaknesses:
      - No trend component: assumes the level is flat across the pre-period
      - If a genuine upward/downward drift exists in the pre-period, the
        projection will be biased

    Best for: short pre-periods (≤14 days), stable pre-period, clear outage.
    """
    seasonal_mean = pre_agg.groupby(["hour", "dow"])[col].mean()
    fallback      = float(pre_agg[col].mean())

    def _lookup(row):
        return float(seasonal_mean.get((int(row["hour"]), int(row["dow"])), fallback))

    return all_agg.apply(_lookup, axis=1)


def _its_fourier_trend(pre_agg: pd.DataFrame, all_agg: pd.DataFrame,
                       col: str, n_harmonics_day: int = 4,
                       n_harmonics_week: int = 2,
                       mtu_minutes: int = 15) -> pd.Series:
    """
    Model 2 — Fourier regression with linear trend.

    Counterfactual: OLS fit on pre-period of the form
      Y_t = α + β·t + Σ_{k=1}^{K1} [a_k·sin(2πk·t/T_day) + b_k·cos(2πk·t/T_day)]
                    + Σ_{k=1}^{K2} [c_k·sin(2πk·t/T_week) + d_k·cos(2πk·t/T_week)] + ε_t

    where T_day  = 24 × 60 / mtu_minutes (number of MTUs per day = 96 for 15-min)
          T_week = 7 × T_day

    Then project Y(0) = fitted model evaluated at the during/post timestamps.

    Strengths:
      - Captures both trend and smooth continuous seasonality
      - Correctly extrapolates a slope observed in the pre-period
      - Interpretable OLS coefficients
      - Already uses statsmodels (existing dependency)

    Weaknesses:
      - Smooth Fourier seasonality may miss sharp morning ramps
      - Needs ≥ 2 seasonal cycles (≥2 days) to estimate daily harmonics;
        ≥ 2 weeks to estimate weekly harmonics reliably

    Best for: pre-periods with a visible trend; ≥7 days pre-data.
    """
    if sm is None:
        return _its_seasonal_naive(pre_agg, all_agg, col)

    T_day  = int(24 * 60 / mtu_minutes)   # 96 for 15-min data
    T_week = 7 * T_day

    def _fourier_features(df: pd.DataFrame, t0: pd.Timestamp) -> pd.DataFrame:
        """Convert timestamps to integer MTU offsets and build Fourier matrix."""
        dt_ns = (df["dateTimeUtc"] - t0).dt.total_seconds() / (mtu_minutes * 60)
        t_idx = dt_ns.values.astype(float)
        feat  = {"t": t_idx, "const": 1.0}
        for k in range(1, n_harmonics_day + 1):
            feat[f"sin_d{k}"] = np.sin(2 * np.pi * k * t_idx / T_day)
            feat[f"cos_d{k}"] = np.cos(2 * np.pi * k * t_idx / T_day)
        for k in range(1, n_harmonics_week + 1):
            feat[f"sin_w{k}"] = np.sin(2 * np.pi * k * t_idx / T_week)
            feat[f"cos_w{k}"] = np.cos(2 * np.pi * k * t_idx / T_week)
        return pd.DataFrame(feat, index=df.index)

    t0   = pre_agg["dateTimeUtc"].min()
    X_pre = _fourier_features(pre_agg, t0)
    y_pre = pre_agg[col].values.astype(float)

    # Drop columns with zero variance (can happen with very short pre-periods)
    nz = X_pre.std(axis=0) > 1e-12
    X_pre = X_pre.loc[:, nz]

    valid = np.isfinite(y_pre)
    if valid.sum() < len(X_pre.columns) + 2:
        # Not enough data for OLS → fall back to seasonal naive
        return _its_seasonal_naive(pre_agg, all_agg, col)

    try:
        res   = sm.OLS(y_pre[valid], X_pre.iloc[valid].values).fit()
        X_all = _fourier_features(all_agg, t0).loc[:, nz]
        projected = res.predict(X_all.values)
        return pd.Series(projected, index=all_agg.index)
    except Exception:
        return _its_seasonal_naive(pre_agg, all_agg, col)


def _its_stl(pre_agg: pd.DataFrame, all_agg: pd.DataFrame,
             col: str, mtu_minutes: int = 15) -> pd.Series:
    """
    Model 3 — STL decomposition (Seasonal-Trend decomposition via Loess).

    Steps:
      1. Fit STL on the pre-period with period = T_day = 96 (15-min data).
      2. Extract trend and seasonal components from the pre-period decomposition.
      3. Project trend: fit a linear trend on the STL trend component over the
         last week of the pre-period, extrapolate forward.
      4. Project seasonal: use the fitted STL seasonal component, repeating
         the last full seasonal cycle.
      5. Counterfactual = projected_trend + projected_seasonal.

    Strengths:
      - Robust to outliers in the pre-period (Loess smoother is resistant)
      - Handles level shifts and irregular spikes in the pre-period without
        distorting the seasonal pattern
      - Captures sharp morning ramps that Fourier misses

    Weaknesses:
      - Requires ≥ 2 full daily cycles (≥2 days) minimum; ≥7 days for stable estimate
      - Does not model weekly seasonality unless period=672, which needs ≥2 full weeks
      - Projection method (linear trend extrapolation) can diverge for long outages

    Best for: pre-periods with outliers or structural breaks; ≥7 days pre-data.
    """
    try:
        from statsmodels.tsa.seasonal import STL
    except ImportError:
        return _its_fourier_trend(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    T_day = int(24 * 60 / mtu_minutes)   # 96

    # Need a regular time series — resample pre to MTU frequency
    pre_ts = (pre_agg.set_index("dateTimeUtc")[col]
              .resample(f"{mtu_minutes}min").mean()
              .interpolate("time"))

    if len(pre_ts) < 2 * T_day:
        # Too short for STL → fall back
        return _its_fourier_trend(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    try:
        stl_res = STL(pre_ts, period=T_day, robust=True).fit()
    except Exception:
        return _its_fourier_trend(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    # Project trend: linear extrapolation from slope of last T_day points
    trend   = stl_res.trend
    n_trend = min(T_day, len(trend))
    t_idx   = np.arange(n_trend, dtype=float)
    slope   = np.polyfit(t_idx, trend.values[-n_trend:], 1)[0]  # MW per MTU
    trend_last = float(trend.values[-1])

    # Project seasonal: repeat the last full seasonal cycle
    seasonal = stl_res.seasonal.values  # shape (n_pre,)
    # The seasonal pattern repeats with period T_day
    def _seasonal_at(offset: int) -> float:
        """Seasonal value at MTU offset from end of pre-period."""
        idx = (len(seasonal) + offset) % T_day
        # Build index into seasonal array: find position of same phase
        positions = [j for j in range(len(seasonal)) if j % T_day == idx]
        if positions:
            return float(np.mean([seasonal[p] for p in positions]))
        return 0.0

    # For each timestamp in all_agg, compute its MTU offset from pre-end
    pre_end_ts = pre_ts.index[-1]
    projected  = []
    for ts in all_agg["dateTimeUtc"]:
        offset_mtu = int(round((ts - pre_end_ts).total_seconds() / (mtu_minutes * 60)))
        proj_trend    = trend_last + slope * offset_mtu
        proj_seasonal = _seasonal_at(offset_mtu)
        projected.append(proj_trend + proj_seasonal)

    return pd.Series(projected, index=all_agg.index)


def _its_arima(pre_agg: pd.DataFrame, all_agg: pd.DataFrame,
               col: str, mtu_minutes: int = 15) -> pd.Series:
    """
    ARIMA on deseasonalized residuals.
    See _ITS_METHODS["arima"]["description"] for full documentation.
    """
    try:
        from statsmodels.tsa.arima.model import ARIMA as _SMArima
    except ImportError:
        return _its_fourier_trend(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    if len(pre_agg) < 32:
        return _its_seasonal_naive(pre_agg, all_agg, col)

    # Step 1: seasonal means from PRE only
    seas_mean = pre_agg.groupby(["hour", "dow"])[col].mean()
    fallback  = float(pre_agg[col].mean())

    def _seas(h, d):
        return float(seas_mean.get((int(h), int(d)), fallback))

    # Step 2: deseasonalize the pre-series
    deseas = np.array([pre_agg[col].values[i] - _seas(pre_agg["hour"].values[i],
                                                        pre_agg["dow"].values[i])
                       for i in range(len(pre_agg))], dtype=float)
    deseas = deseas[np.isfinite(deseas)]
    if len(deseas) < 20:
        return _its_seasonal_naive(pre_agg, all_agg, col)

    # Step 3: auto-select ARIMA(p,0,q) order by AIC on a subsample
    _sample   = deseas[:min(800, len(deseas))]
    best_aic, best_order = float("inf"), (1, 0, 1)
    for _p in range(3):
        for _q in range(3):
            try:
                _m = _SMArima(_sample, order=(_p, 0, _q)).fit()
                if _m.aic < best_aic:
                    best_aic, best_order = _m.aic, (_p, 0, _q)
            except Exception:
                pass

    # Step 4: fit selected order on full pre-series
    try:
        model = _SMArima(deseas, order=best_order).fit()
    except Exception:
        return _its_seasonal_naive(pre_agg, all_agg, col)

    # Step 5: forecast the number of future MTUs
    pre_end = pre_agg["dateTimeUtc"].max()
    n_future = (all_agg["dateTimeUtc"] > pre_end).sum()

    try:
        fc = model.forecast(steps=n_future) if n_future > 0 else np.array([])
    except Exception:
        fc = np.zeros(n_future)

    # Step 6: reconstruct counterfactual for every row in all_agg
    projected  = []
    fc_iter    = iter(fc)
    for _, row in all_agg.iterrows():
        seas = _seas(row["hour"], row["dow"])
        resid = float(next(fc_iter, 0.0)) if row["dateTimeUtc"] > pre_end else 0.0
        projected.append(seas + resid)

    return pd.Series(projected, index=all_agg.index)


def _its_sarima(pre_agg: pd.DataFrame, all_agg: pd.DataFrame,
                col: str, mtu_minutes: int = 15) -> pd.Series:
    """
    SARIMA(p,d,q)(P,D,Q)[24] on hourly-aggregated data.
    See _ITS_METHODS["sarima"]["description"] for full documentation.
    """
    try:
        from statsmodels.tsa.statespace.sarimax import SARIMAX as _SARIMAX
    except ImportError:
        return _its_arima(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    # ── Step 1: resample pre to hourly ──────────────────────────────────────
    pre_ts = (pre_agg.set_index("dateTimeUtc")[col]
              .resample("1h").mean()
              .interpolate("time"))
    if len(pre_ts) < 48:   # need ≥2 full daily cycles
        return _its_arima(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    # ── Step 2: auto-select SARIMA orders by AIC ────────────────────────────
    # Grid: (p,d,q) × (P,D,Q) — small grid for speed
    # d=0 (flow data stationary), D=0 (seasonal differencing instability risk on short series)
    # D=1 only when we have ≥3 full cycles (≥72h) and it doesn't blow up
    _endo   = pre_ts.values.astype(float)
    _use_D1 = len(_endo) >= 72
    _orders = [(1,0,1),(1,0,0),(0,0,1),(2,0,1)]
    _sorders= [(1,0,1,24),(1,1,1,24)] if _use_D1 else [(1,0,1,24)]
    best_aic, best_o, best_so = float("inf"), (1,0,1), (1,0,1,24)

    for _o in _orders:
        for _so in _sorders:
            try:
                _m = _SARIMAX(_endo, order=_o, seasonal_order=_so,
                               enforce_stationarity=False,
                               enforce_invertibility=False).fit(disp=False)
                if np.isfinite(_m.aic) and _m.aic < best_aic:
                    best_aic, best_o, best_so = _m.aic, _o, _so
            except Exception:
                pass

    # ── Step 3: fit best model on full pre-series ───────────────────────────
    try:
        model = _SARIMAX(_endo, order=best_o, seasonal_order=best_so,
                          enforce_stationarity=False,
                          enforce_invertibility=False).fit(disp=False)
    except Exception:
        return _its_arima(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    # ── Step 4: forecast hourly ─────────────────────────────────────────────
    pre_end_h = pre_ts.index[-1]
    all_end   = all_agg["dateTimeUtc"].max()
    n_hours_fc = int(np.ceil((all_end - pre_end_h).total_seconds() / 3600)) + 1
    if n_hours_fc <= 0:
        return _its_seasonal_naive(pre_agg, all_agg, col)

    try:
        fc_hourly = model.forecast(steps=n_hours_fc)
    except Exception:
        return _its_arima(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)

    # Build an hourly index starting just after pre_end_h
    hourly_fc_idx = pd.date_range(
        start=pre_end_h + pd.Timedelta(hours=1),
        periods=n_hours_fc, freq="1h", tz="UTC")
    hourly_fc_series = pd.Series(fc_hourly, index=hourly_fc_idx)

    # ── Step 5: within-hour seasonal pattern from pre (for sub-hourly detail) ─
    # Δ_{mtu} = mean(Y_t - hourly_mean_t | same mtu-within-hour and dow)
    pre_detailed = pre_agg.copy()
    pre_detailed["hour"]  = pre_detailed["dateTimeUtc"].dt.hour
    pre_detailed["dow"]   = pre_detailed["dateTimeUtc"].dt.dayofweek
    pre_detailed["mtu_in_hour"] = (
        pre_detailed["dateTimeUtc"].dt.minute // mtu_minutes)
    # hourly mean per timestamp
    pre_hrly = (pre_detailed.set_index("dateTimeUtc")[col]
                .resample("1h").transform("mean"))
    pre_detailed["hourly_mean"] = pre_hrly.values
    pre_detailed["within_hour_dev"] = pre_detailed[col] - pre_detailed["hourly_mean"]
    sub_hour_mean = (pre_detailed.groupby(["hour","dow","mtu_in_hour"])
                     ["within_hour_dev"].mean())

    # ── Step 6: reconstruct MTU-level counterfactual ────────────────────────
    projected = []
    for _, row in all_agg.iterrows():
        ts = row["dateTimeUtc"]
        if ts <= pre_end_h:
            # Pre-period: use in-sample seasonal mean (no SARIMA forecast)
            seas = float(pre_agg.groupby(["hour","dow"])[col].mean()
                         .get((int(row["hour"]), int(row["dow"])),
                              float(pre_agg[col].mean())))
            projected.append(seas)
        else:
            # Future: SARIMA hourly forecast + within-hour deviation
            hour_ts = ts.floor("1h")
            # Find nearest hourly forecast
            if hour_ts in hourly_fc_series.index:
                h_fc = float(hourly_fc_series[hour_ts])
            else:
                nearest = hourly_fc_series.index.get_indexer([hour_ts], method="nearest")[0]
                h_fc = float(hourly_fc_series.iloc[nearest]) if nearest >= 0 else float(hourly_fc_series.iloc[-1])
            mtu_in_h = int(ts.minute // mtu_minutes)
            dev_key  = (int(row["hour"]), int(row["dow"]), mtu_in_h)
            dev      = float(sub_hour_mean.get(dev_key, 0.0))
            projected.append(h_fc + dev)

    return pd.Series(projected, index=all_agg.index)


def _clamp_recovery_frac(impact: float, recovery_residual: float) -> float:
    """Compute recovery fraction clamped to [-1.0, 1.0].

    recovery_frac = 1 - |post_gap| / |impact|
      1.0  = full recovery (post returns exactly to counterfactual)
      0.0  = no recovery  (post deviation equals during deviation)
     <0.0  = overshoot    (post deviates MORE than during — macro shock in post window)
     -1.0  = floor (worst interpretable value; anything below is clamped)

    Values below -1 occur when a different shock in the post-window dominates over
    the original outage effect. In that case the metric is not meaningful and -1.0
    is reported as a floor. The raw recovery_residual is always available separately.
    """
    if abs(impact) < 1e-9:
        return float("nan")
    raw = 1.0 - abs(recovery_residual) / abs(impact)
    clamped = max(-1.0, min(1.0, raw))
    return round(clamped, 3)


_ITS_METHODS = {
    "seasonal_naive": {
        "label":       "Seasonal Naive",
        "min_days":    2,
        "description": (
            "Mean by (hour, weekday) from pre-period. "
            "Zero estimation variance — reliable with as little as 2 days of data. "
            "Best when the pre-period is short or when there is no systematic trend. "
            "Minimum baseline: 2 days."),
        "fn": _its_seasonal_naive,
    },
    "fourier_trend": {
        "label":       "Fourier + Linear Trend (OLS)",
        "min_days":    7,
        "description": (
            "OLS with Fourier harmonics for daily/weekly seasonality plus a linear trend. "
            "Correctly extrapolates a slope observed in the pre-period. "
            "Best when a drift is visible in the pre-window. "
            "Minimum baseline: 7 days (to see a weekly cycle)."),
        "fn": _its_fourier_trend,
    },
    "stl": {
        "label":       "STL Decomposition (Loess)",
        "min_days":    7,
        "description": (
            "Seasonal-Trend decomposition via Loess. Robust to outliers and spikes "
            "in the pre-period. Projects trend via linear extrapolation and repeats "
            "the fitted seasonal component. "
            "Best when the pre-period contains other irregular events. "
            "Minimum baseline: 7 days."),
        "fn": _its_stl,
    },
    "arima": {
        "label":       "ARIMA (deseasonalized residuals)",
        "min_days":    14,
        "description": (
            "Deseasonalizes using seasonal naive, then fits ARIMA(p,0,q) on the residuals "
            "with automatic order selection (AIC grid over p,q ∈ {0,1,2}). "
            "Captures autocorrelation in flow data driven by multi-hour hydro dispatch "
            "and weather patterns — not captured by the other methods. "
            "Forecast uncertainty grows with horizon; best for outages ≤48h. "
            "Minimum baseline: 14 days. Recommended: 30 days."),
        "fn": _its_arima,
    },
    "sarima": {
        "label":       "SARIMA on hourly data  [m=24]",
        "min_days":    14,
        "description": (
            "Aggregates the pre-period to hourly resolution, fits "
            "SARIMA(p,d,q)(P,D,Q)[24] with AIC-based order selection, forecasts hourly, "
            "then expands back to MTU resolution using the within-hour seasonal pattern. "
            "Most principled method when ≥1 year of JAO history is available — "
            "captures both the autocorrelation structure and the 24-hour seasonal cycle "
            "in a single unified model. "
            "Minimum baseline: 14 days. Recommended: 30–90 days."),
        "fn": _its_sarima,
    },
}

ITS_METHOD_NAMES = list(_ITS_METHODS.keys())
ITS_DEFAULT_METHOD = "seasonal_naive"


def _build_its_for_col(pre_agg: pd.DataFrame, all_agg: pd.DataFrame,
                       col: str, method: str, mtu_minutes: int = 15) -> pd.Series:
    """Dispatch to the chosen ITS counterfactual model."""
    if method not in _ITS_METHODS:
        method = ITS_DEFAULT_METHOD
    fn = _ITS_METHODS[method]["fn"]
    # All methods except seasonal_naive accept mtu_minutes
    if method == "seasonal_naive":
        return fn(pre_agg, all_agg, col)
    return fn(pre_agg, all_agg, col, mtu_minutes=mtu_minutes)


def single_event_analysis(no3_df: pd.DataFrame, outage_row: pd.Series,
                           baseline_days: int = 7,
                           post_days: int = 3,
                           log_cb: LogCallback = _noop,
                           src: str = "fi",
                           its_method: str = ITS_DEFAULT_METHOD,
                           mtu_minutes: int = 15) -> dict:
    """
    Deep-dive analysis for one specific maintenance event.

    its_method: counterfactual model for the interrupted time series.
        "seasonal_naive" — mean by (hour, weekday) from pre-period [default]
        "fourier_trend"  — OLS with Fourier harmonics + linear trend
        "stl"            — STL decomposition (Loess), robust to outliers
        "all"            — run all three; dashboard can compare them

    Returns:
      summary      : dict of scalar statistics
      its          : interrupted time series DataFrame (actual vs projected)
      its_all      : dict method→DataFrame when its_method="all", else {}
      decomp       : ΔRAM decomposition DataFrame
      did          : DiD table (high vs low PTDF_FI CNECs)
      cnec_table   : per-CNEC before/after + counterfactual table
    """
    def _to_utc(val):
        ts = pd.Timestamp(val)
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")

    s = _to_utc(outage_row["start_utc"])
    e = _to_utc(outage_row["end_utc"])

    pre_start = s - pd.Timedelta(days=baseline_days)
    post_end  = e + pd.Timedelta(days=post_days)

    # ── Guard: event window must overlap JAO data ───────────────────────────
    jao_min = no3_df["dateTimeUtc"].min()
    jao_max = no3_df["dateTimeUtc"].max()
    if s > jao_max or e < jao_min:
        raise ValueError(
            f"Event window {s.date()} → {e.date()} is entirely outside the "
            f"JAO data range {jao_min.date()} → {jao_max.date()}.\n"
            f"Load a JAO CSV that covers the event date, or pick a different event."
        )
    # Warn if event duration looks implausible (> 30 days = likely bad ENTSO-E record)
    duration_h = (e - s).total_seconds() / 3600
    if duration_h > 30 * 24:
        raise ValueError(
            f"Event duration is {duration_h:.0f} h ({duration_h/24:.0f} days) — "
            f"likely a spurious ENTSO-E record (status update stored as a new event).\n"
            f"A {duration_h/24:.0f}-day forced outage would consume the entire JAO window "
            f"leaving no pre/post baseline. Select a real short-duration event instead."
        )
    if pre_start < jao_min:
        log_cb(f"  ⚠ Pre-period clipped: JAO data starts {jao_min.date()} "
               f"(need {pre_start.date()}). Baseline may be short.")
    if post_end > jao_max:
        log_cb(f"  ⚠ Post-period clipped: JAO data ends {jao_max.date()} "
               f"(need {post_end.date()}). Recovery window may be short.")
    # ───────────────────────────────────────────────────────────────────────

    pre     = no3_df[(no3_df.dateTimeUtc >= pre_start) & (no3_df.dateTimeUtc < s)]
    during  = no3_df[(no3_df.dateTimeUtc >= s)          & (no3_df.dateTimeUtc < e)]
    post    = no3_df[(no3_df.dateTimeUtc >= e)           & (no3_df.dateTimeUtc < post_end)]
    window  = no3_df[(no3_df.dateTimeUtc >= pre_start)   & (no3_df.dateTimeUtc < post_end)]

    n_cnecs = int(no3_df.cneName.nunique())
    log_cb(f"  Periods (×{n_cnecs} CNECs):  "
           f"pre={len(pre):,} rows "
           f"({pre_start.date()} → {s.date()})  |  "
           f"during={len(during):,} rows "
           f"({s.date()} → {e.date()})  |  "
           f"post={len(post):,} rows")
    if len(during) == 0:
        log_cb(f"  ⚠ During-period empty — event may be entirely outside JAO window, "
               f"or JAO data has a gap at {s.date()} → {e.date()}.")
    if len(pre) == 0:
        log_cb(f"  ⚠ Pre-period empty — event starts before JAO data "
               f"({jao_min.date()}). Δ cannot be computed.")

    # Final guard: if pre AND post are both empty there is no baseline at all
    if pre.empty and post.empty:
        raise ValueError(
            f"Event {s.date()} → {e.date()} ({duration_h:.0f} h) covers the entire "
            f"JAO window — no pre/post baseline rows exist. "
            f"Load a longer JAO CSV or select a shorter event."
        )

    # ── 1. Per-CNEC before/during/after + counterfactual table ─────────────
    # The seasonal naive counterfactual Y(0) is built from PRE-PERIOD ONLY.
    # It is then applied to both DURING and POST windows.
    # This means the DURING interval never contaminates the counterfactual.
    #
    # Columns returned:
    #   pre_*        actual mean in pre-window
    #   during_*     actual mean during outage
    #   post_*       actual mean in post-window
    #   cf_during_*  counterfactual Y(0) for the during period
    #   cf_post_*    counterfactual Y(0) for the post period
    #   impact_*     actual_during  - cf_during  (treatment effect)
    #   recovery_*   actual_post    - cf_post    (residual after outage ends; 0 = full recovery)
    #   delta_*      actual_during  - actual_pre (raw, no counterfactual correction)

    # Which FB params to include
    _all_fb = ["fall_signed", "fall", "f0", "fref", "ram",
               "shadowPrice_clean", "shadowPrice", "frm", "iva",
               "ptdf_FI", "ptdf_FI_FS", "ptdf_FI_EL", "amr", "faac"]
    fb_params = [c for c in _all_fb if c in no3_df.columns]
    # Deduplicate redundant pairs
    _seen_fb = set()
    _fb_final = []
    for c in fb_params:
        canon = "fall" if c == "fall_signed" else \
                "shadowPrice" if c == "shadowPrice_clean" else c
        if canon not in _seen_fb:
            _seen_fb.add(canon)
            _fb_final.append(c)
    fb_params = _fb_final

    # Build per-(cnec,hour,dow) seasonal lookup from PRE only
    _pre_lookup = pre.copy()
    _pre_lookup["hour"] = _pre_lookup["dateTimeUtc"].dt.hour
    _pre_lookup["dow"]  = _pre_lookup["dateTimeUtc"].dt.dayofweek
    _pooled_seasonal: dict = {}
    _cnec_seasonal:   dict = {}
    for col in fb_params:
        if col not in _pre_lookup.columns:
            continue
        _pooled_seasonal[col] = _pre_lookup.groupby(["hour","dow"])[col].mean()
        _cnec_seasonal[col]   = _pre_lookup.groupby(["cneName","hour","dow"])[col].mean()

    def _cf_mean_for(df_subset: pd.DataFrame, col: str) -> float:
        """Y(0) expected value for df_subset using PRE-ONLY seasonal model."""
        if df_subset.empty or col not in _pooled_seasonal:
            return np.nan
        ds = df_subset.copy()
        ds["hour"] = ds["dateTimeUtc"].dt.hour
        ds["dow"]  = ds["dateTimeUtc"].dt.dayofweek
        vals = []
        fallback = float(_pre_lookup[col].mean()) if col in _pre_lookup.columns else np.nan
        for _, r in ds.iterrows():
            v = _cnec_seasonal[col].get((r["cneName"], int(r["hour"]), int(r["dow"])),
                _pooled_seasonal[col].get((int(r["hour"]), int(r["dow"])), fallback))
            vals.append(float(v) if pd.notna(v) else np.nan)
        return round(float(np.nanmean(vals)), 2) if vals else np.nan

    cnec_rows = []
    for cnec in sorted(no3_df.cneName.unique()):
        r = {"cnec": cnec}
        for pdata, label in [(pre,"pre"), (during,"during"), (post,"post")]:
            sub = pdata[pdata.cneName == cnec]
            for col in fb_params:
                if col in sub.columns:
                    r[f"{label}_{col}"] = round(float(sub[col].mean()), 2) \
                        if not sub.empty else np.nan

        # Counterfactual and derived metrics (PRE model, applied to during/post)
        during_sub = during[during.cneName == cnec]
        post_sub   = post[post.cneName == cnec]
        for col in fb_params:
            cf_d = _cf_mean_for(during_sub, col)
            cf_p = _cf_mean_for(post_sub, col)
            r[f"cf_during_{col}"] = cf_d
            r[f"cf_post_{col}"]   = cf_p
            r[f"delta_{col}"]  = round(r.get(f"during_{col}", np.nan)
                                       - r.get(f"pre_{col}", np.nan), 2) \
                if not any(np.isnan(r.get(k, np.nan))
                           for k in [f"during_{col}", f"pre_{col}"]) else np.nan
            r[f"impact_{col}"] = round(r.get(f"during_{col}", np.nan) - cf_d, 2) \
                if not np.isnan(cf_d) else np.nan
            r[f"recovery_{col}"] = round(r.get(f"post_{col}", np.nan) - cf_p, 2) \
                if not np.isnan(cf_p) else np.nan
        cnec_rows.append(r)
    cnec_table = pd.DataFrame(cnec_rows)
    # ── 2. Interrupted time series (ITS) ────────────────────────────────────
    # Counterfactual construction rule (Rubin Potential Outcomes):
    #   Y(0)_t is estimated from PRE-PERIOD data ONLY.
    #   The maintenance interval itself is NEVER used to build Y(0).
    #   Y(0) is then projected into both DURING and POST windows.
    #
    # Three models available (see _ITS_METHODS and _build_its_for_col):
    #   "seasonal_naive" — mean by (hour, weekday) from pre-period [default]
    #   "fourier_trend"  — OLS with Fourier harmonics + linear trend
    #   "stl"            — STL decomposition (Loess), robust to outliers
    #   "all"            — run all three, return its_all dict for comparison
    #
    # Use fall_signed (verified RAM formula input) as primary dep var.
    _its_cols = []
    for c in ["fall_signed", "fall", "ram", "shadowPrice_clean", "shadowPrice"]:
        if c in no3_df.columns and no3_df[c].notna().any():
            if c in ("fall_signed", "fall") and any(x in _its_cols for x in ("fall_signed","fall")):
                continue
            if c in ("shadowPrice_clean","shadowPrice") and any(x in _its_cols for x in ("shadowPrice_clean","shadowPrice")):
                continue
            _its_cols.append(c)
    if "f0" in no3_df.columns and not any(x in _its_cols for x in ("fall_signed","fall")):
        _its_cols.append("f0")

    # Decide which methods to run
    _run_methods = list(_ITS_METHODS.keys()) if its_method == "all" else [its_method]
    if its_method not in _ITS_METHODS and its_method != "all":
        log_cb(f"Unknown ITS method '{its_method}'; falling back to seasonal_naive")
        _run_methods = ["seasonal_naive"]

    log_cb(f"ITS counterfactual model(s): {', '.join(_run_methods)}")

    # seasonal_lookups and pre_col_means used later for per-CNEC table
    seasonal_lookups: dict = {}
    pre_col_means:    dict = {}

    def _run_its_for_method(method_key: str) -> tuple[list, dict, dict]:
        """Run one ITS method across all columns. Returns (rows, its_summary, lookups)."""
        rows = []
        s_summary = {}
        s_lookups = {}
        s_means   = {}
        for col in _its_cols:
            pre_agg = pre.groupby("dateTimeUtc")[col].mean().reset_index()
            if len(pre_agg) < 4:
                continue
            pre_agg["hour"] = pre_agg["dateTimeUtc"].dt.hour
            pre_agg["dow"]  = pre_agg["dateTimeUtc"].dt.dayofweek

            # Always build the seasonal_naive lookup for per-CNEC table reuse
            _sn = pre_agg.groupby(["hour","dow"])[col].mean()
            s_lookups[col] = _sn
            s_means[col]   = float(pre_agg[col].mean())

            all_agg = window.groupby("dateTimeUtc")[col].mean().reset_index()
            all_agg = all_agg.sort_values("dateTimeUtc").reset_index(drop=True)
            all_agg["hour"] = all_agg["dateTimeUtc"].dt.hour
            all_agg["dow"]  = all_agg["dateTimeUtc"].dt.dayofweek

            projected = _build_its_for_col(pre_agg, all_agg, col, method_key, mtu_minutes)
            all_agg["projected"] = projected.values
            all_agg["gap"]    = all_agg[col] - all_agg["projected"]
            all_agg["period"] = "pre"
            all_agg.loc[all_agg.dateTimeUtc >= s, "period"] = "during"
            all_agg.loc[all_agg.dateTimeUtc >= e, "period"] = "post"
            all_agg["param"]  = col
            all_agg["method"] = method_key
            rows.append(all_agg.drop(columns=["hour","dow"]))

            dur_rows  = all_agg[all_agg.period == "during"]
            post_rows = all_agg[all_agg.period == "post"]
            s_summary[col] = {
                "impact":            round(float(dur_rows["gap"].mean()),  2) if not dur_rows.empty  else np.nan,
                "recovery_residual": round(float(post_rows["gap"].mean()), 2) if not post_rows.empty else np.nan,
                "projected_during":  round(float(dur_rows["projected"].mean()),  2) if not dur_rows.empty  else np.nan,
                "projected_post":    round(float(post_rows["projected"].mean()), 2) if not post_rows.empty else np.nan,
                "recovery_frac":     _clamp_recovery_frac(
                    float(dur_rows["gap"].mean()),
                    float(post_rows["gap"].mean()))
                    if (not dur_rows.empty and not post_rows.empty) else np.nan,
                "method":            method_key,
                "method_label":      _ITS_METHODS[method_key]["label"],
            }
        return rows, s_summary, s_lookups, s_means

    its_all: dict = {}   # method_key → DataFrame (when its_method="all")
    its_rows_primary = []
    its_summary: dict = {}

    for method_key in _run_methods:
        rows, s_summary, s_lookups, s_means = _run_its_for_method(method_key)
        method_df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
        its_all[method_key] = method_df

        # The first method (or the single chosen one) is the primary
        if not its_rows_primary:
            its_rows_primary = rows
            its_summary      = s_summary
            seasonal_lookups = s_lookups
            pre_col_means    = s_means

    its = pd.concat(its_rows_primary, ignore_index=True) if its_rows_primary else pd.DataFrame()


    # ── 3. ΔRAM decomposition ───────────────────────────────────────────────
    # Use first CNEC that has both pre and during data
    decomp = pd.DataFrame()
    for cnec in sorted(no3_df.cneName.unique()):
        d = decompose_delta_ram(no3_df, cnec, s, e, baseline_h=baseline_days * 24)
        if not d.empty:
            d.insert(0, "cnec", cnec)
            decomp = pd.concat([decomp, d], ignore_index=True)

    # ── 4. DiD: PTDF_FI × post indicator — DURING excluded from sample ──────
    # KEY PRINCIPLE: the maintenance interval is the treatment.
    # It must NEVER appear in the regression sample used to estimate
    # the post-vs-pre counterfactual.
    #
    # Correct DiD estimand (PRE ∪ POST only):
    #   Y_it = α_i + β_post × d_post_t
    #          + β_int  × |PTDF_FI_i| × d_post_t    ← treatment heterogeneity
    #          + γ_h × hour_h                         ← diurnal FE
    #          + ε_it
    #
    # where d_post_t = 1 for t ≥ outage_end (POST period), 0 for t < outage_start (PRE).
    # DURING rows (outage_start ≤ t < outage_end) are DROPPED from the sample.
    #
    # β_post captures the average post-vs-pre level change (includes recovery).
    # β_int  captures: do high-PTDF CNECs recover more/less than low-PTDF CNECs?
    #   β_int < 0 → high-PTDF CNECs had a larger impact AND show less recovery
    #   β_int ≈ 0 → recovery is PTDF-agnostic (macro shock, not outage-specific)
    #
    # The entity FE α_i are now estimated from PRE + POST only, so they are
    # not contaminated by during-period anomalies.
    did = pd.DataFrame()
    did_estimates = {}
    _ptdf_raw_col = f"ptdf_{src.upper()}"
    if _ptdf_raw_col not in no3_df.columns:
        _ptdf_raw_col = "ptdf_FI"

    # Use PRE-period PTDF to classify CNECs (no post-treatment contamination)
    if _ptdf_raw_col in no3_df.columns:
        ptdf_fi_abs = pre.groupby("cneName")[_ptdf_raw_col].mean().abs()
        if ptdf_fi_abs.empty:
            ptdf_fi_abs = no3_df.groupby("cneName")[_ptdf_raw_col].mean().abs()
    else:
        ptdf_fi_abs = pd.Series(dtype=float)

    if _ptdf_raw_col in no3_df.columns and sm is not None and not ptdf_fi_abs.empty:
        # Build estimation sample: PRE ∪ POST, DURING excluded
        pre_post = pd.concat([pre, post], ignore_index=True)
        pre_post = pre_post.copy()
        pre_post["ptdf_fi_abs"] = pre_post["cneName"].map(ptdf_fi_abs)
        pre_post["d_post"]      = (pre_post.dateTimeUtc >= e).astype(float)
        pre_post["interaction"] = pre_post["ptdf_fi_abs"] * pre_post["d_post"]
        pre_post["hour"]        = pre_post.dateTimeUtc.dt.hour
        pre_post["dow"]         = pre_post.dateTimeUtc.dt.dayofweek

        # Primary dep var: fall_signed if available, else f0
        _did_dep_vars = []
        for _c in ["fall_signed", "f0", "ram", "shadowPrice_clean", "shadowPrice"]:
            if _c in pre_post.columns and pre_post[_c].notna().any():
                if _c in ("shadowPrice_clean","shadowPrice") and any(
                        x in _did_dep_vars for x in ("shadowPrice_clean","shadowPrice")):
                    continue
                _did_dep_vars.append(_c)

        for col in _did_dep_vars:
            sub = pre_post[["cneName","dateTimeUtc", col,
                             "interaction","d_post","ptdf_fi_abs",
                             "hour","dow"]].dropna()
            if len(sub) < 30 or sub["cneName"].nunique() < 2:
                continue
            sub_idx = sub.set_index(["cneName","dateTimeUtc"])
            y = sub_idx[col].astype(float)
            X = sub_idx[["interaction","d_post"]].astype(float)
            X = pd.concat([X, pd.get_dummies(sub_idx["hour"],
                                              prefix="h", drop_first=True).astype(float)],
                          axis=1)
            X = sm.add_constant(X, has_constant="add")
            try:
                from linearmodels.panel import PanelOLS as _PanelOLS
                mod = _PanelOLS(y, X, entity_effects=True, drop_absorbed=True,
                                check_rank=False)
                res = mod.fit(cov_type="clustered", cluster_entity=True)
                beta_int  = float(res.params.get("interaction", np.nan))
                p_int     = float(res.pvalues.get("interaction", np.nan))
                beta_post = float(res.params.get("d_post", np.nan))
                p_post    = float(res.pvalues.get("d_post", np.nan))
                did_estimates[col] = {
                    "beta_interaction": round(beta_int, 4),
                    "p_interaction":    round(p_int, 4),
                    "beta_post":        round(beta_post, 4),
                    "p_post":           round(p_post, 4),
                    "sample":           "PRE ∪ POST (DURING excluded)",
                    "interpretation": (
                        f"Post-vs-pre level change: {beta_post:+.2f} MW "
                        f"({'sign.' if p_post < 0.05 else 'insig.'}); "
                        f"PTDF heterogeneity: {beta_int:+.2f} MW per unit |PTDF| "
                        f"({'sign.' if p_int < 0.05 else 'insig.'})")
                }
            except Exception:
                pass

        # Group-means table (still informative; DURING included only for display)
        med = ptdf_fi_abs.median()
        high_cnecs = ptdf_fi_abs[ptdf_fi_abs >= med].index.tolist()
        low_cnecs  = ptdf_fi_abs[ptdf_fi_abs <  med].index.tolist()
        did_rows = []
        for group, cnec_list in [("high_|PTDF_FI|", high_cnecs),
                                  ("low_|PTDF_FI|",  low_cnecs)]:
            for period_label, pdata in [("pre", pre), ("during", during), ("post", post)]:
                sub = pdata[pdata.cneName.isin(cnec_list)]
                if sub.empty:
                    continue
                for _c in ["fall_signed", "f0", "ram", "shadowPrice"]:
                    if _c in sub.columns:
                        did_rows.append({
                            "group": group, "period": period_label,
                            "param": _c, "mean": float(sub[_c].mean()),
                            "n_cnecs": len(cnec_list)})
        did = pd.DataFrame(did_rows)

    # ── 5. Summary scalars ──────────────────────────────────────────────────
    # Three types of metrics reported:
    #   delta_*          = during_mean   − pre_mean   (raw level shift)
    #   impact_*         = during_actual − Y(0)_during (counterfactual-corrected impact)
    #   recovery_resid_* = post_actual   − Y(0)_post  (how far post deviates from Y(0))
    #   recovery_frac_*  = 1 − |recovery_resid| / |impact|  (1=full, 0=none)
    summary = {
        "outage_id":         outage_row.get("outage_id", ""),
        "asset_name":        outage_row.get("asset_name", ""),
        "asset_type":        outage_row.get("asset_type", ""),
        "planned_or_forced": outage_row.get("planned_or_forced", ""),
        "start_utc":   str(s), "end_utc": str(e),
        "duration_h":  round((e - s).total_seconds() / 3600, 1),
        "n_cnecs":     int(no3_df.cneName.nunique()),
        "n_pre_rows":  len(pre), "n_during_rows": len(during), "n_post_rows": len(post),
    }

    def _safe_mean(df_subset, col):
        if df_subset.empty or col not in df_subset.columns:
            return np.nan
        v = df_subset[col].mean()
        return np.nan if pd.isna(v) else round(float(v), 2)

    # Raw descriptive stats (pre/during/post actual means)
    _summary_cols = ["fall_signed", "fall", "f0", "ram", "shadowPrice_clean",
                     "shadowPrice", "frm", "iva"]
    for col in _summary_cols:
        if col not in no3_df.columns:
            continue
        pm = _safe_mean(pre,    col)
        dm = _safe_mean(during, col)
        qm = _safe_mean(post,   col)
        summary[f"pre_mean_{col}"]    = pm
        summary[f"during_mean_{col}"] = dm
        summary[f"post_mean_{col}"]   = qm
        summary[f"delta_{col}"]       = round(dm - pm, 2) if not (np.isnan(pm) or np.isnan(dm)) else np.nan
        summary[f"recovery_raw_{col}"] = round(qm - pm, 2) if not (np.isnan(pm) or np.isnan(qm)) else np.nan

    # Counterfactual-corrected impact and recovery from ITS seasonal model
    for col, its_s in its_summary.items():
        summary[f"impact_cf_{col}"]         = its_s["impact"]            # during_actual - Y(0)_during
        summary[f"recovery_resid_{col}"]     = its_s["recovery_residual"] # post_actual   - Y(0)_post
        summary[f"recovery_frac_{col}"]      = its_s["recovery_frac"]     # 1=full, 0=none
        summary[f"projected_during_{col}"]   = its_s["projected_during"]  # Y(0)_during
        summary[f"projected_post_{col}"]     = its_s["projected_post"]    # Y(0)_post

    summary["did_estimates"]  = did_estimates
    summary["its_summary"]    = its_summary

    _log_col = next((c for c in ["fall_signed","fall","f0"] if c in no3_df.columns), None)
    log_cb(f"Single event analysis complete: {summary['asset_name']} | "
           f"impact={summary.get(f'impact_cf_{_log_col}', summary.get('delta_f0','n/a'))} MW | "
           f"ΔRAM={summary.get('delta_ram', summary.get('delta_fall_signed','n/a'))} MW | "
           f"recovery={summary.get(f'recovery_frac_{_log_col}','n/a')} frac")

    return {"summary": summary, "its": its, "its_all": its_all,
            "decomp": decomp, "did": did, "cnec_table": cnec_table,
            "its_summary": its_summary}

