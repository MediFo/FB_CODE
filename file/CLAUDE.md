# FI -> NO3 Flow-Based Propagation — Claude Code Project

## What this project does
Validates whether Finnish (FI) maintenance outages propagate to NO3 CNEC
parameters (F0, PTDF_FI, RAM, shadow price) in the Nordic day-ahead
flow-based capacity calculation. Uses real JAO publication-tool data and
ENTSO-E outage events. Runs PanelOLS regressions and produces an HTML report.

This is the `fi_no3` installable package (see `pyproject.toml`) — the flat
directory it lives in doubles as the package root (`package-dir = {"fi_no3"
= "."}`). It supersedes the root-level flat copy of the same modules one
directory up; that root copy is being kept only as historical reference.

## File layout (flat — all in this directory, doubles as the `fi_no3` package)
```
__init__.py                    — re-exports the fi_no3 public API
dashboard.py                   — tkinter 6-tab GUI  ← MAIN ENTRY POINT
app_jao_NP_API_fix_d14.py      — tkinter 9-tab GUI, adds JAO/Nord Pool fetch
                                  tooling (day-batched PowerShell fetch) on
                                  top of the same propagation.py backend;
                                  d07–d13 are superseded prior iterations,
                                  kept for reference only — d14 is current
propagation.py                 — analytical pipeline (no UI code)
synthetic.py                   — synthetic JAO + outage data generator for testing
test_pipeline.py                — full test suite (pytest)
run_analysis.py                — CLI entry point (also installed as the
                                  fi-no3-analyse console script)
pyproject.toml                 — packaging: `pip install -e .` gives you
                                  `fi-no3-analyse` / `fi-no3-dash` console
                                  scripts
manual_outages.csv             — hand-curated FI outage events (edit this)
ma_output/                     — generated Maintenance-Analysis outputs (Tab 9)
map.png, map2.png, Slide1w.PNG — reference images used by the GUIs
```

## How to run
```bash
# Editable install (one time) — also creates the fi-no3-analyse /
# fi-no3-dash console scripts
pip install -e .
# or, without installing: pip install pandas numpy matplotlib requests jinja2 \
#   statsmodels linearmodels entsoe-py

# Launch the main dashboard
python dashboard.py
# or, if installed:
fi-no3-dash

# Launch the extended 9-tab GUI (JAO/Nord Pool fetch tooling)
python app_jao_NP_API_fix_d14.py

# CLI (no GUI)
python run_analysis.py --synthetic --days 30 --out results/report.html
# or, if installed:
fi-no3-analyse --synthetic --days 30 --out results/report.html

# Run tests
pytest test_pipeline.py -v
```

## Key domain facts Claude Code should know
- Nordic RAM formula: RAM = Fmax - FRM - fall + fnrao + AMR - FAAC - IVA
  (build_covariates() and decompose_delta_ram() implement all 7 terms — a
  previous version silently dropped AMR/IVA; the only in-repo check of that
  was circular on synthetic data, see git history)
- fall = F_allReference (NOT fref/f0 which is the CGMA-NP reference flow)
- NO3 corridor CNECs: "300KLABU-ORKDAL", "300VERDAL-TUNNSJODAL", "300AURA-VAGAMO" etc.
  Some CNECs appear as UUIDs — the NO3 filter uses biddingZoneFrom/To == "NO3"
- ENTSO-E token: hardcoded as ENTSOE_TOKEN constant in propagation.py
- ENTSO-E A77 = production outages; A78 = transmission outages
- Confirmed live (entsoe-py 0.8.0): both A77 and A78 return tz-AWARE
  timestamps localized to the queried country's own zone (Europe/Helsinki for
  FI, Europe/Stockholm for SE, Europe/Tallinn for EE) — not naive, not UTC,
  not CET. propagation.py converts these to UTC internally, which is correct.
- User-facing time convention is CET/CEST (Europe/Oslo), everywhere: the
  dashboard's date-window fields, logs, single-event summaries and the HTML
  report timestamp are all CET. Internal storage/computation (JAO alignment,
  regressions, CSV columns) stays UTC. propagation.cet_input_to_utc() and
  utc_to_cet_str() are the two conversion points — route any new human-facing
  time field through them rather than adding another ad-hoc conversion.
- A78 returns mostly forced events (BSNTYPE A54); planned line outages need manual CSV
- manual_outages.csv start_utc/end_utc: if you paste a time with no explicit
  offset, it's interpreted as CET/CEST (not UTC) — matching how a human
  actually thinks when typing into that file.
- FRM is structural (yearly calibration); it should NOT move with individual outages (H6 placebo)
- Two-way clustered SE fails on unbalanced panels -> silent fallback to entity-only clustering
- Outage covariates are zero when no outage overlaps JAO window -> verdicts show as n/a
- app_jao_NP_API_fix_d14.py's data fetch is day-batched (one PowerShell/
  Invoke-WebRequest call per calendar day, CET-aligned) rather than
  per-15-minute-MTU; it still shells out to Windows PowerShell, so it only
  runs on Windows or somewhere PowerShell Core is installed.
- app_jao_NP_API_fix_d14.py's Strategic Briefing sub-tab (under Tab 2) is a
  UI shell only — its generate/export handlers show an honest "not
  implemented" message rather than silently doing nothing or fabricating
  output. Use the Analysis sub-tab or Tab 9 (Maintenance Analysis) instead.

## Known limitations
- ENTSO-E API returning 403 from cloud/server IPs is environment-dependent,
  not universal — a live call succeeded from this Claude Code sandbox's
  outbound proxy. Don't assume either outcome; test from wherever you're
  actually deploying.
- ENTSO-E A77 returns "File is not a zip file" when no FI production outages exist in window
- IVA is zero on NO3 CNECs in short windows; H5 logit needs longer history
- FRM in synthetic data moves with outages (December 2024 regime change is encoded)
- Two-way clustered SE always falls back to entity-only for unbalanced panels
- With few independent outage episodes, a binary outage-active dummy and its
  paired MW-lost dose variable can be near-perfectly collinear;
  run_panel_regression() drops the binary duplicate and keeps the dose
  variable in that case (see _prune_collinear_dose_pairs) rather than let the
  estimator split the coefficient arbitrarily between them.
- Credentials (ENTSO-E token, any Nord Pool key) are still hardcoded as
  source constants, and app_jao_NP_API_fix_d14.py's JAO fetch still shells
  out to Windows PowerShell per calendar day — both known, currently
  out-of-scope limitations, not oversights.
