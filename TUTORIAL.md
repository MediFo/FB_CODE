# The Nordic Flow-Based Propagation Pipeline
### A Maintainer's Field Guide

---

## Preface

This is not a quick-start (that's `README.md`) or a session log for an AI
agent (that's `CLAUDE.md`, which is dense, terse, and written to be read by
a tool, not a person). This is the book you read once, slowly, before you
touch the code — so that everything CLAUDE.md later tells you about a bug
fix or a design decision lands on top of an actual mental model, instead of
floating free.

It's organized in eight parts. Parts I–III build the domain and codebase
vocabulary. Parts IV–VI walk through the analytical core in the order data
actually flows through it. Part VII covers what comes out the other end.
Part VIII is what you need once you're the one maintaining it.

If you only read one section, read Part I, Chapter 3 (the vocabulary) and
Part VI, Chapter 20 (the one rule that governs all counterfactual
modeling). Nearly every subtle bug this project has had traces back to
someone — sometimes the original author, sometimes an AI agent working on
it — momentarily forgetting one of those two things.

---

## Part I — The Problem This Project Solves

### Chapter 1: What Flow-Based Market Coupling Is

Before 2021, the Nordic day-ahead electricity market allocated
cross-border trading capacity zone-pair by zone-pair: "how much power can
flow from SE3 to FI." That's intuitive but wasteful, because the grid
doesn't actually work zone-pair by zone-pair — a MWh traded between two
zones physically redistributes across many lines according to Kirchhoff's
laws, and a capacity limit that binds on one specific line can be reached
by many different combinations of zone-to-zone trades.

**Flow-Based Market Coupling (FBMC)** replaces that with a different
question: instead of "how much can flow directly between A and B," it
asks "for each critical line in the grid, how much MORE can every zone's
net position (total export minus import) change before that specific
line's limit is reached — and who contributes how much to that line's
loading?" The market-coupling algorithm (Euphemia) then finds the
day-ahead prices and volumes that maximize total welfare subject to EVERY
critical line's limit simultaneously, not zone-pair by zone-pair. This is
why FBMC parameters are always **CNEC-centric** (line-centric), not
zone-pair-centric — that single fact explains most of the shape of the
data this codebase works with.

### Chapter 2: The Question This Codebase Answers

TSOs (Transmission System Operators — Fingrid, Svenska kraftnät,
Statnett, Energinet) take planned and forced maintenance outages on
their own grids. This codebase asks a narrow, falsifiable question: **when
a Nordic country takes an outage, does it show up — statistically and
economically — in another bidding zone's published FBMC parameters?**

Concretely: does an FI outage move `fall` (the reference flow) or `PTDF_FI`
(the sensitivity) or `RAM` (the remaining capacity) or the shadow price on
NO3's CNECs? That's a real, useful question — outage-driven capacity
reduction that ripples into a *different* zone's constraints is exactly
the kind of cross-zonal externality market participants and regulators
care about — but it's a *validation/diagnostic* question, not a market
simulation. This project never re-implements FBMC's own capacity
calculation; it reads what JAO already published and asks whether outages
correlate with movements in it.

That distinction matters for how you read everything downstream: every
number here is either (a) a fact read directly from a JAO export, (b) a
fact read from ENTSO-E, or (c) a statistic *computed from* (a) and (b).
Nothing is a physical simulation of grid flow.

### Chapter 3: A Tour of the Vocabulary

Read this chapter twice. Every other chapter assumes you know these terms
cold.

| Term | What it means | Where it shows up in code |
|---|---|---|
| **CNEC** | Critical Network Element with Contingency — a specific grid line, evaluated under a specific contingency (N or N-1), that FBMC tracks a capacity limit for | `cneName` column |
| **Bidding zone** | A market area with one day-ahead price (FI, NO1–5, SE1–4, DK1–2, …) | `biddingZoneFrom`/`biddingZoneTo` |
| **PTDF** | Power Transfer Distribution Factor — how much a CNEC's flow changes per MW of net-position change in a given zone. A linear sensitivity, derived from the grid's admittance matrix (topology), not from any single trade | `ptdf_<ZONE>` columns |
| **Net position** | A zone's total scheduled export minus import for a given hour/MTU | `netpos_<ZONE>` columns (this codebase's own addition — see Ch. 11) |
| **`fall` (F_allReference)** | The CNEC's reference-case flow from the D-2 common grid model — the flow term that actually enters the RAM formula. **Not** the same thing as `fref`/`f0` (see below) — this distinction cost real debugging time; see `METHODOLOGY.md` Issue 1 and Issue 2 | `fall` column |
| **`fref` / `f0`** | The reference flow at the CGMA (Common Grid Model Assessment) net position — numerically tracks `fall` but is a *different* physical quantity and is NOT an input to the RAM formula | `fref`, `f0` columns |
| **`Fmax`** | The CNEC's thermal rating (MW) | `fmax` |
| **`FRM`** | Flow Reliability Margin — a *structural*, slowly-recalibrated margin (think: annual/seasonal, not per-outage). If you ever see FRM moving in lockstep with a single outage, treat it as a red flag, not a finding — see H6 | `frm` |
| **`fnrao` (a.k.a. FRA)** | Flow from Non-costly Remedial Actions and Other adjustments — RA capacity credited back to the CNEC. Missing this from the RAM formula was a real, since-fixed bug (300–400 MW error) — see `METHODOLOGY.md` Issue 1 | `fnrao` / `fra` |
| **`AMR`** | Adjustment for Minimum RAM — a floor adjustment ensuring RAM stays non-negative | `amr` |
| **`FAAC` (a.k.a. AAC)** | Already Allocated Capacity — capacity pre-committed via long-term rights, subtracted from what's left for day-ahead | `faac` / `aac` |
| **`IVA`** | Individual Validation Adjustment — a TSO-discretionary correction, usually zero, nonzero mainly during forced outages | `iva` |
| **`RAM`** | Remaining Available Margin — the capacity actually left for day-ahead trading on that CNEC, after every other term is netted out. This is the accounting identity the whole pipeline is built around (Ch. 13) | `ram` |
| **Shadow price** | The marginal value (€/MW) of relaxing a CNEC's constraint by 1 MW — zero when the CNEC isn't binding, positive when it is | `shadowPrice` |
| **MTU** | Market Time Unit — 15 minutes, the native resolution of JAO's Nordic publications | the implicit row granularity |

Two formulas you should be able to write from memory by the time you've
read Part IV:

```
RAM = Fmax − FRM − fall + fnrao + AMR − FAAC − IVA         (the identity)
flow_CNEC ≈ Σ_zone PTDF_zone × NetPosition_zone             (the physics)
```

---

## Part II — Codebase Architecture

### Chapter 4: The Package and Its Entry Points

The repository root **is** the installable `fi_no3` package — there's no
`src/` layout, `pyproject.toml` maps `package-dir = {"fi_no3": "."}`
directly. `pip install -e .` gives you two console scripts:
`fi-no3-analyse` (the CLI) and `fi-no3-dash` (the main GUI).

There are exactly **four ways into this codebase**, and it's worth
knowing all four exist before you assume you only need to change one:

1. `dashboard.py` — the main GUI, 8 tabs (Setup, Outage sources, Run
   analysis, Results, Plots, Single event, Explain, Export). Note: some
   older docs in this repo (including this project's own `CLAUDE.md`) say
   "6-tab GUI" — that's now stale; verify tab count against the actual
   `self.nb.add(...)` calls near the top of `dashboard.py`'s `App` class
   before trusting a written tab count anywhere, including this book.
2. `app_jao_NP_API_fix_d14.py` — the extended GUI, genuinely 9 tabs
   (Fetch/Upload, Analysis, Shadow/RAM, Impact/PTDF, Gen/Cons, Price
   History, Net Position, Nordic Map, Maintenance Analysis). This is the
   only entry point with live JAO/Nord Pool fetch tooling. It shells out to
   Windows PowerShell per calendar day for the fetch, so it only runs
   where PowerShell (native or PowerShell Core) is available.
3. `run_analysis.py` — the CLI, for scripted/headless runs. Either a
   single source→target pair or, with `--all-nordic-zones`, the full
   Nordic sweep.
4. Direct import — `propagation.py`'s functions are plain, GUI-free
   Python; nothing stops a notebook or another script from calling
   `run_pipeline()` directly.

All four call into the same backend: `propagation.py`. There is no
alternate/duplicate analytical logic hiding in either GUI file — if you
fix a bug in `propagation.py`, both GUIs and the CLI see the fix
immediately.

### Chapter 5: `propagation.py`, the Analytical Core

This is the one file worth reading top-to-bottom before you touch
anything else — over 5000 lines, but it decomposes cleanly into sections
in roughly this order:

1. **Constants and config** — `ENTSOE_TOKEN`, `EXPECTED_NUMERIC`,
   `NORDIC_SOURCE_COUNTRIES`/`NORDIC_TARGET_ZONES`, `PipelineConfig`.
2. **Time conversion** — `cet_input_to_utc()`, `utc_to_cet_str()`,
   `jao_datetime_to_utc()`. Read Chapter 12 before touching any of these.
3. **Data ingestion** — `load_jao_csv()`, `fetch_entsoe_outages()`,
   `load_manual_outages()`, and (added later) `fetch_nordpool_net_positions()`
   / `merge_nordpool_net_positions()`.
4. **Covariate construction** — `build_covariates()`, which is where the
   RAM identity is checked and every outage-derived regressor is built.
5. **The hypothesis framework** — `_build_hypotheses()`,
   `run_panel_regression()`, `run_logit_iva()`, `summarize_hypotheses()`.
6. **Event-level analysis** — `build_event_time_dummies()`,
   `run_event_study()`, `decompose_delta_ram()`.
7. **The ITS counterfactual section** — `_ITS_METHODS`, all fifteen
   `_its_*()` functions, `single_event_analysis()`. This is the largest
   single section by line count and the focus of Part VI.
8. **Reporting** — `build_report_ctx()`, `render_html_report()`,
   `run_nordic_matrix()`, `render_nordic_matrix_report()`.
9. **Orchestration** — `run_pipeline()`, the function every entry point
   ultimately calls.

If you're trying to find where a specific *number* in a report comes
from, work backward from step 9 to step 4 — everything traces back to
`build_covariates()`'s output DataFrame eventually.

### Chapter 6: The Two GUIs

`dashboard.py` is the one to point a new user at. Its flow mirrors the
pipeline itself: pick outage sources → load JAO data → run the
regressions → look at results/plots → drill into one specific outage
event → get an explanation in plain language → export.

`app_jao_NP_API_fix_d14.py` exists because a later requirement needed
*live* JAO and Nord Pool fetching (not just CSV upload), plus richer
single-CNEC charting (Shadow/RAM, Impact/PTDF, Gen/Cons, Price History,
Net Position, a Nordic map view) and a "Maintenance Analysis" tab (Tab 9)
that runs a fuller version of the same propagation pipeline per-CNEC. Its
**Strategic Briefing** sub-tab (under Tab 2) is worth knowing about
specifically because it's a UI shell only — the generate/export buttons
show an honest "not implemented" message rather than either doing nothing
silently or fabricating output. If a user reports "Strategic Briefing
doesn't work," that's expected; point them at the Analysis sub-tab or Tab
9 instead.

Both GUIs currently only ever run **one** source-country → target-zone
pair per click — the Nordic-matrix sweep (Chapter 7) is CLI-only for now.
Wiring a matrix-sweep button into either GUI is a real, scoped, not-yet-done
piece of future work, not a hidden capability.

### Chapter 7: The CLI and the Nordic Matrix Sweep

```bash
# One pair
python run_analysis.py --jao data/jao_export.csv --source SE --target NO1 --out results/

# Every Nordic source country x every Nordic bidding zone, one sweep
python run_analysis.py --jao data/jao_export.csv --all-nordic-zones --out results/
```

The sweep is `run_nordic_matrix()`. The one design point worth
internalizing: it loads the JAO CSV **once** and fetches outages **once
per source country** (not once per pair) — the target zone is just a
CNEC-name filter applied afterward, so there's no reason to redo the
expensive I/O for every (source, target) combination. A pair with no
matching CNECs, or no outage data overlapping the JAO window, is recorded
in the result's `skipped` list with a reason, rather than aborting the
whole sweep. Output: one `nordic_matrix_report.html` index (a
verdict-count matrix with links) plus a full `results/<SRC>_<TGT>/report.html`
per pair.

---

## Part III — Data: Where It Comes From and How It's Shaped

### Chapter 8: JAO Publication Tool Exports

`load_jao_csv()` is deliberately defensive: any expected column that's
missing gets filled with `NaN` rather than raising, column-name aliases
(`flowFb`→`flowFB`, `aac`→`faac`, `fra`→`fnrao`, etc.) are resolved
automatically, and PTDF columns for a fixed zone list are coerced to
numeric even if absent. This matters because real JAO exports are not
perfectly consistent across schema versions or vendors — the loader's job
is to make the rest of the pipeline see one canonical shape regardless.

The one thing it does *not* do automatically is guess your timestamp
convention — see Chapter 12.

### Chapter 9: ENTSO-E Outage Events

Two ENTSO-E event types matter here:

- **A77** = production (generator) outages — carries both `nominal_power`
  and `avail_qty`, so a genuine "MW lost" (`nominal − avail`) is
  computable.
- **A78** = transmission outages — carries only `avail_qty` (capacity
  *still available*, not capacity lost), with no rated capacity in
  entsoe-py's A78 parser to net it against. `fetch_entsoe_outages()` used
  to write that raw `avail_qty` straight into `capacity_mw` — silently
  feeding the *wrong* quantity into the dose-variable regressors. It's
  fixed now: A78 rows leave `capacity_mw` unset, with `avail_qty` kept
  under `raw_payload["avail_qty_mw"]` for reference. The binary
  `*_hvdc_outage_active`/`*_ac_line_outage_active` dummies (built from the
  outage *interval*, not `capacity_mw`) are unaffected — only the
  secondary continuous "MW lost" dose covariates lose their A78
  contribution, and only where no manual-CSV or A77 row also covers that
  window.

Both A77 and A78 return timezone-*aware* timestamps localized to the
*queried country's own zone* (Europe/Helsinki for FI, etc.) — not UTC,
not CET generically. `fetch_entsoe_outages()` converts to UTC internally.
A77 raising "File is not a zip file" is ENTSO-E's own way of saying "zero
matching events in this window," not a transport error.

If a fetch returns 0 events and you can't tell whether that's a genuine
empty window or a network/proxy failure, run `check_entsoe.py` — it
checks DNS/TCP → TLS → raw HTTPS → entsoe-py in that order, independent of
the rest of this codebase's dependencies.

### Chapter 10: Manual Outage Curation

`manual_outages.csv` is how you cover what ENTSO-E can't: A78 mostly
returns *forced* events (BSNTYPE A54), so *planned* line outages need to
be entered by hand. The checked-in copy in this repo is the auto-generated
single-row template the code writes when the file is missing — it is
*not* curated data, and treating it as such is a real trap for a new
user. The `bidding_zone` column is what makes one file usable for any
Nordic source country, not just FI.

One easy-to-miss rule: `start_utc`/`end_utc` values with **no explicit UTC
offset** are interpreted as **CET/CEST**, not UTC — matching how a human
actually types a time into a spreadsheet. If you paste a genuinely-UTC
timestamp without a `Z` or `+00:00` suffix, it will be silently
misinterpreted by one or two hours.

### Chapter 11: Synthetic Data — Why It Exists and How It's Built

`synthetic.py` produces a plausible JAO CSV plus a matching outage CSV,
with outage effects deliberately planted in `fall`, `PTDF_FI`, `RAM`,
shadow price, and `IVA` so the pipeline's own hypothesis tests can detect
them — this is what every test in `test_pipeline.py` runs against, since
there is no committed real JAO/ENTSO-E dataset in this repo.

The one thing worth understanding deeply here, because it's a genuine
modeling lesson and not just a testing detail: **a synthetic generator's
statistical structure directly determines what a model built against it
can be shown to be good at.** Two concrete episodes from this project's
own history illustrate it:

- Early on, five of the RAM identity's seven components (`fnrao`, `amr`,
  `faac`, `iva`, and effectively `fmax`/`frm`) were either near-constant or
  close to i.i.d. noise — only `fall` carried real calendar structure
  (diurnal + weekly). A physics-informed forecasting method that
  *decomposes* RAM into those seven terms and forecasts each separately
  (`ram_identity`, Chapter 23) had nothing genuine to exploit in five of
  its seven pieces, so it looked no better than forecasting RAM directly
  — not because decomposition doesn't work, but because the test data
  gave it nothing extra to find. Giving those terms genuine day-level
  persistence (an AR(1) "regime" component, `_ar1_day_regime()`, mean-
  reverting, one value per calendar day, broadcast to MTU resolution — see
  the function itself for the mechanics) turned that into a real,
  measurable accuracy edge.
- A parallel story played out for the `ptdf_flow` method: `fall` had zero
  dependency on net position at all, and four of six `ptdf_<ZONE>`
  columns were literally redrawn from independent noise on *every single
  row* — not a real PTDF's behavior at all. Fixing that (adding
  `netpos_<ZONE>` columns and stabilizing the PTDF columns) wasn't
  enough by itself; it took two more rounds — folding an unexplained
  per-CNEC bias into net position's own mean, and moving `fall`'s
  independent diurnal/weekly pattern into the net-position-mediated
  channel — before the physics reconstruction had a fair fight against a
  direct forecast. See Chapter 23 for the full, honest before/after
  numbers on both.

The general principle to carry forward: **when a synthetic quantity is
meant to represent something a real physical/economic process would
have — persistence, seasonality, a genuine causal dependency — and the
generator instead gives it independent noise, any test built on top of it
will silently misjudge whichever method *should* exploit that missing
structure.** Before trusting a synthetic-data benchmark result (accuracy
comparison, significance test, anything), it's worth asking what
structure the generator actually gives the quantity in question, the same
way you'd ask what a real dataset's structure looks like.

`effect_scale` (default 1.0) is a deliberate escape hatch: it multiplies
*only* the outage-induced terms, leaving noise/diurnal/CNEC-bias terms
untouched, so you can generate a small, economically-realistic-effect
scenario (`effect_scale=0.1`) to test whether the pipeline still detects
— or correctly fails to detect — a subtle signal, rather than only ever
validating against an oversized one.

### Chapter 12: The UTC/CET Boundary

This is the single most consistently error-prone seam in the whole
project, and it's worth a dedicated chapter rather than a bullet point.

**The convention:** every user-facing surface (GUI date fields, logs,
single-event summaries, the HTML report timestamp) speaks **CET/CEST**
(`Europe/Oslo`). Every internal storage/computation surface (JAO
alignment, regression panel indices, CSV columns) stays **UTC**.
`cet_input_to_utc()` and `utc_to_cet_str()` are the only two conversion
points — any new human-facing time field should route through one of
them, not grow its own ad-hoc `tz_localize` call.

**The wrinkle:** JAO's own Nordic Publication Handbook documents that its
`dateTimeUtc` API field can *actually be CET/CEST wall-clock* despite the
name (the handbook's own words: "CET time stamp (yes… CET?!)"). This has
never been confirmed against a live fetch from this specific codebase's
environment — it's a documented possibility from JAO's own
documentation, not a proven defect in every export — so the default stays
`"UTC"`. Every real-data entry point (`dashboard.py`, both tabs in the
extended GUI that touch JAO data, and `run_analysis.py --jao-timestamp-zone`)
has its **own independent** `"UTC"`/`"CET"` toggle — deliberately not
shared, so you can experiment with one tab's assumption without disturbing
another's already-fetched, already-displayed data. If outage/event
alignment in a report looks consistently off by exactly one hour
(winter) or two hours (summer), that's the diagnostic signature to look
for, and flipping the toggle on the relevant tab is the first thing to
try — not a code change.

---

## Part IV — The RAM Identity and Covariate Construction

### Chapter 13: The RAM Formula and Why It's the Load-Bearing Wall

```
RAM = Fmax − FRM − fall + fnrao + AMR − FAAC − IVA
```

`build_covariates()` computes this from the seven raw terms and compares
it against the JAO-published `ram` column — `ram_check` — reporting the
percentage of rows that balance within 1 MW. On real JAO data this
verifies at essentially 100%. That check is not decorative: an earlier
version of this pipeline silently dropped `AMR`/`IVA` from the formula, and
the only in-repo check of that error was circular (checked against
synthetic data generated with the *same* incomplete formula, so it
"passed" by construction). `METHODOLOGY.md` Issue 1 is the historical
record of that bug and its fix. If `ram_check`'s pass rate ever drops
below ~95% on real data you trust, treat it as a schema mismatch to chase
down immediately, not a warning to ignore — every downstream hypothesis
result implicitly assumes this identity holds.

`decompose_delta_ram()` uses this same formula *retrospectively*, to
attribute how much of an observed ΔRAM around a specific outage came from
each of the seven terms moving. `_its_ram_identity()` (Chapter 23) is the
*forecasting* use of the identical formula — same seven terms, same
signs, but projecting each one forward instead of decomposing an
already-observed change.

### Chapter 14: `build_covariates()` and the Outage Dose Variables

For each outage, `build_covariates()` builds both a **binary "active"
dummy** (was an outage of this type/asset in effect at this timestamp) and
a **continuous "dose" variable** (how many MW were lost). The dummies are
the primary H1/H2 treatment variables; the dose variables are secondary,
and — per Chapter 9 — currently only available where a manual-CSV or A77
row provides a genuine lost-capacity figure, since A78 alone can't produce
one.

### Chapter 15: Collinearity and Event-Time Dummies

With few independent outage episodes, a binary active dummy and its
paired dose variable can become near-perfectly collinear.
`_prune_collinear_dose_pairs()` runs before every regression fit and drops
the binary duplicate, keeping the dose variable (falling back to a
deterministic column drop when neither/both candidates are themselves
binary) — rather than let the estimator arbitrarily split the coefficient
between two variables carrying the same information. This applies to any
`indep` list passed to `run_panel_regression()`, not just the built-in
H1–H6 specs.

`build_event_time_dummies()` builds `event_k` (event-relative time,
leads/lags around an outage start) for event-study designs, and is
order-independent with respect to how multiple outages are passed in —
worth knowing if you're ever debugging why an event study's coefficients
seem to shift when you reorder an outages DataFrame (they shouldn't, and
a passing test guards exactly this).

---

## Part V — The Hypothesis Framework

### Chapter 16: H1 through H6, One at a Time

Shown for the FI → NO3 default; `_build_hypotheses(src, tgt)` builds the
same six for any pair.

| ID | Hypothesis | What it's really asking |
|----|---|---|
| H1 | SRC HVDC outage shifts `fall` on TGT CNECs | Does losing an interconnector change the reference flow elsewhere? |
| H2 | SRC AC line outage shifts `\|PTDF_SRC\|` on TGT CNECs | Does a topology change alone (not a flow/volume change) alter sensitivity? |
| H3 | SRC forced outage changes RAM on TGT CNECs | Does the *combined* effect (via the identity) move remaining capacity? |
| H4 | SRC forced outage changes shadow price on binding TGT CNECs | Does it move the *economic* value of the constraint, not just its physical numbers? |
| H5 | IVA is more frequent under forced than planned SRC outages | Is the TSO's discretionary correction responding to *unplanned* events specifically? |
| H6 | Placebo — FRM should NOT move with individual outages | Sanity check: is the estimator manufacturing spurious effects on a variable that structurally shouldn't respond? |

H6 is not an afterthought. It's the project's own falsifiability check on
itself: FRM is calibrated on an annual/structural basis, so if a
regression finds FRM moving significantly with a single outage, that's
evidence the *methodology*, not the grid, is producing the signal — and a
failed H6 placebo demotes significance claims made elsewhere in the same
report, exactly because it undermines trust in the estimator on this
dataset.

### Chapter 17: Panel Regression Mechanics — Clustering and the SE Fallback Chain

`run_panel_regression()` fits a `PanelOLS` per hypothesis, requesting
clustered standard errors. There is exactly one hard rule here, and
getting it wrong produces a failure mode that's easy to miss because it
doesn't crash: **cluster codes must be passed to `.fit(cov_type="clustered",
clusters=...)` as (entity, time)-indexed pandas objects** — a `Series` for
one-way clustering, a `DataFrame` for two-way — **never** `.values` or a
reshaped bare NumPy array. `linearmodels` re-wraps whatever it receives in
its own `PanelData` and validates *that* object's inferred entity/time
shape against the model's own; an index-less array can never satisfy that
check, so passing one always raised `"clusters must have the same number
of entities and time periods as the model data"` — meaning time-clustered
and two-way-clustered standard errors could never actually succeed until
this was found and fixed, and *every* regression silently fell back to
entity-clustered the whole time. If you ever see that exact error string
again, this is the first thing to check.

The fallback chain itself (requested mode → time-clustered →
entity-clustered → robust → unadjusted, in that order) is intentional and
*recorded*, not silent: every result carries `cov_type_used` and
`se_fallback_occurred`, and `summarize_hypotheses()` appends `n=…, se=…`
to every verdict so a reader can see when a result is resting on
something weaker than what was requested. `run_pipeline()` always
requests `cluster="time"` (which now actually succeeds); `"two_way"` is
implemented and available but not currently invoked by the built-in
pipeline.

### Chapter 18: Statistical vs Economic Significance

A coefficient can clear `p < 0.05` and still be *economically*
meaningless — a statistically significant 0.5 MW shift in a 1000 MW RAM
budget isn't something anyone would act on. `ECONOMIC_SIGNIFICANCE_THRESHOLDS`
sets a conservative, documented minimum-magnitude floor per dependent
variable; a coefficient that clears the p-value bar but not the magnitude
floor is reported as merely "statistically significant," never
"SUPPORTED"/"SIGNIFICANT." These floors are not calibrated against live
market data (there is none committed to this repo) — treat them as
reasonable defaults to revisit, not settled science, if a better-grounded
number becomes available.

### Chapter 19: Multiple-Testing Correction

Holm–Bonferroni correction is applied across H1–H4 (the "does an effect
exist" hypotheses), since testing four related hypotheses on the same
dataset without correction inflates the false-positive rate. H5 and H6
are structurally different (H5 is a frequency comparison, H6 is a
placebo) and aren't pooled into the same correction family.

---

## Part VI — Counterfactual Modeling (ITS)

### Chapter 20: What "Interrupted Time Series" Means Here, and the One Rule That Governs Everything

For a single outage event, "what would this CNEC's shadow price/RAM/fall
have looked like if the outage hadn't happened?" is a counterfactual
question — you only ever observe the *with-outage* world. **Interrupted
Time Series (ITS)** answers it by fitting a model to the **pre-period**
(before the outage) and projecting it forward through the **during** and
**post** windows, then comparing that projection to what actually
happened.

Here is the one rule, stated as plainly as possible, because every method
in this section exists to obey it and at least one real bug in this
codebase's history came from a near-violation of it:

> **A counterfactual projection may only ever be fit on pre-period data.
> It must never read a real observed value from the during/post window —
> not directly, and not indirectly through a feature (like a lag) that
> happens to point into that window.**

The "indirectly" clause is the subtle part. A naive "value 24 hours ago"
lag feature, computed the ordinary way, would read *real, potentially
outage-contaminated* data for any during/post timestamp whose lag source
also falls inside the during/post window — which defeats the entire
point of a counterfactual. Every lag-feature method here (`lightgbm`,
`catboost`, `ridge`) instead projects the during/post window
**recursively**: predictions are made in chronological order, and each
one is immediately fed back in as the lag source for later steps, so the
model only ever "sees" its own prior *predictions*, never real
contaminated data — the same discipline ARIMA/SARIMA already follow via
`.forecast()`. This is tested directly: a synthetic-data test sabotages
the real during/post values to a wild outlier and confirms the projection
neither reproduces nor tracks it (`TestLagFeatureItsMethods`,
`TestEnsembleItsMethod` in `test_pipeline.py`).

### Chapter 21: The Method Catalog

Fifteen methods, in `_ITS_METHODS` / `ITS_METHOD_NAMES`. Grouped by family
rather than in dictionary order, because the grouping is more useful for
understanding *when* to reach for each one:

**Deterministic lookup (cheapest, zero estimation variance)**
- `seasonal_naive` — the pre-period mean for each (hour, weekday) cell.
  The baseline every other method is implicitly judged against.
- `hurdle` — for zero-inflated columns (shadow price is the canonical
  case: exactly 0 when not binding, positive only when binding). Fits two
  (hour, weekday) tables — P(binding) and E[value | binding] — and
  projects their product. Degrades harmlessly to plain seasonal_naive on
  a genuinely continuous column (P(binding) saturates near 1).

**Trend/seasonal decomposition**
- `fourier_trend`, `stl` — classical decomposition approaches. Measured
  clearly worst in this project's own backtests on data with a day-level
  regime shock, because both get misled by extrapolating a trend through
  what's actually a level shift, not a trend.
- `theta` — deseasonalizes via seasonal_naive, then models the residual
  with the Theta method (a strong M3/M4-competition performer,
  specifically good on *short* series — the regime where ARIMA's order
  search and the lag-feature methods both struggle for lack of data).

**Classical time series**
- `arima`, `sarima` — SARIMA additionally handles weekly seasonality via a
  within-hour-deviation lookup layered on top of an hourly (m=24) seasonal
  fit; it's the single slowest method (AIC grid search) but also the most
  accurate in this project's own backtests, when you can afford the time.
- `structural`, `tbats` — both instead model daily AND weekly seasonality
  **jointly** in one coherent model (SARIMA only ever models one seasonal
  period at a time). `tbats` needs ≥2 full weekly cycles of pre-period
  before it adds the weekly component at all; falls back to `structural`
  if the `tbats` package isn't installed, then to `sarima`, then `arima`,
  then `fourier_trend`, then `seasonal_naive`.

**Lag-feature (machine learning)**
- `lightgbm`, `catboost`, `ridge` — all three share one implementation
  (`_its_lag_model()`) for feature engineering (lag1d/lag2d/lag7d + their
  mean, plus cyclical hour/weekday features) and the recursive
  leak-avoidance projection described in Chapter 20. They need real
  day-to-day structure in the pre-period to earn their keep — this
  project's own backtest found little-to-no improvement over
  `seasonal_naive` below ~14 days of pre-period (too little to separate
  real persistence from noise), but roughly a third better at 30+ days.
  `lightgbm`/`catboost` fall back to `arima` if their library (or, for
  `lightgbm` specifically, `scikit-learn`) isn't installed; all three fall
  back to `seasonal_naive` if the pre-period is too short to fit at all.

**Physics-informed** — see Chapter 23, they get a full treatment there.
- `ram_identity`, `ptdf_flow`

**Meta**
- `ensemble` — backtests every *other* method (never `ram_identity`/
  `ptdf_flow` — see Chapter 23 for why that exclusion is load-bearing, not
  cosmetic) on a held-out slice of the pre-period itself, drops anything
  that backtests notably worse than the best member, and blends the
  survivors' real projections weighted by inverse backtest error. By far
  the slowest single method, since most members effectively get fit
  twice.

### Chapter 22: The Ensemble and Hyperparameter Tuning

Two refinements worth understanding if you're going to touch either:

**De-duplication.** `arima`, `hurdle`, and `seasonal_naive` can
legitimately collapse to *bit-for-bit identical* predictions when there's
no extra signal for either to find — ARIMA's own order search picking
`(0,0,0)`, hurdle's P(binding) saturating near 1. Left unhandled, a
coincidental three-way tie gets counted three times in the ensemble's
weighting, diluting genuinely different members rather than adding
robustness. `_ensemble_backtest_weights()` de-duplicates by comparing
actual backtest *predictions* (`np.allclose`), not just MAE, since
different methods can tie on MAE by coincidence without producing the
same predictions.

**Hyperparameter tuning.** `lightgbm`/`catboost`/`ridge` each tune a small
grid (depth/learning-rate/n_estimators for the GBMs, L2 alpha for ridge)
via `_tune_lag_hyperparams()`, graded on a held-out slice of the
pre-period itself — same leak-safety story as fitting itself. This is
gated by a module-level toggle, `propagation.ITS_TUNE_HYPERPARAMS`
(default `True`), read fresh on every call (not cached), so flipping it
takes effect immediately — but it is **not currently wired to any GUI
checkbox or CLI flag**; today it's a code-only lever (`False` roughly
6x faster for ridge alone, cuts `ensemble`'s total runtime by nearly
half). Wiring it up is exactly the kind of "known gap, not an oversight"
item worth checking CLAUDE.md's running list for before you assume it's
undocumented.

### Chapter 23: The Physics-Informed Methods, In Depth

This is the newest and most conceptually distinct part of the ITS
catalog, and the part most likely to need care if you're extending it.

**The idea.** Some of this pipeline's target columns aren't independent
physical quantities — they're *derived* from other columns already in the
data, by a known formula. A model that respects that formula by
construction, instead of having to rediscover it statistically from a
limited pre-period, should in principle do better. Two instances:

**`ram_identity`.** RAM is the accounting identity from Chapter 13, not
an independently observed quantity. `_its_ram_identity()` forecasts each
of the seven terms with a sub-method matched to *that term's own*
dynamics (not one blanket choice — see `_RAM_COMPONENT_METHOD` in
`propagation.py` for the current mapping and the backtest evidence behind
each pick: `fmax`/`fnrao` get `seasonal_naive` since every candidate ties
within noise on a near-constant series; `frm` gets `ridge` specifically
because `seasonal_naive` catastrophically mis-forecasts it whenever the
pre-period straddles the December-2024 structural recalibration step —
its (hour, dow) lookup table averages pre- and post-step values into one
contaminated mean, while `ridge`'s lag features track the current level
instead; `amr`/`iva` get `hurdle` for their zero-inflated shape; `fall`
gets `catboost`, the richest method available, because it's the term
outages actually move) and **computes** RAM from the projections — the
returned series literally equals Σ(sign × projected component), verified
to floating-point precision, not just "close." It falls back to
`_its_ensemble()` if any of the seven raw components is missing from the
data, rather than silently computing RAM from an incomplete subset — the
exact AMR/IVA-dropping mistake from `METHODOLOGY.md` Issue 1, just
reintroduced at forecast time instead of at formula-definition time,
would be worse than simply not offering the method at all.

**`ptdf_flow`.** Reconstructs a flow-type column (`fall`/`flowFB`) as
`Σ_zone PTDF_zone × NetPosition_zone` — the physics Nordic FBMC is
actually built on. Unlike `ram_identity`, this is explicitly **not**
presented as a proven identity in this codebase: `fall` is a
reference-case flow from the D-2 common grid model, not necessarily
numerically identical to `Σ PTDF × REALIZED net position`. So the method
**validates itself** — it computes the same reconstruction against the
*actual* observed target on pre-period rows first, and only trusts the
projection if that correlation clears `min_validation_corr` (default
0.5), falling back to `_its_ensemble()` otherwise, with the validation
outcome always recorded in `.attrs["ptdf_flow_validation"]` so a caller
can see which path was taken and why. This is opt-in on real data: it
needs `netpos_<ZONE>` columns merged in via
`fetch_nordpool_net_positions()` + `merge_nordpool_net_positions()`
(genuinely new plumbing — before this, Nord Pool net-position data only
ever reached a single GUI chart, never anything `propagation.py` could
see), which no real-data entry point runs automatically yet.

**Why both are excluded from `_its_ensemble()`'s own member pool.** Not
habit — a real, previously-latent bug risk. Both methods fall back to
`_its_ensemble()` when their physics doesn't apply. If either were *also*
listed as one of ensemble's own candidate members, that fallback call
would recurse straight back into ensemble's own member-evaluation loop,
which would try to evaluate them again, triggering the same fallback
again — unbounded recursion the moment either one's fallback condition is
met, which (missing component columns, or no net-position data) is the
*default* state for any caller that hasn't specifically set things up
otherwise. A regression test (`TestEnsembleItsMethod
::test_excludes_ram_identity_and_ptdf_flow_from_its_own_members`) guards
this by asserting the literal exclusion string is present in
`_its_ensemble()`'s own source — if you ever refactor that exclusion
into a different shape, update the test alongside it, not after.

**The honest accuracy story, and the lesson in it.** This is worth
reading even if you never touch either method, because it's a small
case study in how a synthetic benchmark can mislead you in *either*
direction if you don't interrogate what the generator actually gives a
method to work with (see also Chapter 11):

- First pass: neither method beat a direct forecast of the same target —
  a "wash," traced to the synthetic generator giving five of RAM's seven
  terms and four of six PTDF columns essentially no genuine structure to
  decompose.
- After giving those terms real day-level persistence and re-picking each
  sub-method from an actual backtest (not assumption): `ram_identity`
  turned into a genuine accuracy **win** over direct forecasting at
  60+ day pre-periods (roughly tied at 30 days).
- The equivalent fix for `ptdf_flow` took three attempts, not one, and
  the middle attempt made things *worse*: adding real net-position data
  and re-picking its sub-method first produced a regression (MAE ~44 vs.
  a direct catboost forecast's ~26) before the actual dominant blind spot
  — `fall`'s own independent diurnal/weekly pattern, generated separately
  from net position and therefore invisible to a reconstruction that only
  ever looks at net position — was found and fixed. The end state is
  genuine **parity** with the best generic method, not an outright win
  the way `ram_identity` achieved, and that's reported as the honest
  result rather than pushed further toward a specific benchmark number,
  which would have crossed from "fixing real unfairness in the test data"
  into "overfitting the generator to a target outcome."

If you're ever asked to "improve the model" here again, the discipline
that made both of these fixes trustworthy — and that's worth repeating on
anything similar — was: (1) diagnose *why* a number looks the way it does
by reading the generator, not by guessing; (2) fix the specific,
articulable unfairness; (3) re-run the *full* test suite before and after
every change, not just the benchmark you're optimizing; (4) report
whatever the number actually comes out to, including when it's a tie or a
regression, rather than only the version of the story that looks best.

---

## Part VII — Reports and Output

### Chapter 24: The HTML Report

`build_report_ctx()` assembles one context dict from a pipeline run's
results — regression tables, hypothesis verdicts, plots, event-study
output — and `render_html_report()` turns it into the standalone
`report.html`. Both the single-pair CLI path and each pair inside a
Nordic-matrix sweep call the exact same `build_report_ctx()`/
`render_html_report()` pair, so there is only one report template to
maintain, not two.

### Chapter 25: The Nordic Matrix Report

`render_nordic_matrix_report()` writes the consolidated
`nordic_matrix_report.html` — a verdict-count matrix across every
(source, target) pair in the sweep, with links out to each pair's own
`report.html`. This is the index page a user should open first after a
`--all-nordic-zones` run, before drilling into any individual pair.

---

## Part VIII — Maintaining This Codebase

### Chapter 26: Testing Strategy

`test_pipeline.py` is a single, fairly large pytest file, entirely
synthetic-data-driven — no live API keys or network access required (HTTP
calls are mocked via `unittest.mock.patch("propagation.requests")` where
tested at all, e.g. `TestNordPoolFetch`). As of this writing it's around
134 tests and takes roughly 15–26 minutes to run in full, dominated by
the ensemble/tuning-heavy ITS tests — budget accordingly; don't wrap a
full run in an aggressive shell timeout, or you'll get a false failure
(seen firsthand: editing the source file *while* a previous run was still
executing produced one spurious `inspect.getsource`-based test failure,
purely from line numbers shifting mid-run — always let a full suite run
finish against a stable file before trusting its result).

If you're changing `synthetic.py`, the two invariants most worth
re-verifying directly (not just via the full suite, since they're easy to
break silently) are:
1. **RAM formula balance** — `ram` should still equal the seven-term
   formula to within ~1 MW on generated data.
2. **`effect_scale` determinism** — two `generate_jao_csv()` calls with
   the same `rng_seed` but different `effect_scale` values should produce
   *bit-identical* non-outage terms, differing only in the outage-scaled
   ones. This only holds if every new `rng.*()` call you add is
   unconditional (never gated by `effect_scale` itself) and doesn't
   disturb the call *order* that produces earlier-computed columns
   (`fall`'s own noise draws, in particular, must stay ahead of anything
   new you add for later columns in the same per-CNEC loop iteration).

### Chapter 27: Known Limitations, and Why They're Still There

These are documented, not forgotten:

- **Credentials hardcoded as source constants** — `ENTSOE_TOKEN`, the
  Nord Pool user/password pair. A known, currently out-of-scope
  limitation, not an oversight; don't "fix" it unilaterally without
  understanding the deployment model this repo assumes.
- **`app_jao_NP_API_fix_d14.py`'s live fetch requires Windows
  PowerShell** — day-batched, not per-MTU, and platform-dependent by
  design.
- **IVA is zero on NO3 CNECs in short windows** — H5's logit needs a
  longer history to have anything to fit.
- **A78 has no derivable "MW lost" figure** — see Chapter 9. This is a
  genuine ENTSO-E data limitation, not something fixable in this
  codebase alone.
- **`ECONOMIC_SIGNIFICANCE_THRESHOLDS` are not calibrated against live
  market data** — reasonable defaults, not settled numbers.

### Chapter 28: Things That Have Bitten Us Before

A running list, worth skimming before you touch the related code:

- Passing `.values`/a reshaped NumPy array as cluster codes to
  `PanelOLS.fit()` — silently degrades every "clustered" regression to
  entity-only clustering with no error raised. See Chapter 17.
- Computing `fall` "backwards" as an algebraic residual of the RAM
  identity in the synthetic generator — makes the RAM-formula check pass
  100% by construction regardless of whether the formula being checked
  is actually complete. `fall` is generated *forward*, independently, for
  exactly this reason.
- `LGBMRegressor(...)` raising a non-`ImportError` `LightGBMError` when
  `scikit-learn` is missing (lightgbm itself imports fine without it) —
  slipped past an `except ImportError` guard and fell all the way back to
  `seasonal_naive` instead of the intended `arima` fallback, silently
  discarding the lag-feature engineering. Fixed by constructing (not
  fitting) the regressor immediately after import inside a broader
  `except Exception`.
- `np.nanmean` on an all-NaN feature column (e.g. `lag7d` with under 7
  days of pre-period) returning `NaN` and silently poisoning every
  downstream prediction — including, transitively, `_its_ensemble()`'s
  own per-timestamp median across *every* method, since `np.median`
  propagates a single NaN through the whole timestamp.
- Editing a `.py` file while a `pytest` run against it is still in
  progress — `inspect.getsource()`-based tests can read a shifted line
  range from the now-different file on disk and fail spuriously. Not a
  real regression; re-run against a stable file to confirm.

### Chapter 29: Where the Documentation Lives

Four documents, four different jobs — know which one to update:

- **This book (`TUTORIAL.md`)** — fundamentals and architecture, written
  for a human building a mental model for the first time. Update when the
  *shape* of the system changes (a new entry point, a new major
  subsystem, a formula correction), not for every individual bug fix.
- **`README.md`** — the quick-start. Update when a command, install step,
  or the file layout changes.
- **`CLAUDE.md`** — a dense, terse, cumulative log written for an AI
  coding agent to load as working context every session. Update it (or
  have your agent update it) whenever you fix a subtle bug, make a
  non-obvious design choice, or discover a gotcha worth not re-discovering
  next time — its style deliberately prioritizes "why," "what was tried
  and rejected," and exact numbers over readability, which is why this
  book exists as a companion rather than a replacement.
- **`METHODOLOGY.md`** — a point-in-time audit record (issues found, fixes
  applied) from an earlier review. Treat its narrative claims as
  background reading, not verified fact, per its own README-linked
  caveat — reproduce any number you need to rely on against your own data
  first.

---

## Appendix: Glossary

**AMR** — Adjustment for Minimum RAM.
**Bidding zone** — a market area with one day-ahead price.
**CGMA** — Common Grid Model Assessment; the reference net position `fref`/`f0` is defined relative to.
**CNEC** — Critical Network Element with Contingency.
**Dose variable** — a continuous "how much" outage regressor (MW lost), paired with a binary "active" dummy.
**ENTSO-E** — European Network of Transmission System Operators for Electricity; source of A77/A78 outage events.
**FAAC / AAC** — Already Allocated Capacity.
**`fall` / F_allReference** — the reference-case flow that enters the RAM formula; not `fref`/`f0`.
**FBMC** — Flow-Based Market Coupling.
**`fnrao` / FRA** — Flow from Non-costly Remedial Actions and Other adjustments.
**FRM** — Flow Reliability Margin; structural, should not move with individual outages.
**ITS** — Interrupted Time Series; the pre-period-only counterfactual-forecasting discipline in Part VI.
**IVA** — Individual Validation Adjustment; TSO-discretionary, zero-inflated.
**JAO** — Joint Allocation Office; publishes the Nordic FBMC parameters this codebase reads.
**MTU** — Market Time Unit, 15 minutes.
**Net position** — a zone's total scheduled export minus import.
**PTDF** — Power Transfer Distribution Factor.
**RAM** — Remaining Available Margin; the central accounting identity, see Chapter 13.
**Shadow price** — the marginal value of relaxing a CNEC's binding constraint by 1 MW.
**TSO** — Transmission System Operator (Fingrid, Svenska kraftnät, Statnett, Energinet).
