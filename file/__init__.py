"""fi_no3 — FI → NO3 Flow-Based Propagation Analysis"""
from .propagation import (
    load_jao_csv, filter_no3, build_covariates, deduplicate_outages,
    run_panel_regression, run_logit_iva, decompose_delta_ram,
    summarize_hypotheses, PipelineConfig, run_pipeline, render_html_report,
)
from .synthetic import generate_demo_dataset
