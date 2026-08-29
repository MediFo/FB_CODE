"""
fi_no3_synthetic.py
===================
Synthetic JAO-format data and outage event generator for testing the
FI -> NO3 propagation pipeline without real JAO/Fingrid/ENTSO-E access.

The generator produces a plausible JAO CSV (15-min MTUs, NO3 corridor
CNECs) with simulated FI outages whose effects are deliberately encoded
in F0, PTDF_FI, FI_FS PTDF, RAM, shadowPrice and IVA, so the validation
regressions should detect them with the expected signs.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from pathlib import Path


NO3_CNECS = [
    ("420 Klaebu-Surna + 300 Klaebu-Orkdal", "NO3", "NO4"),
    ("300 Refsdal-Modalen + Aurland1 T4",    "NO5", "NO3"),
    ("420 Tunnsjodal-Verdal",                "NO3", "NO4"),
    ("420 Viklandet-Aura",                   "NO3", "NO4"),
    ("300 Namsos-Roan",                      "NO4", "NO3"),
]

ALL_ZONES = ["NO1","NO2","NO3","NO4","NO5","SE1","SE2","SE3","SE4",
             "FI","DK1","DK2","FI_FS","FI_EL","SE3_FS"]


def _seed(rng_seed: int) -> np.random.Generator:
    return np.random.default_rng(rng_seed)


def generate_outage_events(start: datetime, end: datetime,
                           rng_seed: int = 42) -> pd.DataFrame:
    """Create a small but meaningful set of FI outage events spanning the window."""
    rng = _seed(rng_seed)
    total_days = (end - start).days
    events = []

    # 1. Planned FI AC line outage (ID-able propagation through PTDF_FI)
    s1 = start + timedelta(days=int(total_days * 0.20),
                            hours=int(rng.integers(0, 24)))
    e1 = s1 + timedelta(hours=72)
    events.append({
        "outage_id": "synthetic:fi_ac_planned_1",
        "start_utc": s1.isoformat(),
        "end_utc":   e1.isoformat(),
        "asset_id":  "SYN_AC_001",
        "asset_name": "FI 400 kV internal line (Pyhanselka-Petajaskoski)",
        "asset_type": "ac_line",
        "voltage_kv": 400.0,
        "capacity_mw": 1300.0,
        "planned_or_forced": "planned",
        "bidding_zone": "FI",
        "control_area": "FI",
        "source": "manual",
        "raw_payload": "{}",
    })

    # 2. Forced FI generator outage (large, drives F0 strongly)
    s2 = start + timedelta(days=int(total_days * 0.45))
    e2 = s2 + timedelta(hours=18)
    events.append({
        "outage_id": "synthetic:fi_gen_forced_1",
        "start_utc": s2.isoformat(),
        "end_utc":   e2.isoformat(),
        "asset_id":  "SYN_GEN_OL3",
        "asset_name": "FI nuclear unit (Olkiluoto-3 simulated)",
        "asset_type": "generator",
        "voltage_kv": None,
        "capacity_mw": 1600.0,
        "planned_or_forced": "forced",
        "bidding_zone": "FI",
        "control_area": "FI",
        "source": "manual",
        "raw_payload": "{}",
    })

    # 3. Planned HVDC outage (Fenno-Skan), drives FI_FS PTDF -> 0
    s3 = start + timedelta(days=int(total_days * 0.65))
    e3 = s3 + timedelta(hours=48)
    events.append({
        "outage_id": "synthetic:fennoskan_planned",
        "start_utc": s3.isoformat(),
        "end_utc":   e3.isoformat(),
        "asset_id":  "SYN_HVDC_FS",
        "asset_name": "Fenno-Skan PTC simulated",
        "asset_type": "hvdc",
        "voltage_kv": 400.0,
        "capacity_mw": 800.0,
        "planned_or_forced": "planned",
        "bidding_zone": "FI",
        "control_area": "FI",
        "source": "manual",
        "raw_payload": "{}",
    })

    # 4. Forced HVDC outage (Estlink) - shorter
    s4 = start + timedelta(days=int(total_days * 0.80))
    e4 = s4 + timedelta(hours=12)
    events.append({
        "outage_id": "synthetic:estlink_forced",
        "start_utc": s4.isoformat(),
        "end_utc":   e4.isoformat(),
        "asset_id":  "SYN_HVDC_EL",
        "asset_name": "Estlink simulated",
        "asset_type": "hvdc",
        "voltage_kv": 350.0,
        "capacity_mw": 650.0,
        "planned_or_forced": "forced",
        "bidding_zone": "FI",
        "control_area": "FI",
        "source": "manual",
        "raw_payload": "{}",
    })

    # 5. Smaller forced FI gen outage
    s5 = start + timedelta(days=int(total_days * 0.30))
    e5 = s5 + timedelta(hours=8)
    events.append({
        "outage_id": "synthetic:fi_gen_forced_2",
        "start_utc": s5.isoformat(),
        "end_utc":   e5.isoformat(),
        "asset_id":  "SYN_GEN_LV",
        "asset_name": "FI Loviisa simulated",
        "asset_type": "generator",
        "voltage_kv": None,
        "capacity_mw": 500.0,
        "planned_or_forced": "forced",
        "bidding_zone": "FI",
        "control_area": "FI",
        "source": "manual",
        "raw_payload": "{}",
    })

    return pd.DataFrame(events)


def _interval_active_simple(times: np.ndarray, starts: np.ndarray,
                             ends: np.ndarray) -> np.ndarray:
    if len(starts) == 0:
        return np.zeros(len(times), dtype=int)
    out = np.zeros(len(times), dtype=int)
    for s, e in zip(starts, ends):
        out[(times >= s) & (times < e)] = 1
    return out


def generate_jao_csv(start: datetime, end: datetime,
                     outages: pd.DataFrame,
                     out_path: str, rng_seed: int = 7) -> pd.DataFrame:
    """Generate a synthetic JAO CSV with realistic FB params and embedded outage effects."""
    rng = _seed(rng_seed)
    if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo   is None: end   = end.replace(tzinfo=timezone.utc)
    timestamps = pd.date_range(start=start, end=end, freq="15min", tz="UTC", inclusive="left")
    n = len(timestamps)

    # Pre-compute outage indicators per timestamp (numpy arrays)
    out = outages.copy()
    out["start_utc"] = pd.to_datetime(out["start_utc"], utc=True)
    out["end_utc"]   = pd.to_datetime(out["end_utc"], utc=True)

    ts_np = timestamps.values.astype("datetime64[ns]")
    is_ac    = _interval_active_simple(
        ts_np,
        out.loc[out["asset_type"]=="ac_line","start_utc"].values.astype("datetime64[ns]"),
        out.loc[out["asset_type"]=="ac_line","end_utc"].values.astype("datetime64[ns]"))
    is_gen   = _interval_active_simple(
        ts_np,
        out.loc[out["asset_type"]=="generator","start_utc"].values.astype("datetime64[ns]"),
        out.loc[out["asset_type"]=="generator","end_utc"].values.astype("datetime64[ns]"))
    is_hvdc  = _interval_active_simple(
        ts_np,
        out.loc[out["asset_type"]=="hvdc","start_utc"].values.astype("datetime64[ns]"),
        out.loc[out["asset_type"]=="hvdc","end_utc"].values.astype("datetime64[ns]"))
    is_forced = _interval_active_simple(
        ts_np,
        out.loc[out["planned_or_forced"]=="forced","start_utc"].values.astype("datetime64[ns]"),
        out.loc[out["planned_or_forced"]=="forced","end_utc"].values.astype("datetime64[ns]"))

    # MW lost (for gen and hvdc, intensity matters)
    mw_gen = np.zeros(n)
    for _, r in out[out["asset_type"]=="generator"].iterrows():
        mask = (ts_np >= r["start_utc"].to_datetime64()) & (ts_np < r["end_utc"].to_datetime64())
        mw_gen[mask] += float(r["capacity_mw"])
    mw_hvdc = np.zeros(n)
    for _, r in out[out["asset_type"]=="hvdc"].iterrows():
        mask = (ts_np >= r["start_utc"].to_datetime64()) & (ts_np < r["end_utc"].to_datetime64())
        mw_hvdc[mask] += float(r["capacity_mw"])

    # Diurnal & weekly patterns
    hr = timestamps.hour
    diurnal = 50 * np.sin(2 * np.pi * hr / 24)
    dow = timestamps.dayofweek
    weekly = -30 * (dow >= 5)  # weekends slightly lower

    rows = []
    for cnec, zfrom, zto in NO3_CNECS:
        # Baseline values
        fmax_base = rng.uniform(900, 1500)
        frm_base  = fmax_base * rng.uniform(0.07, 0.12)
        ptdf_FI_base = rng.uniform(-0.06, -0.02)  # NO3 CNECs typically negative on FI
        ptdf_NO3_base = rng.uniform(0.20, 0.40)
        ptdf_FI_FS_base = rng.uniform(-0.04, -0.01)
        ptdf_FI_EL_base = rng.uniform(-0.03, -0.01)

        # fall (F_allReference): the reference flow that actually enters the RAM
        # formula (see CLAUDE.md / METHODOLOGY.md) — this is where outage effects
        # belong, as an INDEPENDENT simulated quantity. It used to be computed
        # backwards as fmax-frm+fnrao-faac-ram (an algebraic residual of the RAM
        # identity), which made the pipeline's RAM-formula check pass 100% by
        # construction regardless of whether the formula it was checking was
        # actually complete — see audit L2-1. Generating it forward, independently
        # of ram, makes that check a real test again.
        fall = (rng.normal(0, 30, n)                   # noise
              + diurnal + weekly                       # patterns
              + 80 * is_hvdc * np.sign(ptdf_FI_FS_base)  # HVDC -> reference-flow jump
              + 0.04 * mw_gen * np.sign(ptdf_FI_base) * is_forced  # gen forced
              + 25 * is_ac * np.sign(ptdf_FI_base))    # AC topology
        fall += rng.uniform(-30, 30)  # CNEC-specific bias

        # PTDF_FI shifts only during AC line outages (small magnitude)
        ptdf_FI = ptdf_FI_base + 0.025 * is_ac * np.sign(ptdf_FI_base) * (-1) \
                  + rng.normal(0, 0.001, n)

        # FI_FS PTDF collapses to ~0 during HVDC outage on Fenno-Skan
        ptdf_FI_FS = np.where(is_hvdc & (mw_hvdc > 700),
                              rng.normal(0, 0.001, n),
                              ptdf_FI_FS_base + rng.normal(0, 0.001, n))
        ptdf_FI_EL = np.where(is_hvdc & (mw_hvdc > 600) & (mw_hvdc < 700),
                              rng.normal(0, 0.001, n),
                              ptdf_FI_EL_base + rng.normal(0, 0.001, n))

        # FRM: structural, with December 2024 step change
        frm = np.full(n, frm_base * 0.5)  # pre-Dec-2024 lower
        post_dec24 = timestamps >= pd.Timestamp("2024-12-10", tz="UTC")
        frm[post_dec24] = frm_base
        frm += rng.normal(0, 1, n)

        # FRA: small, sometimes positive during HVDC outages (cross-zonal RA)
        fra = np.where(is_hvdc, rng.uniform(0, 30, n), rng.uniform(0, 5, n))

        # AMR: zero most of the time
        amr = np.where(rng.uniform(0, 1, n) > 0.97, rng.uniform(20, 80, n), 0.0)

        # FAAC: tiny constant
        faac = rng.uniform(0, 5, n)

        # IVA: more frequent during forced outages, especially HVDC forced
        iva_prob = 0.01 + 0.10 * is_forced + 0.15 * (is_hvdc * is_forced)
        iva_active = rng.uniform(0, 1, n) < iva_prob
        iva = np.where(iva_active, rng.uniform(20, 150, n), 0.0)

        # FB linearised flow
        net_position_cgma = rng.normal(500, 300, n)
        flowFB = fall + ptdf_FI * net_position_cgma  # net positions

        # RAM identity — the full formula (matches build_covariates() in
        # propagation.py): RAM = Fmax - FRM - fall + fnrao + AMR - AAC - IVA
        ram = fmax_base - frm - fall + fra + amr - faac - iva
        ram = np.maximum(ram, 0)

        # f0 / fref: the CGMA-NP reference flow. Per METHODOLOGY.md this is a
        # DIFFERENT physical quantity from fall, related through the net
        # position at CGMA (fref ~ fall + PTDF * NP_CGMA), with its own noise —
        # correlated with fall, not identical to it, and NOT an input to the
        # RAM identity above. fref and f0 are the same quantity in JAO exports
        # (see METHODOLOGY.md), so they're set equal here.
        f0 = fall + ptdf_FI_base * net_position_cgma * 0.1 + rng.normal(0, 15, n)

        # Shadow price: positive only when RAM near binding & with prob
        binding_score = 1 - ram / fmax_base
        sp = np.where(rng.uniform(0, 1, n) < (binding_score * 0.30 + 0.05 * is_forced),
                       rng.uniform(2, 50, n), 0.0)

        for i, t in enumerate(timestamps):
            rows.append({
                "dateTimeUtc": t.isoformat(),
                "cneName": cnec,
                "biddingZoneFrom": zfrom,
                "biddingZoneTo": zto,
                "contingencies": "N",
                "shadowPrice": round(float(sp[i]), 3),
                "ram": round(float(ram[i]), 2),
                "fall": round(float(fall[i]), 2),
                "flowFb": round(float(flowFB[i]), 2),
                "fmax": round(fmax_base, 2),
                "fref": round(float(f0[i]), 2),
                "f0": round(float(f0[i]), 3),
                "frm": round(float(frm[i]), 3),
                "fnrao": round(float(fra[i]), 3),   # correct column name
                "fra":   round(float(fra[i]), 3),   # alias kept
                "amr": round(float(amr[i]), 3),
                "faac": round(float(faac[i]), 3),
                "aac":  round(float(faac[i]), 3),   # JAO uses 'aac'
                "iva": round(float(iva[i]), 3),
                "ptdf_FI": round(float(ptdf_FI[i]), 5),
                "ptdf_FI_FS": round(float(ptdf_FI_FS[i]), 5),
                "ptdf_FI_EL": round(float(ptdf_FI_EL[i]), 5),
                "ptdf_NO3": round(float(ptdf_NO3_base + rng.normal(0, 0.005)), 5),
                "ptdf_NO4": round(float(rng.uniform(0.10, 0.20)), 5),
                "ptdf_SE1": round(float(rng.uniform(0.05, 0.15)), 5),
                "ptdf_SE2": round(float(rng.uniform(0.02, 0.08)), 5),
                "ptdf_SE3": round(float(rng.uniform(0.01, 0.05)), 5),
            })

    df = pd.DataFrame(rows)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    return df


def generate_demo_dataset(out_dir: str, days: int = 90,
                          rng_seed: int = 7) -> dict:
    """Generate both a synthetic JAO CSV and a synthetic outage CSV."""
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    end = datetime(2025, 3, 1, tzinfo=timezone.utc)
    start = end - timedelta(days=days)

    outages = generate_outage_events(start, end, rng_seed=rng_seed)
    outages_path = str(out / "synthetic_outages.csv")
    outages.to_csv(outages_path, index=False)

    jao_path = str(out / "synthetic_jao.csv")
    jao = generate_jao_csv(start, end, outages, jao_path, rng_seed=rng_seed)

    return {"jao_path": jao_path, "outages_path": outages_path,
            "jao_rows": len(jao), "outage_events": len(outages),
            "start": start.isoformat(), "end": end.isoformat()}


if __name__ == "__main__":
    info = generate_demo_dataset("./fb_demo", days=90)
    print(info)
