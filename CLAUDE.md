# FI → NO3 Flow-Based Propagation — Claude Code Project

## What this project does
Validates whether Finnish (FI) maintenance outages propagate to NO3 CNEC
parameters (F0, PTDF_FI, RAM, shadow price) in the Nordic day-ahead
flow-based capacity calculation. Uses real JAO publication-tool data and
ENTSO-E outage events. Runs PanelOLS regressions and produces an HTML report.

## Architecture
```
src/fi_no3/
  propagation.py   — pure analytical pipeline (no UI code)
  synthetic.py     — synthetic JAO + outage data generator for testing
  dashboard.py     — tkinter 6-tab GUI (runs standalone)
tests/
  test_pipeline.py — full test suite (pytest)
  test_synthetic.py
scripts/
  run_analysis.py  — CLI entry point
  fetch_entsoe.py  — standalone ENTSO-E outage fetcher
data/
  manual_outages.csv   — hand-curated FI outage events (edit this)
  jao_export.csv       — place your JAO CSV here
```

## Key domain facts Claude Code should know
- Nordic RAM formula: RAM = Fmax - FRM - F0 + FRA + AMR - FAAC - IVA
- F0 proxy: JAO publishes `fref` and `f0`; both are populated in real CSVs
- NO3 corridor CNECs: "300KLABU-ORKDAL", "300VERDAL-TUNNSJODAL", "300AURA-VAGAMO" etc.
  Some CNECs appear as UUIDs — the NO3 filter uses biddingZoneFrom/To == "NO3"
- ENTSO-E token: set env var ENTSOE_API_KEY (never hard-code)
- Fingrid key: set env var FINGRID_API_KEY
- ENTSO-E A77 = production outages; A78 = transmission outages
- A78 returns mostly forced events (BSNTYPE A54); planned line outages need manual CSV
- FRM is structural (yearly calibration); it should NOT move with individual outages (H6 placebo)
- Two-way clustered SE fails on unbalanced panels → silent fallback to entity-only clustering
- Outage covariates are zero when no outage overlaps JAO window → verdicts show as n/a

## Commands Claude Code should know
```bash
# Install dependencies
pip install pandas numpy matplotlib statsmodels linearmodels requests entsoe-py jinja2

# Run tests (uses synthetic data, no API keys needed)
pytest tests/ -v

# Run GUI dashboard
python src/fi_no3/dashboard.py

# Run CLI analysis
python scripts/run_analysis.py --jao data/jao_export.csv --out results/

# Generate synthetic test data
python -c "from src.fi_no3.synthetic import generate_demo_dataset; generate_demo_dataset('data/synthetic', days=90)"

# Fetch ENTSO-E outages for a date range
export ENTSOE_API_KEY=your-token-here
python scripts/fetch_entsoe.py --start 2026-04-19 --end 2026-05-07 --out data/outages.csv
```

## Common tasks and how to do them
**Add a new hypothesis**: edit `HYPOTHESES` list in `propagation.py` and add
fallback vars in `summarize_hypotheses()`.

**Add a new NO3 CNEC pattern**: add a regex to `DEFAULT_NO3_PATTERNS` in
`propagation.py` or pass custom patterns to `filter_no3()`.

**Test with real JAO data**: place CSV in `data/jao_export.csv`, add outages
to `data/manual_outages.csv`, run `python scripts/run_analysis.py`.

**Fix "hypothesis tab empty"**: this means no outage events overlap the JAO
window. Check the overlap warning in the run log. The outage fetch date window
must match the JAO CSV date range.

## Known limitations
- ENTSO-E API returns 403 from cloud/server IPs (works from local machine)
- ENTSO-E A77 returns "File is not a zip file" when no FI production outages exist in window
- IVA is zero on NO3 CNECs in short windows; H5 logit needs longer history
- FRM in synthetic data moves with outages (December 2024 regime change is encoded)
- Two-way clustered SE always falls back to entity-only for unbalanced panels
