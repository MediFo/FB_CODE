# Methodology Review: FI → NO3 Flow-Based Propagation Analysis

## Executive Summary

The audit identified **8 issues** of varying severity, spanning power-systems
mathematics, econometric identification, and statistical practice. Three are
critical and affect the validity of the main results. All are fixed in the
updated code.

---

## Issue 1 — CRITICAL: RAM formula is incomplete (missing `fnrao`)

**Finding.** The JAO Nordic CSV exposes `fnrao` (Flow from Non-costly Remedial
Actions and Other adjustments). This is the FRA term in the Nordic CCM formula.
Without it, every RAM reconstruction was 300–400 MW wrong.

**Correct Nordic RAM formula (from JAO Nordic Publication Handbook v1.5):**

```
RAM = Fmax − FRM − Fref + fnrao + AMR − AAC − IVA
```

Where:
| Column   | Meaning |
|----------|---------|
| `fmax`   | Thermal rating of the CNE (MW) |
| `frm`    | Flow Reliability Margin — statistical, backward-looking (MW) |
| `fref`   | Reference flow at CGMA NP — the pre-calculated F0 (MW) |
| `fnrao`  | Flow from Non-costly RAs / FRA — remedial action capacity (MW) |
| `amr`    | Adjustment for Minimum RAM — ensures RAM ≥ 0 (MW) |
| `aac`    | Already Allocated Capacity (FAAC) — pre-allocated reserves (MW) |
| `iva`    | Individual Validation Adjustment — TSO discretionary (MW) |
| `fall`   | Reference flow at the line limit — NOT an input to RAM |

**Note on f0 vs fref.** In the JAO export, `fref` and what JAO labels `f0`
are numerically identical (correlation = 1.000, max |diff| = 0.0 MW). The
JAO publication tool exports the CGMA-NP reference flow as both fields. The
theoretical distinction (F0 at zero NP vs Fref at CGMA NP) requires per-MTU
net-position data not available in the shadow-price CSV. For all analysis
purposes, `fref` is the correct dependent variable and outage-propagation proxy.

**Fix applied:** The pipeline now uses `fnrao` as `fra`, and the RAM formula
is verified to balance before proceeding. A `verify_ram_formula()` function
is added that reports percentage of rows within 1 MW tolerance.

---

## Issue 2 — CRITICAL: Selection bias from shadow-price filter

**Finding.** The user's JAO fetch tool filters `shadowPrice > 0`, so the CSV
contains **only binding CNECs**. This creates selection on the outcome variable:
- Non-binding CNECs (RAM > 0, shadow price = 0) are systematically excluded
- Outage effects that reduce RAM without making a CNEC binding are invisible
- Shadow-price regression is not biased on the retained sample (no zeros to
  cause Tobit issues), but the population inference is wrong — coefficients
  describe the effect *conditional on already being binding*, not the general
  effect

**Fix applied:**
1. The pipeline now warns explicitly when all shadow prices are non-zero
   (indicating the filtered dataset was loaded).
2. The shadow-price regression is labelled "conditional on binding CNECs" in
   all outputs.
3. Recommendation: fetch JAO data with `shadowPrice` filter **off** for
   general population analysis. The filter is appropriate only when studying
   price-impact specifically on binding constraints.

---

## Issue 3 — CRITICAL: PTDF_FI sign heterogeneity across NO3 CNECs

**Finding.** Of the 21 NO3 CNECs in the sample:
- 17 have positive mean PTDF_FI (FI export loads these CNECs)
- 4 have negative mean PTDF_FI (FI export relieves these CNECs)

The sign depends on CNEC orientation (which terminal is "from" and which is
"to") set by each TSO when defining the CNEC. It is not a physical property.

**Consequence.** Regressing raw PTDF_FI on outage covariates, or using raw
`fall` across mixed-sign CNECs, produces coefficient attenuation. A FI outage
that moves all CNECs in the physically-correct direction will produce near-zero
coefficients because positive and negative PTDF CNECs partially cancel.

**Fix applied:**
1. Each CNEC gets a per-CNEC sign `σ_i = sign(median fall over pre-period)`.
2. The signed dependent variables `fall_signed = σ_i × fall` and
   `ptdf_FI_abs = |PTDF_FI|` are computed before regression.
3. Hypothesis H1 now tests `fall_signed` (sign-normalised F_allReference),
   H2 tests `ptdf_FI_abs`.
4. The `build_covariates` function adds `fall_signed` and `ptdf_FI_abs` columns.

**Physical interpretation after fix:** A positive β on `fi_hvdc_outage_active`
for `fall_signed` means "the outage loads the CNE in the congested direction
for CNECs where FI normally loads that direction" — which is the correct
physical hypothesis.

**Note on `fref` / `f0` vs `fall`:** `fref` (identical to `f0` in JAO exports)
is the flow at CGMA NP — it is NOT the reference flow in the RAM formula.
`fall` (F_allReference) is the correct term. See Issue 1 for full details.

---

## Issue 4 — Moderate: Event study time scale is in MTUs (15 min), not hours

**Finding.** With `lags=8`, the event study captures only 2 hours post-event.
Fenno-Skan outages last 12–72 hours; generator outages 4–24 hours. The dynamic
response (persistence, decay) is not visible at MTU scale.

**Fix applied:** The event study now accepts a `scale` parameter:
- `scale='mtu'` (default, 15-min steps) — for short events and granular analysis
- `scale='hour'` — bins MTUs into hourly averages first, then runs leads/lags
- `scale='day'` — daily bins, for multi-day outages

Default changes to `scale='hour'` with `leads=24, lags=48` (24h pre, 48h post).
This gives a full picture of impact and recovery for any outage lasting ≤ 2 days.

---

## Issue 5 — Moderate: Clustering at CNEC level, but treatment is at event level

**Finding.** Standard errors are clustered by CNEC (21 clusters). But the
treatment — the FI outage — happens at a point in time and hits ALL 21 CNECs
simultaneously. Within each event, the CNEC errors are not independent; they
share the common time shock of the outage. Clustering by CNEC alone does not
account for this cross-sectional correlation within events.

**Correct clustering:** Two-way clustering on (CNEC, event-date). Our code
already attempts this but falls back to entity-only because of the unbalanced
panel. The right fix for staggered DiD is to cluster by calendar date (which
groups all CNECs within the same outage event):

```python
# Cluster at the calendar-date level, not CNEC level
res = mod.fit(cov_type="clustered", cluster_time=True)
```

This is now implemented as a time-clustered fallback before the entity-clustered
fallback. Number of clusters = number of distinct dates (~365 for a 1-year window).

**Implication for inference.** With entity clustering, SEs are too small (under-
rejection of H0). With time clustering, SEs are correctly sized for the outage
design. Expect p-values to increase (some borderline significant results may
become insignificant).

---

## Issue 6 — Moderate: ITS baseline ignores diurnal and weekly seasonality

**Finding.** The interrupted time series fits a linear time trend on the
7-day pre-period and projects it forward. F0 has strong diurnal patterns
(day/night peak of 50–100 MW) and weekly patterns (weekend reduction of ~30 MW).
A linear trend projected over a 48-hour outage window will systematically
over- or under-predict depending on what time of day the outage starts.

**Fix applied:** The ITS now fits:
```
F0_t = α + β·t + Σ_{h=0}^{23} δ_h · 1(hour=h) + Σ_{d=0}^{6} γ_d · 1(dow=d) + ε_t
```
and projects the seasonal pattern into the outage window. The "gap" (actual
minus projected) is the deseasonalised outage effect.

---

## Issue 7 — Minor: DiD PTDF split uses full-sample mean, not pre-outage mean

**Finding.** We classify CNECs as "high PTDF_FI" vs "low PTDF_FI" using the
average PTDF_FI across the whole window. But PTDF_FI changes during AC topology
outages (the very thing we're studying). This creates post-treatment
contamination in the classification.

**Fix applied:** The DiD split now uses PTDF_FI from the pre-outage baseline
period only.

---

## Issue 8 — Minor: Binary outage dummy ignores dose (MW lost)

**Finding.** A 100 MW generator outage and a 1,600 MW Olkiluoto unit outage both
get `fi_gen_outage_mw_lost` in the covariates, but the binary dummy
`fi_forced_outage_active` treats them equally. The dose-response relationship
(larger outage → larger F0 shift) is econometrically more informative and
physically more defensible.

**Status.** `fi_gen_outage_mw_lost`, `fi_hvdc_outage_mw_lost`, and
`fi_ac_outage_mw_lost` are already in the default regression (MW-valued, not
binary). The binary dummies are kept as baseline controls. No additional code
change needed — the specification is already correct. Ensure MW covariates are
included in the primary specification, not just the binary ones.

---

## Correct econometric specification (revised)

### Population regression (H1–H6)

```
Y_{it} = Σ_k β_k · D_{k,it}      (event-time dummies, leads/lags in hours)
        + Σ_m γ_m · MW_{m,it}     (MW dose: HVDC, AC, generator lost)
        + α_i                      (CNEC fixed effect)
        + δ_h                      (hour-of-day fixed effect)
        + δ_d                      (day-of-week fixed effect)
        + ε_{it}
```

Dependent variables and transformations:
| Hypothesis | Dependent variable | Transformation |
|---|---|---|
| H1 | fall (F_allReference) | Sign-normalised per CNEC (`fall_signed`) |
| H2 | ptdf_FI | Absolute value per CNEC (`ptdf_FI_abs`) |
| H3 | ram | As published |
| H4 | shadowPrice | Conditional on binding; note selection |
| H5 | iva>0 (binary) | Logit |
| H6 | frm | As published (placebo — should be flat) |

Standard errors: time-clustered (calendar date), giving ~365 clusters for a
1-year window. This correctly accounts for cross-CNEC correlation within events.

### Single-event analysis (ITS + DiD)

ITS baseline: `F0_t = α + β·t + hourFE + dowFE + ε_t`, fit on 7-day pre-window.

DiD: treatment = CNECs with `|PTDF_FI_pre| > median(|PTDF_FI_pre|)` across all
NO3 CNECs. ATT = (Δ treatment) − (Δ control) in event window vs pre-window.
Pre-trend test: verify that high-PTDF and low-PTDF CNECs have parallel trends
in the 7 days before the outage.

---

## What each result actually means physically

| Significant result | Physical mechanism | Expected in data? |
|---|---|---|
| β(HVDC outage, fref_signed) > 0 | HVDC down → FI net import decreases → AC rerouting loads NO3 corridor in congested direction | Yes for forced Fenno-Skan when FI is net importer |
| β(HVDC outage, fref_signed) < 0 | HVDC down → FI net export decreases → less AC transit through SE1→SE2→NO3 | Yes when FI is net exporter |
| β(AC line, ptdf_FI_abs) > 0 | FI topology change → PTDF matrix recomputed → larger sensitivity of NO3 CNECs to FI | Yes for any FI internal 400 kV line outage |
| β(HVDC, ram) > 0 | HVDC out → F0 decreases (less loading) → RAM increases | Yes when FI→SE3 export reduces NO3 loading |
| β(HVDC, ram) < 0 | HVDC out → AC rerouting → more NO3 loading → RAM decreases | Yes when SE3→FI import reduces and AC picks up |
| β(forced, iva>0) > β(planned) | Forced outage not in IGM → TSO applies IVA to correct domain | Documented (Fenno-Skan Nov 2024) |
| β(frm, any) ≈ 0 | FRM is statistical, updated annually | Yes by construction |

The direction of the HVDC effect depends on whether FI is a net importer or
exporter during the outage period. This is not a methodological ambiguity —
it is the core empirical finding. Separate the analysis by season and by
FI net position (positive = net exporter, negative = net importer) to resolve
the direction.
