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
                     out_path: str, rng_seed: int = 7,
                     effect_scale: float = 1.0) -> pd.DataFrame:
    """Generate a synthetic JAO CSV with realistic FB params and embedded outage effects.

    effect_scale: multiplier on the OUTAGE-INDUCED terms only (not the
    baseline noise/diurnal/weekly/CNEC-bias terms). The default (1.0)
    plants effects on the order of the whole FRM margin (~80 MW HVDC jump
    against a ~65-180 MW frm_base) -- large enough to make the estimation
    machinery easy to validate, but not representative of the small,
    topologically-expected effect a non-adjacent zone pair like FI/NO3
    should actually produce in practice (see H1/H2 economic-significance
    discussion in propagation.py). Pass e.g. effect_scale=0.1 to generate a
    small-effect scenario for testing whether the pipeline can still detect
    (or correctly fails to detect) an economically realistic signal, rather
    than only ever validating against an oversized one.
    """
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

    # Day-level index per timestamp, used below to give fnrao/amr/faac/iva
    # genuine day-to-day persistence -- distinct from the diurnal/weekly
    # hour-of-day/day-of-week pattern already on 'fall' (which
    # seasonal_naive/theta already see via hour+dow grouping), a specific
    # calendar day's regime level is invisible to any method that only
    # conditions on (hour, weekday). Without this, fnrao/amr/faac/iva were
    # each close to i.i.d. noise or near-constant, giving lag-feature/
    # decomposition-based ITS methods (see _its_ram_identity() in
    # propagation.py) nothing to exploit that a direct forecast of 'ram'
    # couldn't already get from the aggregate's own hour/dow pattern --
    # making any accuracy comparison between them unfair to the
    # decomposition approach. See CLAUDE.md's "ACCURACY REALITY CHECK".
    day_index = (timestamps.normalize() - timestamps.normalize().min()).days.values
    n_days = int(day_index.max()) + 1

    def _ar1_day_regime(phi: float = 0.65) -> np.ndarray:
        """Mean-reverting AR(1) walk, one value per calendar day, unit std,
        broadcast to MTU resolution. A fresh call consumes n_days rng draws."""
        days = np.zeros(n_days)
        innovations = rng.normal(0, 1, n_days)
        for d in range(1, n_days):
            days[d] = phi * days[d - 1] + innovations[d]
        std = days.std()
        if std > 1e-9:
            days = days / std
        return days[day_index]

    rows = []
    for cnec, zfrom, zto in NO3_CNECS:
        # Baseline values
        fmax_base = rng.uniform(900, 1500)
        frm_base  = fmax_base * rng.uniform(0.07, 0.12)
        ptdf_FI_base = rng.uniform(-0.06, -0.02)  # NO3 CNECs typically negative on FI
        ptdf_NO3_base = rng.uniform(0.20, 0.40)
        ptdf_FI_FS_base = rng.uniform(-0.04, -0.01)
        ptdf_FI_EL_base = rng.uniform(-0.03, -0.01)
        # NO4/SE1/SE2/SE3 PTDFs used to be redrawn fresh, INDEPENDENTLY, on
        # every single row -- no persistent per-CNEC value at all, unlike a
        # real PTDF (stable for a CNEC, shifting only slowly with grid
        # topology) or ptdf_FI/ptdf_NO3 above (both base + small per-MTU
        # noise). Fixed to the same base + small-noise shape so they're
        # usable as genuine physics inputs below and in _its_ptdf_flow()
        # (propagation.py).
        ptdf_NO4_base = rng.uniform(0.10, 0.20)
        ptdf_SE1_base = rng.uniform(0.05, 0.15)
        ptdf_SE2_base = rng.uniform(0.02, 0.08)
        ptdf_SE3_base = rng.uniform(0.01, 0.05)

        # Zone net positions (day-ahead market schedules): real economic
        # quantities, so -- unlike fnrao/amr/faac/iva above, which are
        # grid-operational -- they get the diurnal/weekly demand pattern
        # too, on top of their own day-level AR(1) regime (cold snaps /
        # demand swings a single global diurnal/weekly curve can't
        # capture). Written out as netpos_<ZONE> columns for
        # _its_ptdf_flow() to reconstruct 'fall' from, and also feeds an
        # UNSCALED (not effect_scale-multiplied -- a base-case market
        # quantity, not an outage effect) Σ PTDF_base × NetPosition term
        # into 'fall' itself below: the real F_allReference genuinely
        # depends on the D-2 base-case net positions, which 'fall'
        # previously had no dependency on at all. Amplitudes kept modest
        # (well under fall's existing noise+diurnal+outage-effect scale)
        # so this doesn't drown out the outage effects H1/H2 detect.
        _ptdf_base_by_zone = {
            "FI": ptdf_FI_base, "NO3": ptdf_NO3_base, "NO4": ptdf_NO4_base,
            "SE1": ptdf_SE1_base, "SE2": ptdf_SE2_base, "SE3": ptdf_SE3_base,
        }
        netpos_by_zone = {}
        fall_netpos_term = np.zeros(n)
        for _zone, _ptdf_base in _ptdf_base_by_zone.items():
            _regime_np = _ar1_day_regime()
            _npzone = (40 * np.sin(2 * np.pi * hr / 24) - 15 * (dow >= 5)
                      + 30 * _regime_np + rng.normal(0, 15, n))
            netpos_by_zone[_zone] = _npzone
            fall_netpos_term += _ptdf_base * _npzone

        # CNEC-specific base-case level: previously an arbitrary constant
        # added directly to 'fall' with no physical explanation, and
        # invisible to _its_ptdf_flow() (propagation.py), which only ever
        # looks at netpos_<ZONE>, never 'fall' itself -- an unexplained
        # bias it structurally could never correct for, unlike a direct
        # forecast fit on 'fall' (seasonal_naive/catboost), which trivially
        # absorbs any constant level via its own hour/dow mean. Folded into
        # netpos_NO3's own per-CNEC mean level instead -- NO3 has the
        # largest PTDF magnitude of the six zones (always in [0.20, 0.40],
        # comfortably away from zero), so dividing by it here is safe. Same
        # rng draw and same net effect on 'fall' as before, but now
        # genuinely reconstructible from net position like the rest of the
        # netpos-driven term.
        _fall_bias = rng.uniform(-30, 30)
        netpos_by_zone["NO3"] = netpos_by_zone["NO3"] + _fall_bias / ptdf_NO3_base
        fall_netpos_term += _fall_bias

        # fall (F_allReference): the reference flow that actually enters the RAM
        # formula (see CLAUDE.md / METHODOLOGY.md) — this is where outage effects
        # belong, as an INDEPENDENT simulated quantity. It used to be computed
        # backwards as fmax-frm+fnrao-faac-ram (an algebraic residual of the RAM
        # identity), which made the pipeline's RAM-formula check pass 100% by
        # construction regardless of whether the formula it was checking was
        # actually complete. Generating it forward, independently of ram, makes
        # that check a real test again.
        fall = (rng.normal(0, 30, n)                   # noise
              + diurnal + weekly                       # patterns
              + fall_netpos_term                       # D-2 base-case net-position term
              + effect_scale * 80 * is_hvdc * np.sign(ptdf_FI_FS_base)  # HVDC -> reference-flow jump
              + effect_scale * 0.04 * mw_gen * np.sign(ptdf_FI_base) * is_forced  # gen forced
              + effect_scale * 25 * is_ac * np.sign(ptdf_FI_base))    # AC topology

        # PTDF_FI shifts only during AC line outages (small magnitude)
        ptdf_FI = ptdf_FI_base + effect_scale * 0.025 * is_ac * np.sign(ptdf_FI_base) * (-1) \
                  + rng.normal(0, 0.001, n)

        # FI_FS PTDF collapses to ~0 during HVDC outage on Fenno-Skan
        ptdf_FI_FS = np.where(is_hvdc & (mw_hvdc > 700),
                              rng.normal(0, 0.001, n),
                              ptdf_FI_FS_base + rng.normal(0, 0.001, n))
        ptdf_FI_EL = np.where(is_hvdc & (mw_hvdc > 600) & (mw_hvdc < 700),
                              rng.normal(0, 0.001, n),
                              ptdf_FI_EL_base + rng.normal(0, 0.001, n))

        # NO3/NO4/SE1/SE2/SE3 PTDFs: base + small per-MTU noise, vectorized
        # (this used to be drawn one rng call at a time inside the per-row
        # loop below -- moved up here alongside the other ptdf_* arrays,
        # same base+noise shape, just precomputed instead of drawn 96*days
        # times in a Python loop).
        ptdf_NO3 = ptdf_NO3_base + rng.normal(0, 0.005, n)
        ptdf_NO4 = ptdf_NO4_base + rng.normal(0, 0.005, n)
        ptdf_SE1 = ptdf_SE1_base + rng.normal(0, 0.005, n)
        ptdf_SE2 = ptdf_SE2_base + rng.normal(0, 0.005, n)
        ptdf_SE3 = ptdf_SE3_base + rng.normal(0, 0.005, n)

        # FRM: structural, with December 2024 step change
        frm = np.full(n, frm_base * 0.5)  # pre-Dec-2024 lower
        post_dec24 = timestamps >= pd.Timestamp("2024-12-10", tz="UTC")
        frm[post_dec24] = frm_base
        frm += rng.normal(0, 1, n)

        # FRA: small, sometimes positive during HVDC outages (cross-zonal RA),
        # plus a day-level regime (TSO cross-zonal RA allocation tends to
        # persist across a day, not reset every 15 min).
        regime_fra = _ar1_day_regime()
        fra = np.where(is_hvdc, rng.uniform(0, 30, n), rng.uniform(0, 5, n))
        fra = np.maximum(fra + 5.0 * regime_fra, 0.0)

        # AMR: zero most of the time, but the daily spike PROBABILITY (not
        # just each spike's magnitude) is day-regime-modulated -- redispatch
        # actions cluster on high-stress grid days rather than firing
        # independently every 15 min. Bounded so it stays "zero most of the
        # time" (~90-99% zero rows) like before.
        regime_amr = _ar1_day_regime()
        amr_prob = np.clip(0.03 + 0.02 * regime_amr, 0.005, 0.10)
        amr = np.where(rng.uniform(0, 1, n) < amr_prob, rng.uniform(20, 80, n), 0.0)

        # FAAC: tiny, but with a slow day-level level shift under the
        # per-MTU noise rather than pure i.i.d. noise around one constant.
        regime_faac = _ar1_day_regime()
        faac = np.clip(1.5 + 1.2 * regime_faac + rng.normal(0, 0.7, n), 0.0, None)

        # IVA: more frequent during forced outages, especially HVDC forced.
        # The non-outage-induced baseline rate now drifts by day (a mild
        # "congestion regime" independent of any specific outage) instead of
        # sitting fixed at 0.01 -- still deliberately left unscaled by
        # effect_scale, same as before, since it isn't outage-induced.
        regime_iva = _ar1_day_regime()
        iva_baseline = np.clip(0.01 + 0.02 * np.clip(regime_iva, -1.5, 1.5), 0.002, 0.05)
        iva_prob = iva_baseline + effect_scale * (0.10 * is_forced + 0.15 * (is_hvdc * is_forced))
        iva_active = rng.uniform(0, 1, n) < iva_prob
        iva = np.where(iva_active, rng.uniform(20, 150, n), 0.0)

        # FB linearised flow
        net_position_cgma = rng.normal(500, 300, n)
        flowFB = fall + ptdf_FI * net_position_cgma  # net positions

        # RAM identity — the full formula (matches build_covariates() in
        # propagation.py): RAM = Fmax - FRM - fall + fnrao + AMR - AAC - IVA
        ram = fmax_base - frm - fall + fra + amr - faac - iva
        ram = np.maximum(ram, 0)

        # f0 / fref: the CGMA-NP reference flow. This is a DIFFERENT physical
        # quantity from fall, related through the net position at CGMA
        # (fref ~ fall + PTDF * NP_CGMA), with its own noise — correlated with
        # fall, not identical to it, and NOT an input to the RAM identity
        # above. fref and f0 are the same quantity in JAO exports, so they're
        # set equal here.
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
                "ptdf_NO3": round(float(ptdf_NO3[i]), 5),
                "ptdf_NO4": round(float(ptdf_NO4[i]), 5),
                "ptdf_SE1": round(float(ptdf_SE1[i]), 5),
                "ptdf_SE2": round(float(ptdf_SE2[i]), 5),
                "ptdf_SE3": round(float(ptdf_SE3[i]), 5),
                "netpos_FI":  round(float(netpos_by_zone["FI"][i]), 2),
                "netpos_NO3": round(float(netpos_by_zone["NO3"][i]), 2),
                "netpos_NO4": round(float(netpos_by_zone["NO4"][i]), 2),
                "netpos_SE1": round(float(netpos_by_zone["SE1"][i]), 2),
                "netpos_SE2": round(float(netpos_by_zone["SE2"][i]), 2),
                "netpos_SE3": round(float(netpos_by_zone["SE3"][i]), 2),
            })

    df = pd.DataFrame(rows)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    return df


def generate_demo_dataset(out_dir: str, days: int = 90,
                          rng_seed: int = 7, effect_scale: float = 1.0) -> dict:
    """Generate both a synthetic JAO CSV and a synthetic outage CSV.

    effect_scale: forwarded to generate_jao_csv() -- 1.0 (default) plants
    large, easy-to-detect outage effects; pass a smaller value (e.g. 0.1)
    to generate a small, economically-realistic-effect scenario instead.
    """
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    end = datetime(2025, 3, 1, tzinfo=timezone.utc)
    start = end - timedelta(days=days)

    outages = generate_outage_events(start, end, rng_seed=rng_seed)
    outages_path = str(out / "synthetic_outages.csv")
    outages.to_csv(outages_path, index=False)

    jao_path = str(out / "synthetic_jao.csv")
    jao = generate_jao_csv(start, end, outages, jao_path, rng_seed=rng_seed,
                           effect_scale=effect_scale)

    return {"jao_path": jao_path, "outages_path": outages_path,
            "jao_rows": len(jao), "outage_events": len(outages),
            "start": start.isoformat(), "end": end.isoformat()}


if __name__ == "__main__":
    info = generate_demo_dataset("./fb_demo", days=90)
    print(info)
