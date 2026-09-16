"""
scripts/run_analysis.py
=======================
CLI entry point for the Nordic flow-based propagation pipeline. Runs a
single source-country -> bidding-zone pair (FI -> NO3 by default) or, with
--all-nordic-zones, sweeps every Nordic source-country x target-zone pair.

Usage:
    python scripts/run_analysis.py --jao data/jao_export.csv --out results/
    python scripts/run_analysis.py --jao data/jao_export.csv --source SE --target NO1
    python scripts/run_analysis.py --jao data/jao_export.csv --all-nordic-zones --out results/

Or run without arguments for interactive prompts.
"""
import argparse
import os
import sys
from pathlib import Path

# propagation.py is a flat module next to this script (see CLAUDE.md's
# documented "flat — all in project root" layout) -- there is no installed
# `fi_no3` package to import from unless this project has been `pip install
# -e`'d (see pyproject.toml's package-dir mapping). Support both: prefer an
# installed `fi_no3` package if present, otherwise fall back to importing
# the flat module directly.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

try:
    from fi_no3.propagation import (
        PipelineConfig, run_pipeline, load_jao_csv,
        load_manual_outages, render_html_report, summarize_hypotheses,
        utc_to_cet_str, build_report_ctx, run_nordic_matrix,
        render_nordic_matrix_report, NORDIC_SOURCE_COUNTRIES, NORDIC_TARGET_ZONES,
    )
except ImportError:
    from propagation import (
        PipelineConfig, run_pipeline, load_jao_csv,
        load_manual_outages, render_html_report, summarize_hypotheses,
        utc_to_cet_str, build_report_ctx, run_nordic_matrix,
        render_nordic_matrix_report, NORDIC_SOURCE_COUNTRIES, NORDIC_TARGET_ZONES,
    )
import pandas as pd


def main():
    parser = argparse.ArgumentParser(
        description="Nordic Flow-Based Propagation Analysis "
                    "(any Nordic source-country -> bidding-zone pair, or "
                    "--all-nordic-zones for the full sweep)")
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
    parser.add_argument("--effect-scale", type=float, default=1.0,
                        help="Scale outage-induced effects in synthetic data "
                             "(with --synthetic); 1.0 = large/easy-to-detect "
                             "default, e.g. 0.1 for a small, economically "
                             "realistic effect size")
    parser.add_argument("--all-nordic-zones", action="store_true",
                        help="Batch mode: run the pipeline across every Nordic "
                             "source-country x target-zone pair (default: "
                             f"sources={','.join(NORDIC_SOURCE_COUNTRIES)}, "
                             f"targets={','.join(NORDIC_TARGET_ZONES)}) instead "
                             "of the single --source/--target pair, and write "
                             "one consolidated nordic_matrix_report.html plus "
                             "a per-pair report.html under --out/<SRC>_<TGT>/.")
    parser.add_argument("--source-countries", default="",
                        help="Comma-separated ENTSO-E country codes to sweep "
                             "with --all-nordic-zones (default: all Nordic "
                             f"countries, {','.join(NORDIC_SOURCE_COUNTRIES)})")
    parser.add_argument("--target-zones", default="",
                        help="Comma-separated bidding-zone codes to sweep "
                             "with --all-nordic-zones (default: all Nordic "
                             f"zones, {','.join(NORDIC_TARGET_ZONES)})")
    parser.add_argument("--source", default="FI",
                        help="ENTSO-E country code for the outage source "
                             "(single-pair mode only; ignored with "
                             "--all-nordic-zones)")
    parser.add_argument("--target", default="NO3",
                        help="Bidding zone to analyse / CNEC filter "
                             "(single-pair mode only; ignored with "
                             "--all-nordic-zones)")
    parser.add_argument("--jao-timestamp-zone", choices=["UTC", "CET"], default="UTC",
                        help="What timezone the JAO CSV's dateTimeUtc column "
                             "is ACTUALLY in (with --jao; ignored with "
                             "--synthetic, which always generates genuine "
                             "UTC). Default UTC trusts the field at face "
                             "value, matching this pipeline's original "
                             "behaviour. JAO's own Nordic Publication "
                             "Handbook documents that despite the field's "
                             "name, its values can actually be CET/CEST -- "
                             "if outage/event alignment looks consistently "
                             "off by exactly 1h (winter) or 2h (summer), "
                             "try --jao-timestamp-zone CET.")
    args = parser.parse_args()

    def log(msg): print(msg, flush=True)

    # ── Data source ────────────────────────────────────────────────────────
    if args.synthetic:
        try:
            from fi_no3.synthetic import generate_demo_dataset
        except ImportError:
            from synthetic import generate_demo_dataset
        out_dir = args.out
        log(f"Generating {args.days} days of synthetic data...")
        info = generate_demo_dataset(out_dir + "/synthetic", days=args.days,
                                     effect_scale=args.effect_scale)
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
        if args.jao_timestamp_zone == "CET":
            log("  NOTE: treating dateTimeUtc as CET/CEST wall-clock, not "
                "true UTC, per --jao-timestamp-zone CET.")
        jao_df = load_jao_csv(jao_path, jao_timestamp_zone=args.jao_timestamp_zone)
        log(f"  Rows: {len(jao_df):,} | CNECs: {jao_df['cneName'].nunique()}")
        # CET, matching this codebase's documented human-facing time
        # convention -- a raw .date() here would show the previous UTC
        # calendar day for any window starting just after CET midnight.
        log(f"  Window: {utc_to_cet_str(jao_df['dateTimeUtc'].min(), '%Y-%m-%d')} → "
            f"{utc_to_cet_str(jao_df['dateTimeUtc'].max(), '%Y-%m-%d')} CET")
        outages_df = None

    # ── Config ─────────────────────────────────────────────────────────────
    # If no explicit window given, default to JAO CSV dates
    if not args.start and jao_df is not None:
        args.start = jao_df["dateTimeUtc"].min().strftime("%Y-%m-%dT%H:%M:%SZ")
    if not args.end and jao_df is not None:
        args.end   = jao_df["dateTimeUtc"].max().strftime("%Y-%m-%dT%H:%M:%SZ")

    # NOTE: PipelineConfig has no fingrid_api_key/entsoe_api_key/use_fingrid
    # fields -- there is no Fingrid integration anywhere in this codebase
    # (propagation.py only fetches ENTSO-E outages, via the hardcoded
    # ENTSOE_TOKEN module constant, not a per-run key), so passing those
    # kwargs used to crash this script immediately with a TypeError. Warn
    # once if the user supplied flags that don't correspond to real
    # functionality, rather than silently dropping them or crashing.
    if args.fingrid_key or args.no_fingrid:
        log("NOTE: --fingrid-key/--no-fingrid have no effect -- this "
            "codebase has no Fingrid data source.")
    if args.entsoe_key:
        log("NOTE: --entsoe-key has no effect -- propagation.py uses a "
            "fixed ENTSOE_TOKEN constant, not a per-run key.")

    cfg = PipelineConfig(
        jao_csv        = args.jao or "",
        out_dir        = args.out,
        manual_csv     = args.outages,
        start_utc      = args.start,
        end_utc        = args.end,
        use_entsoe     = not args.no_entsoe,
        use_manual     = True,
        source_country = args.source.upper(),
        target_zone    = args.target.upper(),
        jao_timestamp_zone = args.jao_timestamp_zone,
    )

    # ── All-Nordic-zones batch mode ───────────────────────────────────────────
    if args.all_nordic_zones:
        src_list = ([s.strip().upper() for s in args.source_countries.split(",") if s.strip()]
                   or list(NORDIC_SOURCE_COUNTRIES))
        tgt_list = ([t.strip().upper() for t in args.target_zones.split(",") if t.strip()]
                   or list(NORDIC_TARGET_ZONES))
        log(f"Running all-Nordic-zones sweep: {len(src_list)} source countries x "
            f"{len(tgt_list)} target zones = {len(src_list) * len(tgt_list)} pairs...")
        matrix = run_nordic_matrix(cfg, jao_df=jao_df, outages_df=outages_df,
                                   source_countries=src_list, target_zones=tgt_list,
                                   log_cb=log)
        index_path = render_nordic_matrix_report(cfg.out_dir, matrix)

        print()
        print("=" * 65)
        print("NORDIC BIDDING-ZONE SWEEP SUMMARY")
        print("=" * 65)
        for (src, tgt), pair in matrix["pairs"].items():
            n_sig = sum(1 for h in pair["hypotheses"]
                       if h["id"] in ("H1", "H2", "H3", "H4")
                       and ("SIGNIFICANT" in h["verdict"] or "SUPPORTED" in h["verdict"]))
            print(f"  {src:>3} -> {tgt:<4}  {n_sig}/4 significant  "
                 f"(n_no3={pair['n_no3']:,}, n_outages={pair['n_outages']})")
        if matrix["skipped"]:
            print()
            print(f"  {len(matrix['skipped'])} pair(s) skipped (no CNECs/outage overlap/regression failure):")
            for s in matrix["skipped"]:
                print(f"    {s['source_country']} -> {s['target_zone']}: "
                     f"{str(s['reason']).splitlines()[0][:120]}")

        print()
        print(f"Outputs saved to: {matrix['out_dir']}/")
        print(f"  nordic_matrix_report.html  (open in browser — {index_path})")
        print("  <SRC>_<TGT>/report.html  (per-pair detail, one per successful pair)")
        return

    # ── Run (single source/target pair) ───────────────────────────────────────
    res = run_pipeline(cfg, jao_df=jao_df, outages_df=outages_df, log_cb=log)
    report_path = render_html_report(cfg.out_dir, build_report_ctx(res, n_jao=len(jao_df)))

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
    _src = cfg.source_country.lower()
    for name, label in [("f0", cfg.source_country + " reference flow (F0)"),
                         (f"ptdf_{cfg.source_country}_abs", f"|PTDF_{cfg.source_country}|"),
                         ("ram", "RAM"), ("shadowPrice", "Shadow price"),
                         ("frm", "FRM (H6 placebo)")]:
        r = res["regressions"].get(name)
        if not r:
            print(f"  {label}: not available")
            continue
        print(f"  {label}: n={r['n_obs']:,}  R²_within={r['rsquared_within']:.3f}")
        cf = r["coefs"]
        outage_vars = [f"{_src}_planned_outage_active", f"{_src}_forced_outage_active",
                       f"{_src}_hvdc_outage_active", f"{_src}_ac_line_outage_active",
                       f"{_src}_gen_outage_mw_lost"]
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

    print()
    print(f"Outputs saved to: {res['out_dir']}/")
    print("  no3_with_outage_covariates.csv")
    print("  outages_unified.csv")
    print("  report.html  (open in browser)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        # A raw Python traceback is not a useful CLI error for a data or
        # config problem (bad path, malformed CSV, unreachable API) -- print
        # a clear one-line message and exit non-zero. Re-raise unexpected
        # internal errors with the full traceback so they're still
        # debuggable, distinguished from the "known input problem" cases.
        print(f"ERROR: {e}", file=sys.stderr)
        if os.environ.get("FI_NO3_DEBUG"):
            raise
        sys.exit(1)
