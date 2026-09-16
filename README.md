# fi_no3 — Nordic Flow-Based Propagation

Validates whether a Nordic country's maintenance and forced outages
propagate to a given bidding zone's CNEC parameters (`F_allReference`,
PTDF, `RAM`, shadow price) in the Nordic day-ahead flow-based capacity
calculation. The pipeline works from real JAO Publication Tool exports and
ENTSO-E outage events, runs panel regressions per hypothesis, and produces
an HTML report — either for a single source-country → target-zone pair
(FI → NO3 by default) or, with `--all-nordic-zones`, for every Nordic
source country (FI/SE/NO/DK) against every Nordic bidding zone
(FI, SE1–4, NO1–5, DK1–2) in one consolidated report.

This is a diagnostic/statistical analysis tool that reads already-published
Nordic FBMC parameters — it does not implement or simulate the capacity
calculation or market coupling itself.

This repository is the installable `fi_no3` package (`pyproject.toml` maps
the repo root itself to the package, since there is no `src/` layout). This
is the `fi-no3-app` branch: the package flattened to the repo root, split
off from `claude/energy-model-audit-fafl07`, which keeps the same code
nested under a `file/` directory next to an older, independent flat-file
copy of the same project — see `CLAUDE.md` for background if you need it.
`Old/` in this repo predates both and is unrelated: an even earlier
generation of the flat-file app, kept as-is.

## Quick start

```bash
# Editable install — also creates the fi-no3-analyse / fi-no3-dash console
# scripts
pip install -e .

# Main GUI (6 tabs — outage sources, JAO load, hypotheses, single-event,
# report, and the reverse-engineered richer pipeline)
python dashboard.py

# Extended GUI (9 tabs — adds JAO/Nord Pool fetch tooling, day-batched via
# Windows PowerShell, on top of the same propagation.py backend)
python app_jao_NP_API_fix_d14.py

# CLI, no GUI required — single pair (defaults to FI -> NO3)
python run_analysis.py --synthetic --days 30 --out results/
python run_analysis.py --jao data/jao_export.csv --source SE --target NO1 --out results/

# CLI — every Nordic source-country x bidding-zone pair in one sweep
python run_analysis.py --jao data/jao_export.csv --all-nordic-zones --out results/

# Test suite
pytest test_pipeline.py -v
```

## Layout

```
__init__.py                    fi_no3 package init — re-exports the public API
dashboard.py                   6-tab tkinter GUI — primary entry point
app_jao_NP_API_fix_d14.py      9-tab GUI — adds JAO/Nord Pool data-fetch
                                tooling (superseded d07–d13 prior
                                iterations were removed in a repo cleanup;
                                nothing else referenced them)
propagation.py                 analytical pipeline (CET/UTC boundary, JAO/
                                ENTSO-E ingestion, covariate construction,
                                panel regressions, hypothesis tests H1–H6,
                                HTML report generation)
synthetic.py                   synthetic JAO + outage data generator, for
                                testing without live API access
run_analysis.py                CLI entry point / fi-no3-analyse console script
test_pipeline.py               pytest suite
pyproject.toml                 packaging (editable install, console scripts)
manual_outages.csv             hand-curated FI outage events (edit this; the
                                checked-in copy is the auto-generated template
                                — replace it with real events before relying
                                on it)
ma_output/                     generated Maintenance-Analysis (Tab 9) output
map.png, map2.png, Slide1w.PNG reference images used by the GUIs
METHODOLOGY.md                 prior methodology notes (unreproduced claims
                                — see note below)
```

There is no `data/` directory and no committed real JAO export or model
output in this package — `manual_outages.csv` here is the single-row
template the code writes when the file is missing, not curated data. Point
`dashboard.py` / `app_jao_NP_API_fix_d14.py` at your own JAO CSV, or use
`synthetic.py` to generate a test fixture.

## What it tests

Shown below for the FI → NO3 default; the same six hypotheses are built for
any source country / target zone pair (`SRC`/`TGT`) via
`_build_hypotheses(src, tgt)` — running `--all-nordic-zones` evaluates all
six for every pair in the sweep.

| ID | Hypothesis |
|----|------------|
| H1 | SRC HVDC outage shifts `fall` (sign-normalised F_allReference) on TGT CNECs |
| H2 | SRC AC line outage shifts `|PTDF_SRC|` on TGT CNECs (topology-only effect) |
| H3 | SRC forced outage changes RAM on TGT CNECs |
| H4 | SRC forced outage changes shadow price on binding TGT CNECs |
| H5 | IVA is more frequent under forced than planned SRC outages |
| H6 | Placebo — FRM should **not** move with individual outage events |

Multiple-testing correction (Holm–Bonferroni) is applied across H1–H4, and a
failed H6 placebo demotes significance claims elsewhere in the report.

## Key domain facts

- Nordic RAM identity: `RAM = Fmax − FRM − Fall + fnrao + AMR − FAAC − IVA`.
  `fall` = F_allReference (not `fref`/`f0`, the CGMA-NP reference flow).
- ENTSO-E A77 = production outages, A78 = transmission outages. Timestamps
  are tz-aware, localized to the queried country's own zone.
- User-facing time (GUI inputs, logs, reports) is CET/CEST
  (`Europe/Oslo`, DST-aware); internal storage and computation stay UTC.
  `propagation.cet_input_to_utc()` / `utc_to_cet_str()` are the two
  conversion points.
- `manual_outages.csv` timestamps with no explicit UTC offset are
  interpreted as CET/CEST, matching how a person types them.
- FRM is structural (annual calibration) and should not move with
  individual outages — that's what H6 checks.
- `app_jao_NP_API_fix_d14.py`'s fetch is day-batched (one request per
  calendar day) via a Windows PowerShell subprocess, not per-15-minute MTU
  — it only runs where PowerShell is available.
- JAO's own Publication Handbook documents that its `dateTimeUtc` field can
  actually be CET, not UTC, despite the name. Every real-data entry point
  (dashboard.py, both GUIs, the CLI) has its own independent, optional
  "UTC"/"CET" toggle for this — default stays UTC (unverified against live
  data; not changed without confirmation), separate per tab so you can
  compare interpretations without re-fetching. If outage/event alignment
  looks consistently off by 1h (winter) or 2h (summer), try the CET setting
  on the relevant tab. See `CLAUDE.md` for the full mechanism.

See `CLAUDE.md` for the fuller list of domain facts and known limitations
this codebase currently has, and the JAO Nordic Publication Handbook for the
authoritative definition of each CNEC field.

## Note on `METHODOLOGY.md`

That file is a standalone methodology write-up already present in the
repository, asserting specific figures (e.g. a CNEC count and PTDF sign
split) without a committed dataset or script to reproduce them. Treat its
narrative as background reading, not as verified fact — reproduce any
number you need to rely on against your own JAO export first.

## License

MIT — see `LICENSE`.
