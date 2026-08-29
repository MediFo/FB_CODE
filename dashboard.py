"""
fi_no3_dashboard.py
===================
Tkinter dashboard for FI -> NO3 flow-based propagation validation.

Designed to integrate with an existing JAO CSV workflow (loads the same
CSV format produced by your tool's tabs 1-2). Adds:
  - Setup tab: choose synthetic vs real data
  - Outages tab: toggle Fingrid / ENTSO-E / Manual sources, fetch
  - Run tab: kick off the pipeline with progress
  - Results tab: hypothesis verdicts + regression summaries
  - Plots tab: time series, distributions, scatter, decomposition
  - Export tab: HTML report + CSVs

Run:
    python fi_no3_dashboard.py
or import:
    from fi_no3_dashboard import App, launch
    launch()
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Auto-install missing dependencies before any other imports
# ---------------------------------------------------------------------------
import subprocess, sys as _sys

def _ensure(*packages):
    """Install packages that are not yet importable."""
    import importlib
    for pip_name, import_name in packages:
        try:
            importlib.import_module(import_name)
        except ImportError:
            print(f"[setup] installing {pip_name}...")
            subprocess.check_call(
                [_sys.executable, "-m", "pip", "install", "--quiet", pip_name],
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )

_ensure(
    ("pandas",       "pandas"),
    ("numpy",        "numpy"),
    ("matplotlib",   "matplotlib"),
    ("statsmodels",  "statsmodels"),
    ("linearmodels", "linearmodels"),
    ("requests",     "requests"),
    ("entsoe-py",    "entsoe"),
)
del _ensure, subprocess
# ---------------------------------------------------------------------------

import math
import os
import sys
import threading
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk,
)

# All files live in the same folder as this script.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import propagation as pipe
import synthetic as syn


APP_TITLE = "Flow-Based Propagation Dashboard"
DEFAULT_OUT_DIR = str(Path.home() / "fb_no3_output")


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x900")

        # --- shared state ---
        self.jao_df: pd.DataFrame | None = None
        self.outages_df: pd.DataFrame | None = None
        self.results: dict | None = None
        self.last_jao_path: str = ""
        self.last_outage_path: str = ""

        # Configurable source country and target zone
        self.source_country = tk.StringVar(value="FI")
        self.target_zone    = tk.StringVar(value="NO3")

        # styles
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except Exception:
            pass
        s.configure("Header.TLabel", font=("Helvetica", 12, "bold"))
        s.configure("OK.TLabel", foreground="#1a7a1a")
        s.configure("Bad.TLabel", foreground="#a01a1a")
        s.configure("Hint.TLabel", foreground="#555", font=("Helvetica", 9))
        s.configure("Big.TButton", padding=(12, 8))

        # tabs
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_setup    = ttk.Frame(self.nb)
        self.tab_outages  = ttk.Frame(self.nb)
        self.tab_run      = ttk.Frame(self.nb)
        self.tab_results  = ttk.Frame(self.nb)
        self.tab_plots    = ttk.Frame(self.nb)
        self.tab_event    = ttk.Frame(self.nb)
        self.tab_explain  = ttk.Frame(self.nb)
        self.tab_export   = ttk.Frame(self.nb)

        self.nb.add(self.tab_setup,   text="1. Setup")
        self.nb.add(self.tab_outages, text="2. Outage sources")
        self.nb.add(self.tab_run,     text="3. Run analysis")
        self.nb.add(self.tab_results, text="4. Results")
        self.nb.add(self.tab_plots,   text="5. Plots")
        self.nb.add(self.tab_event,   text="6. Single event")
        self.nb.add(self.tab_explain, text="7. Explain")
        self.nb.add(self.tab_export,  text="8. Export")

        self._build_setup_tab()
        self._build_outages_tab()
        self._build_run_tab()
        self._build_results_tab()
        self._build_plots_tab()
        self._build_event_tab()
        self._build_explain_tab()
        self._build_export_tab()

    # -----------------------------------------------------------------
    # Setup tab
    # -----------------------------------------------------------------
    def _build_setup_tab(self):
        f = self.tab_setup
        for c in range(3): f.columnconfigure(c, weight=1)

        ttk.Label(f, text="Data source", style="Header.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 4))

        self.data_mode = tk.StringVar(value="real")
        rb1 = ttk.Radiobutton(f, text="Real JAO CSV (loaded from disk)",
                              variable=self.data_mode, value="real",
                              command=self._on_mode_change)
        rb2 = ttk.Radiobutton(f, text="Synthetic data (generate for testing)",
                              variable=self.data_mode, value="synthetic",
                              command=self._on_mode_change)
        rb1.grid(row=1, column=0, sticky="w", padx=12)
        rb2.grid(row=1, column=1, sticky="w", padx=12)

        # --- Real data block ---
        self.real_frame = ttk.LabelFrame(f, text="Real JAO CSV", padding=10)
        self.real_frame.grid(row=2, column=0, columnspan=3, sticky="ew",
                             padx=12, pady=8)
        self.real_frame.columnconfigure(1, weight=1)

        ttk.Label(self.real_frame, text="JAO CSV path:").grid(row=0, column=0, sticky="w")
        self.jao_path_var = tk.StringVar()
        ttk.Entry(self.real_frame, textvariable=self.jao_path_var).grid(
            row=0, column=1, sticky="ew", padx=6)
        ttk.Button(self.real_frame, text="Browse", command=self._browse_jao).grid(
            row=0, column=2)
        ttk.Button(self.real_frame, text="Load", command=self._load_jao).grid(
            row=0, column=3, padx=(6, 0))

        ttk.Label(self.real_frame,
                  text=("Expected columns: dateTimeUtc, cneName, biddingZoneFrom/To,"
                        " ram, shadowPrice, fmax, frm, f0 (and optionally fra/amr/faac/iva),"
                        " ptdf_<zone> per zone."),
                  style="Hint.TLabel", wraplength=900).grid(
            row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # --- Synthetic block ---
        self.syn_frame = ttk.LabelFrame(f, text="Synthetic data", padding=10)
        self.syn_frame.grid(row=3, column=0, columnspan=3, sticky="ew",
                            padx=12, pady=8)
        ttk.Label(self.syn_frame, text="Days of data:").grid(row=0, column=0, sticky="w")
        self.syn_days = tk.IntVar(value=90)
        ttk.Spinbox(self.syn_frame, from_=14, to=365, textvariable=self.syn_days,
                    width=6).grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(self.syn_frame, text="Random seed:").grid(row=0, column=2, sticky="w", padx=(20, 0))
        self.syn_seed = tk.IntVar(value=7)
        ttk.Spinbox(self.syn_frame, from_=0, to=10000, textvariable=self.syn_seed,
                    width=6).grid(row=0, column=3, sticky="w", padx=6)
        ttk.Button(self.syn_frame, text="Generate synthetic JAO + outages",
                   style="Big.TButton",
                   command=self._generate_synthetic).grid(row=0, column=4, padx=(20, 0))

        ttk.Label(self.syn_frame,
                  text=("Generates 5 NO3 corridor CNECs and 5 FI outage events"
                        " (planned AC line, forced gen, planned/forced HVDC)."
                        " Effects on F0, PTDF_FI, IVA are deliberately encoded"
                        " so the regressions should pick them up."),
                  style="Hint.TLabel", wraplength=900).grid(
            row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # --- Analysis scope (source country + target zone) ---
        scope = ttk.LabelFrame(f, text="Analysis scope  (what to study)", padding=10)
        scope.grid(row=4, column=0, columnspan=3, sticky="ew", padx=12, pady=4)
        scope.columnconfigure(1, weight=0); scope.columnconfigure(3, weight=0)

        ttk.Label(scope, text="Outage source country (ENTSO-E code):").grid(
            row=0, column=0, sticky="w")
        src_cb = ttk.Combobox(scope, textvariable=self.source_country, width=8,
                              state="normal",
                              values=["FI","NO","SE","DK","EE","LV","LT"])
        src_cb.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(scope,
                  text="(e.g. FI = fetch Finnish outages; NO = Norwegian outages)",
                  style="Hint.TLabel").grid(row=0, column=2, sticky="w", padx=8)

        ttk.Label(scope, text="Target bidding zone (CNECs to analyse):").grid(
            row=1, column=0, sticky="w", pady=(6,0))
        tgt_cb = ttk.Combobox(scope, textvariable=self.target_zone, width=8,
                              state="normal",
                              values=["NO3","NO1","NO2","NO4","NO5",
                                      "SE1","SE2","SE3","SE4",
                                      "DK1","DK2",
                                      "FI","EE","LV","LT"])
        tgt_cb.grid(row=1, column=1, sticky="w", padx=6, pady=(6,0))
        ttk.Label(scope,
                  text="(filters JAO CNECs and sets PTDF column; default NO3)",
                  style="Hint.TLabel").grid(row=1, column=2, sticky="w", padx=8, pady=(6,0))

        # --- Output folder ---
        of = ttk.LabelFrame(f, text="Output folder", padding=10)
        of.grid(row=5, column=0, columnspan=3, sticky="ew", padx=12, pady=8)
        of.columnconfigure(1, weight=1)
        self.out_dir_var = tk.StringVar(value=DEFAULT_OUT_DIR)
        ttk.Entry(of, textvariable=self.out_dir_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(of, text="Output dir:").grid(row=0, column=0, sticky="w")
        ttk.Button(of, text="Browse", command=self._browse_outdir).grid(row=0, column=2)

        # --- Status panel ---
        sp = ttk.LabelFrame(f, text="Loaded data status", padding=10)
        sp.grid(row=6, column=0, columnspan=3, sticky="ew", padx=12, pady=8)
        self.status_jao = tk.StringVar(value="No JAO data loaded")
        self.status_out = tk.StringVar(value="No outage data")
        ttk.Label(sp, textvariable=self.status_jao).pack(anchor="w")
        ttk.Label(sp, textvariable=self.status_out).pack(anchor="w")

        self._on_mode_change()

    def _on_mode_change(self):
        mode = self.data_mode.get()
        for child in self.real_frame.winfo_children():
            child.configure(state="normal" if mode == "real" else "disabled")
        for child in self.syn_frame.winfo_children():
            child.configure(state="normal" if mode == "synthetic" else "disabled")

    def _browse_jao(self):
        path = filedialog.askopenfilename(
            title="Select JAO CSV", filetypes=[("CSV", "*.csv"), ("All", "*.*")])
        if path:
            self.jao_path_var.set(path)

    def _browse_outdir(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.out_dir_var.set(path)

    def _load_jao(self):
        path = self.jao_path_var.get().strip()
        if not path:
            messagebox.showinfo("Pick a file", "Select a JAO CSV first.")
            return
        try:
            df = pipe.load_jao_csv(path, log_cb=self._log)
        except Exception as e:
            messagebox.showerror("Load failed", f"{e}")
            return
        self.jao_df = df
        self.last_jao_path = path
        dt_min = df["dateTimeUtc"].min()
        dt_max = df["dateTimeUtc"].max()
        self.status_jao.set(
            f"Loaded {len(df):,} rows | {df['cneName'].nunique()} CNECs | "
            f"{pipe.utc_to_cet_str(dt_min)} → {pipe.utc_to_cet_str(dt_max)} CET")
        self._log(f"Loaded JAO CSV: {path} ({len(df)} rows)")

        # Auto-update the outage fetch window to match JAO dates (shown in CET)
        self.start_cet.set(pipe.utc_to_cet_str(dt_min, "%Y-%m-%dT%H:%M:%S"))
        self.end_cet.set(pipe.utc_to_cet_str(dt_max, "%Y-%m-%dT%H:%M:%S"))
        self._log(f"  Outage fetch window auto-set to JAO range: "
                  f"{pipe.utc_to_cet_str(dt_min, '%Y-%m-%d')} → "
                  f"{pipe.utc_to_cet_str(dt_max, '%Y-%m-%d')} CET")

    def _generate_synthetic(self):
        out_dir = self.out_dir_var.get().strip() or DEFAULT_OUT_DIR
        days = int(self.syn_days.get())
        seed = int(self.syn_seed.get())
        try:
            info = syn.generate_demo_dataset(out_dir, days=days, rng_seed=seed)
        except Exception as e:
            messagebox.showerror("Generation failed", str(e))
            return
        # auto-load
        self.jao_df = pipe.load_jao_csv(info["jao_path"], log_cb=self._log)
        self.last_jao_path = info["jao_path"]
        self.outages_df = pd.read_csv(info["outages_path"])
        self.last_outage_path = info["outages_path"]

        self.status_jao.set(
            f"Synthetic JAO: {len(self.jao_df):,} rows | "
            f"{self.jao_df['cneName'].nunique()} CNECs | "
            f"{info['start']} - {info['end']}")
        self.status_out.set(
            f"Synthetic outages: {len(self.outages_df)} events "
            f"(loaded into Outages tab)")
        self._refresh_outage_view()
        self._log(f"Synthetic data generated under {out_dir}")
        messagebox.showinfo("Generated",
                            f"Synthetic JAO + outages written to {out_dir}.\n"
                            f"You can now go to tab 3 to run the analysis.")

    # -----------------------------------------------------------------
    # Outages tab
    # -----------------------------------------------------------------
    def _build_outages_tab(self):
        f = self.tab_outages
        f.columnconfigure(0, weight=1); f.rowconfigure(3, weight=1)

        ttk.Label(f, text="Outage data sources", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        # toggles + keys
        src = ttk.LabelFrame(f, text="Sources", padding=10)
        src.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        for c in range(4): src.columnconfigure(c, weight=1)

        self.use_entsoe  = tk.BooleanVar(value=True)
        self.use_manual  = tk.BooleanVar(value=True)

        ttk.Checkbutton(src, text="ENTSO-E Transparency (A77/A78)",
                        variable=self.use_entsoe).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(src, text="Manual outage CSV",
                        variable=self.use_manual).grid(row=1, column=0, sticky="w")

        # ENTSO-E token is hard-coded in the pipeline; show masked confirmation
        token_preview = f"{pipe.ENTSOE_TOKEN[:8]}...{pipe.ENTSOE_TOKEN[-4:]}"
        entsoe_status = (f"ENTSO-E token: {token_preview}  |  "
                         + ("entsoe-py installed ✓"
                            if pipe.EntsoePandasClient is not None
                            else "entsoe-py NOT installed — run: pip install entsoe-py"))
        entsoe_style = "OK.TLabel" if pipe.EntsoePandasClient is not None else "Bad.TLabel"
        ttk.Label(src, text=entsoe_status, style=entsoe_style).grid(
            row=0, column=1, columnspan=3, sticky="w", padx=8)

        ttk.Label(src, text="Manual CSV path:").grid(row=1, column=1, sticky="e")
        self.manual_path = tk.StringVar(value="./manual_outages.csv")
        ttk.Entry(src, textvariable=self.manual_path).grid(
            row=1, column=2, sticky="ew", padx=4)
        ttk.Button(src, text="Browse",
                   command=lambda: self.manual_path.set(
                       filedialog.askopenfilename(filetypes=[("CSV","*.csv")]) or self.manual_path.get())
                   ).grid(row=1, column=3, sticky="w", padx=4)

        # date window — entered and displayed in CET/CEST; converted to UTC
        # internally right before use (see propagation.cet_input_to_utc).
        dw = ttk.LabelFrame(f, text="Window (CET) — auto-set when JAO CSV is loaded", padding=10)
        dw.grid(row=2, column=0, sticky="ew", padx=12, pady=4)
        ttk.Label(dw, text="Start:").grid(row=0, column=0)
        self.start_cet = tk.StringVar(value="2024-10-29T00:00:00")
        ttk.Entry(dw, textvariable=self.start_cet, width=24).grid(row=0, column=1, padx=4)
        ttk.Label(dw, text="End:").grid(row=0, column=2)
        self.end_cet = tk.StringVar(
            value=pipe.utc_to_cet_str(datetime.now(timezone.utc), "%Y-%m-%dT00:00:00"))
        ttk.Entry(dw, textvariable=self.end_cet, width=24).grid(row=0, column=3, padx=4)
        ttk.Button(dw, text="Fetch outages (ENTSO-E + Manual)",
                   style="Big.TButton",
                   command=self._fetch_outages).grid(row=0, column=4, padx=20)
        ttk.Button(dw, text="Upload manual CSV directly",
                   command=self._upload_manual).grid(row=0, column=5, padx=4)
        ttk.Button(dw, text="Test ENTSO-E connection",
                   command=self._test_entsoe).grid(row=1, column=4, padx=20, pady=(6,0))

        # outage table (smaller now to make room for log)
        bf = ttk.LabelFrame(f, text="Loaded outage events", padding=6)
        bf.grid(row=3, column=0, sticky="nsew", padx=12, pady=4)
        bf.columnconfigure(0, weight=1); bf.rowconfigure(0, weight=1)
        f.rowconfigure(3, weight=2)
        f.rowconfigure(4, weight=1)

        cols = ("source","planned_or_forced","asset_type","asset_name",
                "start_utc","end_utc","capacity_mw")
        self.outage_tree = ttk.Treeview(bf, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (90, 90, 90, 280, 170, 170, 100)):
            self.outage_tree.heading(c, text=c)
            self.outage_tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(bf, orient="vertical", command=self.outage_tree.yview)
        self.outage_tree.configure(yscrollcommand=sb.set)
        self.outage_tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # Log section on this tab
        lf = ttk.LabelFrame(f, text="Outage fetch log", padding=4)
        lf.grid(row=4, column=0, sticky="nsew", padx=12, pady=(4, 12))
        lf.columnconfigure(0, weight=1); lf.rowconfigure(0, weight=1)
        self.outage_log = scrolledtext.ScrolledText(
            lf, height=10, font=("Consolas", 9), background="#fbfbf8")
        self.outage_log.grid(row=0, column=0, sticky="nsew")
        ttk.Button(lf, text="Clear log",
                   command=lambda: self.outage_log.delete("1.0", "end")).grid(
            row=0, column=1, sticky="n", padx=4)
        ttk.Button(lf, text="Copy log to clipboard",
                   command=self._copy_outage_log).grid(
            row=1, column=1, sticky="n", padx=4)

    def _outage_log(self, msg: str):
        """Log to both the Outages tab and the Run tab log."""
        line = f"[{pipe.utc_to_cet_str(datetime.now(timezone.utc), '%H:%M:%S')} CET] {msg}\n"
        self.root.after(0, self._append_outage_log, line)
        self.root.after(0, self._append_log, line)

    def _append_outage_log(self, line: str):
        if hasattr(self, "outage_log"):
            self.outage_log.insert("end", line)
            self.outage_log.see("end")

    def _copy_outage_log(self):
        try:
            text = self.outage_log.get("1.0", "end")
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            messagebox.showinfo("Copied", "Outage log copied to clipboard.")
        except Exception as e:
            messagebox.showerror("Copy failed", str(e))

    def _test_entsoe(self):
        """Quick connectivity test using the hard-coded ENTSO-E token."""
        threading.Thread(target=self._test_entsoe_worker, daemon=True).start()

    def _test_entsoe_worker(self):
        log = self._outage_log
        log("=== ENTSO-E connection test ===")
        if pipe.EntsoePandasClient is None:
            log("FAIL: 'entsoe-py' library is NOT installed.")
            log("FIX:  pip install entsoe-py  then restart the dashboard.")
            return
        log("OK: entsoe-py library is available.")
        token = pipe.ENTSOE_TOKEN
        log(f"Using token: {token[:8]}...{token[-4:]}")
        try:
            client = pipe.EntsoePandasClient(api_key=token)
            log("Pinging ENTSO-E with a tiny FI A77 query (last 24h)...")
            end = pd.Timestamp.now(tz="UTC")
            start = end - pd.Timedelta(days=1)
            try:
                df = client.query_unavailability_of_production_units(
                    country_code="FI", start=start, end=end, docstatus=None)
                log(f"SUCCESS: {len(df)} rows returned. Token works.")
            except Exception as inner:
                msg = str(inner)
                if "not a zip file" in msg.lower() or "no matching data" in msg.lower():
                    log("RESULT: token works — no FI outages in the last 24h (normal).")
                    log("        Fetch outages over the full JAO window instead.")
                elif "401" in msg or "Unauthorized" in msg:
                    log("DIAGNOSIS: 401 Unauthorized — token may need activation.")
                    log("  Email transparency@entsoe.eu requesting 'Restful API access'.")
                elif "403" in msg or "Forbidden" in msg:
                    log("DIAGNOSIS: 403 Forbidden — ENTSO-E may be blocking this IP.")
                    log("  This happens from cloud/server IPs. Run from your local machine.")
                else:
                    log(f"REQUEST FAILED: {msg}")
        except Exception as e:
            log(f"CLIENT INIT FAILED: {e}")

    def _refresh_outage_view(self):
        for it in self.outage_tree.get_children():
            self.outage_tree.delete(it)
        if self.outages_df is None or self.outages_df.empty:
            return
        for _, r in self.outages_df.iterrows():
            self.outage_tree.insert(
                "", "end",
                values=(r.get("source",""), r.get("planned_or_forced",""),
                        r.get("asset_type",""), str(r.get("asset_name",""))[:60],
                        r.get("start_utc",""), r.get("end_utc",""),
                        r.get("capacity_mw","")))

    def _upload_manual(self):
        path = filedialog.askopenfilename(filetypes=[("CSV","*.csv")])
        if not path: return
        try:
            df = pd.read_csv(path)
        except Exception as e:
            messagebox.showerror("Load failed", str(e))
            return
        if "raw_payload" not in df.columns:
            df["raw_payload"] = "{}"
        # merge with existing
        if self.outages_df is not None and not self.outages_df.empty:
            self.outages_df = pd.concat([self.outages_df, df], ignore_index=True)
        else:
            self.outages_df = df
        self.outages_df = pipe.deduplicate_outages(self.outages_df)
        self.last_outage_path = path
        self.status_out.set(f"Manual + cached: {len(self.outages_df)} events")
        self._refresh_outage_view()
        self._log(f"Loaded manual outage CSV: {path}")

    def _fetch_outages(self):
        # Run in a thread so the UI stays responsive
        threading.Thread(target=self._fetch_outages_worker, daemon=True).start()

    def _fetch_outages_worker(self):
        try:
            collected = []
            log = self._outage_log

            if self.use_entsoe.get():
                cc = self.source_country.get().strip().upper() or "FI"
                start_utc = pipe.cet_input_to_utc(self.start_cet.get()).isoformat()
                end_utc   = pipe.cet_input_to_utc(self.end_cet.get()).isoformat()
                df = pipe.fetch_entsoe_outages(
                    start_utc, end_utc,
                    log_cb=log, country_code=cc)
                if not df.empty: collected.append(df)

            if self.use_manual.get():
                df = pipe.load_manual_outages(self.manual_path.get(), log_cb=log)
                if not df.empty: collected.append(df)

            if not collected:
                log("No outage events collected.")
                self.outages_df = pd.DataFrame()
            else:
                self.outages_df = pipe.deduplicate_outages(pd.concat(collected, ignore_index=True))

            self.status_out.set(f"Outages: {len(self.outages_df)} (deduped)")
            self.root.after(0, self._refresh_outage_view)
            self.root.after(0, self._populate_event_selector)
            log(f"Outage fetch complete: {len(self.outages_df)} events")
        except Exception as e:
            log("ERROR: " + str(e))
            log(traceback.format_exc())

    # -----------------------------------------------------------------
    # Run tab
    # -----------------------------------------------------------------
    def _build_run_tab(self):
        f = self.tab_run
        f.columnconfigure(0, weight=1); f.rowconfigure(2, weight=1)

        ctrl = ttk.Frame(f)
        ctrl.grid(row=0, column=0, sticky="ew", padx=12, pady=12)
        ttk.Button(ctrl, text="RUN ANALYSIS", style="Big.TButton",
                   command=self._run_pipeline).pack(side="left")
        ttk.Label(ctrl,
                  text=("This loads NO3 CNECs from the JAO data, joins outage covariates,"
                        " runs PanelOLS regressions on F0, PTDF_FI, RAM, shadowPrice, FRM,"
                        " and a logit on IVA active. Output: hypothesis verdicts + summaries."),
                  style="Hint.TLabel", wraplength=900).pack(side="left", padx=14)

        prog = ttk.Frame(f); prog.grid(row=1, column=0, sticky="ew", padx=12)
        self.progress = ttk.Progressbar(prog, mode="indeterminate", length=300)
        self.progress.pack(side="left")
        self.run_status = tk.StringVar(value="Idle")
        ttk.Label(prog, textvariable=self.run_status, style="Hint.TLabel").pack(
            side="left", padx=10)

        # log
        ttk.Label(f, text="Log").grid(row=2, column=0, sticky="nw", padx=12)
        self.log_box = scrolledtext.ScrolledText(f, height=22, font=("Consolas", 9),
                                                  background="#fbfbf8")
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0,12))

    def _log(self, msg: str):
        line = f"[{pipe.utc_to_cet_str(datetime.now(timezone.utc), '%H:%M:%S')} CET] {msg}\n"
        # threadsafe
        self.root.after(0, self._append_log, line)

    def _append_log(self, line: str):
        if not hasattr(self, "log_box"): return
        self.log_box.insert("end", line)
        self.log_box.see("end")

    def _run_pipeline(self):
        if self.jao_df is None:
            messagebox.showinfo("No JAO data",
                                "Load a JAO CSV (or generate synthetic) on the Setup tab first.")
            return
        if self.outages_df is None or self.outages_df.empty:
            if not messagebox.askyesno(
                    "No outages",
                    "No outage events loaded. Run with zero outages? "
                    "(Regressions will produce no useful results.)"):
                return

        self.run_status.set("Running...")
        self.progress.start(10)
        threading.Thread(target=self._run_pipeline_worker, daemon=True).start()

    def _run_pipeline_worker(self):
        try:
            cfg = pipe.PipelineConfig(
                jao_csv=self.last_jao_path,
                out_dir=self.out_dir_var.get(),
                manual_csv=self.manual_path.get(),
                start_utc=pipe.cet_input_to_utc(self.start_cet.get()).isoformat(),
                end_utc=pipe.cet_input_to_utc(self.end_cet.get()).isoformat(),
                use_entsoe=self.use_entsoe.get(),
                use_manual=self.use_manual.get(),
                source_country=self.source_country.get().strip().upper() or "FI",
                target_zone=self.target_zone.get().strip().upper() or "NO3",
            )
            self._log(f"Analysis scope: source={cfg.source_country}  target={cfg.target_zone}")
            res = pipe.run_pipeline(cfg, jao_df=self.jao_df,
                                    outages_df=self.outages_df,
                                    log_cb=self._log)
            self.results = res
            self.root.after(0, self._update_results_view)
            self.root.after(0, self._populate_plots_options)
            self._log("Pipeline complete.")
        except Exception as e:
            self._log("ERROR: " + str(e))
            self._log(traceback.format_exc())
        finally:
            self.root.after(0, self.progress.stop)
            self.root.after(0, lambda: self.run_status.set("Done"))

    # -----------------------------------------------------------------
    # Results tab
    # -----------------------------------------------------------------
    def _build_results_tab(self):
        f = self.tab_results
        f.columnconfigure(0, weight=1); f.rowconfigure(2, weight=1)

        ttk.Label(f, text="Hypothesis verdicts", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 4))

        cols = ("ID", "Hypothesis", "Verdict")
        self.hyp_tree = ttk.Treeview(f, columns=cols, show="headings", height=8)
        for c, w in zip(cols, (60, 520, 520)):
            self.hyp_tree.heading(c, text=c)
            self.hyp_tree.column(c, width=w, anchor="w")
        self.hyp_tree.grid(row=1, column=0, sticky="ew", padx=12)

        # sub-notebook for each regression
        sub = ttk.Notebook(f)
        sub.grid(row=2, column=0, sticky="nsew", padx=12, pady=10)
        self.res_text = {}
        for name, label in [("f0","F0"), ("ptdf_FI","PTDF_FI"),
                             ("ram","RAM"), ("shadowPrice","Shadow price"),
                             ("frm","FRM (placebo H6)"), ("logit","IVA logit (H5)")]:
            tab = ttk.Frame(sub); sub.add(tab, text=label)
            tab.columnconfigure(0, weight=1); tab.rowconfigure(0, weight=1)
            txt = scrolledtext.ScrolledText(tab, font=("Consolas", 9),
                                             background="#fafafa")
            txt.grid(row=0, column=0, sticky="nsew")
            self.res_text[name] = txt

    def _update_results_view(self):
        if not self.results: return
        self._populate_event_selector()
        self._update_explain_view()

        # Hypotheses
        for it in self.hyp_tree.get_children():
            self.hyp_tree.delete(it)
        for h in self.results.get("hypotheses", []):
            self.hyp_tree.insert("", "end",
                                  values=(h["id"], h["text"], h["verdict"]))

        # Regression summaries
        for name in ("f0","ptdf_FI","ram","shadowPrice","frm"):
            box = self.res_text[name]
            box.delete("1.0", "end")
            r = self.results["regressions"].get(name)
            if not r:
                box.insert("end", f"No result for {name}.\n")
                continue
            box.insert("end", f"Dependent: {r['dep']}\n")
            box.insert("end", f"Observations: {r['n_obs']:,} | "
                              f"Entities: {r['n_entities']} | "
                              f"R^2: {r['rsquared']:.4f} (within: {r.get('rsquared_within','n/a')})\n")
            if r.get("bp_p") is not None:
                box.insert("end",
                           f"Breusch-Pagan p={r['bp_p']:.4f} | "
                           f"Durbin-Watson={r.get('durbin_watson','n/a'):.3f}\n")
            if r.get("vif"):
                box.insert("end", "VIF: " + ", ".join(
                    f"{k}={v:.2f}" for k,v in r["vif"].items()) + "\n")
            box.insert("end", "\n--- Coefficients ---\n")
            cf = r.get("coefs")
            if cf is not None:
                box.insert("end", cf.to_string(index=False) + "\n\n")
            box.insert("end", "--- Full summary ---\n")
            box.insert("end", r.get("summary_text",""))

        # Logit
        box = self.res_text["logit"]
        box.delete("1.0", "end")
        r = self.results.get("logit", {})
        if not r:
            box.insert("end", "No logit result (insufficient IVA-active rows).\n")
        else:
            box.insert("end", f"Pseudo-R^2: {r.get('pseudo_r2','n/a'):.4f} | "
                              f"n_obs: {r.get('n_obs',0)} | "
                              f"positives: {r.get('n_positive',0)}\n\n")
            cf = r.get("coefs")
            if cf is not None:
                box.insert("end", "--- Coefficients ---\n")
                box.insert("end", cf.to_string(index=False) + "\n\n")
            box.insert("end", r.get("summary_text",""))

    # -----------------------------------------------------------------
    # Plots tab
    # -----------------------------------------------------------------
    def _build_plots_tab(self):
        f = self.tab_plots
        f.columnconfigure(0, weight=1); f.rowconfigure(2, weight=1)

        ctrl = ttk.Frame(f); ctrl.grid(row=0, column=0, sticky="ew", padx=10, pady=8)

        ttk.Label(ctrl, text="Plot:").pack(side="left")
        self.plot_kind = tk.StringVar(value="time_series")
        kind_cb = ttk.Combobox(ctrl, textvariable=self.plot_kind, state="readonly",
                                values=["time_series", "violin_before_during",
                                        "scatter_mw_vs_dF0", "decomposition_bar",
                                        "ptdf_evolution"])
        kind_cb.pack(side="left", padx=6)

        ttk.Label(ctrl, text="CNEC:").pack(side="left", padx=(20,0))
        self.plot_cnec = tk.StringVar()
        self.cnec_cb = ttk.Combobox(ctrl, textvariable=self.plot_cnec, width=50)
        self.cnec_cb.pack(side="left", padx=6)

        ttk.Label(ctrl, text="Outage event:").pack(side="left", padx=(20,0))
        self.plot_event = tk.StringVar()
        self.event_cb = ttk.Combobox(ctrl, textvariable=self.plot_event, width=50)
        self.event_cb.pack(side="left", padx=6)

        ttk.Button(ctrl, text="Render", command=self._render_plot,
                   style="Big.TButton").pack(side="left", padx=20)
        ttk.Button(ctrl, text="Save PNG", command=self._save_plot).pack(side="left")

        self.toolbar_frame = ttk.Frame(f); self.toolbar_frame.grid(row=1, column=0, sticky="ew", padx=10)
        plot_holder = ttk.Frame(f); plot_holder.grid(row=2, column=0, sticky="nsew", padx=10, pady=8)
        plot_holder.columnconfigure(0, weight=1); plot_holder.rowconfigure(0, weight=1)

        self.fig = Figure(figsize=(11, 6), dpi=100)
        self.canvas = FigureCanvasTkAgg(self.fig, plot_holder)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        for child in self.toolbar_frame.winfo_children(): child.destroy()
        NavigationToolbar2Tk(self.canvas, self.toolbar_frame).update()

    def _populate_plots_options(self):
        if not self.results: return
        no3 = self.results.get("no3")
        outages = self.results.get("outages")
        overlapping = self.results.get("overlapping_outages", pd.DataFrame())

        if no3 is not None and not no3.empty:
            cnec_list = sorted(no3["cneName"].unique().tolist())
            self.cnec_cb["values"] = cnec_list
            if cnec_list and not self.plot_cnec.get():
                self.plot_cnec.set(cnec_list[0])

        # Build event list — prefer overlapping events; show all with label
        ev_list = []
        if outages is not None and not outages.empty:
            # Sort: overlapping first, then by start date
            is_overlap = set()
            if not overlapping.empty and "outage_id" in overlapping.columns:
                is_overlap = set(overlapping["outage_id"].tolist())
            for _, r in outages.sort_values("start_utc").iterrows():
                marker = "✓ " if r.get("outage_id") in is_overlap else "✗ "
                label = (f"{marker}{r['source']} | {r['asset_type']} | "
                         f"{str(r['asset_name'])[:35]} | "
                         f"{pipe.utc_to_cet_str(r['start_utc'], '%Y-%m-%d')} → "
                         f"{pipe.utc_to_cet_str(r['end_utc'], '%Y-%m-%d')}")
                ev_list.append(label)

        self.event_cb["values"] = ev_list
        # Auto-select first overlapping event (✓) if available
        for lbl in ev_list:
            if lbl.startswith("✓"):
                self.plot_event.set(lbl)
                break
        else:
            if ev_list:
                self.plot_event.set(ev_list[0])

    def _render_plot(self):
        if not self.results:
            messagebox.showinfo("No results", "Run the analysis first.")
            return
        self.fig.clear()
        kind = self.plot_kind.get()
        try:
            if kind == "time_series":
                self._plot_time_series()
            elif kind == "violin_before_during":
                self._plot_violin()
            elif kind == "scatter_mw_vs_dF0":
                self._plot_scatter()
            elif kind == "decomposition_bar":
                self._plot_decomposition()
            elif kind == "ptdf_evolution":
                self._plot_ptdf_evolution()
        except Exception as e:
            self._log("Plot error: " + str(e))
            self._log(traceback.format_exc())
        self.fig.tight_layout()
        self.canvas.draw()

    def _selected_event_window(self):
        ev_label = self.plot_event.get()
        out = self.results.get("outages")
        if out is None or out.empty or not ev_label:
            return None, None
        # Strip the ✓/✗ prefix added in _populate_plots_options
        stripped = ev_label.lstrip("✓✗ ")
        for _, r in out.iterrows():
            label = (f"{r['source']} | {r['asset_type']} | "
                     f"{str(r['asset_name'])[:35]} | "
                     f"{pipe.utc_to_cet_str(r['start_utc'], '%Y-%m-%d')} → "
                     f"{pipe.utc_to_cet_str(r['end_utc'], '%Y-%m-%d')}")
            if label == stripped:
                return pd.Timestamp(r["start_utc"]), pd.Timestamp(r["end_utc"])
        return None, None

    def _plot_time_series(self):
        no3 = self.results["no3"]
        cnec = self.plot_cnec.get()
        sub = no3[no3["cneName"] == cnec].copy()
        if sub.empty:
            self.fig.text(0.5, 0.5, "No data for selected CNEC", ha="center")
            return

        s, e = self._selected_event_window()
        # Only filter around the event if the event actually overlaps the data
        jao_start = sub["dateTimeUtc"].min()
        jao_end   = sub["dateTimeUtc"].max()
        event_overlaps = (s is not None and e is not None
                          and s <= jao_end and e >= jao_start)

        if event_overlaps:
            buf = pd.Timedelta(hours=48)
            plot_sub = sub[(sub["dateTimeUtc"] >= s - buf) &
                           (sub["dateTimeUtc"] <= e + buf)]
            if plot_sub.empty:
                plot_sub = sub   # fallback to full
        else:
            plot_sub = sub
            if s is not None:
                # Show an annotation that the event is outside the window
                self.fig.text(0.5, 0.98,
                              f"Note: selected outage ({s.strftime('%Y-%m-%d')} → "
                              f"{e.strftime('%Y-%m-%d')}) is outside the JAO data window. "
                              f"Showing full time series.",
                              ha="center", va="top", fontsize=8, color="#a01a1a",
                              transform=self.fig.transFigure)

        panels = [("f0", "F0 / fref (MW)"), ("ptdf_FI", "PTDF_FI"),
                  ("ram", "RAM (MW)"), ("shadowPrice", "Shadow price (€/MWh)"),
                  ("iva", "IVA (MW)")]
        n = len(panels)
        for i, (col, label) in enumerate(panels):
            ax = self.fig.add_subplot(n, 1, i + 1)
            if col not in plot_sub.columns or plot_sub[col].isna().all():
                ax.text(0.5, 0.5, f"{col} not available", ha="center",
                        transform=ax.transAxes, fontsize=8)
            else:
                ax.plot(plot_sub["dateTimeUtc"], plot_sub[col],
                        lw=1.0, color="#2c3e50")
                if event_overlaps:
                    ax.axvspan(s, e, color="#e67e22", alpha=0.2, label="outage")
            ax.set_ylabel(label, fontsize=8)
            ax.grid(True, ls=":", alpha=0.4)
            if i == 0:
                ax.set_title(f"{cnec}", fontsize=9)
            if i < n - 1:
                ax.set_xticklabels([])
            else:
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
                for lbl in ax.get_xticklabels():
                    lbl.set_rotation(20); lbl.set_fontsize(7)

    def _plot_violin(self):
        no3 = self.results["no3"]
        # Use dynamic source prefix from results
        _src_v = self.results.get("source_country", self.source_country.get() or "FI").lower()
        forced_col = f"{_src_v}_forced_outage_active"
        if forced_col not in no3.columns:
            # fallback
            forced_col = next((c for c in no3.columns if c.endswith("_forced_outage_active")), None)
        if forced_col is None:
            self.fig.text(0.5, 0.5, "Run analysis first", ha="center")
            return
        ax_specs = [("f0","F0 (MW)"), ("ptdf_FI","PTDF_FI"), ("ram","RAM (MW)")]
        for i, (col, label) in enumerate(ax_specs):
            ax = self.fig.add_subplot(1, 3, i+1)
            data_no = no3.loc[no3[forced_col]==0, col].dropna().values
            data_yes = no3.loc[no3[forced_col]==1, col].dropna().values
            if len(data_no) == 0 and len(data_yes) == 0:
                ax.text(0.5, 0.5, "no data", ha="center"); continue
            data = []
            labels = []
            if len(data_no) > 0:
                data.append(data_no); labels.append("no outage")
            if len(data_yes) > 0:
                data.append(data_yes); labels.append("forced outage")
            parts = ax.violinplot(data, showmedians=True)
            for pc, color in zip(parts["bodies"], ["#3498db", "#e67e22"][:len(data)]):
                pc.set_facecolor(color); pc.set_alpha(0.6)
            ax.set_xticks(range(1, len(labels)+1))
            ax.set_xticklabels(labels, fontsize=8)
            ax.set_title(label, fontsize=9)
            ax.grid(True, ls=":", alpha=0.4)

    def _plot_scatter(self):
        no3 = self.results["no3"]
        ax = self.fig.add_subplot(111)
        _src_s = self.results.get("source_country", self.source_country.get() or "FI").lower()
        gen_col  = f"{_src_s}_gen_outage_mw_lost"
        hvdc_col = f"{_src_s}_hvdc_outage_mw_lost"
        ac_col   = f"{_src_s}_ac_outage_mw_lost"
        if gen_col not in no3.columns:
            ax.text(0.5, 0.5, "Run analysis first", ha="center"); return
        df = no3.copy()
        df["total_mw"] = (df.get(gen_col,  pd.Series(0, index=df.index)).fillna(0) +
                          df.get(hvdc_col, pd.Series(0, index=df.index)).fillna(0) +
                          df.get(ac_col,   pd.Series(0, index=df.index)).fillna(0))
        if df["total_mw"].max() < 1:
            ax.text(0.5, 0.5, "No outages active in JAO window", ha="center"); return
        if "f0" not in df.columns:
            ax.text(0.5, 0.5, "F0 missing", ha="center"); return
        df["dF0"] = df["f0"] - df.groupby("cneName")["f0"].transform("median")
        sub = df[df["total_mw"] > 0]
        cnecs = sub["cneName"].unique()
        cmap = matplotlib.colormaps["tab10"]
        for i, c in enumerate(cnecs):
            s = sub[sub["cneName"] == c]
            ax.scatter(s["total_mw"], s["dF0"], s=12, alpha=0.5,
                       color=cmap(i % 10), label=c[:40])
        _tgt_s = self.results.get("target_zone", self.target_zone.get() or "NO3")
        ax.set_xlabel(f"Total MW lost ({_src_s.upper()} outages)")
        ax.set_ylabel("ΔF0 vs CNEC median (MW)")
        ax.set_title(f"ΔF0 on {_tgt_s} CNECs vs {_src_s.upper()} outage MW lost")
        ax.grid(True, ls=":", alpha=0.4)
        ax.legend(fontsize=7, loc="best", ncol=2)

    def _plot_decomposition(self):
        no3 = self.results["no3"]
        cnec = self.plot_cnec.get()
        s, e = self._selected_event_window()
        if s is None:
            self.fig.text(0.5, 0.5, "Pick an outage event", ha="center"); return
        df = pipe.decompose_delta_ram(no3, cnec, s, e)
        if df.empty:
            self.fig.text(0.5, 0.5, "Insufficient data for decomposition", ha="center")
            return
        ax = self.fig.add_subplot(111)
        # color last two rows differently (sum & observed)
        colors = ["#2c3e50"] * (len(df) - 2) + ["#16a085", "#c0392b"]
        ax.barh(df["component"], df["MW"], color=colors)
        ax.axvline(0, color="black", lw=0.5)
        ax.set_xlabel("MW contribution to ΔRAM (during minus pre-baseline)")
        ax.set_title(f"ΔRAM decomposition: {cnec}\n{s} -> {e}", fontsize=9)
        ax.grid(True, axis="x", ls=":", alpha=0.4)

    def _plot_ptdf_evolution(self):
        no3 = self.results["no3"]
        cnec = self.plot_cnec.get()
        sub = no3[no3["cneName"] == cnec].copy()
        if sub.empty:
            self.fig.text(0.5, 0.5, "No data for selected CNEC", ha="center"); return
        s, e = self._selected_event_window()
        if s is not None:
            buf = pd.Timedelta(hours=72)
            sub = sub[(sub["dateTimeUtc"] >= s - buf) & (sub["dateTimeUtc"] <= e + buf)]

        ax = self.fig.add_subplot(111)
        for col, c, label in [("ptdf_FI", "#2c3e50", "PTDF_FI"),
                               ("ptdf_FI_FS", "#e67e22", "PTDF_FI_FS"),
                               ("ptdf_FI_EL", "#16a085", "PTDF_FI_EL")]:
            if col in sub.columns and sub[col].notna().any():
                ax.plot(sub["dateTimeUtc"], sub[col], lw=1.0, color=c, label=label)
        if s is not None:
            ax.axvspan(s, e, color="#e74c3c", alpha=0.2, label="outage")
        ax.set_title(f"PTDF evolution: {cnec}", fontsize=10)
        ax.set_ylabel("PTDF (zone to slack)")
        ax.legend(fontsize=8, loc="best")
        ax.grid(True, ls=":", alpha=0.4)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(20); lbl.set_fontsize(7)

    def _save_plot(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".png",
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf")])
        if not path: return
        self.fig.savefig(path, dpi=150, bbox_inches="tight")
        self._log(f"Saved plot to {path}")

    # -----------------------------------------------------------------
    # Export tab
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # Single event tab (tab 6)
    # -----------------------------------------------------------------
    def _build_event_tab(self):
        f = self.tab_event
        f.columnconfigure(0, weight=1)
        f.rowconfigure(3, weight=1)

        ttk.Label(f, text="Single maintenance event deep-dive",
                  style="Header.TLabel").grid(row=0, column=0, sticky="w",
                                               padx=12, pady=(10, 4))

        # Controls
        ctrl = ttk.LabelFrame(f, text="Select event", padding=10)
        ctrl.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        ctrl.columnconfigure(1, weight=1)

        ttk.Label(ctrl, text="Event:").grid(row=0, column=0, sticky="w")
        self.event_sel_var = tk.StringVar()
        self.event_sel_cb  = ttk.Combobox(ctrl, textvariable=self.event_sel_var,
                                           state="readonly", width=80)
        self.event_sel_cb.grid(row=0, column=1, sticky="ew", padx=6)

        ttk.Label(ctrl, text="Baseline (days before):").grid(row=1, column=0, sticky="w")
        self.baseline_days = tk.IntVar(value=7)
        ttk.Spinbox(ctrl, from_=2, to=30, textvariable=self.baseline_days,
                    width=6).grid(row=1, column=1, sticky="w", padx=6)

        ttk.Label(ctrl, text="Post-event (days after):").grid(row=1, column=2,
                                                               sticky="w", padx=(20,0))
        self.post_days = tk.IntVar(value=3)
        ttk.Spinbox(ctrl, from_=1, to=14, textvariable=self.post_days,
                    width=6).grid(row=1, column=3, sticky="w", padx=6)

        ttk.Button(ctrl, text="Run single-event analysis",
                   style="Big.TButton",
                   command=self._run_single_event).grid(row=0, column=4,
                                                         rowspan=2, padx=20)

        # Sub-notebook for results
        self.event_nb = ttk.Notebook(f)
        self.event_nb.grid(row=3, column=0, sticky="nsew", padx=12, pady=8)

        self._ev_summary_frame = ttk.Frame(self.event_nb)
        self._ev_its_frame     = ttk.Frame(self.event_nb)
        self._ev_decomp_frame  = ttk.Frame(self.event_nb)
        self._ev_did_frame     = ttk.Frame(self.event_nb)
        self._ev_cnec_frame    = ttk.Frame(self.event_nb)

        self.event_nb.add(self._ev_summary_frame, text="Summary")
        self.event_nb.add(self._ev_its_frame,     text="ITS — before/after trend")
        self.event_nb.add(self._ev_decomp_frame,  text="ΔRAM decomposition")
        self.event_nb.add(self._ev_did_frame,     text="DiD (high vs low PTDF_FI)")
        self.event_nb.add(self._ev_cnec_frame,    text="Per-CNEC table")

        # Summary text
        for frm in [self._ev_summary_frame, self._ev_did_frame]:
            frm.columnconfigure(0, weight=1); frm.rowconfigure(0, weight=1)
        self._ev_summary_txt = scrolledtext.ScrolledText(
            self._ev_summary_frame, font=("Consolas", 10), background="#fafafa")
        self._ev_summary_txt.grid(row=0, column=0, sticky="nsew")

        # ITS figure
        self._ev_its_frame.columnconfigure(0, weight=1)
        self._ev_its_frame.rowconfigure(1, weight=1)
        self._ev_its_toolbar = ttk.Frame(self._ev_its_frame)
        self._ev_its_toolbar.grid(row=0, column=0, sticky="ew")
        self._ev_its_fig = Figure(figsize=(10, 6), dpi=95)
        self._ev_its_canvas = FigureCanvasTkAgg(self._ev_its_fig, self._ev_its_frame)
        self._ev_its_canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew")
        NavigationToolbar2Tk(self._ev_its_canvas, self._ev_its_toolbar).update()

        # Decomp figure
        self._ev_decomp_frame.columnconfigure(0, weight=1)
        self._ev_decomp_frame.rowconfigure(1, weight=1)
        self._ev_decomp_toolbar = ttk.Frame(self._ev_decomp_frame)
        self._ev_decomp_toolbar.grid(row=0, column=0, sticky="ew")
        self._ev_decomp_fig = Figure(figsize=(10, 5), dpi=95)
        self._ev_decomp_canvas = FigureCanvasTkAgg(self._ev_decomp_fig, self._ev_decomp_frame)
        self._ev_decomp_canvas.get_tk_widget().grid(row=1, column=0, sticky="nsew")
        NavigationToolbar2Tk(self._ev_decomp_canvas, self._ev_decomp_toolbar).update()

        # DiD text
        self._ev_did_txt = scrolledtext.ScrolledText(
            self._ev_did_frame, font=("Consolas", 10), background="#fafafa")
        self._ev_did_txt.grid(row=0, column=0, sticky="nsew")
        self._ev_did_frame.columnconfigure(0, weight=1)
        self._ev_did_frame.rowconfigure(0, weight=1)

        # Per-CNEC treeview
        self._ev_cnec_frame.columnconfigure(0, weight=1)
        self._ev_cnec_frame.rowconfigure(0, weight=1)
        cnec_cols = ("cnec", "pre_f0", "during_f0", "delta_f0",
                     "pre_ram", "during_ram", "delta_ram",
                     "pre_shadowPrice", "during_shadowPrice", "delta_shadowPrice")
        self._ev_cnec_tree = ttk.Treeview(self._ev_cnec_frame,
                                           columns=cnec_cols, show="headings",
                                           height=15)
        widths = [220] + [100] * (len(cnec_cols) - 1)
        for col, w in zip(cnec_cols, widths):
            self._ev_cnec_tree.heading(col, text=col.replace("_", "\n"))
            self._ev_cnec_tree.column(col, width=w, anchor="center")
        sc = ttk.Scrollbar(self._ev_cnec_frame, orient="vertical",
                           command=self._ev_cnec_tree.yview)
        self._ev_cnec_tree.configure(yscrollcommand=sc.set)
        self._ev_cnec_tree.grid(row=0, column=0, sticky="nsew")
        sc.grid(row=0, column=1, sticky="ns")

    def _populate_event_selector(self):
        """Populate the event dropdown after results are available."""
        outages = self.results.get("outages") if self.results else self.outages_df
        if outages is None or outages.empty:
            return
        overlapping = self.results.get("overlapping_outages", pd.DataFrame()) \
            if self.results else pd.DataFrame()
        is_overlap = set()
        if not overlapping.empty and "outage_id" in overlapping.columns:
            is_overlap = set(overlapping["outage_id"].tolist())

        ev_list = []
        for _, r in outages.sort_values("start_utc").iterrows():
            mark = "✓ " if r.get("outage_id") in is_overlap else "✗ "
            lbl  = (f"{mark}{r.get('source','')} | {r.get('asset_type','')} | "
                    f"{str(r.get('asset_name',''))[:40]} | "
                    f"{pipe.utc_to_cet_str(r['start_utc'], '%Y-%m-%d')} → "
                    f"{pipe.utc_to_cet_str(r['end_utc'], '%Y-%m-%d')}")
            ev_list.append(lbl)
        self.event_sel_cb["values"] = ev_list
        # Auto-select first overlapping
        for lbl in ev_list:
            if lbl.startswith("✓"):
                self.event_sel_var.set(lbl)
                break
        else:
            if ev_list:
                self.event_sel_var.set(ev_list[0])

    def _get_selected_outage_row(self) -> pd.Series | None:
        label = self.event_sel_var.get().lstrip("✓✗ ")
        outages = self.results.get("outages") if self.results else self.outages_df
        if outages is None or outages.empty:
            return None
        for _, r in outages.iterrows():
            lbl = (f"{r.get('source','')} | {r.get('asset_type','')} | "
                   f"{str(r.get('asset_name',''))[:40]} | "
                   f"{pipe.utc_to_cet_str(r['start_utc'], '%Y-%m-%d')} → "
                   f"{pipe.utc_to_cet_str(r['end_utc'], '%Y-%m-%d')}")
            if lbl == label:
                return r
        return None

    def _run_single_event(self):
        if not self.results:
            messagebox.showinfo("Run analysis first",
                                "Complete the population analysis on Tab 3 first.")
            return
        row = self._get_selected_outage_row()
        if row is None:
            messagebox.showinfo("No event selected", "Pick an outage from the dropdown.")
            return
        threading.Thread(target=self._single_event_worker,
                         args=(row,), daemon=True).start()

    def _single_event_worker(self, outage_row):
        no3 = self.results["no3"]
        _src = (self.results.get("source_country") or self.source_country.get() or "FI").lower()
        try:
            res = pipe.single_event_analysis(
                no3, outage_row,
                baseline_days=int(self.baseline_days.get()),
                post_days=int(self.post_days.get()),
                log_cb=self._log,
                src=_src,
            )
            self.root.after(0, self._display_single_event, res, outage_row)
        except Exception as e:
            self._log(f"Single event error: {e}")
            import traceback; self._log(traceback.format_exc())

    def _display_single_event(self, res: dict, outage_row):
        try:
            self._display_single_event_inner(res, outage_row)
        except Exception as exc:
            import traceback
            self._log(f"[Tab6 display error] {exc}\n{traceback.format_exc()}")
            self._ev_summary_txt.delete("1.0", "end")
            self._ev_summary_txt.insert("end",
                f"⚠ Display error — check Log tab for details.\n\n{exc}")

    @staticmethod
    def _fv(v, width: int = 9) -> str:
        """Format a scalar that may be NaN/None — returns 'n/a' instead of 'nan'."""
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return " " * (width - 3) + "n/a"
        return f"{v:>{width}.2f}"

    def _display_single_event_inner(self, res: dict, outage_row):
        s = res["summary"]

        # ── row-count warnings ────────────────────────────────────────────
        n_pre    = s.get("n_pre_rows",    0)
        n_during = s.get("n_during_rows", 0)
        n_post   = s.get("n_post_rows",   0)
        warnings = []
        if n_pre == 0:
            warnings.append("⚠ Pre-period is EMPTY — event starts before JAO data. "
                            "Δ values cannot be computed.")
        if n_during == 0:
            warnings.append("⚠ During-period is EMPTY — event lies outside JAO window. "
                            "No comparison possible.")

        # ── Summary text ──────────────────────────────────────────────────
        self._ev_summary_txt.delete("1.0", "end")
        tgt = (self.results.get("target_zone") if self.results else None) or "NO3"
        lines = [
            f"Event:    {s['asset_name']} ({s['asset_type']}, {s['planned_or_forced']})",
            f"Window:   {pipe.utc_to_cet_str(s['start_utc'])} → "
            f"{pipe.utc_to_cet_str(s['end_utc'])} CET  "
            f"({s['duration_h']} h)",
            f"CNECs:    {s['n_cnecs']}  |  "
            f"Pre rows: {n_pre:,}  During: {n_during:,}  Post: {n_post:,}",
        ]
        if warnings:
            lines.append("")
            lines.extend(warnings)
        lines += [
            "",
            f"── Average FB parameter shift (ALL {tgt} CNECs) ──────────────────",
        ]
        for col in ["f0", "ram", "shadowPrice", "frm", "iva"]:
            pre_k  = f"pre_mean_{col}"
            dur_k  = f"during_mean_{col}"
            dlt_k  = f"delta_{col}"
            if pre_k in s:
                pre_v = s[pre_k]
                dur_v = s[dur_k]
                dlt_v = s[dlt_k]
                pre_s = self._fv(pre_v)
                dur_s = self._fv(dur_v)
                dlt_ok = (dlt_v is not None and not (isinstance(dlt_v, float)
                                                      and math.isnan(dlt_v)))
                if dlt_ok:
                    arrow = "↑" if dlt_v > 0 else "↓"
                    lines.append(
                        f"  {col:15s}: pre={pre_s}  during={dur_s}"
                        f"  Δ={dlt_v:>+9.2f} MW {arrow}")
                else:
                    reason = "(no pre-period data)" if n_pre == 0 else "(no during data)" if n_during == 0 else ""
                    lines.append(
                        f"  {col:15s}: pre={pre_s}  during={dur_s}"
                        f"  Δ=      n/a {reason}")
        if s.get("did_estimates"):
            lines += ["", "── DiD estimates (high − low PTDF_FI effect) ────────────────"]
            for col, val in s["did_estimates"].items():
                if isinstance(val, dict):
                    beta = val.get("beta", float("nan"))
                    p    = val.get("p",    float("nan"))
                    interp = val.get("interpretation", "")
                    lines.append(
                        f"  {col:15s}: β={beta:>+9.4f}  p={p:.3f}  {interp}")
                else:
                    lines.append(f"  {col:15s}: ATT = {float(val):>+9.2f} MW")

        event_study = res.get("event_study") or {}
        if event_study:
            lines += ["", "── Event study (β_k, hourly, -24h..+48h) ─────────────────────"]
            for col, es in event_study.items():
                coef_df = es.get("coefs")
                if coef_df is None or coef_df.empty:
                    continue
                pre_ok = "ok" if es.get("pre_trend_ok") else "FAILED (pre-trend not flat)"
                lines.append(f"  {col}  (n_obs={es.get('n_obs','n/a')}, pre-trend {pre_ok})")
                post = coef_df[coef_df["k"] >= 0].head(6)
                for _, row in post.iterrows():
                    sig = "*" if row["p"] < 0.05 else " "
                    lines.append(f"    k=+{int(row['k']):<3d}h  "
                                 f"β={row['beta']:>+9.2f}  p={row['p']:.3f} {sig}")
        self._ev_summary_txt.insert("end", "\n".join(lines))

        # ── ITS plot ──────────────────────────────────────────────────────
        self._ev_its_fig.clear()
        its = res.get("its", pd.DataFrame())
        if not its.empty:
            params_in_its = its["param"].unique()
            n = len(params_in_its)
            s_ts = pd.Timestamp(s["start_utc"])
            e_ts = pd.Timestamp(s["end_utc"])
            for i, param in enumerate(params_in_its):
                sub = its[its.param == param].sort_values("dateTimeUtc")
                ax  = self._ev_its_fig.add_subplot(n, 1, i + 1)
                ax.plot(sub.dateTimeUtc, sub[param], lw=1.0,
                        color="#2c3e50", label="actual")
                ax.plot(sub.dateTimeUtc, sub["projected"], lw=1.0,
                        ls="--", color="#e74c3c", label="projected trend")
                ax.axvspan(s_ts, e_ts, alpha=0.15, color="#e67e22", label="outage")
                ax.set_ylabel(param, fontsize=8)
                ax.legend(fontsize=7, loc="upper right")
                ax.grid(True, ls=":", alpha=0.4)
                if i == 0:
                    ax.set_title(f"Interrupted time series — {s['asset_name'][:50]}",
                                 fontsize=9)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H"))
                for lbl in ax.get_xticklabels():
                    lbl.set_rotation(15); lbl.set_fontsize(7)
        self._ev_its_fig.tight_layout()
        self._ev_its_canvas.draw()

        # ── Decomp plot ───────────────────────────────────────────────────
        self._ev_decomp_fig.clear()
        decomp = res.get("decomp", pd.DataFrame())
        if not decomp.empty:
            # Aggregate across CNECs: mean MW contribution per component
            agg = decomp.groupby("component")["MW"].mean().reset_index()
            agg = agg[~agg.component.isin(["Δram observed"])]
            ax  = self._ev_decomp_fig.add_subplot(111)
            colors = ["#27ae60" if v >= 0 else "#e74c3c" for v in agg.MW]
            ax.barh(agg.component, agg.MW, color=colors)
            ax.axvline(0, color="black", lw=0.8)
            ax.set_xlabel("Average MW contribution to ΔRAM across NO3 CNECs")
            ax.set_title(f"ΔRAM decomposition — {s['asset_name'][:50]}", fontsize=9)
            ax.grid(True, axis="x", ls=":", alpha=0.4)
        self._ev_decomp_fig.tight_layout()
        self._ev_decomp_canvas.draw()

        # ── DiD text ──────────────────────────────────────────────────────
        self._ev_did_txt.delete("1.0", "end")
        did = res.get("did", pd.DataFrame())
        if not did.empty:
            self._ev_did_txt.insert("end",
                "Difference-in-Differences: CNECs with high |PTDF_FI| (treatment)\n"
                "vs CNECs with low |PTDF_FI| (control).\n"
                "ATT = (high_during − high_pre) − (low_during − low_pre)\n"
                "Positive ATT means the outage loaded the high-PTDF CNECs more.\n\n")
            pivot = did.pivot_table(index=["group","param"],
                                    columns="period", values="mean")
            self._ev_did_txt.insert("end", pivot.to_string())
            if s.get("did_estimates"):
                self._ev_did_txt.insert("end", "\n\n── ATT estimates ──\n")
                for col, val in s["did_estimates"].items():
                    if isinstance(val, dict):
                        beta   = val.get("beta", float("nan"))
                        p      = val.get("p",    float("nan"))
                        interp = val.get("interpretation", "")
                        self._ev_did_txt.insert("end",
                            f"  {col}: β={beta:+.4f}  p={p:.3f}  {interp}\n")
                    else:
                        self._ev_did_txt.insert("end",
                            f"  {col}: ATT={float(val):+.2f} MW\n")

        # ── Per-CNEC table ────────────────────────────────────────────────
        for it in self._ev_cnec_tree.get_children():
            self._ev_cnec_tree.delete(it)
        cnec_table = res.get("cnec_table", pd.DataFrame())
        if not cnec_table.empty:
            for _, row in cnec_table.iterrows():
                def fmt(v):
                    try: return f"{float(v):+.1f}"
                    except: return str(v)
                self._ev_cnec_tree.insert("", "end", values=(
                    str(row.get("cnec",""))[:35],
                    fmt(row.get("pre_f0",   "")), fmt(row.get("during_f0",   "")),
                    fmt(row.get("delta_f0", "")),
                    fmt(row.get("pre_ram",  "")), fmt(row.get("during_ram",  "")),
                    fmt(row.get("delta_ram","")),
                    fmt(row.get("pre_shadowPrice",  "")),
                    fmt(row.get("during_shadowPrice","")),
                    fmt(row.get("delta_shadowPrice", "")),
                ))

        # Switch to this tab
        self.nb.select(self.tab_event)
        self._log(f"Single event analysis complete: {s['asset_name']}")

    # -----------------------------------------------------------------
    # Explain tab
    # -----------------------------------------------------------------
    def _build_explain_tab(self):
        f = self.tab_explain
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        ttk.Label(f, text="Plain-language explainability report",
                  style="Header.TLabel").grid(row=0, column=0,
                  sticky="w", padx=12, pady=(10, 4))

        self.explain_text = scrolledtext.ScrolledText(
            f, font=("Helvetica", 10), background="#fefefe",
            wrap="word", state="disabled")
        self.explain_text.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))

        # colour tags
        t = self.explain_text
        t.tag_configure("title",     font=("Helvetica", 13, "bold"),
                        foreground="#1a1a2e")
        t.tag_configure("section",   font=("Helvetica", 11, "bold"),
                        foreground="#16213e", spacing1=12)
        t.tag_configure("confirmed", font=("Helvetica", 10, "bold"),
                        foreground="#27ae60")
        t.tag_configure("indicative",font=("Helvetica", 10, "bold"),
                        foreground="#d4a017")
        t.tag_configure("no_detect", font=("Helvetica", 10, "bold"),
                        foreground="#c0392b")
        t.tag_configure("nodata",    font=("Helvetica", 10, "bold"),
                        foreground="#888")
        t.tag_configure("label",     font=("Helvetica", 10, "bold"))
        t.tag_configure("body",      font=("Helvetica", 10))
        t.tag_configure("muted",     font=("Helvetica", 9, "italic"),
                        foreground="#666")
        t.tag_configure("warn",      font=("Helvetica", 9, "italic"),
                        foreground="#c0392b")
        t.tag_configure("rule",      font=("Helvetica", 8),
                        foreground="#ccc")

    def _update_explain_view(self):
        if not self.results:
            return
        res   = self.results
        regs  = res.get("regressions", {})
        hyps  = {h["id"]: h for h in res.get("hypotheses", [])}
        no3   = res.get("no3")
        logit = res.get("logit", {})
        t     = self.explain_text

        # Dynamic column-name prefix from source country
        _src = res.get("source_country", self.source_country.get() or "FI").lower()
        _tgt = res.get("target_zone",    self.target_zone.get()    or "NO3")
        _SRC = _src.upper()

        # ── helpers ─────────────────────────────────────────────────────────
        def _coef(reg_name, param):
            """Return (beta, p) or (None, None)."""
            r = regs.get(reg_name)
            if not r or r.get("coefs") is None:
                return None, None
            cf = r["coefs"]
            row = cf[cf.param == param]
            if row.empty:
                return None, None
            return float(row.coef.iloc[0]), float(row.p.iloc[0])

        def _pct(val, ref):
            if ref and ref != 0:
                return f"{abs(val/ref)*100:.1f}%"
            return "n/a"

        def _tier(beta, p):
            """Return (icon, tag, label) confidence tier."""
            if beta is None:
                return "—", "nodata", "No data"
            if p < 0.05:
                return "✅", "confirmed", "Confirmed (p<0.05)"
            if p < 0.10:
                return "⚠", "indicative", "Indicative (p<0.10)"
            return "✗", "no_detect", "Not detected (p≥0.10)"

        def w(text, tag="body"):
            t.insert("end", text, tag)

        def nl(n=1):
            t.insert("end", "\n" * n)

        def rule():
            t.insert("end", "─" * 80 + "\n", "rule")

        # ── reference values for magnitude context ───────────────────────────
        avg_ram  = float(no3["ram"].mean())  if no3 is not None and "ram"  in no3.columns else None
        avg_fall = float(no3["fall"].abs().mean()) if no3 is not None and "fall" in no3.columns else None
        avg_sp   = float(no3["shadowPrice"][no3["shadowPrice"] > 0].mean()) \
                   if no3 is not None and "shadowPrice" in no3.columns \
                   and (no3["shadowPrice"] > 0).any() else None
        n_cnecs  = no3["cneName"].nunique() if no3 is not None else 0
        n_obs    = len(no3) if no3 is not None else 0

        # ── extract key coefficients (dynamic: use _src prefix) ─────────────
        b_hvdc_fall, p_hvdc_fall = _coef("fall_signed", f"{_src}_hvdc_outage_active")
        b_ac_fall,   p_ac_fall   = _coef("fall_signed", f"{_src}_ac_line_outage_active")
        b_gen_fall,  p_gen_fall  = _coef("fall_signed", f"{_src}_gen_outage_mw_lost")

        b_hvdc_ptdf, p_hvdc_ptdf = _coef("ptdf_FI_abs", f"{_src}_hvdc_outage_active")
        b_ac_ptdf,   p_ac_ptdf   = _coef("ptdf_FI_abs", f"{_src}_ac_line_outage_active")

        b_hvdc_ram,  p_hvdc_ram  = _coef("ram", f"{_src}_hvdc_outage_active")
        b_ac_ram,    p_ac_ram    = _coef("ram", f"{_src}_ac_line_outage_active")
        b_gen_ram,   p_gen_ram   = _coef("ram", f"{_src}_gen_outage_mw_lost")

        b_hvdc_sp,   p_hvdc_sp   = _coef("shadowPrice", f"{_src}_hvdc_outage_active")
        b_forced_sp, p_forced_sp = _coef("shadowPrice", f"{_src}_forced_outage_active")

        b_frm_hvdc,  p_frm_hvdc  = _coef("frm", f"{_src}_hvdc_outage_active")
        b_frm_ac,    p_frm_ac    = _coef("frm", f"{_src}_ac_line_outage_active")

        # ── count supported hypotheses for headline ───────────────────────────
        supported = sum(
            1 for h in hyps.values()
            if "SUPPORTED" in h["verdict"] or "CONSISTENT" in h["verdict"])
        total_testable = sum(
            1 for h in hyps.values()
            if "n/a" not in h["verdict"] and "not run" not in h["verdict"]
            and "did not converge" not in h["verdict"])

        # ════════════════════════════════════════════════════════════════════
        t.config(state="normal")
        t.delete("1.0", "end")

        # ── TITLE ────────────────────────────────────────────────────────────
        w(f"{_SRC} → {_tgt} Propagation  —  Plain-language findings\n", "title"); nl()
        w(f"Dataset: {n_obs:,} MTU-CNEC rows  |  {n_cnecs} {_tgt} CNECs  |  "
          f"Avg RAM {avg_ram:.0f} MW  |  "
          f"Avg |fall| {avg_fall:.0f} MW\n" if avg_ram and avg_fall else
          f"Dataset: {n_obs:,} MTU-CNEC rows  |  {n_cnecs} {_tgt} CNECs\n", "muted")
        nl()

        # ── OVERALL FINDING ───────────────────────────────────────────────────
        rule()
        w("OVERALL FINDING\n", "section")
        rule()
        if total_testable == 0:
            w("Regressions could not run — linearmodels/statsmodels missing "
              "or no outage events overlap the JAO window.\n", "warn")
        else:
            # Build a one-paragraph narrative
            lines = []
            if b_hvdc_fall is not None and p_hvdc_fall < 0.10:
                direction = "increases" if b_hvdc_fall > 0 else "decreases"
                lines.append(
                    f"{_SRC} HVDC outages {direction} reference flow on {_tgt} CNECs "
                    f"by {abs(b_hvdc_fall):.1f} MW on average "
                    f"({_pct(b_hvdc_fall, avg_fall)} of typical loading)")
            if b_ac_ptdf is not None and p_ac_ptdf < 0.10:
                lines.append(
                    f"{_SRC} AC line outages shift {_tgt} PTDF sensitivities "
                    f"by {abs(b_ac_ptdf):.4f} p.u. — the topology "
                    f"change redistributes AC flows through the {_tgt} corridor")
            if b_hvdc_ram is not None and p_hvdc_ram < 0.10:
                direction = "reduces" if b_hvdc_ram < 0 else "increases"
                lines.append(
                    f"Available margin (RAM) on {_tgt} {direction} by "
                    f"{abs(b_hvdc_ram):.1f} MW during {_SRC} HVDC outages "
                    f"({_pct(b_hvdc_ram, avg_ram)} of avg RAM)")
            if lines:
                w(f"{supported} of {total_testable} testable hypotheses are supported. ", "body")
                w("  ".join(lines) + ".\n", "body")
            else:
                w(f"{supported} of {total_testable} testable hypotheses supported. "
                  f"No propagation channel reached statistical significance at p<0.10 "
                  f"— either the effect is too small for the available sample, or {_SRC} "
                  f"outages do not systematically load the {_tgt} corridor in this window.\n",
                  "body")
        nl()

        # ── SCORECARD ─────────────────────────────────────────────────────────
        rule()
        w("SCORECARD\n", "section")
        rule()
        scorecard = [
            ("H1", f"{_SRC} HVDC outage → F_allReference shift",
             b_hvdc_fall, p_hvdc_fall),
            ("H2", f"{_SRC} AC line outage → |PTDF_{_SRC}| shift",
             b_ac_ptdf,   p_ac_ptdf),
            ("H3", f"{_SRC} HVDC outage → RAM change",
             b_hvdc_ram,  p_hvdc_ram),
            ("H4", f"{_SRC} forced outage → shadow price change",
             b_forced_sp, p_forced_sp),
            ("H5", f"{_SRC} forced > planned → IVA trigger (logit)",
             None, None),   # handled separately
            ("H6", "FRM unchanged [placebo]",
             b_frm_hvdc,  p_frm_hvdc),
        ]
        for hid, desc, beta, p in scorecard:
            if hid == "H5":
                if logit:
                    icon, tag, lbl = "✅", "confirmed", "Logit converged"
                else:
                    icon, tag, lbl = "—", "nodata", "No data (insufficient IVA rows)"
            elif hid == "H6":
                # Placebo: NOT significant is the GOOD result
                if beta is None:
                    icon, tag, lbl = "—", "nodata", "No data"
                elif p >= 0.10:
                    icon, tag, lbl = "✅", "confirmed", "Placebo clean (p≥0.10) ✓"
                else:
                    icon, tag, lbl = "⚠", "warn", f"WARNING — FRM moves! (p={p:.3f})"
            else:
                icon, tag, lbl = _tier(beta, p)
            w(f"  {icon}  {hid}  {desc:<45s}  {lbl}\n", tag)
        nl()

        # ── PER-HYPOTHESIS DEEP DIVES ─────────────────────────────────────────
        # ── H1 ───────────────────────────────────────────────────────────────
        rule()
        w(f"H1  —  Does a {_SRC} HVDC outage shift F_allReference on {_tgt} CNECs?\n", "section")
        rule()
        icon, tag, lbl = _tier(b_hvdc_fall, p_hvdc_fall)
        w(f"  Verdict: {icon} {lbl}\n", tag); nl()
        w("  Physical mechanism\n", "label")
        w(f"  When a {_SRC} HVDC link trips, the AC network must absorb the\n"
          f"  power imbalance. Depending on the {_SRC} net position at the time:\n"
          f"  • {_SRC} was net importer → sudden loss of import → {_SRC} price spikes,\n"
          f"    AC flows reroute → {_tgt} corridor may load up.\n"
          f"  • {_SRC} was net exporter → export disrupted → less AC transit\n"
          f"    through {_tgt} → corridor relieves.\n"
          f"  The sign of β tells you which regime dominated in your dataset.\n\n",
          "body")
        w("  What the numbers say\n", "label")
        for param, label, beta, p in [
            (f"{_src}_hvdc_outage_active",   "HVDC outage (binary)",  b_hvdc_fall, p_hvdc_fall),
            (f"{_src}_ac_line_outage_active","AC line outage (binary)",b_ac_fall,   p_ac_fall),
            (f"{_src}_gen_outage_mw_lost",   "Generator MW lost",     b_gen_fall,  p_gen_fall),
        ]:
            if beta is None:
                w(f"    {label:35s}  —  no result\n", "muted")
            else:
                icon2, tag2, _ = _tier(beta, p)
                ctx = f"  ({_pct(beta, avg_fall)} of avg |fall|)" if avg_fall else ""
                w(f"    {label:35s}  β={beta:+.2f} MW  p={p:.3f}  {icon2}{ctx}\n", tag2)
        nl()
        w("  Operational implication\n", "label")
        if b_hvdc_fall is not None and p_hvdc_fall < 0.10:
            direction = "tightens" if b_hvdc_fall > 0 else "relieves"
            w(f"  The {_tgt} corridor {direction} during {_SRC} HVDC outages. "
              f"TSOs should pre-check {_tgt} CNEC headroom before approving "
              f"maintenance schedules that coincide with high {_SRC} net import.\n\n", "body")
        else:
            w(f"  No statistically detectable systematic loading of the {_tgt} corridor "
              f"was found for {_SRC} HVDC outages in this dataset window.\n\n", "body")

        # ── H2 ───────────────────────────────────────────────────────────────
        rule()
        w(f"H2  —  Does a {_SRC} AC line outage shift |PTDF_{_SRC}| on {_tgt} CNECs?\n", "section")
        rule()
        icon, tag, lbl = _tier(b_ac_ptdf, p_ac_ptdf)
        w(f"  Verdict: {icon} {lbl}\n", tag); nl()
        w("  Physical mechanism\n", "label")
        w(f"  A {_SRC} internal transmission line outage changes the network\n"
          f"  impedance matrix. ENTSO-E recomputes PTDFs for the next MTU.\n"
          f"  If the lost line carried significant transit, {_tgt} CNECs may\n"
          f"  become more sensitive (larger |PTDF_{_SRC}|) to {_SRC} injections\n"
          f"  — meaning future {_SRC} outages will have greater {_tgt} impact.\n\n", "body")
        w("  What the numbers say\n", "label")
        for param, label, beta, p in [
            (f"{_src}_ac_line_outage_active","AC line outage (binary)", b_ac_ptdf,  p_ac_ptdf),
            (f"{_src}_hvdc_outage_active",   "HVDC outage (binary)",   b_hvdc_ptdf, p_hvdc_ptdf),
        ]:
            if beta is None:
                w(f"    {label:35s}  —  no result\n", "muted")
            else:
                icon2, tag2, _ = _tier(beta, p)
                w(f"    {label:35s}  β={beta:+.4f} p.u.  p={p:.3f}  {icon2}\n", tag2)
        nl()
        w("  Operational implication\n", "label")
        if b_ac_ptdf is not None and p_ac_ptdf < 0.10:
            w(f"  {_SRC} AC line outages measurably change PTDF_{_SRC} on {_tgt} CNECs. "
              f"This means the sensitivity of {_tgt} capacity to {_SRC} scheduling "
              f"changes with {_SRC} topology — relevant for the FB domain computation.\n\n", "body")
        else:
            w(f"  No significant PTDF shift detected. {_SRC} AC outages in this sample "
              f"did not systematically alter {_tgt} sensitivity to {_SRC} injections.\n\n", "body")

        # ── H3 ───────────────────────────────────────────────────────────────
        rule()
        w(f"H3  —  Does a {_SRC} outage change Available Margin (RAM) on {_tgt} CNECs?\n", "section")
        rule()
        icon, tag, lbl = _tier(b_hvdc_ram, p_hvdc_ram)
        w(f"  Verdict: {icon} {lbl}\n", tag); nl()
        w("  Physical mechanism\n", "label")
        w(f"  RAM = Fmax − FRM − fall + fnrao + AMR − FAAC − IVA\n"
          f"  A {_SRC} outage that loads the corridor (fall↑) directly reduces RAM.\n"
          f"  A {_SRC} outage that relieves the corridor (fall↓) increases RAM.\n"
          f"  Forced outages may also trigger IVA adjustments (see H5).\n\n", "body")
        w("  What the numbers say\n", "label")
        for param, label, beta, p in [
            (f"{_src}_hvdc_outage_active",   "HVDC outage",       b_hvdc_ram, p_hvdc_ram),
            (f"{_src}_ac_line_outage_active","AC line outage",     b_ac_ram,   p_ac_ram),
            (f"{_src}_gen_outage_mw_lost",   "Generator MW lost", b_gen_ram,  p_gen_ram),
        ]:
            if beta is None:
                w(f"    {label:35s}  —  no result\n", "muted")
            else:
                icon2, tag2, _ = _tier(beta, p)
                ctx = f"  ({_pct(beta, avg_ram)} of avg RAM)" if avg_ram else ""
                w(f"    {label:35s}  β={beta:+.2f} MW  p={p:.3f}  {icon2}{ctx}\n", tag2)
        nl()
        w("  Operational implication\n", "label")
        if b_hvdc_ram is not None and p_hvdc_ram < 0.10:
            if b_hvdc_ram < 0:
                w(f"  RAM shrinks by {abs(b_hvdc_ram):.1f} MW ({_pct(b_hvdc_ram, avg_ram)} "
                  f"of avg) during {_SRC} HVDC outages. If RAM was already near zero, the CNEC\n"
                  f"  becomes binding, restricting cross-border capacity and raising prices.\n\n",
                  "body")
            else:
                w(f"  RAM grows by {b_hvdc_ram:.1f} MW during {_SRC} HVDC outages — "
                  f"the outage relieves the corridor, potentially un-binding CNECs.\n\n", "body")
        else:
            w(f"  No significant RAM change detected for {_SRC} HVDC outages.\n\n", "body")

        # ── H4 ───────────────────────────────────────────────────────────────
        rule()
        w(f"H4  —  Does a {_SRC} outage change shadow prices on binding {_tgt} CNECs?\n", "section")
        rule()
        icon, tag, lbl = _tier(b_forced_sp, p_forced_sp)
        w(f"  Verdict: {icon} {lbl}\n", tag); nl()
        w("  Physical mechanism\n", "label")
        sp_pct = no3["shadowPrice"][no3["shadowPrice"] > 0].count() / len(no3) * 100 \
                 if no3 is not None and "shadowPrice" in no3.columns else 0
        w(f"  Only binding MTUs (shadow price > 0, {sp_pct:.1f}% of dataset) are\n"
          f"  included in this regression. Shadow price measures how much the\n"
          f"  market-clearing price would fall if the CNEC limit were relaxed by 1 MW.\n"
          f"  A positive β means FI outages worsen existing congestion.\n"
          f"  A negative β means they relieve it (or push the CNEC toward non-binding).\n\n",
          "body")
        w("  What the numbers say\n", "label")
        if b_forced_sp is None:
            w("    Regression not run or did not converge.\n", "muted")
        else:
            icon2, tag2, _ = _tier(b_forced_sp, p_forced_sp)
            ctx = f"  (avg binding SP = {avg_sp:.2f} €/MWh)" if avg_sp else ""
            w(f"    Forced outage (binary)              β={b_forced_sp:+.3f}  "
              f"p={p_forced_sp:.3f}  {icon2}{ctx}\n", tag2)
        nl()

        # ── H5 ───────────────────────────────────────────────────────────────
        rule()
        w(f"H5  —  Are forced {_SRC} outages more likely to trigger IVA on {_tgt} CNECs?\n", "section")
        rule()
        if not logit:
            w("  Verdict: — No data\n", "nodata"); nl()
            w("  IVA (Individual Validation Adjustment) is applied by TSOs when\n"
              "  the CGMA model does not reflect a topology change. It requires\n"
              "  IVA-active rows in the NO3 dataset — there were none in this window.\n"
              "  Use a longer JAO CSV or include a window with a known forced outage.\n\n",
              "body")
        else:
            w(f"  Verdict: ✅ Logit converged  |  pseudo-R²={logit.get('pseudo_r2',0):.3f}  "
              f"|  n_positive={logit.get('n_positive',0)}\n", "confirmed"); nl()
            w("  Physical mechanism\n", "label")
            w("  Forced outages are not in the IGCC / individual grid model (IGM) used\n"
              "  to compute PTDFs. TSOs apply IVA to correct the domain when an unplanned\n"
              "  topology is detected. The logit tests whether forced outages predict IVA.\n\n",
              "body")

        # ── H6 ───────────────────────────────────────────────────────────────
        rule()
        w(f"H6  —  PLACEBO: Does FRM move with {_SRC} outages? (it should NOT)\n", "section")
        rule()
        if b_frm_hvdc is None:
            w("  Verdict: — No data\n", "nodata"); nl()
            w("  FRM is calibrated annually and should be flat within any single\n"
              "  year. No result here means the regression did not run.\n\n", "body")
        elif p_frm_hvdc >= 0.10:
            w(f"  Verdict: ✅ Placebo clean  —  β={b_frm_hvdc:+.3f} MW, p={p_frm_hvdc:.3f}\n",
              "confirmed"); nl()
            w("  FRM did not move with FI outage covariates. This is the expected\n"
              "  result and confirms the model is well-specified. FRM is a structural\n"
              "  margin set once per year — it should not respond to individual events.\n\n",
              "body")
        else:
            w(f"  Verdict: ⚠ WARNING — FRM IS MOVING  β={b_frm_hvdc:+.3f} MW, "
              f"p={p_frm_hvdc:.3f}\n", "warn"); nl()
            w("  FRM is correlated with FI outage covariates. This is a model\n"
              "  misspecification flag — FRM should be structural. Possible causes:\n"
              "  • The JAO window spans a December FRM recalibration (regime break)\n"
              "  • Seasonal patterns in FRM are aliased with outage seasonality\n"
              "  • Data quality issue in the JAO export\n"
              "  Treat H1–H4 results with caution until this is resolved.\n\n", "warn")

        # ── DATA QUALITY NOTES ────────────────────────────────────────────────
        rule()
        w("DATA QUALITY & METHODOLOGY NOTES\n", "section")
        rule()
        w("  Selection bias (H4):  ", "label")
        w("Only binding MTUs (shadow price > 0) are included in the shadow\n"
          "  price regression. Coefficients describe the effect conditional on\n"
          "  the CNEC already being congested — not the general population effect.\n\n",
          "body")
        w("  FWER correction:  ", "label")
        w("H1–H4 are tested as a family. Holm–Bonferroni correction is applied;\n"
          "  results marked 'Confirmed' have survived family-wise error rate control.\n\n",
          "body")
        w("  Time clustering:  ", "label")
        w("Standard errors are clustered by calendar date to account for the\n"
          "  fact that a single FI outage hits all NO3 CNECs simultaneously.\n\n",
          "body")
        w("  Sign normalisation (H1):  ", "label")
        w("fall_signed = σᵢ × fall, where σᵢ = sign(median fall in pre-period).\n"
          "  This removes CNEC orientation artifacts — a positive β always means\n"
          "  'the outage loads the CNE in its congested direction'.\n\n",
          "body")

        t.config(state="disabled")

    # -----------------------------------------------------------------
    # Export tab
    # -----------------------------------------------------------------
    def _build_export_tab(self):
        f = self.tab_export
        for c in range(2): f.columnconfigure(c, weight=1)

        ttk.Label(f, text="Export results", style="Header.TLabel").grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 4))

        b1 = ttk.Button(f, text="Export NO3 + covariates CSV",
                        command=self._export_no3_csv)
        b1.grid(row=1, column=0, sticky="ew", padx=12, pady=4)

        b2 = ttk.Button(f, text="Export outage events CSV",
                        command=self._export_outages_csv)
        b2.grid(row=1, column=1, sticky="ew", padx=12, pady=4)

        b3 = ttk.Button(f, text="Export regression coefficients (all) CSV",
                        command=self._export_coefs_csv)
        b3.grid(row=2, column=0, sticky="ew", padx=12, pady=4)

        b4 = ttk.Button(f, text="Export hypothesis verdicts CSV",
                        command=self._export_hypotheses_csv)
        b4.grid(row=2, column=1, sticky="ew", padx=12, pady=4)

        b5 = ttk.Button(f, text="Generate HTML report (with embedded plots)",
                        command=self._export_html_report,
                        style="Big.TButton")
        b5.grid(row=3, column=0, columnspan=2, sticky="ew", padx=12, pady=12)

        ttk.Label(f, text="All exports go to the output folder set on the Setup tab.",
                  style="Hint.TLabel").grid(row=4, column=0, columnspan=2,
                                              sticky="w", padx=12)

    def _check_results(self) -> bool:
        if not self.results:
            messagebox.showinfo("No results", "Run the analysis first.")
            return False
        return True

    def _export_no3_csv(self):
        if not self._check_results(): return
        out = Path(self.out_dir_var.get()); out.mkdir(parents=True, exist_ok=True)
        path = out / "no3_with_covariates.csv"
        self.results["no3"].to_csv(path, index=False)
        self._log(f"Exported NO3 covariates CSV: {path}")
        messagebox.showinfo("Saved", str(path))

    def _export_outages_csv(self):
        if not self._check_results(): return
        out = Path(self.out_dir_var.get()); out.mkdir(parents=True, exist_ok=True)
        path = out / "outages_unified.csv"
        self.results["outages"].to_csv(path, index=False)
        self._log(f"Exported outages CSV: {path}")
        messagebox.showinfo("Saved", str(path))

    def _export_coefs_csv(self):
        if not self._check_results(): return
        out = Path(self.out_dir_var.get()); out.mkdir(parents=True, exist_ok=True)
        rows = []
        for name, r in self.results["regressions"].items():
            if not r: continue
            cf = r.get("coefs")
            if cf is None: continue
            tmp = cf.copy()
            tmp.insert(0, "regression", name)
            rows.append(tmp)
        if self.results.get("logit", {}).get("coefs") is not None:
            tmp = self.results["logit"]["coefs"].copy()
            tmp.insert(0, "regression", "logit_iva")
            rows.append(tmp)
        if not rows:
            messagebox.showinfo("Nothing to export", "No coefficients available.")
            return
        df = pd.concat(rows, ignore_index=True)
        path = out / "all_coefficients.csv"
        df.to_csv(path, index=False)
        self._log(f"Exported coefficients CSV: {path}")
        messagebox.showinfo("Saved", str(path))

    def _export_hypotheses_csv(self):
        if not self._check_results(): return
        out = Path(self.out_dir_var.get()); out.mkdir(parents=True, exist_ok=True)
        path = out / "hypothesis_verdicts.csv"
        pd.DataFrame(self.results["hypotheses"]).to_csv(path, index=False)
        self._log(f"Exported hypothesis verdicts: {path}")
        messagebox.showinfo("Saved", str(path))

    def _export_html_report(self):
        if not self._check_results(): return
        out = Path(self.out_dir_var.get()); out.mkdir(parents=True, exist_ok=True)

        # Generate plot images
        figures = []
        no3 = self.results["no3"]
        outages = self.results.get("outages")
        cnecs = sorted(no3["cneName"].unique().tolist())[:3]

        # 1. Time series for top CNECs around the largest outage
        if outages is not None and not outages.empty:
            ev = outages.iloc[0]
            s, e = pd.Timestamp(ev["start_utc"]), pd.Timestamp(ev["end_utc"])
            for cnec in cnecs:
                fig = Figure(figsize=(10, 11), dpi=110)
                buf = pd.Timedelta(hours=48)
                sub = no3[(no3["cneName"]==cnec) &
                           (no3["dateTimeUtc"]>=s-buf) &
                           (no3["dateTimeUtc"]<=e+buf)]
                if sub.empty: continue
                panels = [("f0", "F0"), ("ptdf_FI","PTDF_FI"),
                          ("ram","RAM"), ("shadowPrice","Shadow"), ("iva","IVA")]
                for i, (col, label) in enumerate(panels):
                    ax = fig.add_subplot(len(panels), 1, i+1)
                    if col in sub.columns:
                        ax.plot(sub["dateTimeUtc"], sub[col], lw=0.9, color="#2c3e50")
                    ax.axvspan(s, e, color="#e67e22", alpha=0.2)
                    ax.set_ylabel(label, fontsize=8)
                    ax.grid(True, ls=":", alpha=0.4)
                    if i == 0: ax.set_title(cnec[:60], fontsize=9)
                fig.tight_layout()
                fp = out / f"ts_{abs(hash(cnec)) % 10**6}.png"
                fig.savefig(fp, dpi=110, bbox_inches="tight")
                figures.append(fp.name)

        # 2. Violin
        try:
            fig = Figure(figsize=(10, 4), dpi=110)
            for i, (col, label) in enumerate([("f0","F0"),("ptdf_FI","PTDF_FI"),("ram","RAM")]):
                ax = fig.add_subplot(1,3,i+1)
                no = no3.loc[no3.get("fi_forced_outage_active",0)==0, col].dropna().values
                yes = no3.loc[no3.get("fi_forced_outage_active",0)==1, col].dropna().values
                data, lab = [], []
                if len(no)>0: data.append(no); lab.append("no outage")
                if len(yes)>0: data.append(yes); lab.append("forced FI")
                if data:
                    parts = ax.violinplot(data, showmedians=True)
                    for pc, c in zip(parts["bodies"], ["#3498db","#e67e22"][:len(data)]):
                        pc.set_facecolor(c); pc.set_alpha(0.6)
                    ax.set_xticks(range(1, len(lab)+1))
                    ax.set_xticklabels(lab, fontsize=8)
                ax.set_title(label, fontsize=9)
                ax.grid(True, ls=":", alpha=0.4)
            fig.tight_layout()
            fp = out / "violin.png"
            fig.savefig(fp, dpi=110, bbox_inches="tight")
            figures.append(fp.name)
        except Exception as ex:
            self._log(f"violin export err: {ex}")

        # Build context
        ctx = {
            "ts": pipe.utc_to_cet_str(datetime.now(timezone.utc), "%Y-%m-%d %H:%M:%S") + " CET",
            "n_jao": len(self.jao_df) if self.jao_df is not None else 0,
            "n_no3": len(no3),
            "n_outages": len(outages) if outages is not None else 0,
            "hypotheses": self.results["hypotheses"],
            "summary_f0":   self._build_reg_block("fall_signed"),
            "summary_ptdf_FI": self._build_reg_block("ptdf_FI"),
            "summary_ram":  self._build_reg_block("ram"),
            "summary_shadowPrice": self._build_reg_block("shadowPrice"),
            "summary_frm":  self._build_reg_block("frm"),
            "summary_logit": self._build_logit_block(),
            "figures": figures,
            "caveats": (
                "Statistical power is heterogeneous across hypotheses: H1/H3 "
                "have many treatment hours; H5 only a handful. Confounders "
                "include Statnett's December 2024 FRM revision, the NO3-NO5 "
                "Aurland reinforcement (early 2026), and seasonal Fmax derating "
                "on Fenno-Skan. ENTSO-E A78 returns mostly forced events; "
                "planned FI line outages are best curated manually. "
                "Use the placebo on FRM (H6) as a sanity check; if FRM moves "
                "with outage covariates, your model is mis-specified."
            ),
        }
        path = pipe.render_html_report(self.out_dir_var.get(), ctx)
        self._log(f"HTML report: {path}")
        messagebox.showinfo("Saved", path)

    def _build_reg_block(self, name: str) -> str:
        r = self.results["regressions"].get(name)
        if not r: return f"No result for {name}.\n"
        lines = [
            f"Dependent: {r['dep']}",
            f"Observations: {r['n_obs']:,} | Entities: {r['n_entities']} | "
            f"R^2: {r['rsquared']:.4f}",
        ]
        if r.get("bp_p") is not None:
            lines.append(f"BP p={r['bp_p']:.4f} | DW={r.get('durbin_watson','n/a'):.3f}")
        cf = r.get("coefs")
        if cf is not None:
            lines.append("\nCoefficients:")
            lines.append(cf.to_string(index=False))
        return "\n".join(lines)

    def _build_logit_block(self) -> str:
        r = self.results.get("logit", {})
        if not r: return "No logit result.\n"
        lines = [
            f"Pseudo-R^2: {r.get('pseudo_r2','n/a'):.4f} | "
            f"n_obs: {r.get('n_obs',0)} | positives: {r.get('n_positive',0)}",
        ]
        cf = r.get("coefs")
        if cf is not None:
            lines.append("\nCoefficients:")
            lines.append(cf.to_string(index=False))
        return "\n".join(lines)


def launch():
    root = tk.Tk()
    app = App(root)
    root.mainloop()


if __name__ == "__main__":
    launch()
