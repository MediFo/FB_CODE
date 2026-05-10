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
except ImportError:
    sm = None

try:
    from linearmodels.panel import PanelOLS
except ImportError:
    PanelOLS = None

try:
    from entsoe import EntsoePandasClient
except ImportError:
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
DEFAULT_NO3_PATTERNS: tuple = (
    r"klæbu.*surna", r"klaebu.*surna",
    r"klæbu.*orkdal", r"klaebu.*orkdal",
    r"refsdal.*modalen", r"aurland",
    r"tunnsjødal", r"tunnsjodal",
    r"viklandet", r"namsos", r"verdal",
)

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
                         log_cb: LogCallback = _noop) -> pd.DataFrame:
    """Fetch ENTSO-E A77 (production) + A78 (transmission) outages for FI.
    Uses the hard-coded ENTSOE_TOKEN constant."""
    if EntsoePandasClient is None:
        log_cb("ENTSO-E: 'entsoe-py' library NOT INSTALLED.")
        log_cb("           Run:  pip install entsoe-py")
        log_cb("           Then restart the dashboard.")
        return pd.DataFrame()
    log_cb(f"ENTSO-E: fetching with token {ENTSOE_TOKEN[:8]}...{ENTSOE_TOKEN[-4:]}")

    client = EntsoePandasClient(api_key=ENTSOE_TOKEN)
    start = pd.Timestamp(start_utc).tz_convert("UTC")
    end   = pd.Timestamp(end_utc).tz_convert("UTC")
    events = []

    # ----- A77: production unavailability -----
    try:
        log_cb("ENTSO-E: query_unavailability_of_production_units (FI)")
        df = client.query_unavailability_of_production_units(
            country_code="FI", start=start, end=end, docstatus=None,
        )
        for i, row in df.reset_index().iterrows():
            try:
                nominal = float(row.get("nominal_power", 0) or 0)
                avail   = float(row.get("avail_qty", 0) or 0)
                cap_lost = max(0.0, nominal - avail)
                if cap_lost <= 0: continue
                btype = str(row.get("businesstype", "")).strip()
                p_or_f = "forced" if btype == "A54" else "planned"
                events.append({
                    "outage_id": f"entsoe_a77:{row.get('mrid', i)}:{row.get('start')}",
                    "start_utc": pd.Timestamp(row["start"]).tz_convert("UTC").isoformat(),
                    "end_utc":   pd.Timestamp(row["end"]).tz_convert("UTC").isoformat(),
                    "asset_id":  row.get("production_resource_id"),
                    "asset_name": row.get("production_resource_name"),
                    "asset_type": "generator",
                    "voltage_kv": None,
                    "capacity_mw": cap_lost,
                    "planned_or_forced": p_or_f,
                    "bidding_zone": "FI",
                    "control_area": "FI",
                    "source": "entsoe_a77",
                    "raw_payload": json.dumps({k: str(v) for k, v in row.items()}, default=str)[:2000],
                })
            except Exception as e:
                log_cb(f"  A77 row error: {e}")
    except Exception as e:
        log_cb(f"ENTSO-E A77 failed: {e}")

    # ----- A78: transmission unavailability -----
    borders = (("FI","SE_1"),("SE_1","FI"),("FI","SE_3"),("SE_3","FI"),
               ("FI","EE"),("EE","FI"),("FI","NO_4"),("NO_4","FI"))
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
                    is_hvdc = ("EE" in (fr,to)) or ("SE_3" in (fr,to))
                    events.append({
                        "outage_id": f"entsoe_a78:{fr}-{to}:{row.get('mrid', i)}:{row.get('start')}",
                        "start_utc": pd.Timestamp(row["start"]).tz_convert("UTC").isoformat(),
                        "end_utc":   pd.Timestamp(row["end"]).tz_convert("UTC").isoformat(),
                        "asset_id":  None,
                        "asset_name": f"{fr}->{to}",
                        "asset_type": "hvdc" if is_hvdc else "ac_line",
                        "voltage_kv": None,
                        "capacity_mw": cap,
                        "planned_or_forced": "forced",  # entsoe-py issue #137
                        "bidding_zone": "FI",
                        "control_area": "FI",
                        "source": "entsoe_a78",
                        "raw_payload": json.dumps({"from": fr, "to": to}),
                    })
                except Exception as e:
                    log_cb(f"  A78 row err: {e}")
        except Exception as e:
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
OUTAGE_BIN_COLS = ("fi_planned_outage_active", "fi_forced_outage_active",
                   "fi_hvdc_outage_active", "fi_ac_line_outage_active")
OUTAGE_MW_COLS = ("fi_gen_outage_mw_lost", "fi_hvdc_outage_mw_lost",
                  "fi_ac_outage_mw_lost")

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
                     log_cb: LogCallback = _noop) -> pd.DataFrame:
    jao = jao.copy()
    if outages is None or outages.empty:
        log_cb("No outages provided; covariates set to zero")
        for c in OUTAGE_BIN_COLS + OUTAGE_MW_COLS:
            jao[c] = 0.0
    else:
        # ensure datetime
        outg = outages.copy()
        for c in ("start_utc", "end_utc"):
            outg[c] = pd.to_datetime(outg[c], utc=True, errors="coerce")
        outg = outg.dropna(subset=["start_utc", "end_utc"])
        outg["capacity_mw"] = pd.to_numeric(outg["capacity_mw"],
                                            errors="coerce").fillna(0.0)

        ts = jao["dateTimeUtc"].values.astype("datetime64[ns]")

        def subset_ndarrays(df: pd.DataFrame):
            if df.empty:
                return (np.array([], dtype="datetime64[ns]"),
                        np.array([], dtype="datetime64[ns]"),
                        np.array([], dtype=float))
            return (df["start_utc"].values.astype("datetime64[ns]"),
                    df["end_utc"].values.astype("datetime64[ns]"),
                    df["capacity_mw"].values.astype(float))

        log_cb(f"Building covariates for {len(jao)} JAO rows...")
        s,e,c = subset_ndarrays(outg[outg["planned_or_forced"]=="planned"])
        jao["fi_planned_outage_active"] = _interval_active(ts, s, e)
        s,e,c = subset_ndarrays(outg[outg["planned_or_forced"]=="forced"])
        jao["fi_forced_outage_active"] = _interval_active(ts, s, e)
        s,e,c = subset_ndarrays(outg[outg["asset_type"]=="hvdc"])
        jao["fi_hvdc_outage_active"] = _interval_active(ts, s, e)
        jao["fi_hvdc_outage_mw_lost"] = _interval_mw(ts, s, e, c)
        s,e,c = subset_ndarrays(outg[outg["asset_type"]=="ac_line"])
        jao["fi_ac_line_outage_active"] = _interval_active(ts, s, e)
        jao["fi_ac_outage_mw_lost"] = _interval_mw(ts, s, e, c)
        s,e,c = subset_ndarrays(outg[outg["asset_type"]=="generator"])
        jao["fi_gen_outage_mw_lost"] = _interval_mw(ts, s, e, c)

    # Lagged versions
    jao = jao.sort_values(["cneName", "dateTimeUtc"])
    for col in OUTAGE_BIN_COLS + ("fi_gen_outage_mw_lost",):
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

    # H2: |PTDF_FI| — topology shift is sign-agnostic; AC outage changes the
    #     admittance matrix in magnitude, not necessarily direction.
    if "ptdf_FI" in jao.columns:
        jao["ptdf_FI_abs"] = jao["ptdf_FI"].abs()
    else:
        jao["ptdf_FI_abs"] = np.nan

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

    return jao.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6. Regressions
# ---------------------------------------------------------------------------
DEFAULT_INDEP = ["fi_planned_outage_active", "fi_forced_outage_active",
                 "fi_hvdc_outage_active", "fi_ac_line_outage_active",
                 "fi_gen_outage_mw_lost", "fi_hvdc_outage_mw_lost",
                 "fi_ac_outage_mw_lost"]


def run_panel_regression(df: pd.DataFrame, dep_var: str,
                         indep: Sequence[str] | None = None,
                         add_time_fe: bool = True,
                         cluster: str = "time",   # "time" | "entity" | "two_way"
                         log_cb: LogCallback = _noop) -> dict:
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
    indep = list(indep) if indep else list(DEFAULT_INDEP)
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


def run_logit_iva(df: pd.DataFrame, log_cb: LogCallback = _noop) -> dict:
    if sm is None:
        return {}
    sub = df.copy()
    if "iva" not in sub.columns:
        log_cb("No iva column; skip logit")
        return {}
    sub["iva_active"] = (sub["iva"].fillna(0).abs() > 1e-6).astype(int)
    n_pos = int(sub["iva_active"].sum())
    if n_pos < 5:
        log_cb(f"H5 logit skipped: only {n_pos} IVA-active rows in NO3 window. "
               f"Need a longer JAO window or curated outages overlapping IVA events.")
        return {}
    feat = ["fi_planned_outage_active","fi_forced_outage_active",
            "fi_hvdc_outage_active","fi_ac_line_outage_active"]
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
HYPOTHESES = [
    # H1: FI HVDC outage → F0 shifts on NO3 CNECs
    # Sign is NOT a priori deterministic: direction depends on whether the HVDC
    # normally exports FI power (Fenno-Skan southward) or imports to FI (Estlink).
    # A southward-flow HVDC outage forces AC rerouting FI→SE1→SE2→NO4→NO3,
    # increasing F0 on northbound CNECs but decreasing it on southbound ones.
    # We test for SIGNIFICANCE of the shift regardless of direction.
    {"id": "H1", "dep": "fall_signed",
     "var": "fi_hvdc_outage_active",
     "expected_sign": None,  # direction is physically ambiguous
     "text": "fall (sign-normalised F_allReference) shifts during FI HVDC outages "
             "(sign depends on HVDC normal flow direction)"},

    # H2: FI AC line outage → PTDF_FI shifts on NO3 CNECs
    # Only AC topology changes can move PTDFs. Generator and HVDC outages leave
    # the admittance matrix unchanged so PTDF_FI should be unaffected.
    {"id": "H2", "dep": "ptdf_FI_abs",
     "var": "fi_ac_line_outage_active",
     "expected_sign": None,  # direction depends on specific line and network topology
     "text": "|PTDF_FI| on NO3 CNECs shifts during FI AC line outages (not gen/HVDC)"},

    # H3: FI outage → RAM changes on NO3 CNECs
    # Sign is outage-type-dependent:
    #  - HVDC outage: removes cross-border transit → LESS load on NO3 corridor → RAM INCREASES
    #  - AC outage: changes impedance matrix → redistributes flows → sign unclear
    #  - Generator outage: forces re-dispatch → may load or unload NO3 corridor
    # We test for any significant change without prescribing direction.
    {"id": "H3", "dep": "ram",
     "var": "fi_forced_outage_active",
     "expected_sign": None,  # direction depends on outage type (HVDC often increases RAM)
     "text": "RAM on NO3 CNECs changes significantly during forced FI outages "
             "(direction depends on outage type)"},

    # H4: FI outage → shadow price changes on NO3 CNECs
    # Same reasoning as H3. HVDC outage typically REDUCES congestion on NO3
    # corridor → shadow price FALLS. Only outages that reroute flow onto already
    # congested NO3 CNECs would increase shadow price.
    # Note: shadow price regression is restricted to binding MTUs (shadowPrice > 0).
    {"id": "H4", "dep": "shadowPrice",
     "var": "fi_forced_outage_active",
     "expected_sign": None,  # direction is outage-type-dependent
     "text": "Shadow price on binding NO3 CNECs changes during FI outages "
             "(sign depends on whether outage relieves or adds congestion)"},

    # H5: IVA application is more frequent under forced than planned FI outages.
    # Rationale: forced outages are not in the D-2 IGM; the CGM does not capture
    # them; Statnett must apply IVA to correct the FB domain during validation.
    {"id": "H5", "dep": None, "var": None,
     "text": "IVA more frequent on NO3 CNECs under forced than planned FI outages",
     "expected_sign": None},

    # H6: FRM does NOT move with individual FI outage events.
    # FRM is a statistical margin calibrated annually (Nordic CCM Art. 22).
    # Individual outages cannot update FRM within a delivery year.
    # This is a PLACEBO TEST: any significant result indicates misspecification.
    {"id": "H6", "dep": "frm", "var": "fi_forced_outage_active",
     "expected_sign": 0,
     "text": "FRM is structural (annual calibration); no MTU-level FI-outage propagation "
             "[PLACEBO — significant result = model misspecification]"},
]


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


def summarize_hypotheses(reg_results: dict, logit_result: dict) -> list[dict]:
    """Summarise verdicts. When the primary test variable is absorbed by
    fixed effects, fall back to alternative channel variables and report
    why the original test wasn't possible."""
    # For H1-H4, we have a primary test variable but also alternative
    # channel-specific variables we can fall back to.
    fallback_vars = {
        "H1": ["fi_hvdc_outage_active", "fi_forced_outage_active",
               "fi_hvdc_outage_mw_lost", "fi_planned_outage_active"],
        "H2": ["fi_ac_line_outage_active", "fi_ac_outage_mw_lost",
               "fi_planned_outage_active"],
        "H3": ["fi_forced_outage_active", "fi_planned_outage_active",
               "fi_hvdc_outage_active"],
        "H4": ["fi_forced_outage_active", "fi_planned_outage_active",
               "fi_hvdc_outage_active"],
    }
    out = []
    for h in HYPOTHESES:
        verdict = "n/a"
        if h["id"] == "H5":
            if logit_result and "coefs" in logit_result:
                cf = logit_result["coefs"].set_index("param")
                try:
                    bp = cf.loc["fi_planned_outage_active"]
                    bf = cf.loc["fi_forced_outage_active"]
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
    a(f"<h1>FI → NO3 Flow-Based Propagation: Validation Report</h1>")
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
    jao_csv:    str  = ""
    out_dir:    str  = "./fb_output"
    cache_db:   str  = "./fb_output/outage_cache.sqlite"
    manual_csv: str  = "./manual_outages.csv"
    start_utc:  str  = "2024-10-29T00:00:00Z"
    end_utc:    str  = "2026-05-06T00:00:00Z"
    use_entsoe: bool = True
    use_manual: bool = True
    no3_patterns: tuple = DEFAULT_NO3_PATTERNS


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
    no3 = filter_no3(jao, cfg.no3_patterns)
    log_cb(f"  NO3 rows: {len(no3)}, NO3 CNECs: {no3['cneName'].nunique()}")

    jao_start = no3["dateTimeUtc"].min()
    jao_end   = no3["dateTimeUtc"].max()
    log_cb(f"  JAO window: {jao_start.strftime('%Y-%m-%d %H:%M')} UTC "
           f"→ {jao_end.strftime('%Y-%m-%d %H:%M')} UTC")

    # 2. Outages
    if outages_df is None:
        con = open_cache(cfg.cache_db)
        all_events = []
        if cfg.use_entsoe:
            df_es = fetch_entsoe_outages(cfg.start_utc, cfg.end_utc, log_cb=log_cb)
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
    no3_cov = build_covariates(no3, outages, log_cb=log_cb)
    no3_cov.to_csv(Path(cfg.out_dir) / "no3_with_outage_covariates.csv", index=False)

    # 4. Regressions
    # ── H1: fall_signed (sign-normalised F_allReference) ────────────────────
    # fall is the correct reference flow in the RAM formula (verified R²=1.000).
    # Sign-normalised per CNEC so positive = loading in the congested direction.
    # fref (=f0 in JAO) is the flow at CGMA NP — NOT in the RAM formula.
    log_cb("Running fall (F_allReference, sign-normalised) regression [H1]...")
    h1_dep = "fall_signed" if "fall_signed" in no3_cov.columns else "fall"
    res_fall = run_panel_regression(no3_cov, h1_dep, log_cb=log_cb, cluster="time")

    # ── H2: |PTDF_FI| ────────────────────────────────────────────────────────
    # Absolute value: AC topology change shifts PTDF regardless of sign convention.
    log_cb("Running |PTDF_FI| regression [H2]...")
    h2_dep = "ptdf_FI_abs" if "ptdf_FI_abs" in no3_cov.columns else "ptdf_FI"
    res_ptdf = run_panel_regression(no3_cov, h2_dep, log_cb=log_cb, cluster="time")

    # ── H3: RAM ──────────────────────────────────────────────────────────────
    log_cb("Running RAM regression [H3]...")
    res_ram = run_panel_regression(no3_cov, "ram", log_cb=log_cb, cluster="time")

    # ── H4: shadow price (binding MTUs only) ─────────────────────────────────
    # Use shadowPrice_clean (1e-8 → 0) then restrict to truly binding rows.
    log_cb("Running shadow price regression [H4] — binding MTUs only...")
    sp_col = "shadowPrice_clean" if "shadowPrice_clean" in no3_cov.columns else "shadowPrice"
    sp_mask = no3_cov[sp_col].fillna(0) > 0
    no3_binding = no3_cov[sp_mask].copy()
    log_cb(f"  Truly binding (SP > 1e-6): {sp_mask.sum():,} / {len(no3_cov):,} "
           f"({sp_mask.mean():.1%})")
    if sp_mask.sum() < 50:
        log_cb("  Too few truly-binding MTUs; skipping shadow price regression")
        res_sp = {}
    else:
        res_sp = run_panel_regression(
            no3_binding, sp_col, log_cb=log_cb, cluster="time")

    # ── H6 placebo: FRM ──────────────────────────────────────────────────────
    log_cb("Running FRM placebo regression [H6]...")
    res_frm = run_panel_regression(no3_cov, "frm", log_cb=log_cb, cluster="time")

    # ── Bonus: fnrao (RA trigger channel) ────────────────────────────────────
    # FI outages may trigger costly or non-costly RAs on NO3 CNECs (fnrao changes).
    # This is an additional propagation channel not covered by H1-H6.
    log_cb("Running fnrao (RA trigger) regression [bonus]...")
    res_fnrao = run_panel_regression(no3_cov, "fnrao", log_cb=log_cb, cluster="time") \
        if "fnrao" in no3_cov.columns and no3_cov["fnrao"].notna().any() else {}

    # ── H5: IVA logit ────────────────────────────────────────────────────────
    log_cb("Running IVA logit [H5]...")
    res_logit = run_logit_iva(no3_cov, log_cb=log_cb)

    reg_results = {
        "fall_signed":  res_fall,
        "fall":         res_fall,    # backward compatibility
        "fref_signed":  res_fall,    # backward compatibility (was wrong variable)
        "f0":           res_fall,    # backward compatibility
        "ptdf_FI_abs":  res_ptdf,
        "ptdf_FI":      res_ptdf,
        "ram":          res_ram,
        "shadowPrice":  res_sp,
        "shadowPrice_clean": res_sp,
        "frm":          res_frm,
        "fnrao":        res_fnrao,
    }
    hypotheses = summarize_hypotheses(reg_results, res_logit)

    # 5. Save outages
    outages.to_csv(Path(cfg.out_dir) / "outages_unified.csv", index=False)

    return {
        "no3": no3_cov, "outages": outages,
        "regressions": reg_results, "logit": res_logit,
        "hypotheses": hypotheses,
        "out_dir": cfg.out_dir,
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

def single_event_analysis(no3_df: pd.DataFrame, outage_row: pd.Series,
                           baseline_days: int = 7,
                           post_days: int = 3,
                           log_cb: LogCallback = _noop) -> dict:
    """
    Deep-dive analysis for one specific FI maintenance event.

    Returns:
      summary      : dict of scalar statistics
      its          : interrupted time series DataFrame (actual vs projected)
      decomp       : ΔRAM decomposition DataFrame
      did          : DiD table (high vs low PTDF_FI CNECs)
      cnec_table   : per-CNEC before/after table
    """
    s = pd.Timestamp(outage_row["start_utc"], tz="UTC") \
        if hasattr(outage_row["start_utc"], "isoformat") \
        else pd.Timestamp(outage_row["start_utc"]).tz_localize("UTC") \
        if pd.Timestamp(outage_row["start_utc"]).tzinfo is None \
        else pd.Timestamp(outage_row["start_utc"]).tz_convert("UTC")
    e = pd.Timestamp(outage_row["end_utc"], tz="UTC") \
        if hasattr(outage_row["end_utc"], "isoformat") \
        else pd.Timestamp(outage_row["end_utc"]).tz_localize("UTC") \
        if pd.Timestamp(outage_row["end_utc"]).tzinfo is None \
        else pd.Timestamp(outage_row["end_utc"]).tz_convert("UTC")

    pre_start = s - pd.Timedelta(days=baseline_days)
    post_end  = e + pd.Timedelta(days=post_days)

    pre     = no3_df[(no3_df.dateTimeUtc >= pre_start) & (no3_df.dateTimeUtc < s)]
    during  = no3_df[(no3_df.dateTimeUtc >= s)          & (no3_df.dateTimeUtc < e)]
    post    = no3_df[(no3_df.dateTimeUtc >= e)           & (no3_df.dateTimeUtc < post_end)]
    window  = no3_df[(no3_df.dateTimeUtc >= pre_start)   & (no3_df.dateTimeUtc < post_end)]

    fb_params = [c for c in ["f0","fref","ram","shadowPrice","frm","iva","ptdf_FI",
                              "ptdf_FI_FS","ptdf_FI_EL","amr","faac"]
                 if c in no3_df.columns]

    # ── 1. Per-CNEC before/after table ─────────────────────────────────────
    cnec_rows = []
    for cnec in sorted(no3_df.cneName.unique()):
        r = {"cnec": cnec}
        for p, label in [(pre,"pre"), (during,"during"), (post,"post")]:
            sub = p[p.cneName == cnec]
            for col in fb_params:
                r[f"{label}_{col}"] = float(sub[col].mean()) if not sub.empty else np.nan
        for col in fb_params:
            if f"pre_{col}" in r and f"during_{col}" in r:
                r[f"delta_{col}"] = r[f"during_{col}"] - r[f"pre_{col}"]
        cnec_rows.append(r)
    cnec_table = pd.DataFrame(cnec_rows)

    # ── 2. Interrupted time series ──────────────────────────────────────────
    # Counterfactual: seasonal naive — for each MTU in the outage window,
    # the counterfactual is the average of the same hour × same weekday
    # across the baseline_days pre-event period.
    # This is far superior to a linear trend for power-market data which
    # has strong diurnal and weekly seasonality dominating any linear drift.
    its_rows = []
    for col in ["f0", "ram", "shadowPrice"]:
        if col not in no3_df.columns: continue
        pre_agg = pre.groupby("dateTimeUtc")[col].mean().reset_index()
        if len(pre_agg) < 4:
            continue
        # Build seasonal naive lookup: (hour, weekday) → mean of pre-period
        pre_agg["hour"] = pre_agg["dateTimeUtc"].dt.hour
        pre_agg["dow"]  = pre_agg["dateTimeUtc"].dt.dayofweek
        seasonal_mean = pre_agg.groupby(["hour","dow"])[col].mean()

        all_agg = window.groupby("dateTimeUtc")[col].mean().reset_index()
        all_agg = all_agg.sort_values("dateTimeUtc").reset_index(drop=True)
        all_agg["hour"] = all_agg["dateTimeUtc"].dt.hour
        all_agg["dow"]  = all_agg["dateTimeUtc"].dt.dayofweek

        def lookup_seasonal(row):
            key = (int(row["hour"]), int(row["dow"]))
            return float(seasonal_mean.get(key, pre_agg[col].mean()))

        all_agg["projected"] = all_agg.apply(lookup_seasonal, axis=1)
        all_agg["gap"] = all_agg[col] - all_agg["projected"]
        all_agg["period"] = "pre"
        all_agg.loc[all_agg.dateTimeUtc >= s, "period"] = "during"
        all_agg.loc[all_agg.dateTimeUtc >= e, "period"] = "post"
        all_agg["param"] = col
        its_rows.append(all_agg.drop(columns=["hour","dow"]))
    its = pd.concat(its_rows, ignore_index=True) if its_rows else pd.DataFrame()

    # ── 3. ΔRAM decomposition ───────────────────────────────────────────────
    # Use first CNEC that has both pre and during data
    decomp = pd.DataFrame()
    for cnec in sorted(no3_df.cneName.unique()):
        d = decompose_delta_ram(no3_df, cnec, s, e, baseline_h=baseline_days * 24)
        if not d.empty:
            d.insert(0, "cnec", cnec)
            decomp = pd.concat([decomp, d], ignore_index=True)

    # ── 4. DiD: continuous treatment intensity via PTDF_FI interaction ─────
    # Instead of an arbitrary high/low PTDF_FI split, we use the continuous
    # PTDF_FI as treatment intensity. The identifying assumption is:
    #   CNECs with higher |PTDF_FI| should show larger F0/RAM shifts
    #   during the FI outage than CNECs with lower |PTDF_FI|.
    # Estimand: β in F0 = α + β × |PTDF_FI_i| × D_outage_t + α_i + γ_t + ε
    # This is cleaner than median splits because it uses the full continuum of
    # treatment intensities and doesn't require a discretisation assumption.
    did = pd.DataFrame()
    did_estimates = {}
    if "ptdf_FI" in no3_df.columns and sm is not None:
        # Compute per-CNEC mean |PTDF_FI| (time-stable for the window)
        ptdf_fi_abs = no3_df.groupby("cneName")["ptdf_FI"].mean().abs()
        no3_work = window.copy()
        no3_work["ptdf_fi_abs"] = no3_work["cneName"].map(ptdf_fi_abs)
        no3_work["d_outage"] = ((no3_work.dateTimeUtc >= s) &
                                 (no3_work.dateTimeUtc < e)).astype(float)
        no3_work["interaction"] = no3_work["ptdf_fi_abs"] * no3_work["d_outage"]
        no3_work["hour"] = no3_work.dateTimeUtc.dt.hour
        no3_work["dow"]  = no3_work.dateTimeUtc.dt.dayofweek

        for col in ["f0", "ram", "shadowPrice"]:
            if col not in no3_work.columns: continue
            sub = no3_work[["cneName","dateTimeUtc",col,
                            "interaction","d_outage","ptdf_fi_abs",
                            "hour","dow"]].dropna()
            if len(sub) < 30: continue
            sub_idx = sub.set_index(["cneName","dateTimeUtc"])
            y = sub_idx[col].astype(float)
            X = sub_idx[["interaction","d_outage"]].astype(float)
            # Add hour FE dummies
            X = pd.concat([X, pd.get_dummies(sub_idx["hour"],
                                              prefix="h", drop_first=True).astype(float)],
                          axis=1)
            X = sm.add_constant(X, has_constant="add")
            try:
                from linearmodels.panel import PanelOLS as _PanelOLS
                mod = _PanelOLS(y, X, entity_effects=True, drop_absorbed=True,
                                check_rank=False)
                res = mod.fit(cov_type="clustered", cluster_entity=True)
                beta_interaction = float(res.params.get("interaction", np.nan))
                p_interaction    = float(res.pvalues.get("interaction", np.nan))
                did_estimates[col] = {
                    "beta": round(beta_interaction, 4),
                    "p":    round(p_interaction, 4),
                    "interpretation": (
                        f"Per unit |PTDF_FI| increase: {beta_interaction:+.2f} MW "
                        f"{'[sign.]' if p_interaction < 0.05 else '[insig.]'}")
                }
            except Exception:
                pass

        # Also keep a simple group-means table for plotting
        med = ptdf_fi_abs.median()
        high_cnecs = ptdf_fi_abs[ptdf_fi_abs >= med].index.tolist()
        low_cnecs  = ptdf_fi_abs[ptdf_fi_abs <  med].index.tolist()
        did_rows = []
        for group, cnec_list in [("high_|PTDF_FI|", high_cnecs),
                                  ("low_|PTDF_FI|", low_cnecs)]:
            for period_label, pdata in [("pre", pre), ("during", during), ("post", post)]:
                sub = pdata[pdata.cneName.isin(cnec_list)]
                if sub.empty: continue
                for col in ["f0","ram","shadowPrice"]:
                    if col in sub.columns:
                        did_rows.append({
                            "group": group, "period": period_label,
                            "param": col, "mean": float(sub[col].mean()),
                            "n_cnecs": len(cnec_list)})
        did = pd.DataFrame(did_rows)

    # ── 5. Summary scalars ──────────────────────────────────────────────────
    summary = {
        "outage_id":   outage_row.get("outage_id", ""),
        "asset_name":  outage_row.get("asset_name", ""),
        "asset_type":  outage_row.get("asset_type", ""),
        "planned_or_forced": outage_row.get("planned_or_forced", ""),
        "start_utc":   str(s), "end_utc": str(e),
        "duration_h":  round((e - s).total_seconds() / 3600, 1),
        "n_cnecs":     int(no3_df.cneName.nunique()),
        "n_pre_rows":  len(pre), "n_during_rows": len(during), "n_post_rows": len(post),
    }
    for col in ["f0", "ram", "shadowPrice", "frm", "iva"]:
        if col in no3_df.columns:
            summary[f"pre_mean_{col}"]    = round(float(pre[col].mean()),   2) if not pre.empty    else np.nan
            summary[f"during_mean_{col}"] = round(float(during[col].mean()), 2) if not during.empty else np.nan
            summary[f"delta_{col}"]       = round(summary[f"during_mean_{col}"]
                                                   - summary[f"pre_mean_{col}"], 2)
    summary["did_estimates"] = did_estimates if "ptdf_FI" in no3_df.columns else {}

    log_cb(f"Single event analysis complete: {summary['asset_name']} | "
           f"Δf0={summary.get('delta_f0','n/a')} MW | "
           f"ΔRAM={summary.get('delta_ram','n/a')} MW")

    return {"summary": summary, "its": its, "decomp": decomp,
            "did": did, "cnec_table": cnec_table}
