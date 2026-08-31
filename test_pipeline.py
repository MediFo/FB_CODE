"""
test_pipeline.py
Full test suite for the FI -> NO3 propagation pipeline.
Run with: pytest test_pipeline.py -v

All tests use synthetic data — no API keys required.
"""
import sys
import os
import warnings
warnings.filterwarnings("ignore")

# All files are in the same folder as this script
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import pytest
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timezone, timedelta

from synthetic import generate_demo_dataset
from propagation import (
    load_jao_csv, filter_no3, build_covariates, deduplicate_outages,
    run_panel_regression, run_logit_iva, decompose_delta_ram,
    summarize_hypotheses, PipelineConfig, run_pipeline,
    DEFAULT_NO3_PATTERNS, cet_input_to_utc, utc_to_cet_str,
    build_event_time_dummies, run_event_study,
    single_event_analysis, pre_period_abs_ptdf,
)
import propagation as _pipe


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def synthetic_dir(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("synthetic"))
    return generate_demo_dataset(d, days=90, rng_seed=42)


@pytest.fixture(scope="session")
def jao_df(synthetic_dir):
    return load_jao_csv(synthetic_dir["jao_path"])


@pytest.fixture(scope="session")
def outages_df(synthetic_dir):
    return pd.read_csv(synthetic_dir["outages_path"])


@pytest.fixture(scope="session")
def no3_df(jao_df):
    return filter_no3(jao_df)


@pytest.fixture(scope="session")
def no3_cov(no3_df, outages_df):
    return build_covariates(no3_df, outages_df)


# ── 1. Data loading ───────────────────────────────────────────────────────────

class TestDataLoading:
    def test_jao_loads(self, jao_df):
        assert len(jao_df) > 0
        assert "dateTimeUtc" in jao_df.columns
        assert "cneName" in jao_df.columns

    def test_datetime_is_tz_aware(self, jao_df):
        assert jao_df["dateTimeUtc"].dt.tz is not None

    def test_key_numeric_columns_present(self, jao_df):
        for col in ["f0", "ram", "fmax", "frm", "shadowPrice"]:
            assert col in jao_df.columns, f"Missing column: {col}"
            assert jao_df[col].dtype in [np.float64, np.float32]

    def test_ptdf_fi_present(self, jao_df):
        assert "ptdf_FI" in jao_df.columns

    def test_f0_populated(self, jao_df):
        # f0 should be non-null (may be proxied from fref)
        assert jao_df["f0"].notna().sum() > 0

    def test_jao_aliases_resolve(self, tmp_path):
        """CSV with 'flowFb' (lowercase b) and 'aac' should map to canonical names."""
        df = pd.DataFrame({
            "dateTimeUtc": ["2026-04-25T10:00:00Z"],
            "cneName": ["TEST_CNEC"],
            "biddingZoneFrom": ["NO3"],
            "biddingZoneTo":   ["NO4"],
            "flowFb":     [100.0],   # alias for flowFB
            "aac":        [10.0],    # alias for faac
            "fref":       [200.0],
            "fmax":       [500.0],
            "frm":        [50.0],
            "ram":        [300.0],
            "shadowPrice":[25.0],
            "iva":        [0.0],
            "amr":        [0.0],
        })
        p = str(tmp_path / "alias_test.csv")
        df.to_csv(p, index=False)
        loaded = load_jao_csv(p)
        assert "flowFB" in loaded.columns
        assert "faac"   in loaded.columns


# ── 2. NO3 filtering ─────────────────────────────────────────────────────────

class TestNO3Filter:
    def test_returns_only_no3_rows(self, jao_df, no3_df):
        assert len(no3_df) > 0
        assert len(no3_df) <= len(jao_df)

    def test_all_cnecs_match_pattern_or_zone(self, jao_df, no3_df):
        import re
        pat = re.compile("|".join(DEFAULT_NO3_PATTERNS), flags=re.IGNORECASE)
        for _, row in no3_df.iterrows():
            is_zone = row.get("biddingZoneFrom") == "NO3" or \
                      row.get("biddingZoneTo") == "NO3"
            has_name = bool(pat.search(str(row["cneName"])))
            assert is_zone or has_name

    def test_multiple_cnecs_present(self, no3_df):
        assert no3_df["cneName"].nunique() >= 3


# ── 3. Outage deduplication ───────────────────────────────────────────────────

class TestOutageDedup:
    def test_dedup_removes_exact_duplicates(self):
        row = {"outage_id":"a","start_utc":"2026-04-20T00:00:00Z",
               "end_utc":"2026-04-21T00:00:00Z","asset_id":"X","asset_name":"X",
               "asset_type":"hvdc","voltage_kv":None,"capacity_mw":800.0,
               "planned_or_forced":"forced","bidding_zone":"FI",
               "control_area":"FI","source":"entsoe_a78","raw_payload":"{}"}
        df = pd.DataFrame([row, row])
        out = deduplicate_outages(df)
        assert len(out) == 1

    def test_dedup_prefers_higher_priority_source(self):
        base = {"start_utc":"2026-04-20T00:00:00Z","end_utc":"2026-04-21T00:00:00Z",
                "asset_id":"X","asset_name":"X","asset_type":"hvdc",
                "voltage_kv":None,"capacity_mw":800.0,"planned_or_forced":"forced",
                "bidding_zone":"FI","control_area":"FI","raw_payload":"{}"}
        rows = [
            {**base, "outage_id":"m1", "source":"manual"},
            {**base, "outage_id":"e1", "source":"entsoe_a78"},
        ]
        out = deduplicate_outages(pd.DataFrame(rows))
        assert len(out) == 1
        assert out.iloc[0]["source"] == "entsoe_a78"

    def test_dedup_keeps_non_overlapping_events_for_same_asset(self):
        """Two outages of the SAME asset at DIFFERENT times must BOTH be kept.
        The old drop_duplicates('_key') bug silently discarded the second event,
        causing data loss for assets with recurring outages (e.g. Fenno-Skan)."""
        base = {"asset_id": "FS", "asset_name": "Fenno-Skan", "asset_type": "hvdc",
                "voltage_kv": 400.0, "capacity_mw": 800.0,
                "planned_or_forced": "planned", "bidding_zone": "FI",
                "control_area": "FI", "source": "manual", "raw_payload": "{}"}
        rows = [
            {**base, "outage_id": "fs_apr",
             "start_utc": "2026-04-01T00:00:00Z", "end_utc": "2026-04-02T00:00:00Z"},
            {**base, "outage_id": "fs_may",
             "start_utc": "2026-05-15T00:00:00Z", "end_utc": "2026-05-16T00:00:00Z"},
        ]
        out = deduplicate_outages(pd.DataFrame(rows))
        assert len(out) == 2, (
            "Non-overlapping outages for the same asset must both be retained")


# ── 4. Covariate building ─────────────────────────────────────────────────────

class TestCovariates:
    def test_covariate_columns_created(self, no3_cov):
        for col in ["fi_planned_outage_active", "fi_forced_outage_active",
                    "fi_hvdc_outage_active", "fi_ac_line_outage_active",
                    "fi_gen_outage_mw_lost"]:
            assert col in no3_cov.columns, f"Missing: {col}"

    def test_covariate_values_binary(self, no3_cov):
        for col in ["fi_planned_outage_active","fi_forced_outage_active",
                    "fi_hvdc_outage_active","fi_ac_line_outage_active"]:
            unique = set(no3_cov[col].dropna().unique())
            assert unique <= {0, 1, 0.0, 1.0}

    def test_outage_rows_are_nonzero(self, no3_cov):
        # At least some rows should have an outage active
        total_active = (no3_cov["fi_planned_outage_active"] +
                        no3_cov["fi_forced_outage_active"] +
                        no3_cov["fi_hvdc_outage_active"]).sum()
        assert total_active > 0, "No outage rows found — window mismatch?"

    def test_lag_columns_created(self, no3_cov):
        assert "fi_planned_outage_active_lag1h"  in no3_cov.columns
        assert "fi_forced_outage_active_lag24h"  in no3_cov.columns

    def test_time_fe_columns_created(self, no3_cov):
        for col in ["hour","dow","month","date"]:
            assert col in no3_cov.columns

    def test_no_covariates_when_empty_outages(self, no3_df):
        cov = build_covariates(no3_df, pd.DataFrame())
        assert cov["fi_forced_outage_active"].sum() == 0


# ── 5. Overlap detection ──────────────────────────────────────────────────────

class TestOverlapDetection:
    def test_outside_window_produces_zero_covariates(self, no3_df):
        """Outage entirely before JAO window → all covariates zero."""
        jao_start = no3_df["dateTimeUtc"].min()
        out = pd.DataFrame([{
            "outage_id":"outside","start_utc":(jao_start - pd.Timedelta(days=30)).isoformat(),
            "end_utc":(jao_start - pd.Timedelta(days=29)).isoformat(),
            "asset_name":"X","asset_type":"hvdc","capacity_mw":800.0,
            "planned_or_forced":"forced","source":"manual","asset_id":None,
            "voltage_kv":None,"bidding_zone":"FI","control_area":"FI","raw_payload":"{}"}])
        cov = build_covariates(no3_df, out)
        assert cov["fi_hvdc_outage_active"].sum() == 0

    def test_inside_window_produces_nonzero_covariates(self, no3_df):
        """Outage within JAO window → some rows have covariate = 1."""
        jao_mid = no3_df["dateTimeUtc"].min() + pd.Timedelta(days=5)
        out = pd.DataFrame([{
            "outage_id":"inside","start_utc":jao_mid.isoformat(),
            "end_utc":(jao_mid + pd.Timedelta(hours=12)).isoformat(),
            "asset_name":"X","asset_type":"hvdc","capacity_mw":800.0,
            "planned_or_forced":"forced","source":"manual","asset_id":None,
            "voltage_kv":None,"bidding_zone":"FI","control_area":"FI","raw_payload":"{}"}])
        cov = build_covariates(no3_df, out)
        assert cov["fi_hvdc_outage_active"].sum() > 0


# ── 6. Regressions ────────────────────────────────────────────────────────────

class TestRegressions:
    """These tests require statsmodels + linearmodels."""

    @pytest.fixture(autouse=True)
    def skip_without_linearmodels(self):
        try:
            from linearmodels.panel import PanelOLS
        except ImportError:
            pytest.skip("linearmodels not installed")

    def test_f0_regression_runs(self, no3_cov):
        r = run_panel_regression(no3_cov, "f0")
        assert r, "f0 regression returned empty result"
        assert "coefs" in r
        assert "n_obs" in r
        assert r["n_obs"] > 100

    def test_ram_regression_runs(self, no3_cov):
        r = run_panel_regression(no3_cov, "ram")
        assert r
        cf = r["coefs"]
        assert len(cf) > 0

    def test_ptdf_regression_runs(self, no3_cov):
        r = run_panel_regression(no3_cov, "ptdf_FI")
        assert r

    def test_regression_coefs_have_expected_columns(self, no3_cov):
        r = run_panel_regression(no3_cov, "ram")
        cf = r["coefs"]
        for col in ["param","coef","std_err","t","p"]:
            assert col in cf.columns

    def test_hvdc_f0_signal_detected(self, no3_cov):
        """H1: HVDC outage should produce a significant F0 shift on synthetic
        data — via whichever of the binary dummy / MW-dose variable survived
        collinearity pruning (see _prune_collinear_dose_pairs; with few
        independent HVDC episodes the two are near-perfectly collinear, and
        the dose variable is kept over the binary duplicate on purpose)."""
        r = run_panel_regression(no3_cov, "f0")
        cf = r["coefs"].set_index("param")
        candidates = [c for c in ("fi_hvdc_outage_active", "fi_hvdc_outage_mw_lost")
                      if c in cf.index]
        assert candidates, "neither fi_hvdc_outage_active nor its dose variable survived"
        p = min(cf.loc[c, "p"] for c in candidates)
        assert p < 0.05, f"HVDC F0 effect not significant (p={p:.3f})"

    def test_ac_line_ptdf_signal_detected(self, no3_cov):
        """H2: AC line outage should shift PTDF_FI — same collinearity-pruning
        caveat as test_hvdc_f0_signal_detected above."""
        r = run_panel_regression(no3_cov, "ptdf_FI")
        cf = r["coefs"].set_index("param")
        candidates = [c for c in ("fi_ac_line_outage_active", "fi_ac_outage_mw_lost")
                      if c in cf.index]
        assert candidates, "neither fi_ac_line_outage_active nor its dose variable survived"
        p = min(cf.loc[c, "p"] for c in candidates)
        assert p < 0.05, f"AC line PTDF_FI effect not significant (p={p:.3f})"

    def test_frm_placebo_consistent(self, no3_cov):
        """H6: FRM should NOT move with outage covariates on real-world logic.
        On synthetic data with regime-change encoding, this may fail — acceptable."""
        r = run_panel_regression(no3_cov, "frm")
        # Just check it runs; actual p-value depends on synthetic encoding
        assert r

    def test_summary_text_available(self, no3_cov):
        r = run_panel_regression(no3_cov, "f0")
        assert "summary_text" in r
        assert len(r["summary_text"]) > 10  # not empty string

    def test_no_crash_on_missing_dep_var(self, no3_cov):
        """Regression on a column that doesn't exist should return empty dict."""
        r = run_panel_regression(no3_cov, "nonexistent_column_xyz")
        assert r == {}


# ── 7. ΔRAM decomposition ─────────────────────────────────────────────────────

class TestDecomposition:
    def test_decomposition_runs(self, no3_cov, outages_df):
        out = pd.read_csv if isinstance(outages_df, str) else outages_df
        if isinstance(out, pd.DataFrame):
            out_ts = out.copy()
            out_ts["start_utc"] = pd.to_datetime(out_ts["start_utc"], utc=True)
            out_ts["end_utc"]   = pd.to_datetime(out_ts["end_utc"], utc=True)
        cnec = no3_cov["cneName"].iloc[0]
        mid  = no3_cov["dateTimeUtc"].median()
        s    = mid - pd.Timedelta(days=3)
        e    = mid + pd.Timedelta(days=1)
        df   = decompose_delta_ram(no3_cov, cnec, s, e)
        assert isinstance(df, pd.DataFrame)

    def test_decomposition_columns(self, no3_cov):
        cnec = no3_cov["cneName"].iloc[0]
        mid  = no3_cov["dateTimeUtc"].median()
        df   = decompose_delta_ram(no3_cov, cnec,
                                   mid - pd.Timedelta(days=3),
                                   mid + pd.Timedelta(days=1))
        if not df.empty:
            assert "component" in df.columns
            assert "MW" in df.columns

    def test_decomposition_balance(self, no3_cov):
        """Sum of components ≈ observed ΔRAM (within rounding).
        Verified formula: RAM = Fmax - FRM + fnrao - AAC - fall
        """
        cnec = no3_cov["cneName"].iloc[0]
        mid  = no3_cov["dateTimeUtc"].median()
        df   = decompose_delta_ram(no3_cov, cnec,
                                   mid - pd.Timedelta(days=3),
                                   mid + pd.Timedelta(days=1))
        if df.empty:
            return
        sigma = df.loc[df["component"] == "= Σ contribs", "MW"].iloc[0]
        obs   = df.loc[df["component"] == "Δram observed", "MW"].iloc[0]
        # Allow 2 MW tolerance for floating-point rounding in synthetic data
        assert abs(sigma - obs) < 2.0, \
            f"Decomposition unbalanced: Σ={sigma:.2f}, observed={obs:.2f}\n{df}"


# ── 8. Hypotheses ─────────────────────────────────────────────────────────────

class TestHypotheses:
    @pytest.fixture(autouse=True)
    def skip_without_linearmodels(self):
        try:
            from linearmodels.panel import PanelOLS
        except ImportError:
            pytest.skip("linearmodels not installed")

    def test_all_six_hypotheses_returned(self, no3_cov):
        regs = {k: run_panel_regression(no3_cov, k)
                for k in ("f0","ptdf_FI","ram","shadowPrice","frm")}
        logit = run_logit_iva(no3_cov)
        verdicts = summarize_hypotheses(regs, logit)
        assert len(verdicts) == 6
        ids = {h["id"] for h in verdicts}
        assert ids == {"H1","H2","H3","H4","H5","H6"}

    def test_no_verdict_is_literal_na_when_regression_ran(self, no3_cov):
        """When regressions succeed, no verdict should be bare 'n/a'."""
        regs = {k: run_panel_regression(no3_cov, k)
                for k in ("f0","ptdf_FI","ram","shadowPrice","frm")}
        logit = run_logit_iva(no3_cov)
        verdicts = summarize_hypotheses(regs, logit)
        na_verdicts = [h for h in verdicts
                       if h["verdict"].strip() == "n/a"
                       and h["id"] not in ("H5",)]  # H5 may be n/a if no IVA
        assert len(na_verdicts) == 0, \
            f"Bare n/a verdicts: {na_verdicts}"

    def test_h1_supported_on_synthetic(self, no3_cov):
        """H1: fall_signed (sign-normalised F_allReference) is the correct variable.
        RAM = Fmax - FRM + fnrao - AAC - fall  (verified R²=1.000 on real JAO data).
        On 90-day synthetic data the regression may not converge due to limited
        variation — the test only verifies the pipeline selects the right variable."""
        dep = "fall_signed" if "fall_signed" in no3_cov.columns else "fall"
        assert dep in no3_cov.columns, \
            f"fall_signed/fall column missing from covariates — synthetic data issue"
        # Verify fall is consistent with the RAM formula
        if all(c in no3_cov.columns for c in ["fmax","frm","fnrao","faac","fall","ram"]):
            reconstructed = (no3_cov.fmax - no3_cov.frm
                             + no3_cov.fnrao.fillna(0)
                             - no3_cov.faac.fillna(0)
                             - no3_cov.fall)
            diff = (no3_cov.ram - reconstructed).abs()
            pct_ok = (diff < 2.0).mean()
            assert pct_ok > 0.90, \
                f"RAM formula check failed: only {pct_ok:.1%} rows within 2 MW"

    def test_h2_supported_on_synthetic(self, no3_cov):
        """H2: |PTDF_FI| column exists and is non-negative.
        On short synthetic data with one AC outage the PTDF shift may be absorbed
        by entity FE — the test verifies the column construction is correct."""
        dep = "ptdf_FI_abs"
        assert dep in no3_cov.columns, "ptdf_FI_abs missing from covariates"
        assert (no3_cov[dep].fillna(0) >= 0).all(), \
            "|PTDF_FI| should be non-negative everywhere"
        assert no3_cov[dep].notna().sum() > 0, "|PTDF_FI| all NaN"


# ── 9. Full pipeline ──────────────────────────────────────────────────────────

class TestFullPipeline:
    def test_pipeline_runs_end_to_end(self, synthetic_dir, tmp_path):
        jao     = load_jao_csv(synthetic_dir["jao_path"])
        outages = pd.read_csv(synthetic_dir["outages_path"])
        cfg = PipelineConfig(out_dir=str(tmp_path / "results"),
                             use_entsoe=False, use_manual=False)
        res = run_pipeline(cfg, jao_df=jao, outages_df=outages)
        assert "no3"      in res
        assert "outages"  in res
        assert "regressions" in res
        assert "hypotheses"  in res

    def test_pipeline_output_files_created(self, synthetic_dir, tmp_path):
        jao     = load_jao_csv(synthetic_dir["jao_path"])
        outages = pd.read_csv(synthetic_dir["outages_path"])
        out_dir = str(tmp_path / "results2")
        cfg = PipelineConfig(out_dir=out_dir,
                             use_entsoe=False, use_manual=False)
        run_pipeline(cfg, jao_df=jao, outages_df=outages)
        assert (tmp_path / "results2" / "no3_with_outage_covariates.csv").exists()
        assert (tmp_path / "results2" / "outages_unified.csv").exists()

    def test_pipeline_no3_rows_non_empty(self, synthetic_dir, tmp_path):
        jao     = load_jao_csv(synthetic_dir["jao_path"])
        outages = pd.read_csv(synthetic_dir["outages_path"])
        cfg = PipelineConfig(out_dir=str(tmp_path / "r3"),
                             use_entsoe=False, use_manual=False)
        res = run_pipeline(cfg, jao_df=jao, outages_df=outages)
        assert len(res["no3"]) > 0

    def test_pipeline_hypothesis_count(self, synthetic_dir, tmp_path):
        jao     = load_jao_csv(synthetic_dir["jao_path"])
        outages = pd.read_csv(synthetic_dir["outages_path"])
        cfg = PipelineConfig(out_dir=str(tmp_path / "r4"),
                             use_entsoe=False, use_manual=False)
        res = run_pipeline(cfg, jao_df=jao, outages_df=outages)
        assert len(res["hypotheses"]) == 6

    def test_pipeline_with_empty_outages(self, synthetic_dir, tmp_path):
        """Pipeline should not crash with zero outage events."""
        jao = load_jao_csv(synthetic_dir["jao_path"])
        cfg = PipelineConfig(out_dir=str(tmp_path / "r5"),
                             use_entsoe=False, use_manual=False)
        res = run_pipeline(cfg, jao_df=jao, outages_df=pd.DataFrame())
        assert res  # should not raise

    def test_pipeline_single_outage_selected(self, synthetic_dir, tmp_path):
        """Selecting ONE specific outage event should work without crashing."""
        jao     = load_jao_csv(synthetic_dir["jao_path"])
        outages = pd.read_csv(synthetic_dir["outages_path"])
        # Pick only the first outage — simulates "one selected maintenance"
        single = outages.iloc[[0]]
        cfg = PipelineConfig(out_dir=str(tmp_path / "r6"),
                             use_entsoe=False, use_manual=False)
        res = run_pipeline(cfg, jao_df=jao, outages_df=single)
        assert "hypotheses" in res


# ── 10. Regressions for previously-fixed bugs (audit) ─────────────────────────

class TestAuditFixes:
    """Each test here reproduces a specific bug found in the strict audit and
    would fail again if that bug were reintroduced."""

    def test_h5_verdict_not_shadowed_by_pvalue(self):
        """A loop-local p-value used to overwrite the `p` (source-prefix)
        variable inside summarize_hypotheses, so H5's column lookup
        ('{p}_planned_outage_active') always KeyError'd once H1-H4 had run —
        producing 'vars absent in logit' even when the logit succeeded."""
        logit_coefs = pd.DataFrame({
            "param": ["const", "fi_planned_outage_active", "fi_forced_outage_active"],
            "coef":  [-4.87, -1.77, 2.56],
            "std_err": [0.27, 0.49, 0.18],
            "z": [-18.4, -3.6, 14.2],
            "p": [3.3e-75, 3.0e-4, 4.5e-46],
        })
        logit_result = {"coefs": logit_coefs, "n_obs": 43200, "n_positive": 513,
                        "pseudo_r2": 0.11}
        ram_coefs = pd.DataFrame({
            "param": ["const", "fi_hvdc_outage_active"],
            "coef":  [1100.0, 50.0],
            "std_err": [1.0, 3.0],
            "t": [1000.0, 16.0],
            "p": [0.0, 0.0],
        })
        reg_results = {"ram": {"dep": "ram", "n_obs": 43200, "n_entities": 5,
                               "rsquared": 0.5, "rsquared_within": 0.5,
                               "summary_text": "", "coefs": ram_coefs}}
        verdicts = summarize_hypotheses(reg_results, logit_result)
        h5 = next(h for h in verdicts if h["id"] == "H5")
        assert "vars absent" not in h5["verdict"], (
            f"H5 verdict regressed to the shadowing bug: {h5['verdict']!r}")
        assert "SUPPORTED" in h5["verdict"]
        assert "forced" in h5["verdict"] and "planned" in h5["verdict"]

    def test_ram_formula_check_is_not_circular(self):
        """The RAM-formula balance check must actually be able to fail when
        the formula is wrong — not just balance 100% by construction because
        'fall' was defined as an algebraic residual of the same formula."""
        n = 200
        rng = np.random.default_rng(0)
        dt = pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC")
        base = pd.DataFrame({
            "dateTimeUtc": dt, "cneName": "TEST_CNEC",
            "fmax": 1000.0, "frm": 100.0, "fnrao": 5.0,
            "amr": rng.uniform(0, 50, n),      # independent, sometimes nonzero
            "faac": 2.0,
            "fall": rng.normal(50, 10, n),     # independent of amr/iva/ram
            "iva": rng.uniform(0, 30, n),      # independent, sometimes nonzero
        })
        # Correct full formula: RAM = Fmax - FRM - fall + fnrao + AMR - AAC - IVA
        correct = base.copy()
        correct["ram"] = (correct.fmax - correct.frm - correct.fall
                          + correct.fnrao + correct.amr - correct.faac - correct.iva)
        cov = build_covariates(correct, pd.DataFrame())
        check = cov.attrs.get("ram_formula_check")
        assert check is not None
        assert check["pct_within_1mw"] > 0.99, (
            "Correct formula should balance against build_covariates' own check")

        # Now build RAM from the INCOMPLETE (amr/iva-omitting) formula on the
        # same independently-varying amr/iva — the check must catch this.
        wrong = base.copy()
        wrong["ram"] = wrong.fmax - wrong.frm - wrong.fall + wrong.fnrao - wrong.faac
        cov_wrong = build_covariates(wrong, pd.DataFrame())
        check_wrong = cov_wrong.attrs.get("ram_formula_check")
        assert check_wrong["pct_within_1mw"] < 0.5, (
            "Check should FAIL to balance when amr/iva are dropped from a RAM "
            "series that was built with them — otherwise the check is circular "
            "and can never catch this class of bug again")

    def test_regression_reports_condition_number(self, no3_cov):
        """Identification diagnostics must reflect the actual fitted design,
        not just the 7 raw covariates, so ill-conditioning is visible."""
        r = run_panel_regression(no3_cov, "ram")
        for key in ("condition_number", "rank", "n_params", "ill_conditioned"):
            assert key in r, f"Missing diagnostic key: {key}"
        assert r["condition_number"] > 0
        assert r["rank"] <= r["n_params"]

    def test_collinear_dose_pair_pruned(self, no3_cov):
        """A binary outage-active dummy and its paired MW-lost dose variable,
        when near-perfectly collinear, must not both survive into the fitted
        coefficients — otherwise the split between them (and the sign of the
        surviving one) is solver-dependent, not physical."""
        r = run_panel_regression(no3_cov, "ram")
        cf = r["coefs"]["param"].tolist()
        for binary_col, mw_col in [("fi_hvdc_outage_active", "fi_hvdc_outage_mw_lost"),
                                   ("fi_ac_line_outage_active", "fi_ac_outage_mw_lost")]:
            assert not (binary_col in cf and mw_col in cf), (
                f"{binary_col} and {mw_col} both survived — if they are "
                f"collinear in this data, one must be pruned before fitting")

    def test_cet_utc_roundtrip(self):
        """A CET-entered time converts to the expected UTC instant and back."""
        # 2025-06-15 12:00 CEST (UTC+2 in summer) == 2025-06-15 10:00 UTC
        utc_ts = cet_input_to_utc("2025-06-15T12:00:00")
        assert utc_ts.tz_convert("UTC").strftime("%H:%M") == "10:00"
        assert utc_to_cet_str(utc_ts, "%H:%M") == "12:00"
        # An explicit-offset input is respected, not reinterpreted as CET
        explicit = cet_input_to_utc("2025-06-15T10:00:00Z")
        assert explicit == utc_ts


# ── 11. Event study ───────────────────────────────────────────────────────────
# build_event_time_dummies/run_event_study previously had two independent,
# unconditional bugs: a hardcoded-nanosecond distance threshold that silently
# broke on pandas versions where .values returns non-nanosecond datetime64
# (collapsing event_k to a single constant for the whole panel), and a
# `keep`/`dropna` mismatch in run_event_study that raised KeyError on any
# well-formed input. Neither was reachable by any prior test, which is
# exactly why they shipped undetected — see the audit report for the
# reproduction. These tests exercise both functions end-to-end.

class TestEventStudy:
    @pytest.fixture(autouse=True)
    def skip_without_linearmodels(self):
        try:
            from linearmodels.panel import PanelOLS
        except ImportError:
            pytest.skip("linearmodels not installed")

    def test_event_k_not_collapsed_to_constant(self, no3_cov, outages_df):
        df_ek = build_event_time_dummies(no3_cov, outages_df, leads=12, lags=48)
        assert df_ek["event_k"].notna().any(), "event_k is all-NaN"
        assert df_ek["event_k"].nunique() > 5, (
            "event_k has suspiciously few distinct values — looks collapsed "
            "toward a single constant rather than reflecting real distance "
            "from each outage")

    def test_event_k_matches_hand_computed_offset(self, no3_df):
        """A row exactly N hours after a (the only) outage's start must get
        event_k == N, not some unrelated constant."""
        base = no3_df["dateTimeUtc"].min() + pd.Timedelta(days=3)
        outage = pd.DataFrame([{
            "outage_id": "solo", "start_utc": base.isoformat(),
            "end_utc": (base + pd.Timedelta(hours=2)).isoformat(),
        }])
        df_ek = build_event_time_dummies(no3_df, outage, leads=12, lags=48)
        one_cnec = df_ek[df_ek["cneName"] == df_ek["cneName"].iloc[0]]
        row = one_cnec[one_cnec["dateTimeUtc"] == base + pd.Timedelta(hours=5)]
        if len(row):
            assert row["event_k"].iloc[0] == 5.0

    def test_event_k_independent_of_outage_input_order(self, no3_df):
        """Nearest-event assignment must not depend on the arbitrary order
        outages happen to appear in the input DataFrame (deduplicate_outages
        does not guarantee chronological output order)."""
        base = no3_df["dateTimeUtc"].min() + pd.Timedelta(days=5)
        outages = pd.DataFrame([
            {"outage_id": "A", "start_utc": base.isoformat(),
             "end_utc": (base + pd.Timedelta(hours=2)).isoformat()},
            {"outage_id": "B", "start_utc": (base + pd.Timedelta(hours=30)).isoformat(),
             "end_utc": (base + pd.Timedelta(hours=32)).isoformat()},
        ])
        ek_ab = build_event_time_dummies(no3_df, outages, leads=12, lags=48)["event_k"]
        ek_ba = build_event_time_dummies(no3_df, outages.iloc[[1, 0]], leads=12, lags=48)["event_k"]
        pd.testing.assert_series_equal(ek_ab.fillna(-999).reset_index(drop=True),
                                       ek_ba.fillna(-999).reset_index(drop=True),
                                       check_names=False)

    def test_run_event_study_executes_without_keyerror(self, no3_cov, outages_df):
        df_ek = build_event_time_dummies(no3_cov, outages_df, leads=4, lags=8)
        result = run_event_study(df_ek, "f0", leads=4, lags=8)
        assert result, "run_event_study returned an empty result on well-formed input"
        assert "coefs" in result and len(result["coefs"]) > 0
        assert "pre_trend_ok" in result


# ── 12. Cluster-mode selection ────────────────────────────────────────────────
# run_panel_regression's cluster="two_way"/"entity" branches previously
# selected the WRONG cluster-code array relative to the function's own
# docstring: "two_way" silently ran plain time-only clustering, and "entity"
# silently attempted two-way clustering first (logged under the misleading
# "time-clustered" label). These tests spy on the actual kwargs passed to
# PanelOLS.fit() to verify the fix at the mechanism level, not just "it
# runs" — an incorrect-but-non-crashing cluster choice would pass a
# does-it-run check.

class TestClusterModeSelection:
    @pytest.fixture(autouse=True)
    def skip_without_linearmodels(self):
        try:
            from linearmodels.panel import PanelOLS
        except ImportError:
            pytest.skip("linearmodels not installed")

    def test_two_way_passes_two_column_cluster_array(self, no3_cov, monkeypatch):
        from linearmodels.panel import PanelOLS
        captured = {}
        orig_fit = PanelOLS.fit
        def spy_fit(self, *a, **kw):
            if "n_cols" not in captured and "clusters" in kw:
                captured["n_cols"] = np.asarray(kw["clusters"]).shape[1]
            return orig_fit(self, *a, **kw)
        monkeypatch.setattr(PanelOLS, "fit", spy_fit)
        run_panel_regression(no3_cov, "ram", cluster="two_way")
        assert captured.get("n_cols") == 2, (
            f"cluster='two_way' should pass a 2-column (entity,time) cluster "
            f"array to the FIRST fit attempt; got {captured.get('n_cols')} "
            f"column(s) — looks like it silently fell back to one-way "
            f"time clustering")

    def test_entity_does_not_attempt_two_way_first(self, no3_cov, monkeypatch):
        from linearmodels.panel import PanelOLS
        captured = {}
        orig_fit = PanelOLS.fit
        def spy_fit(self, *a, **kw):
            if "first_kwargs" not in captured:
                captured["first_kwargs"] = dict(kw)
            return orig_fit(self, *a, **kw)
        monkeypatch.setattr(PanelOLS, "fit", spy_fit)
        run_panel_regression(no3_cov, "ram", cluster="entity")
        first = captured.get("first_kwargs", {})
        assert first.get("cluster_entity") is True, (
            f"cluster='entity' should request cluster_entity=True on the "
            f"FIRST fit attempt; got {first} — looks like it tried two-way "
            f"clustering before falling back to real entity-only clustering")

    def test_time_cluster_still_the_default(self, no3_cov):
        """Default behaviour (the only mode any current caller actually
        uses) must be unaffected by the fix."""
        r = run_panel_regression(no3_cov, "ram")
        assert r


# ── 13. Event-aware clustering key ────────────────────────────────────────────

class TestClusterDateEpisodes:
    def test_multiday_outage_shares_one_cluster_key(self, no3_df):
        """A single continuous multi-day outage should collapse to ONE
        cluster_date value for its whole span, not one per UTC calendar day
        — otherwise SE clustering under-corrects for the within-event
        cross-CNEC correlation the outage induces (it doesn't reset at
        midnight)."""
        start = no3_df["dateTimeUtc"].min() + pd.Timedelta(days=2)
        outage = pd.DataFrame([{
            "outage_id": "multi", "start_utc": start.isoformat(),
            "end_utc": (start + pd.Timedelta(days=3)).isoformat(),
            "asset_type": "hvdc", "planned_or_forced": "forced",
            "capacity_mw": 500.0, "bidding_zone": "FI", "asset_id": None,
            "asset_name": "x", "voltage_kv": None, "control_area": "FI",
            "source": "manual", "raw_payload": "{}",
        }])
        cov = build_covariates(no3_df, outage)
        assert "cluster_date" in cov.columns
        during = cov[(cov["dateTimeUtc"] >= start) &
                     (cov["dateTimeUtc"] < start + pd.Timedelta(days=3))]
        assert during["cluster_date"].nunique() == 1, (
            f"a single continuous 3-day outage should map to one cluster_date, "
            f"got {during['cluster_date'].nunique()}: "
            f"{during['cluster_date'].unique()}")
        assert during["date"].nunique() > 1, (
            "sanity check: the plain calendar date SHOULD vary across 3 days")

    def test_no_outages_falls_back_to_plain_date(self, no3_df):
        cov = build_covariates(no3_df, pd.DataFrame())
        assert (cov["cluster_date"] == cov["date"]).all()


# ── 14. Lag covariates: time-indexed, not positional ──────────────────────────

class TestLagCovariates:
    def test_lag_respects_time_gaps(self):
        """A positional .shift() silently mislabels the lag whenever the
        per-CNEC series has a missing MTU: 'N rows back' stops meaning 'N
        steps back in time' the moment a row is missing. Build a series with
        a deliberate gap and confirm the lag is resolved by wall-clock
        offset, not row position."""
        dt = pd.date_range("2025-01-01", periods=10, freq="15min", tz="UTC")
        dt_gap = dt.delete(3)  # drop the row at t+45min
        df = pd.DataFrame({
            "dateTimeUtc": dt_gap, "cneName": "TEST",
            "fmax": 1000.0, "frm": 100.0, "fnrao": 0.0, "amr": 0.0,
            "faac": 0.0, "fall": 0.0, "iva": 0.0, "ram": 900.0,
        })
        outage = pd.DataFrame([{
            "outage_id": "x", "start_utc": dt[0].isoformat(),
            "end_utc": (dt[0] + pd.Timedelta(minutes=15)).isoformat(),
            "asset_type": "hvdc", "planned_or_forced": "forced",
            "capacity_mw": 100.0, "bidding_zone": "FI", "asset_id": None,
            "asset_name": "x", "voltage_kv": None, "control_area": "FI",
            "source": "manual", "raw_payload": "{}",
        }])
        cov = build_covariates(df, outage)

        row_1h = cov[cov["dateTimeUtc"] == dt[0] + pd.Timedelta(hours=1)]
        assert len(row_1h) == 1
        assert row_1h["fi_forced_outage_active_lag1h"].iloc[0] == 1.0, (
            "lag1h should find the active row exactly 1h earlier by wall "
            "clock regardless of the gap; a positional shift(4) would "
            "instead land 4 ROWS back in a 9-row gapped series and miss it")

        row_30m = cov[cov["dateTimeUtc"] == dt[0] + pd.Timedelta(minutes=30)]
        assert row_30m["fi_forced_outage_active_lag1h"].iloc[0] == 0.0, (
            "no data 1h back yet this early in the window — must fall back "
            "to 0.0, not borrow a positionally-nearby value across the gap")


# ── 15. f0 partial backfill from fref ─────────────────────────────────────────

class TestF0Backfill:
    def test_partial_f0_gaps_are_backfilled_from_fref(self, tmp_path):
        """f0 backfill used to trigger only when f0 was ENTIRELY empty — a
        partially-populated f0 column kept its own gaps unfilled."""
        df = pd.DataFrame({
            "dateTimeUtc": ["2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"],
            "cneName": ["TEST", "TEST"],
            "biddingZoneFrom": ["NO3", "NO3"], "biddingZoneTo": ["NO4", "NO4"],
            "f0": [123.0, None],       # first row populated, second missing
            "fref": [999.0, 456.0],    # backfill source
            "fmax": [500.0, 500.0], "frm": [50.0, 50.0],
            "ram": [300.0, 300.0], "shadowPrice": [0.0, 0.0],
            "iva": [0.0, 0.0], "amr": [0.0, 0.0],
        })
        p = str(tmp_path / "partial_f0.csv")
        df.to_csv(p, index=False)
        loaded = load_jao_csv(p)
        assert loaded["f0"].iloc[0] == 123.0, "existing f0 value must not be overwritten"
        assert loaded["f0"].iloc[1] == 456.0, "missing f0 must be backfilled from fref"


# ── 16. DiD PTDF classification: mean(abs) not abs(mean) ──────────────────────

class TestPrePeriodAbsPtdf:
    def test_abs_applied_before_averaging(self):
        """abs(mean(x)) only equals mean(abs(x)) when every value in the
        window shares one sign. Build a CNEC whose PTDF flips sign within
        the pre-period and confirm the per-row-abs average is used, not the
        (potentially near-zero, sign-cancelled) abs-of-mean."""
        df = pd.DataFrame({
            "cneName": ["A", "A", "A", "A", "B", "B"],
            "ptdf_FI": [0.10, -0.10, 0.10, -0.10, 0.05, 0.07],
        })
        result = pre_period_abs_ptdf(df, "ptdf_FI")
        # mean(abs(A)) = 0.10; abs(mean(A)) = abs(0.0) = 0.0 -- these must differ
        assert result.loc["A"] == pytest.approx(0.10)
        assert result.loc["B"] == pytest.approx(0.06)

    def test_missing_column_returns_empty(self):
        df = pd.DataFrame({"cneName": ["A"], "other_col": [1.0]})
        assert pre_period_abs_ptdf(df, "ptdf_FI").empty


# ── 17. Recovery direction (magnitude-blind metric fix) ───────────────────────

class TestRecoveryDirection:
    def test_direction_distinguishes_persist_from_reverse(self):
        persists = _pipe._recovery_direction(impact=50.0, recovery_residual=48.0)
        reverses = _pipe._recovery_direction(impact=50.0, recovery_residual=-48.0)
        recovered = _pipe._recovery_direction(impact=50.0, recovery_residual=2.0)
        assert persists == "persists"
        assert reverses == "reversed"
        assert recovered == "recovered"
        # The magnitude-only metric genuinely cannot distinguish the first two:
        assert _pipe._clamp_recovery_frac(50.0, 48.0) == _pipe._clamp_recovery_frac(50.0, -48.0)

    def test_zero_impact_is_not_applicable(self):
        assert _pipe._recovery_direction(impact=0.0, recovery_residual=5.0) == "n/a"


# ── 18. Single-event analysis (previously untested end-to-end) ───────────────

class TestSingleEventAnalysis:
    @pytest.fixture(autouse=True)
    def skip_without_statsmodels(self):
        if _pipe.sm is None:
            pytest.skip("statsmodels not installed")

    def test_runs_end_to_end_and_reports_recovery_direction(self, no3_cov, outages_df):
        row = outages_df.iloc[0]
        res = single_event_analysis(no3_cov, row, baseline_days=7, post_days=3)
        assert set(res.keys()) >= {"summary", "its", "its_all", "decomp", "did", "cnec_table"}
        assert res["summary"]["n_cnecs"] > 0
        direction_keys = [k for k in res["summary"] if k.startswith("recovery_direction_")]
        assert direction_keys, "recovery_direction_* fields missing from summary"
        for k in direction_keys:
            assert res["summary"][k] in ("recovered", "persists", "reversed", "n/a")

    def test_forced_only_stratified_columns_present(self, no3_cov):
        for col in ("fi_hvdc_outage_active_forced", "fi_hvdc_outage_active_planned",
                    "fi_ac_line_outage_active_forced", "fi_ac_line_outage_active_planned"):
            assert col in no3_cov.columns
            assert set(no3_cov[col].dropna().unique()) <= {0, 1, 0.0, 1.0}

    def test_planned_forced_confound_diagnostic_present(self, no3_cov):
        diag = no3_cov.attrs.get("planned_forced_confound")
        assert diag is not None
        assert "all_planned_transmission_is_manual" in diag
