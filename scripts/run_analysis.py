"""
scripts/run_analysis.py
=======================
CLI entry point for the FI -> NO3 propagation pipeline.

Usage:
    python scripts/run_analysis.py --jao data/jao_export.csv --out results/
    python scripts/run_analysis.py --jao data/jao_export.csv \\
        --outages data/manual_outages.csv --out results/ \\
        --entsoe-key $ENTSOE_API_KEY

Or run without arguments for interactive prompts.
"""
import argparse
import os
import sys
from pathlib import Path

# Ensure UTF-8 output on Windows consoles that default to cp1252
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow running from project root or scripts/ directory
_HERE   = Path(__file__).resolve().parent
_ROOT   = _HERE.parent
for p in [str(_ROOT / "src"), str(_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from fi_no3.propagation import (
    PipelineConfig, run_pipeline, load_jao_csv,
    load_manual_outages, render_html_report, summarize_hypotheses,
)
import pandas as pd


def main():
    parser = argparse.ArgumentParser(
        description="FI -> NO3 Flow-Based Propagation Analysis")
    parser.add_argument("--jao",      required=False,
                        help="Path to JAO CSV export")
    parser.add_argument("--outages",  default="data/manual_outages.csv",
                        help="Path to manual outage CSV (created if missing)")
    parser.add_argument("--out",      default="results",
                        help="Output directory")
    parser.add_argument("--start",    default="",
                        help="Outage fetch start (YYYY-MM-DDTHH:MM:SSZ); "
                             "defaults to JAO CSV start date")
    parser.add_argument("--end",      default="",
                        help="Outage fetch end; defaults to JAO CSV end date")
    parser.add_argument("--fingrid-key", default=os.environ.get("FINGRID_API_KEY",""),
                        help="Fingrid Open Data x-api-key")
    parser.add_argument("--entsoe-key", default=os.environ.get("ENTSOE_API_KEY",""),
                        help="ENTSO-E Transparency API token")
    parser.add_argument("--no-fingrid", action="store_true")
    parser.add_argument("--no-entsoe",  action="store_true")
    parser.add_argument("--synthetic", action="store_true",
                        help="Use synthetic data (no JAO CSV needed)")
    parser.add_argument("--days",    type=int, default=90,
                        help="Days of synthetic data (with --synthetic)")
    args = parser.parse_args()

    def log(msg): print(msg, flush=True)

    # ── Data source ────────────────────────────────────────────────────────
    if args.synthetic:
        from fi_no3.synthetic import generate_demo_dataset
        out_dir = args.out
        log(f"Generating {args.days} days of synthetic data...")
        info = generate_demo_dataset(out_dir + "/synthetic", days=args.days)
        jao_df     = load_jao_csv(info["jao_path"])
        outages_df = pd.read_csv(info["outages_path"])
        log(f"  JAO: {len(jao_df):,} rows | Outages: {len(outages_df)}")
    else:
        jao_path = args.jao
        if not jao_path:
            # Interactive prompt if not supplied
            jao_path = input("Path to JAO CSV: ").strip().strip('"').strip("'")
        if not Path(jao_path).exists():
            print(f"ERROR: JAO file not found: {jao_path}")
            sys.exit(1)
        log(f"Loading JAO: {jao_path}")
        jao_df = load_jao_csv(jao_path)
        log(f"  Rows: {len(jao_df):,} | CNECs: {jao_df['cneName'].nunique()}")
        log(f"  Window: {jao_df['dateTimeUtc'].min().date()} → "
            f"{jao_df['dateTimeUtc'].max().date()}")
        outages_df = None

    # ── Config ─────────────────────────────────────────────────────────────
    # If no explicit window given, default to JAO CSV dates
    if not args.start and jao_df is not None:
        args.start = jao_df["dateTimeUtc"].min().strftime("%Y-%m-%dT%H:%M:%SZ")
    if not args.end and jao_df is not None:
        args.end   = jao_df["dateTimeUtc"].max().strftime("%Y-%m-%dT%H:%M:%SZ")

    cfg = PipelineConfig(
        jao_csv    = args.jao or "",
        out_dir    = args.out,
        manual_csv = args.outages,
        start_utc  = args.start,
        end_utc    = args.end,
        use_entsoe = not args.no_entsoe,
        use_manual = True,
    )

    # ── Run ────────────────────────────────────────────────────────────────
    res = run_pipeline(cfg, jao_df=jao_df, outages_df=outages_df, log_cb=log)

    # ── Print results ──────────────────────────────────────────────────────
    print()
    print("=" * 65)
    print("HYPOTHESIS VERDICTS")
    print("=" * 65)
    for h in res["hypotheses"]:
        icon = "✓" if "SUPPORTED" in h["verdict"] or "CONSISTENT" in h["verdict"] \
               else ("?" if "inconclusive" in h["verdict"] or "absorbed" in h["verdict"]
                          or "logit not run" in h["verdict"] else "✗")
        print(f"  {icon} {h['id']}: {h['text']}")
        print(f"       → {h['verdict']}")
        print()

    print("=" * 65)
    print("REGRESSION SUMMARIES")
    print("=" * 65)
    for name, label in [("fall_signed", "fall_signed / F_allReference (H1)"),
                         ("ptdf_FI_abs", "ptdf_FI_abs / |PTDF_FI| (H2)"),
                         ("ram",         "RAM (H3)"),
                         ("shadowPrice", "Shadow price (H4)"),
                         ("frm",         "FRM (H6 placebo)")]:
        r = res["regressions"].get(name)
        if not r:
            print(f"  {label}: not available")
            continue
        print(f"  {label}: n={r['n_obs']:,}  R²_within={r['rsquared_within']:.3f}")
        cf = r["coefs"]
        outage_vars = ["fi_planned_outage_active", "fi_forced_outage_active",
                       "fi_hvdc_outage_active", "fi_ac_line_outage_active",
                       "fi_gen_outage_mw_lost"]
        for v in outage_vars:
            row = cf[cf.param == v]
            if not row.empty:
                p = row.p.iloc[0]; b = row.coef.iloc[0]
                sig = "**" if p < 0.01 else ("*" if p < 0.05 else ("." if p < 0.10 else " "))
                print(f"    {v:35s} β={b:>9.2f}  p={p:.4f} {sig}")
        print()

    logit = res.get("logit", {})
    if logit:
        print(f"  IVA logit: pseudo-R²={logit['pseudo_r2']:.3f}  "
              f"n_pos={logit['n_positive']}")

    # ── HTML report ────────────────────────────────────────────────────────
    def _reg_summary(name):
        r = res["regressions"].get(name)
        if not r:
            return f"No result for {name}.\n"
        lines = [f"Dependent: {r['dep']}",
                 f"Observations: {r['n_obs']:,}  R2_within={r['rsquared_within']:.4f}"]
        for _, row in r["coefs"].iterrows():
            sig = ("**" if row.p < 0.01 else ("*" if row.p < 0.05
                   else ("." if row.p < 0.10 else " ")))
            lines.append(f"  {row.param:40s}  b={row.coef:>9.3f}  p={row.p:.4f} {sig}")
        return "\n".join(lines)

    import datetime as _dt
    ctx = {
        "ts":              _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "n_jao":           len(jao_df) if jao_df is not None else 0,
        "n_no3":           len(res["no3"]),
        "n_outages":       len(res["outages"]),
        "hypotheses":      res["hypotheses"],
        "summary_f0":      _reg_summary("fall_signed"),
        "summary_ptdf_FI": _reg_summary("ptdf_FI_abs"),
        "summary_ram":     _reg_summary("ram"),
        "summary_shadowPrice": _reg_summary("shadowPrice"),
        "summary_frm":     _reg_summary("frm"),
        "summary_logit":   (f"pseudo-R2={logit['pseudo_r2']:.3f}  "
                            f"n_pos={logit['n_positive']}" if logit else "not run"),
        "figures": [],
        "caveats": (
            "Statistical power is heterogeneous across hypotheses: H1/H3 "
            "have many treatment hours; H5 only a handful. Confounders include "
            "Statnett's December 2024 FRM revision, seasonal Fmax derating on "
            "Fenno-Skan, and planned FI line outages (best curated manually). "
            "ENTSO-E A78 returns mostly forced events. FRM (H6) is a placebo: "
            "a significant result indicates model mis-specification."
        ),
    }
    report_path = render_html_report(res["out_dir"], ctx)

    print()
    print(f"Outputs saved to: {res['out_dir']}/")
    print("  no3_with_outage_covariates.csv")
    print("  outages_unified.csv")
    print(f"  report.html  (open in browser)  [{report_path}]")


if __name__ == "__main__":
    main()
