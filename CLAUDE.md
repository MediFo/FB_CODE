# Nordic Flow-Based Propagation — Claude Code Project

## What this project does
Validates whether a Nordic country's maintenance outages propagate to a
given bidding zone's CNEC parameters (F0/fall, PTDF, RAM, shadow price) in
the Nordic day-ahead flow-based capacity calculation. Uses real JAO
publication-tool data and ENTSO-E outage events. Runs PanelOLS regressions
and produces an HTML report.

The pipeline was originally scoped to a single FI -> NO3 check, but
`run_pipeline()` has always taken `source_country`/`target_zone` as
parameters (see `PipelineConfig`), so any Nordic source country
(FI/SE/NO/DK) can be checked against any Nordic target zone
(FI, SE1-4, NO1-5, DK1-2) in one run. `run_nordic_matrix()` (propagation.py)
sweeps the full source x target grid in one go and produces a consolidated
`nordic_matrix_report.html` alongside each pair's own `report.html` — use
`run_analysis.py --all-nordic-zones` (optionally with
`--source-countries`/`--target-zones` to narrow the sweep) rather than
re-running `--source`/`--target` one pair at a time. FI -> NO3 remains the
best-tested pair (it's what the synthetic generator and most of the manual
outage curation target) but nothing in the pipeline is FI/NO3-specific
anymore; `_ZONE_PATTERNS`/`_KNOWN_BORDERS` in propagation.py also carry
Baltic zones (EE/LV/LT) for the same FB coupling, though those are excluded
from the *default* Nordic sweep (`NORDIC_SOURCE_COUNTRIES`/
`NORDIC_TARGET_ZONES`) — pass them explicitly via `run_nordic_matrix`'s
`source_countries`/`target_zones` args if needed.

This is the `fi_no3` installable package (see `pyproject.toml`) — the repo
root doubles as the package root (`package-dir = {"fi_no3" = "."}`). This
branch (`fi-no3-app`) carries only this package, flattened to the repo
root; it was split off from `claude/energy-model-audit-fafl07`, which still
keeps this same code nested under a `file/` directory alongside an older,
independent flat-file copy of the same project (the one this branch's own
root-level files replaced). An even earlier generation of the flat-file
app, inherited as-is from before the fi_no3 package existed under `Old/`,
was removed in a repo cleanup — it wasn't part of this package.

## File layout (flat — all in the repo root, doubles as the `fi_no3` package)
```
__init__.py                    — re-exports the fi_no3 public API
dashboard.py                   — tkinter 6-tab GUI  ← MAIN ENTRY POINT
app_jao_NP_API_fix_d14.py      — tkinter 9-tab GUI, adds JAO/Nord Pool fetch
                                  tooling (day-batched PowerShell fetch) on
                                  top of the same propagation.py backend.
                                  d07–d13, its superseded prior iterations,
                                  were removed (repo cleanup) — d14 was
                                  always the only one referenced by anything
                                  else.
propagation.py                 — analytical pipeline (no UI code)
check_entsoe.py                — standalone ENTSO-E connectivity diagnostic
                                  (DNS/TCP → TLS → raw HTTPS → entsoe-py, in
                                  that order); run this first whenever an
                                  ENTSO-E fetch returns 0 events and you
                                  can't tell if that's genuine or a network/
                                  firewall/proxy problem — it has no GUI/
                                  pandas-stack dependency of its own
synthetic.py                   — synthetic JAO + outage data generator for testing
test_pipeline.py                — full test suite (pytest)
run_analysis.py                — CLI entry point (also installed as the
                                  fi-no3-analyse console script)
pyproject.toml                 — packaging: `pip install -e .` gives you
                                  `fi-no3-analyse` / `fi-no3-dash` console
                                  scripts
requirements.txt                — plain dependency list (core + regression +
                                  entsoe-py + pytest, mirrors pyproject.toml)
                                  for `pip install -r requirements.txt`
                                  without installing this package itself
manual_outages.csv             — hand-curated outage events for any Nordic
                                  source country (edit this; bidding_zone
                                  column selects which country/zone a row
                                  belongs to)
ma_output/                     — generated Maintenance-Analysis outputs (Tab 9);
                                  gitignored except a .gitkeep placeholder —
                                  don't commit run output here
map.png, map2.png, Slide1w.PNG — reference images used by the GUIs
```

## How to run
```bash
# Editable install (one time) — also creates the fi-no3-analyse /
# fi-no3-dash console scripts
pip install -e .
# or, without installing: pip install -r requirements.txt
# Either way, install into the SAME Python/venv you'll actually run the
# scripts with -- a missing statsmodels/linearmodels/entsoe-py doesn't
# crash the app, it silently degrades (empty regressions/DiD, "0 events"
# fetches) with the real cause easy to miss; app_jao_NP_API_fix_d14.py
# now surfaces these explicitly where they matter (see below) but
# installing everything up front avoids hitting them one at a time.

# Launch the main dashboard
python dashboard.py
# or, if installed:
fi-no3-dash

# Launch the extended 9-tab GUI (JAO/Nord Pool fetch tooling)
python app_jao_NP_API_fix_d14.py

# CLI (no GUI) — single source/target pair (defaults to FI -> NO3)
python run_analysis.py --synthetic --days 30 --out results/
python run_analysis.py --jao data/jao_export.csv --source SE --target NO1 --out results/
# or, if installed:
fi-no3-analyse --synthetic --days 30 --out results/

# CLI — all Nordic bidding zones in one sweep (writes nordic_matrix_report.html
# plus a per-pair results/<SRC>_<TGT>/report.html for every pair)
python run_analysis.py --jao data/jao_export.csv --all-nordic-zones --out results/
# narrow the sweep:
python run_analysis.py --jao data/jao_export.csv --all-nordic-zones \
    --source-countries FI,SE --target-zones NO1,NO2,NO3 --out results/

# Run tests
pytest test_pipeline.py -v
```

## Key domain facts Claude Code should know
- All-Nordic-zones batch mode: `run_nordic_matrix()` in propagation.py runs
  `run_pipeline()` once per (source_country, target_zone) pair — defaulting
  to `NORDIC_SOURCE_COUNTRIES` (FI, SE, NO, DK) x `NORDIC_TARGET_ZONES`
  (FI, SE1-4, NO1-5, DK1-2) — loading the JAO CSV once and fetching outages
  once per source country (not once per pair) since the target zone is just
  a CNEC-name filter over the same JAO export. A pair with no matching CNECs
  or no overlapping outage data is recorded in the result's "skipped" list
  with its reason rather than aborting the sweep; `build_report_ctx()` is
  the ctx builder both the single-pair CLI path and each matrix pair use to
  call `render_html_report()`, and `render_nordic_matrix_report()` writes
  the consolidated `nordic_matrix_report.html` index (verdict-count matrix,
  links to each pair's own report.html). Only wired into run_analysis.py
  (`--all-nordic-zones`) so far — dashboard.py and
  app_jao_NP_API_fix_d14.py's GUIs still only run one source/target pair
  per click; extending them to launch a matrix sweep is a natural follow-up
  but hasn't been done.
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
- ENTSO-E A78 (transmission unavailability) rows only carry `avail_qty` —
  the capacity STILL AVAILABLE on that border during the outage — with no
  nominal/rated capacity in entsoe-py's A78 parser to net it against.
  This is NOT the same quantity as A77 (production), which carries both
  `nominal_power` and `avail_qty`, so `cap_lost = nominal - avail` is
  computable there. `capacity_mw` is treated everywhere downstream
  (`build_covariates()`'s `*_hvdc_outage_mw_lost` / `*_ac_outage_mw_lost`
  dose variables) as MW LOST — `fetch_entsoe_outages()` used to write raw
  `avail_qty` straight into `capacity_mw` for A78 rows, silently feeding
  the wrong quantity (available capacity, not lost capacity — roughly the
  inverse relationship) into those regressions' dose covariates. Fixed:
  A78 rows now leave `capacity_mw` unset (None/NULL) — `avail_qty` is kept
  under `avail_qty_mw` in `raw_payload` for reference, but there is
  currently no way to derive a genuine "MW lost" figure for a transmission
  outage from ENTSO-E data alone. The binary `*_hvdc_outage_active` /
  `*_ac_line_outage_active` dummies (built from the outage interval, not
  capacity_mw) are unaffected and remain the primary H1/H2 treatment
  variables — only the secondary continuous dose covariates lose their
  A78 contribution; a genuine dose is still available wherever a manual
  CSV row (human-curated as lost capacity directly) or an A77 row covers
  the window.
- Outage covariates are zero when no outage overlaps JAO window -> verdicts show as n/a
- app_jao_NP_API_fix_d14.py's data fetch is day-batched (one PowerShell/
  Invoke-WebRequest call per calendar day, CET-aligned) rather than
  per-15-minute-MTU; it still shells out to Windows PowerShell, so it only
  runs on Windows or somewhere PowerShell Core is installed.
- app_jao_NP_API_fix_d14.py's Strategic Briefing sub-tab (under Tab 2) is a
  UI shell only — its generate/export handlers show an honest "not
  implemented" message rather than silently doing nothing or fabricating
  output. Use the Analysis sub-tab or Tab 9 (Maintenance Analysis) instead.
- ITS counterfactual models: `_ITS_METHODS` in propagation.py now has 13
  entries — the original seasonal_naive/fourier_trend/stl/arima/sarima plus
  "lightgbm"/"catboost" (gradient-boosted trees), "ridge" (closed-form
  linear), "structural" (state-space Kalman filter), "tbats", "theta",
  "hurdle" (zero-inflated, for shadow price), and "ensemble" (adaptive
  blend of all the others) — see the three entries below this one for the
  latter five. The three
  lag-feature methods (lightgbm, catboost, ridge) share one
  `_its_lag_model()` implementation for feature engineering + the
  leak-avoidance recursive during/post projection — `_its_gbm(...,
  backend=...)` and `_its_ridge()` each just supply a
  `fit_predict(X_train, y_train) -> predict` closure to it. Features:
  lag1d/lag2d/lag7d + their mean (a simplified Neighborhood Days Approach /
  NDA) plus cyclical hour/weekday features. Rationale: lag features are
  reported to consistently improve forecast accuracy across model families
  (Ridge, XGBoost, CatBoost, LightGBM alike) more than model choice does —
  it's the input representation, not model sophistication, doing most of
  the work; NDA extends the classic lag24 feature to a small neighborhood
  of nearby days, distinct from DSTA (day-of-same-type, e.g. previous
  Mondays), which seasonal_naive/Fourier/STL already capture via
  (hour, weekday) grouping. LightGBM/CatBoost handle the resulting
  NaN-heavy lag columns (start of pre-period, gaps) natively; ridge instead
  mean-imputes from TRAINING-set-only column statistics inside
  `_ClosedFormRidge` (see the next entry for a bug this needed fixing).
  Leak-avoidance: a naive lag feature (e.g. "value 24h ago") would read
  real, potentially outage-contaminated data for any during/post timestamp
  whose lag source falls inside the during/post window itself — exactly
  what the "fit on pre-period only" rule at the top of propagation.py's ITS
  section exists to prevent. `_its_lag_model()` avoids this by projecting
  the during/post window RECURSIVELY: predictions are made in chronological
  order and each one is immediately fed back in as the lag source for later
  steps, the same discipline `_its_arima`/`_its_sarima` already follow via
  `.forecast()` never touching real future data. Pre-period rows are still
  batch-predicted from real pre-period lags (no recursion needed there).
  Verified with a synthetic-data test that sabotages the actual during/post
  values to an outlier and confirms the projection neither reproduces it
  nor tracks it — see `TestLagFeatureItsMethods`/`TestEnsembleItsMethod` in
  test_pipeline.py. LightGBM/CatBoost fall back to ARIMA if their library
  isn't installed (`lightgbm`/`catboost`/`scikit-learn`, all optional — see
  requirements.txt/pyproject.toml's `gbm` extra); all three lag-feature
  methods fall back to seasonal_naive if the pre-period is too short to fit
  (same `< 2*T_day` guard as STL). `ITS_METHOD_NAMES`/`_ITS_METHODS` being a
  plain dict/list is why both dashboard.py's and
  app_jao_NP_API_fix_d14.py's Single Event ITS-method dropdowns picked
  these up with zero other GUI code changes — only their
  `_method_labels`/`_method_colors` dicts (cosmetic: friendly display name,
  distinct plot color for "all" mode's overlay) needed new entries.
  "ensemble" (`_its_ensemble()`) backtests every OTHER method on a held-out
  slice of the pre-period ITSELF (`_ensemble_backtest_weights()` — never
  during/post data, so this can't introduce a leak either), drops any that
  backtest more than 2x worse than the best one, and blends the survivors'
  real full-pre-period projections weighted ∝ 1/backtest-MAE — "adaptive"
  and "exclude bad models" per the user request this was built for. Below
  14 days of pre-period (too short for a reliable backtest split) it falls
  back to an unweighted per-timestamp MEDIAN of every member instead of a
  MEAN: a mean was tried first and measured far worse in development (MAE
  ~14.8 vs the best individual member's ~1.7) because a single badly-wrong
  member (fourier_trend/STL both derail on a pre-period level shift) drags
  a mean toward it; the median can't be moved past the next-most-central
  projection by one outlier. By far the slowest method (most members,
  SARIMA included, get fit roughly twice — once for backtest weighting,
  once for the real projection).
- `scikit-learn` is a REQUIRED companion package for the "lightgbm" ITS
  method, not merely a nice-to-have: `import lightgbm` succeeds without it,
  but `LGBMRegressor(...)` (the sklearn wrapper `_its_gbm()` uses) raises a
  non-`ImportError` `LightGBMError` — "scikit-learn is required for
  lightgbm.sklearn..." — the first time it's constructed. That exception
  type slipped past `_its_gbm()`'s original `except ImportError` guard
  and was only caught by a broader `except Exception` guard downstream,
  which silently fell back all the way to seasonal_naive (discarding the
  lag-feature engineering entirely) instead of the documented ARIMA
  fallback — the same failure mode this file's SE-fallback entry above
  warns about in the regression code, just for a dependency instead of a
  clustering mode. Fixed by constructing (not fitting) the regressor
  immediately after import, inside the same `except Exception` (not
  `except ImportError`) block, so a missing scikit-learn now fails fast
  into the intended ARIMA fallback. `TestLagFeatureItsMethods
  ::test_lightgbm_regressor_actually_constructs` in test_pipeline.py
  guards against this regressing again, and the leak test now also asserts
  each lag-feature method's projection isn't bit-identical to
  seasonal_naive's (a silent fallback would otherwise still pass every
  other check in that test, since seasonal_naive is trivially leak-proof
  and could look like a "working, non-leaking" result). `catboost` never
  needed sklearn, so it was unaffected — this only ever silently degraded
  "lightgbm".
- `_ClosedFormRidge` (propagation.py, backs the "ridge" ITS method) must
  guard against a feature column that is NaN in EVERY training row — lag7d
  whenever the pre-period is under 7 days, since it can then never have a
  single real value. `np.nanmean` on an all-NaN column returns NaN (plus a
  "Mean of empty slice" RuntimeWarning), and using that NaN as the
  imputation fill value silently NaN-poisons every prediction from that
  point on, with nothing raising to catch it — found live while backtesting
  the "ensemble" method on a 6-day pre-period: ridge's all-NaN lag7d column
  poisoned its own projection with NaN, which then NaN-poisoned
  `_its_ensemble()`'s per-timestamp median for EVERY method, not just
  ridge, since `np.median` propagates a single NaN through the whole
  timestamp. Fixed by replacing any NaN entries in the computed column-mean
  vector with 0.0 before imputing — an all-NaN column becomes a constant-0
  column, which the ridge normal equations then always fit a coefficient of
  exactly 0 for (equivalent to dropping the feature), rather than a special
  case anywhere else in the class. `TestLagFeatureItsMethods
  ::test_ridge_handles_all_nan_feature_column` guards against this
  regressing again.
- Three more ITS methods target a gap none of the first nine cover:
  SARIMA above only models ONE seasonal period (m=24, hourly) and bolts
  the weekly pattern on afterward via a separate within-hour-deviation
  lookup — it never lets the weekly cycle inform the trend/level estimate
  itself. "structural" (statsmodels `UnobservedComponents`, state-space/
  Kalman filter) and "tbats" (the `tbats` package) both instead model
  daily AND weekly seasonality JOINTLY in one coherent model — structural
  via harmonics inside a general state-space model (no new dependency,
  statsmodels already required), tbats via a model purpose-built for
  exactly this multi-seasonal-period shape (Box-Cox + trend + ARMA errors
  + multiple seasonal components; optional dependency, falls back to
  "structural" if not installed — pure numpy/scipy, no compiled toolchain,
  unlike Prophet, which was considered and rejected earlier in this same
  session for exactly that reason). Both fit on hourly-aggregated data and
  expand back to MTU resolution via a shared `_expand_hourly_forecast_to_mtu()`
  helper — the same within-hour-deviation technique `_its_sarima()` already
  used, factored out so "structural"/"tbats" don't duplicate it, though
  `_its_sarima()` itself was left with its own original inline copy
  untouched to avoid any regression risk to that already-shipped, tested
  method. The weekly seasonal component/period is only added once there's
  ≥336h (2 full weekly cycles) of pre-period; shorter pre-periods get
  daily seasonality only. TBATS' own automatic Box-Cox/damped-trend/ARMA
  model-selection search is deliberately disabled (all fixed explicitly)
  since that search is expensive and SARIMA's AIC grid search here is
  already the slowest single method — letting TBATS run its full default
  search on top would make it slower still for a GUI a person is waiting
  on. Fallback chain: tbats → structural → sarima → arima → fourier_trend
  → seasonal_naive. "theta" (statsmodels `ThetaModel`, a top performer in
  the M3/M4 forecasting competitions) instead targets the opposite
  problem — mirrors `_its_arima()`'s own "deseasonalize via seasonal_naive,
  then model the residual" structure exactly, swapping in the Theta method
  for ARIMA, specifically because Theta is known for punching above its
  weight on SHORT series, the regime where ARIMA/SARIMA's order search and
  the lag-feature methods' need for weeks of lag7d examples both struggle
  (see "Backtested accuracy" below) — `min_days` is 7, the lowest of any
  non-seasonal_naive method. All three were requested together in response
  to "what more time series models [could we add]" after the lag-feature
  methods above, and their leak-safety is verified the same way as every
  other method here — see `TestTimeSeriesItsMethods` in test_pipeline.py.
- "hurdle" is a two-part zero-inflated model, added specifically because
  shadow price is NOT a continuous quantity the way fall/PTDF/RAM are: it
  is exactly 0 on a non-binding CNEC and only positive when binding, so
  every other ITS method here — all of which implicitly fit something
  continuous and roughly Gaussian-shaped — risks projecting a smooth
  trend/seasonal value onto a small positive baseline the real quantity
  structurally cannot take except when actually binding. `_its_hurdle()`
  instead estimates two (hour, weekday) tables from the pre-period —
  P(binding) and E[value | binding] — and projects their product; same
  zero-estimation-variance philosophy and leak-safety story as
  seasonal_naive (a deterministic pre-period-only lookup, no lag features,
  no recursion). Works on any column with a real point mass at zero, and
  degrades harmlessly to plain seasonal_naive on a genuinely continuous
  column (P(binding) saturates near 1) — verified by
  `TestHurdleItsMethod::test_degrades_to_seasonal_mean_on_non_zero_inflated_column`.
  This is a per-outage counterfactual choice a user makes by picking
  "hurdle" in the ITS-method dropdown for a shadow-price single-event
  analysis — nothing auto-detects zero-inflation and switches methods for
  you.
- Backtested accuracy (synthetic data, no real JAO/ENTSO-E history
  available in this repo): with a two-timescale synthetic series — MTU-to-
  MTU AR(1) noise plus a slower per-DAY AR(1) "regime" component shared by
  every MTU within a day (the kind of day-to-day persistence lag1d/lag7d
  are meant to exploit, and that seasonal_naive/Fourier/STL structurally
  cannot see since they only know hour-of-day/day-of-week, not which
  specific day it is) — a pre-period-only backtest (fit on N pre-days,
  project into a held-out continuation, compare against its known true
  values) across pre-period lengths {14, 30} × holdout lengths {3, 7} days
  ranked (after fixing the two bugs above — an earlier run of this same
  backtest, before those fixes, had silently graded "lightgbm" as
  identical to seasonal_naive/ARIMA the whole time): SARIMA best (lowest
  MAE) but ~10-20x slower than the others (~10-25s per fit vs <2s);
  CatBoost, LightGBM and ridge next, beating plain ARIMA/seasonal_naive by
  roughly a third at 30 days pre-period but giving little-to-no
  improvement at only 14 days (lag7d only sees ~1-2 weeks of examples then
  — too little to separate real day-to-day persistence from noise, tree
  ensembles or ridge alike); the "ensemble" method's adaptive backtest
  weighting reliably excluded fourier_trend/STL and matched or slightly
  beat the single best member across every configuration tested, at
  roughly double the runtime cost. Fourier+trend and STL were clearly
  worst on this generative process (STL's linear trend extrapolation and
  Fourier's global trend term both got misled by the day-level regime
  shocks) — exactly the failure mode "ensemble" was built to route around.
  Rank is data-generating-process-dependent, not a universal verdict — the
  qualitative takeaway that matters here is "GBM/ridge need ≥~30 days of
  pre-period to earn their keep over seasonal_naive, ensemble adapts to
  whichever wins without needing that judgment call made in advance" is a
  good sanity check to apply to new results, but don't treat the specific
  MAE numbers as calibrated against real Nordic flow data.

## Known limitations
- ENTSO-E API returning 403 from cloud/server IPs is environment-dependent,
  not universal — a live call succeeded from this Claude Code sandbox's
  outbound proxy. Don't assume either outcome; test from wherever you're
  actually deploying — run `python check_entsoe.py` there to check layer
  by layer (DNS/TCP, TLS, raw HTTP, entsoe-py) rather than guessing from
  the app's "0 events" alone, which looks identical whether every query
  failed or the window genuinely had none (see fetch_entsoe_outages()'s
  entsoe_fetch_failures DataFrame.attrs, added to disambiguate the two).
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
