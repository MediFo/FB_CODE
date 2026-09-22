# The Maintenance Analysis Tab — A Focused Guide

Tab 9 of `app_jao_NP_API_fix_d14.py` ("  Maintenance Analysis  "). This
document covers **only that tab** — for the rest of the app and the
domain fundamentals it builds on, see `TUTORIAL.md`.

---

## 1. What it is

Tab 9 is a self-contained, 8-sub-tab mini-workflow embedded inside the
9-tab extended GUI. It runs the same FI→NO3-style propagation analysis as
`dashboard.py`, but as one tab among several rather than a whole
application, and it's the only place in either GUI where a user can run
the full hypothesis battery *and* a single-event ITS deep-dive *and*
export a report without leaving one screen.

It is its **own implementation**, not a thin wrapper that calls
`propagation.run_pipeline()`. `_ma_pipeline_thread()` calls
`build_covariates()`, `run_panel_regression()`, `run_logit_iva()`, and
`summarize_hypotheses()` directly, one hypothesis at a time — see §4. Keep
this in mind if you're comparing its behavior against `dashboard.py` or
`run_analysis.py`: they should agree on the underlying statistics (same
`propagation.py` functions, same formulas), but the *orchestration* is
duplicated code, not shared code, so a bug fixed in one place does not
automatically fix the other.

All widget-building methods are named `_ma_build_<subtab>`; all
event-handler/logic methods are named `_ma_<verb>`. Every one of them
lives between line ~2543 and the end of `app_jao_NP_API_fix_d14.py`
(`_create_tab9_widgets()` is the entry point).

## 2. State variables

Six instance attributes hold everything Tab 9 knows, set in
`_create_tab9_widgets()` and cleared together by `_ma_reset_all()`:

| Attribute | Set by | Holds |
|---|---|---|
| `self._ma_jao_df` | Setup → Apply Setup | The loaded/generated JAO DataFrame |
| `self._ma_outages_df` | Outage Sources (manual load or ENTSO-E fetch) | Outage events |
| `self._ma_covariates` | Run Analysis | `build_covariates()`'s output |
| `self._ma_results` | Run Analysis | `{dep_var: regression_result_dict}` |
| `self._ma_verdicts` | Run Analysis | `summarize_hypotheses()`'s output list |
| `self._ma_single_res` | Single Event → Analyse Event | `single_event_analysis()`'s full return dict |

Everything downstream (Results, Plots, Explain, Export) reads from these
six — there's no other hidden state. If a sub-tab looks empty or stale,
check whether the attribute it reads from was actually populated by the
step that's supposed to come before it.

## 3. The 8 sub-tabs, one at a time

### 3.1 Setup

Two data-source modes: **"Use data loaded in Tab 1"** (re-reads
`self.raw_filtered_data`, the same records Tab 1 already fetched/loaded,
through `propagation.load_jao_csv()`) or **"Generate synthetic demo
dataset"** (`synthetic.generate_demo_dataset()`, 90-day FI→NO3, always
genuine UTC).

Has its **own** `UTC`/`CET` timestamp-zone toggle
(`self._ma_jao_timestamp_zone`), independent of Tab 1's own toggle — it
only applies when the source is Tab 1 data, and lets you try a different
timestamp assumption for *this analysis* without re-fetching on Tab 1.
See `TUTORIAL.md` Chapter 12 for the full UTC/CET background.

"Apply Setup" runs in a background thread
(`_ma_apply_setup_thread`/`_ma_apply_setup_done`) and always does a full
reset of every downstream state variable first — this is deliberate:
switching datasets mid-analysis with stale covariates/results/verdicts
left over would be a worse bug than making the user re-run.

### 3.2 Outage Sources

Two ways to bring in outage events: **Load Manual CSV** (defaults to the
repo's `manual_outages.csv`, plain `pd.read_csv`) and **Fetch from
ENTSO-E** (calls `propagation.fetch_entsoe_outages()`, dates entered in
CET and converted via `propagation.cet_input_to_utc()` — the comment at
the call site is worth reading if you ever touch this: an earlier local
reimplementation of the CET conversion handled the one-hour-a-year DST
spring-forward gap *differently* from `propagation.py`'s own conversion,
so the two could silently disagree on that single ambiguous hour; routing
through the shared function makes them agree by construction). A
successful ENTSO-E fetch is deduplicated against whatever's already
loaded via `propagation.deduplicate_outages()`.

If a fetch returns very few or zero events, check
`df.attrs["entsoe_fetch_failures"]` (surfaced automatically as an amber
warning in the status label) before concluding the window genuinely had
no outages — see `TUTORIAL.md` Chapter 9.

### 3.3 Run Analysis

One button: "▶ Run Full Pipeline" → `_ma_run_pipeline()` →
`_ma_pipeline_thread()`. Requires Setup applied and outages loaded;
guarded against double-invocation (`self._ma_pipeline_running`) since two
concurrent worker threads writing the same result attributes could pair a
verdict from one run with regression detail from another.

**Important scope detail:** the panel regressions always run across the
**full dataset** (every CNEC in `self._ma_jao_df`), never filtered down to
just the CNEC selected in Setup — `PanelOLS` needs ≥2 entities for entity
fixed effects, so a single-CNEC subset wouldn't fit at all. The selected
"Target CNEC" is used only as a label, passed to `summarize_hypotheses()`.
If you're ever debugging "why did changing the target CNEC not change the
H1–H4/H6 coefficients," this is why — it isn't supposed to.

Hypotheses run here: **H1** (`fall_signed`/`fall`), **H2**
(`ptdf_<SRC>_abs`, falling back to `ptdf_FI_abs`/`ptdf_FI`), **H3**
(`ram`), **H4** (`shadowPrice`/`shadowPrice_clean`, binding rows only,
skipped if fewer than 50 binding MTUs), **H6** (`frm`). H5 (the IVA
logit) is run separately via `run_logit_iva()` and folded into the same
`summarize_hypotheses()` call. All five/six specs go through
`propagation._indep_for_hypothesis()` for their regressor list and
`run_panel_regression(..., cluster="time")` for the fit.

### 3.4 Results

A sortable table of verdicts (one row per hypothesis, color-tagged green/
red/amber by SUPPORTED/rejected/inconclusive) plus a detail pane that
shows the full coefficient table for whichever row is selected. The
hypothesis→dependent-variable mapping used to look up detail
(`_hyp_to_dep` inside both `_ma_refresh_results()` and
`_ma_on_result_select()`) is a **hardcoded dict duplicated in two
places** — if the dep-var choice in §3.3 ever changes (e.g. H2 starts
using a different PTDF column), update both copies or the detail pane will
silently show the wrong (or no) regression table for that row.

### 3.5 Plots

Four chart types (Time Series, PTDF_FI Distribution, Shadow Price vs RAM,
CNEC Binding Frequency), all drawn from `self._ma_covariates` by
`_ma_draw_plot()`. Purely visual, no analytical logic of its own worth
documenting beyond: they read the *population* covariates, not any
single-event slice, so they reflect Run Analysis's output, not Single
Event's.

### 3.6 Single Event

The richest sub-tab, and the only one with its own inner notebook (6
panes: Summary, ITS, ΔRAM decomposition, DiD, Per-CNEC table, Price
Spread). One call does all the work for the first 5: `propagation
.single_event_analysis()`, given the selected outage row,
baseline/post-event day counts, and an ITS method key. The method
dropdown is built directly from `propagation.ITS_METHOD_NAMES` (plus a
synthetic `"all"` entry) — see `TUTORIAL.md` Chapter 21 for what each of
the 15 methods actually does; this tab doesn't add any method-selection
logic of its own, it's a thin UI over the same catalog `dashboard.py`'s
Single Event tab uses.

What each pane reads from `single_event_analysis()`'s return dict:

| Pane | Reads | Notes |
|---|---|---|
| Summary | `summary`, `its_summary`, `did_estimates` | Plain-text digest: parameter shifts, ITS impacts, DiD betas |
| ITS | `its`, `its_all` | `its_all` (populated only when method=`"all"`) overlays every method's projection on one chart |
| ΔRAM decomposition | `decomp` | Horizontal bar chart, one bar per RAM-identity term's average MW contribution |
| DiD | `did`, `did_estimates` | High-\|PTDF_FI\| CNECs (treatment) vs low (control); ATT = (high_during−high_pre) − (low_during−low_pre) |
| Per-CNEC table | `cnec_table` | Per-CNEC pre/during/Δ for f0, ram, shadowPrice |
| Price Spread | `cnec_table` (CNEC list) + `its` (timestamp window) + `summary['start_utc']` (counterfactual cutoff) | **Not** part of `single_event_analysis()`'s own output — see below |

Runs in a background thread (`_ma_single_thread`), same reentrancy guard
pattern as Run Analysis. Rows with a null/blank `cneName` are dropped
before the call — a defensive fix for `sorted(cneName.unique())` crashing
on a mixed float/str column, which is a real shape real JAO exports can
have.

**Scrolling and Full Screen.** ITS, ΔRAM decomposition, and Price Spread
are each wrapped in a scrollable viewport (`_make_scrollable_pane` — a
`tk.Canvas` + inner `Frame` + vertical scrollbar + mousewheel binding,
factored out of ITS's own pre-existing pattern so ΔRAM decomposition and
Price Spread got it too) rather than being clipped on a short window;
Summary, DiD, and Per-CNEC table already had their own scrollbar
(`Text`/`Treeview` built-in) and didn't need it. ITS and ΔRAM
decomposition also carry a **⛶ Full Screen** button next to their
toolbar — `_open_figure_fullscreen()` renders the current `Figure` to a
temp PNG and opens it in its own large, scrollable `Toplevel` (Esc or
Close to dismiss) rather than re-embedding the *same* `Figure` object in
a second live canvas, which would risk the next in-place redraw
(`fig.clear()` + rebuild, which both charts do on every re-analysis)
fighting over which canvas owns it. **Both Full Screen buttons are
gridded as a SIBLING of their toolbar frame, not a child of it** —
`_add_toolbar()` destroys every child of the frame it's given to rebuild
`NavigationToolbar2Tk` on each run, so a button placed inside that frame
would vanish after the first analysis.

**Hover coordinates (app-wide, not just this tab).** Every chart built
through `_setup_ax(ax, labels)` overrides `ax.format_coord` to look the
hovered x position up in `labels` (the same list already used to build
`set_xticklabels`) instead of showing matplotlib's raw index float — this
was silently broken (`x=42.31`) everywhere `_setup_ax` got real labels.
**Twin axes need the identical fix applied SEPARATELY** — `_style_twin`
now takes an optional `labels` argument too, and every `.twinx()` call
site in the file (Tabs 5, 7, and the Shadow-Price-&-RAM chart) passes its
own `xs` through. This isn't a cosmetic duplicate of the primary axis's
fix: matplotlib's mouse-move handler calls `format_coord` on whichever of
the two overlapping twins is `event.inaxes` (topmost at that pixel,
typically the twin itself, since it's created after the primary via
`ax.twinx()`) — fixing only the primary left the twin showing
matplotlib's own default twin-aware `format_coord`, which renders
`"(x, y) = (a, b) | (c, d)"` by calling each twin's `format_xdata()`, and
that comes back BLANK for any x not exactly on a tick once
`set_xticklabels()` has swapped in a `FixedFormatter` — producing exactly
the broken `"(x, y) = (, 453.) | (, 1093.)"` this was fixed for (caught
from a real screenshot of the Shadow Price & RAM twin chart). The ITS
chart plots real `dateTimeUtc` values directly rather than an index, so
it gets its own `format_coord` converting the hovered matplotlib date
float back to a CET string via `mdates.num2date(x).astimezone(_CET)` —
this app's usual human-facing convention, not UTC and not a bare float.
Tab 6's Price History and Tab 9 Plots' Time Series / CNEC Binding
Frequency charts previously passed `_setup_ax(ax, [])` — an empty label
list — so they had no date/category shown anywhere, not on the ticks and
not on hover; all three now pass their real label list through.
`dashboard.py` (the other GUI) has the same underlying pattern in its own
charts and has NOT been touched here — a known follow-up, not an
oversight.

**Price Spread pane (optional, added after the rest of this tab).**
Shows how much ONE CNEC's contribution to a TARGET zone's price has
moved, at one timestamp the user picks, relative to what it would have
been without the outage. The timestamp is entered as two separate,
plain **Date (CET):** / **Time (CET):** `ttk.Entry` fields — typed, not
selected from a dropdown, matching Tab 2's own filter row exactly (an
earlier version used cascading comboboxes; the user asked for typed
fields to match Tab 2's actual widget type, not just its label
convention). `_ma_refresh_spread_timestamps` auto-fills both fields with
this event's own outage `start_utc`, converted to CET — the most
meaningful single default instant — falling back to the earliest
timestamp in this CNEC's own pre/during/post window if `start_utc` is
missing or unparseable; the fields stay freely typeable afterward.
`_ma_run_price_spread` converts whatever ends up in them (default or
hand-edited) back to UTC via `propagation.cet_input_to_utc` before
calling `estimate_cnec_price_impact` (which still matches rows in UTC
internally), and the result panel echoes both the matched timestamp and
the pre-period cutoff back in CET via `propagation.utc_to_cet_str` — the
same two conversion points every other CET input/output in this app goes
through. The whole pane (controls + results) is wrapped in a scrollable
viewport (`_make_scrollable_pane`, see below) so the result panel is
never squeezed off-screen on a short window, and the result `Text`
widget itself uses `wrap='none'` plus a horizontal scrollbar rather than
reflowing its fixed-width aligned columns.

```
impact = (shadowPrice_actual × PTDF_target_actual)
         − (shadowPrice_counterfactual × PTDF_target_counterfactual)
```

Both `shadowPrice` and `PTDF_target` get their own ITS counterfactual
projection (fit on this event's own pre-period, i.e. everything strictly
before the outage's `start_utc`) — not just shadow price — because PTDF
itself can shift during an AC-line outage on the target zone (the same
mechanism H2 tests for). An **earlier version of this pane** compared the
CNEC's contribution to *two different zones'* prices at the same instant
(`shadowPrice × (PTDF_target − PTDF_reference)`), which needed a
user-typed reference-zone price as an anchor — that answers "how does
this CNEC split the price between two zones right now," a different
question from "how has this CNEC's effect on ONE zone's price changed
*because of the outage*," which is what this pane now answers. That's
why there's no reference zone or reference price input anymore.

Backed by `propagation.estimate_cnec_price_impact(df, cnec, tgt_zone,
pre_end, timestamp, its_method=...)` — a small, standalone, unit-tested
function (`TestEstimateCnecPriceImpact` in `test_pipeline.py`,
including a leak-safety test mirroring every other ITS method's own),
independent of `single_event_analysis()`'s own return dict. It is
deliberately **not** a full zonal price forecast: it's ONE CNEC's own
impact, not a sum over every binding CNEC affected by the outage — the
result pane says so explicitly, every time.

The `its_method` passed in is **not** a separate choice in this pane —
it reuses whichever counterfactual model is currently selected in the
"Counterfactual model" dropdown above (Chapter 21 of `TUTORIAL.md`
covers what each of the 15 methods actually does), falling back to
`propagation.ITS_DEFAULT_METHOD` if `"all"` is selected there (which
isn't a single fittable method on its own). The CNEC/zone/timestamp
pickers are populated from the **currently analysed event**, not the
whole dataset: CNECs come from that event's own `cnec_table`; the target
zone list comes from whichever `ptdf_<ZONE>` columns actually exist in
the loaded data (discovered dynamically, never a hardcoded zone list —
real JAO exports don't all carry the same zones); timestamps come from
that CNEC's rows within the event's own pre/during/post window (derived
from `its['dateTimeUtc']`'s min/max). This means the pane is empty until
at least one Single Event analysis has completed —
`_ma_refresh_spread_controls()` is called at the end of `_ma_single_done()`
specifically to populate it.

`propagation.estimate_cnec_price_impact()` **refuses** (returns
`ok=False` with an explanatory `error` string, shown directly in the
result pane) rather than computing from partial data: the CNEC has no
rows at all; the target zone's `ptdf_<ZONE>` column doesn't exist or is
entirely empty; the pre-period (rows strictly before the outage's
`start_utc`) has fewer than 4 distinct timestamps to fit a counterfactual
from; or no row exists within 1 minute of the requested timestamp. This
mirrors `_its_ptdf_flow()`'s own "validate before trusting the physics"
posture elsewhere in `propagation.py`.

### 3.7 Explain

Generates a plain-language digest of the verdicts — **not** a call into
any `propagation.py` report function, just local string formatting over
`self._ma_verdicts`.

**Known inaccuracy, worth fixing if you're in this code:** the generated
report's "METHODOLOGY" footer is a **static string** —

```
"Interrupted time series uses seasonal-naive (hour x weekday) baseline."
```

— regardless of which ITS method was actually selected and run in the
Single Event sub-tab. If a user ran the analysis with `catboost`,
`ram_identity`, or `ensemble`, this text is simply wrong for that run. It
doesn't read `self._ma_single_res` at all. This is a real, fixable gap:
the correct fix is to read the method actually used (from
`self._ma_its_method_var`/`self._ma_single_res`, when a single-event run
exists) and interpolate it, rather than hardcoding `seasonal-naive`.

### 3.8 Export

Three CSV exports (`ma_covariates.csv`, `ma_outages.csv`,
`ma_verdicts.csv`) — plain `DataFrame.to_csv()`, nothing notable.

One HTML export (`_ma_export_html`) — and this is the second thing worth
knowing before you assume it behaves like the rest of the app's reports:
it is a **separate, self-contained report generator**, built directly in
this GUI file, **not** `propagation.render_html_report()`/
`build_report_ctx()` (the function every other report in this codebase —
`dashboard.py`'s Export tab, `run_analysis.py`, the Nordic-matrix sweep —
goes through). It embeds whatever's currently drawn on the `Plots`
figure and, if present, the `Single Event → ITS` figure as base64 PNGs,
plus a verdicts table and the Explain text.

**It does not include the ΔRAM decomposition chart or the DiD estimates**
— only `_ma_fig_plots` and `_ma_fig_single` are captured; `_ma_fig_decomp`
and the DiD text pane are visible in the GUI but never make it into the
exported HTML. If a user reports "the decomposition/DiD numbers I saw on
screen aren't in my exported report," that's this gap, not a bug in the
underlying analysis — the data exists (`self._ma_single_res['decomp']`/
`['did']`/`['did_estimates']`), it just isn't wired into
`_ma_export_html()` yet.

Default output directory for everything in this tab (Setup, Run, Export)
is `<repo_root>/ma_output/` — gitignored except a `.gitkeep`; nothing
generated there should be committed.

## 4. How this differs from `dashboard.py`'s equivalent flow

Both ultimately call the same `propagation.py` functions and should agree
on every number for the same input data. What's different:

- **Orchestration is duplicated, not shared.** `dashboard.py` and
  `run_analysis.py` go through `propagation.run_pipeline()`; Tab 9 inlines
  its own version of that same sequence (§3.3). A bug fixed in
  `run_pipeline()` does not automatically fix Tab 9, and vice versa —
  check both if you're chasing a discrepancy between the two UIs on
  identical input.
- **Report generation is duplicated, not shared** — see §3.8.
- **Its own independent CET/UTC toggle** — see §3.1.
- Both use the same threading pattern (background worker + `root.after(0,
  ...)` to hand results back to the main thread) with the same
  reentrancy-guard idiom (`self._ma_*_running` flags) — if you're adding a
  new long-running action to this tab, copy that pattern rather than
  inventing a new one.

## 5. Maintenance checklist

When something in this tab looks wrong, check in this order:

1. **Is the right state variable populated?** (§2) — a blank pane
   downstream of a step that wasn't actually run is the most common
   "bug report."
2. **Did Run Analysis actually use all CNECs, not just the selected
   one?** (§3.3) — expected behavior, not a bug, but a frequent source of
   confused bug reports ("changing the CNEC didn't change H1's p-value").
3. **Is the discrepancy between this tab and `dashboard.py`?** — check
   both orchestration paths independently (§4); don't assume a fix in one
   propagates to the other.
4. **Is it the Explain tab's methodology text?** — known-stale, always
   says "seasonal-naive" regardless of the actual ITS method used (§3.7).
5. **Is content missing from an exported HTML report but present on
   screen?** — check whether it's the ΔRAM decomposition chart or DiD
   text; both are known-omitted from `_ma_export_html()` (§3.8).
6. **Timestamp misalignment?** — check this tab's own independent
   UTC/CET toggle on the Setup sub-tab before assuming a data problem.
