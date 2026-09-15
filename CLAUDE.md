# FI -> NO3 Flow-Based Propagation — Claude Code Project

## What this project does
Validates whether Finnish (FI) maintenance outages propagate to NO3 CNEC
parameters (F0, PTDF_FI, RAM, shadow price) in the Nordic day-ahead
flow-based capacity calculation. Uses real JAO publication-tool data and
ENTSO-E outage events. Runs PanelOLS regressions and produces an HTML report.

This is the `fi_no3` installable package (see `pyproject.toml`) — the repo
root doubles as the package root (`package-dir = {"fi_no3" = "."}`). This
branch (`fi-no3-app`) carries only this package, flattened to the repo
root; it was split off from `claude/energy-model-audit-fafl07`, which still
keeps this same code nested under a `file/` directory alongside an older,
independent flat-file copy of the same project (the one this branch's own
root-level files replaced). `Old/` in this listing predates both of those —
it's an even earlier generation of the flat-file app, inherited as-is from
before the fi_no3 package existed; it isn't part of this package and isn't
touched by anything in this file.

## File layout (flat — all in the repo root, doubles as the `fi_no3` package)
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
- run_panel_regression()'s cluster codes MUST be passed to
  `PanelOLS.fit(cov_type="clustered", clusters=...)` as (entity, time)
  -indexed pandas objects (a Series for one-way, a DataFrame for two-way),
  never `.values`/`.values.reshape(...)`. linearmodels re-wraps whatever it
  receives in its own PanelData and validates ITS inferred entity/time
  shape against the model's; an index-less NumPy array can never satisfy
  that, so it always raised "clusters must have the same number of
  entities and time periods as the model data" — meaning time-clustered
  and two-way-clustered SEs could never actually succeed until this was
  fixed, and every regression silently fell back to entity-clustered
  (verified via an isolated linearmodels repro and a live run showing
  every H1-H6 verdict's `se=` field). If you ever see that exact error in
  the logs again, this is almost certainly the regression to check first.
- The SE fallback chain (requested mode first, then time-clustered,
  entity-clustered, robust, unadjusted in that order) is recorded, not
  silent: every regression result carries `cov_type_used` and
  `se_fallback_occurred`, and summarize_hypotheses() appends `n=…, se=…`
  to every H1-H4/H6 verdict so a reader can see when a result rests on
  something weaker than the requested clustering. run_pipeline() always
  requests `cluster="time"`, which now actually succeeds (see above) —
  `"two_way"` is supported by run_panel_regression() but not currently
  invoked anywhere in the pipeline.
- JAO's own Nordic Publication Handbook (v1.7, fetched directly from
  publicationtool.jao.eu) documents that its "dateTimeUtc" API field can
  actually be CET/CEST wall-clock despite the name (verbatim: "'dateTimeUtc':
  CET time stamp (yes… CET?!)"), for the same fbDomainShadowPrice endpoint
  this codebase's live fetch uses. This has NOT been confirmed against a
  live fetch from here — it's a documented possibility, not a proven defect
  in every JAO export — so the default stays "UTC" (unchanged original
  behaviour). `jao_datetime_to_utc()` / `load_jao_csv(jao_timestamp_zone=...)`
  in propagation.py are the conversion points; every fetch/load surface that
  touches real (non-synthetic) JAO data has its OWN independent "UTC"/"CET"
  toggle, deliberately not shared, so you can try a different assumption on
  one tab (e.g. re-running Tab 9's analysis under "CET") without disturbing
  another (e.g. Tab 1's already-fetched display): dashboard.py's Real JAO
  CSV section, app_jao_NP_API_fix_d14.py's Tab 1 (covers both Option A
  upload and Option B live fetch) and Tab 9 Data Source section, and
  run_analysis.py's `--jao-timestamp-zone` flag. The live-fetch path
  (app_jao_NP_API_fix_d14.py's `fetch_day_via_powershell`) also runs an
  automatic `_check_jao_tz_assumption()` diagnostic per day fetched: since
  it knows the exact requested CET calendar-day boundary, it checks whether
  the earliest returned row starts near 00:00 raw (consistent with
  mislabeled-CET) or ~22:00-23:00 the previous day (consistent with genuine
  UTC) and warns in the log if that pattern disagrees with the current
  toggle — this is a heuristic, not proof, but it's a season-independent,
  cheap, per-fetch cross-check rather than requiring a user to notice wrong
  results on their own.
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
- With few independent outage episodes, a binary outage-active dummy and its
  paired MW-lost dose variable can be near-perfectly collinear;
  run_panel_regression() calls _prune_collinear_dose_pairs() before fitting,
  which drops the binary duplicate and keeps the dose variable in that case
  (falling back to a deterministic column drop when neither/both candidates
  are binary), rather than let the estimator split the coefficient
  arbitrarily between them. This applies to any `indep` list, not just the
  built-in H1-H6 specs.
- Economic significance is checked alongside statistical significance:
  ECONOMIC_SIGNIFICANCE_THRESHOLDS (propagation.py) sets a conservative,
  documented minimum-magnitude floor per dependent variable; a coefficient
  that clears p<0.05 but not that floor is reported as "statistically
  significant" rather than "SUPPORTED"/"SIGNIFICANT". These floors are not
  calibrated against live market data — adjust them if a better-grounded
  number becomes available.
- Credentials (ENTSO-E token, any Nord Pool key) are still hardcoded as
  source constants, and app_jao_NP_API_fix_d14.py's JAO fetch still shells
  out to Windows PowerShell per calendar day — both known, currently
  out-of-scope limitations, not oversights.
