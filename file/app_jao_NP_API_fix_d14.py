import csv
import json
import os
import re
import subprocess
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.lib import colors as rl_colors
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
        HRFlowable, KeepTogether)
    REPORTLAB_OK = True
except ImportError:
    REPORTLAB_OK = False

try:
    import matplotlib as mpl
    import matplotlib.image as mpl_img
    import matplotlib.ticker as ticker
    import matplotlib.patheffects as patheffects
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
except ImportError:
    pass  # error shown after Tk is up

# ----------------------------------------------------------------------
#  NORD POOL API CONFIGURATION
# ----------------------------------------------------------------------
NORDPOOL_USER = "API_DATA_MEHDI"
NORDPOOL_PASSWORD = "OsloNordpool@123"
TOKEN_URL    = "https://sts.nordpoolgroup.com/connect/token"
PROD_URL     = "https://data-api.nordpoolgroup.com/api/v2/PowerSystem/Productions/ByLocations"
CONS_URL     = "https://data-api.nordpoolgroup.com/api/v2/PowerSystem/Consumptions/ByLocations"
NP_PRICE_URL = "https://data-api.nordpoolgroup.com/api/v2/Auction/Prices/ByAreas"
NP_VOL_URL   = "https://data-api.nordpoolgroup.com/api/v2/Auction/Volumes/ByAreas"
NP_FLOW_URL  = "https://data-api.nordpoolgroup.com/api/v2/Auction/ScheduledPhysicalFlows/ByAreas"

NORDIC_ZONES = ["NO1","NO2","NO3","NO4","NO5","SE1","SE2","SE3","SE4","DK1","DK2","FI"]

# Normalized (x, y) from image top-left corner [0-1]; scaled to actual px at render time
ZONE_POS_NORM = {
    "NO4": (0.300, 0.137), "SE1": (0.498, 0.252), "FI":  (0.716, 0.392),
    "NO3": (0.205, 0.355), "SE2": (0.466, 0.395),
    "NO5": (0.08, 0.450), "NO1": (0.278, 0.530), "NO2": (0.172, 0.578),
    "SE3": (0.436, 0.565), "SE4": (0.398, 0.710),
    "DK1": (0.237, 0.778), "DK2": (0.327, 0.848),
}

# Canonical zone border pairs — defines which arrows to draw
ZONE_CONNECTIONS = [
    ("NO1","NO2"), ("NO1","NO3"), ("NO1","NO5"), ("NO1","SE3"),
    ("NO2","NO5"), ("NO2","DK1"),
    ("NO3","NO4"), ("NO3","SE2"),
    ("NO4","SE1"),
    ("SE1","SE2"), ("SE1","FI"),
    ("SE2","SE3"),
    ("SE3","SE4"), ("SE3","FI"),
    ("SE4","DK1"), ("SE4","DK2"),
    ("DK1","DK2"),
]

_HERE   = os.path.dirname(os.path.abspath(__file__))
MAP_PATH = os.path.join(_HERE, "map.png")

# ----------------------------------------------------------------------
#  DESIGN TOKENS
# ----------------------------------------------------------------------
C_BG      = '#f0f4f8'
C_PANEL   = '#ffffff'
C_ACCENT  = '#1e3a5f'
C_PRIMARY = '#2563eb'
C_BORDER  = '#d1d9e6'
C_TEXT    = '#1e293b'
C_MUTED   = '#64748b'
C_GREEN   = '#059669'
C_RED     = '#dc2626'
C_PURPLE  = '#7c3aed'
C_AMBER   = '#d97706'

FONT_UI   = ('Segoe UI', 9)
FONT_BOLD = ('Segoe UI', 9,  'bold')
FONT_H1   = ('Segoe UI', 11, 'bold')
FONT_MONO = ('Consolas',  10)

CHART_PALETTE = [C_PRIMARY, C_RED, C_GREEN, C_PURPLE, C_AMBER, '#0891b2', '#be185d']

# ----------------------------------------------------------------------
#  MODULE-LEVEL HELPERS
# ----------------------------------------------------------------------
_CET = ZoneInfo("Europe/Oslo")

def _utc_to_cet(ts: str) -> datetime:
    """Parse a UTC ISO-8601 timestamp (Z or +HH:MM suffix) → CET/CEST datetime."""
    s = ts.strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    elif s[-6] not in ('+', '-'):      # no offset at all → assume UTC
        s = s + '+00:00'
    return datetime.fromisoformat(s).astimezone(_CET)

def _cet_key(dt: datetime) -> str:
    """Return the 'YYYYMMDD_HH:MM' key used throughout for CET-aligned look-ups."""
    return f"{dt.strftime('%Y%m%d')}_{dt.strftime('%H:%M')}"

def get_np_access_token():
    headers = {
        'Authorization': 'Basic Y2xpZW50X21hcmtldGRhdGFfYXBpOmNsaWVudF9tYXJrZXRkYXRhX2FwaQ==',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {'grant_type': 'password', 'scope': 'marketdata_api',
            'username': NORDPOOL_USER, 'password': NORDPOOL_PASSWORD}
    try:
        r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=10)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception:
        return None

# ----------------------------------------------------------------------
#  JAO DATA FETCHING — Optimized Daily PowerShell Retrieval
# ----------------------------------------------------------------------

API_URL = "https://publicationtool.jao.eu/nordic/api/data/fbDomainShadowPrice"

def fetch_day_via_powershell(date_str, log_func):
    """Perform a daily batch fetch using PowerShell for maximum speed."""
    try:
        day    = pd.Timestamp(date_str, tz="Europe/Amsterdam")
        d_from = day.replace(hour=0,  minute=0 ).tz_convert("UTC")
        d_to   = day.replace(hour=23, minute=59).tz_convert("UTC")
        
        params = {
            "FromUTC": d_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "ToUTC":   d_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "take":    15000,
            "filter":  "{}"
        }
        query = urllib.parse.urlencode(params)
        full_url = f"{API_URL}?{query}"
        
        log_func(f"🌐 Requesting: {date_str}")

        ps_script = (
            "$ProgressPreference = 'SilentlyContinue'; "
            "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; "
            "try { "
            f"  $resp = Invoke-WebRequest -Uri '{full_url}' -Method Get -TimeoutSec 60 -UseBasicParsing; "
            "  if ($resp.Content) { $resp.Content } else { 'EMPTY_CONTENT' } "
            "} catch { "
            "  Write-Output ('PS_ERROR: ' + $_.Exception.Message) "
            "}"
        )

        result = subprocess.run(
            ["powershell", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, text=True, encoding='utf-8', errors='replace'
        )

        raw_out = result.stdout.strip()
        if not raw_out or raw_out.startswith("PS_ERROR"): return None, raw_out
        if raw_out == "EMPTY_CONTENT": return [], None

        data_json = json.loads(raw_out)
        return data_json.get("data", []), None
    except Exception as e: return None, str(e)


def run_data_fetching_and_processing(params, status_cb=None, progress_cb=None):
    """Orchestrates daily fetching across the requested date range."""
    start_str = params['start_cet'].split('T')[0]
    end_str   = params['end_cet'].split('T')[0]
    
    try:
        # Generate full list of days in the range
        date_list = pd.date_range(start=start_str, end=end_str).strftime('%Y-%m-%d').tolist()
    except Exception as e:
        if status_cb: status_cb(f"ERROR: Invalid date range: {e}")
        return []

    all_data = []
    total_days = len(date_list)
    failed_dates = []

    for idx, d_str in enumerate(date_list):
        day_num = idx + 1
        pct = int(day_num / total_days * 100)
        
        if status_cb:
            status_cb(f"Progress: Day {day_num}/{total_days} ({pct}%) | Fetching Batch: {d_str}")
        
        if progress_cb:
            progress_cb(day_num, total_days)

        batch, err = fetch_day_via_powershell(d_str, lambda m: None)
        
        if batch is None:
            failed_dates.append(d_str)
            if status_cb: status_cb(f"❌ Failed: {d_str} ({err})")
        else:
            all_data.extend(batch)
            if status_cb: status_cb(f"✅ Success: {d_str} ({len(batch)} records)")

    # Apply shadow price filter if requested
    if params.get('shadow_price_filter') == 'positive':
        all_data = [d for d in all_data if d.get('shadowPrice') is not None and float(d.get('shadowPrice')) > 0]

    # Post-process timestamps to CET for app compatibility
    for entry in all_data:
        if entry.get('dateTimeUtc'):
            cet_dt = _utc_to_cet(entry['dateTimeUtc'])
            entry['date'] = cet_dt.strftime('%Y%m%d')
            entry['time'] = cet_dt.strftime('%H:%M:%S')

    summary = f"FETCH COMPLETE. Total Records: {len(all_data)}"
    if failed_dates:
        summary += f"\nFailed Days: {', '.join(failed_dates)}"
    if status_cb:
        status_cb(summary)

    if all_data:
        output_file = params.get('output_file', '')
        if output_file:
            os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                if len(all_data) > 0:
                    writer = csv.DictWriter(f, fieldnames=list(all_data[0].keys()), extrasaction='ignore')
                    writer.writeheader()
                    writer.writerows(all_data)

    return all_data

# ----------------------------------------------------------------------
#  Main Application
# ----------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.root.title("JAO & Nordpool Analytics")
        self.root.geometry("1280x980")
        self.root.configure(bg=C_BG)
        self.root.minsize(900, 700)

        self._setup_styles()
        self.today_str        = datetime.now().strftime('%Y-%m-%d')
        self.raw_filtered_data = []
        self._last_briefing   = None

        self._create_header()

        self.notebook = ttk.Notebook(root, style='App.TNotebook')
        self.notebook.pack(padx=12, pady=4, fill='both', expand=True)

        for attr, label in [
            ('tab1', '  Fetch / Upload  '),
            ('tab2', '  Analysis  '),
            ('tab3', '  Shadow / RAM  '),
            ('tab4', '  Impact / PTDF  '),
            ('tab5', '  Gen / Cons  '),
            ('tab6', '  Price History  '),
            ('tab7', '  Net Position  '),
            ('tab8', '  Nordic Map  '),
            ('tab9', '  Maintenance Analysis  '),
        ]:
            f = ttk.Frame(self.notebook, style='Card.TFrame')
            setattr(self, attr, f)
            self.notebook.add(f, text=label)

        self._create_statusbar()
        self._create_tab1_widgets()
        self._create_tab2_widgets()
        self._create_tab3_widgets()
        self._create_tab4_widgets()
        self._create_tab5_widgets()
        self._create_tab6_widgets()
        self._create_tab7_widgets()
        self._create_tab8_widgets()
        self._create_tab9_widgets()

    # ------------------------------------------------------------------
    #  HEADER BAR
    # ------------------------------------------------------------------
    def _create_header(self):
        hdr = tk.Frame(self.root, bg=C_ACCENT, height=52)
        hdr.pack(fill=tk.X, side=tk.TOP)
        hdr.pack_propagate(False)

        tk.Label(hdr, text="JAO & Nordpool Analytics",
                 bg=C_ACCENT, fg='white', font=('Segoe UI', 13, 'bold')
                 ).pack(side=tk.LEFT, padx=20)

        tk.Label(hdr, text="Energy Market Intelligence Platform",
                 bg=C_ACCENT, fg='#93c5fd', font=('Segoe UI', 9)
                 ).pack(side=tk.LEFT, padx=(0, 20))

        # Data status badge (right-aligned)
        self.data_badge_var = tk.StringVar(value="No data loaded")
        badge = tk.Label(hdr, textvariable=self.data_badge_var,
                         bg='#162d4a', fg='#93c5fd',
                         font=('Segoe UI', 8), padx=12, pady=4)
        badge.pack(side=tk.RIGHT, padx=16, pady=10)

        # Clock
        self._clock_var = tk.StringVar()
        tk.Label(hdr, textvariable=self._clock_var,
                 bg=C_ACCENT, fg='#64748b', font=('Segoe UI', 8)
                 ).pack(side=tk.RIGHT, padx=4)
        self._tick_clock()

    def _tick_clock(self):
        self._clock_var.set(datetime.now().strftime('%Y-%m-%d  %H:%M:%S'))
        self.root.after(1000, self._tick_clock)

    # ------------------------------------------------------------------
    #  STATUS BAR
    # ------------------------------------------------------------------
    def _create_statusbar(self):
        sb = tk.Frame(self.root, bg='#e2e8f0', height=26)
        sb.pack(fill=tk.X, side=tk.BOTTOM)
        sb.pack_propagate(False)

        self._sb_left  = tk.Label(sb, text="Ready", bg='#e2e8f0', fg=C_MUTED, font=('Segoe UI', 8))
        self._sb_right = tk.Label(sb, text="",      bg='#e2e8f0', fg=C_MUTED, font=('Segoe UI', 8))
        self._sb_left.pack(side=tk.LEFT,  padx=10)
        self._sb_right.pack(side=tk.RIGHT, padx=10)

    def _set_status(self, left, right=""):
        self._sb_left.config(text=left)
        self._sb_right.config(text=right)

    # ------------------------------------------------------------------
    #  STYLE SETUP
    # ------------------------------------------------------------------
    def _setup_styles(self):
        s = ttk.Style(self.root)
        s.theme_use('clam')

        s.configure('TFrame',      background=C_BG)
        s.configure('Card.TFrame', background=C_PANEL)

        s.configure('TLabel',       background=C_PANEL, foreground=C_TEXT,   font=FONT_UI)
        s.configure('Muted.TLabel', background=C_PANEL, foreground=C_MUTED,  font=FONT_UI)
        s.configure('H1.TLabel',    background=C_PANEL, foreground=C_ACCENT, font=FONT_H1)
        s.configure('Sum.TLabel',   background='#dcfce7', foreground='#166534',
                    font=FONT_BOLD, padding=(10, 5), relief='flat')

        s.configure('TLabelframe',       background=C_PANEL, relief='solid',
                    bordercolor=C_BORDER, borderwidth=1)
        s.configure('TLabelframe.Label', background=C_PANEL, foreground=C_ACCENT, font=FONT_BOLD)

        s.configure('TButton', background=C_PANEL, foreground=C_TEXT,
                    font=FONT_UI, padding=(10, 5), relief='solid',
                    bordercolor=C_BORDER, borderwidth=1)
        s.map('TButton',
              background=[('active', '#e8edf5'), ('disabled', '#f1f5f9')],
              foreground=[('disabled', C_MUTED)])

        s.configure('Accent.TButton', background=C_PRIMARY, foreground='white',
                    font=FONT_BOLD, padding=(12, 6), relief='flat', borderwidth=0)
        s.map('Accent.TButton',
              background=[('active', '#1d4ed8'), ('disabled', '#93c5fd')],
              foreground=[('disabled', 'white')])

        s.configure('TEntry',    fieldbackground=C_PANEL, foreground=C_TEXT,
                    bordercolor=C_BORDER, font=FONT_UI, padding=(4, 3))
        s.configure('TCombobox', fieldbackground=C_PANEL, foreground=C_TEXT,
                    bordercolor=C_BORDER, font=FONT_UI)
        s.map('TCombobox', fieldbackground=[('readonly', C_PANEL)])

        s.configure('TRadiobutton', background=C_PANEL, foreground=C_TEXT, font=FONT_UI)

        s.configure('App.TNotebook', background=C_BG, bordercolor=C_BORDER, borderwidth=1)
        s.configure('App.TNotebook.Tab', padding=(14, 7), font=FONT_UI,
                    background='#e2e8f0', foreground=C_MUTED)
        s.map('App.TNotebook.Tab',
              background=[('selected', C_PANEL)],
              foreground=[('selected', C_ACCENT)],
              font=[('selected', FONT_BOLD)])

        s.configure('Treeview', background=C_PANEL, foreground=C_TEXT,
                    fieldbackground=C_PANEL, rowheight=24, font=FONT_UI, borderwidth=0)
        s.configure('Treeview.Heading', background=C_ACCENT, foreground='white',
                    font=FONT_BOLD, relief='flat', padding=(6, 4))
        s.map('Treeview',
              background=[('selected', '#dbeafe')],
              foreground=[('selected', C_ACCENT)])

        s.configure('TScrollbar', background=C_BORDER, troughcolor=C_BG,
                    bordercolor=C_BG, arrowcolor=C_MUTED)

    # ------------------------------------------------------------------
    #  MATPLOTLIB HELPERS
    # ------------------------------------------------------------------
    def _style_figure(self, fig):
        fig.patch.set_facecolor(C_PANEL)

    def _setup_ax(self, ax, labels):
        ax.set_facecolor('#f8fafc')
        for sp in ['top', 'right']:
            ax.spines[sp].set_visible(False)
        for sp in ['left', 'bottom']:
            ax.spines[sp].set_color(C_BORDER)
            ax.spines[sp].set_linewidth(0.8)
        ax.tick_params(axis='both', colors=C_MUTED, labelsize=7.5)

        if labels:
            def format_fn(x, pos):
                idx = int(round(x))
                if 0 <= idx < len(labels):
                    return labels[idx]
                return ""
            ax.xaxis.set_major_formatter(ticker.FuncFormatter(format_fn))
        ax.xaxis.set_major_locator(ticker.MaxNLocator(nbins=12, integer=True))
        ax.tick_params(axis='x', rotation=35, labelsize=7.5)
        ax.yaxis.label.set_color(C_TEXT)
        ax.yaxis.label.set_fontsize(8.5)
        ax.title.set_color(C_ACCENT)
        ax.title.set_fontsize(9.5)
        ax.title.set_fontweight('bold')
        ax.grid(True, color='#e2e8f0', linewidth=0.7, linestyle='-', alpha=0.8)
        ax.set_axisbelow(True)

    def _style_twin(self, ax, color):
        ax.tick_params(axis='y', colors=color, labelsize=7.5)
        ax.yaxis.label.set_color(color)
        ax.yaxis.label.set_fontsize(8.5)
        for sp in ['top', 'right', 'left', 'bottom']:
            ax.spines[sp].set_visible(False)
        ax.spines['right'].set_visible(True)
        ax.spines['right'].set_color(color)
        ax.spines['right'].set_linewidth(0.8)

    def _legend(self, ax, **kw):
        ax.legend(frameon=True, framealpha=0.95, fontsize=7.5,
                  edgecolor=C_BORDER, facecolor=C_PANEL,
                  loc='upper left', **kw)

    # ------------------------------------------------------------------
    #  SHARED HELPERS
    # ------------------------------------------------------------------
    @staticmethod
    def _safe_float(d: dict, key: str, default: float = 0.0) -> float:
        v = d.get(key)
        if v is None or v == '':
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _np_headers(token: str) -> dict:
        return {'Authorization': f'Bearer {token}', 'accept': 'application/json'}

    def _show_chart_loading(self, fig, canvas, msg: str = "Loading, please wait…"):
        fig.clear()
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, msg, transform=ax.transAxes,
                ha='center', va='center', fontsize=12, color=C_MUTED)
        ax.axis('off')
        canvas.draw()
        self.root.update_idletasks()

    def _plot_net_pos_twin(self, ax, x_idx, y_np):
        pos = [v >= 0 for v in y_np]
        neg = [v <  0 for v in y_np]
        ax.fill_between(x_idx, y_np, alpha=0.12, color=C_PRIMARY, where=pos)
        ax.fill_between(x_idx, y_np, alpha=0.12, color=C_RED,     where=neg)
        ax.plot(x_idx, y_np, color=C_PRIMARY, linewidth=1.2, label="Net Position (MW)")
        ax.axhline(0, color=C_BORDER, linewidth=0.8, linestyle='--')
        ax.set_ylabel("Net Position (MW)", color=C_PRIMARY, fontsize=8.5)
        self._style_twin(ax, C_PRIMARY)

    def _add_toolbar(self, canvas, frame):
        for w in frame.winfo_children():
            w.destroy()
        tb = NavigationToolbar2Tk(canvas, frame)
        tb.config(background=C_PANEL)
        for child in tb.winfo_children():
            try:
                child.config(background=C_PANEL)
            except tk.TclError:
                pass
        tb.update()
        canvas.draw()

    # ------------------------------------------------------------------
    #  TAB 1 – Fetch / Upload
    # ------------------------------------------------------------------
    def _create_tab1_widgets(self):
        main = ttk.Frame(self.tab1, style='Card.TFrame', padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        top = ttk.Frame(main, style='Card.TFrame')
        top.pack(fill=tk.X, pady=(0, 12))
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=2)

        f0 = ttk.LabelFrame(top, text=" Option A — Load Previous Data ", padding=14)
        f0.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        ttk.Button(f0, text="Upload CSV File", command=self._upload_csv
                   ).pack(anchor='w', pady=(0, 8))
        self.upload_label = ttk.Label(f0, text="No file loaded.", style='Muted.TLabel')
        self.upload_label.pack(anchor='w')

        f1 = ttk.LabelFrame(top, text=" Option B — Fetch New Data from JAO (CET) ", padding=14)
        f1.grid(row=0, column=1, sticky='nsew')

        date_row = ttk.Frame(f1, style='Card.TFrame')
        date_row.pack(fill=tk.X, pady=(0, 4))
        for col, (lbl, val, attr) in enumerate([
            ("Start Date",  self.today_str, 'start_date_entry'),
            ("End Date",    self.today_str, 'end_date_entry'),
        ]):
            f = ttk.Frame(date_row, style='Card.TFrame')
            f.grid(row=0, column=col, padx=(0 if col == 0 else 10, 0))
            ttk.Label(f, text=lbl, style='Muted.TLabel').pack(anchor='w')
            e = ttk.Entry(f, width=14)
            e.insert(0, val)
            e.pack()
            setattr(self, attr, e)
        self.start_date_entry.bind("<KeyRelease>", lambda e: self._sync_date_to_tab2())

        f2 = ttk.LabelFrame(main, text=" Run Settings ", padding=14)
        f2.pack(fill=tk.X, pady=(0, 12))

        settings_row = ttk.Frame(f2, style='Card.TFrame')
        settings_row.pack(fill=tk.X)
        settings_row.columnconfigure(1, weight=1)

        self.shadow_price_filter_var = tk.StringVar(value="positive")
        ttk.Radiobutton(settings_row, text="Shadow Price > 0",
                        variable=self.shadow_price_filter_var, value="positive"
                        ).grid(row=0, column=0, sticky='w')

        fn_f = ttk.Frame(settings_row, style='Card.TFrame')
        fn_f.grid(row=0, column=1, sticky='w', padx=20)
        ttk.Label(fn_f, text="Filename:").pack(side=tk.LEFT)
        self.filename_entry = ttk.Entry(fn_f, width=22)
        self.filename_entry.insert(0, "jao_output.csv")
        self.filename_entry.pack(side=tk.LEFT, padx=(4, 0))

        folder_f = ttk.Frame(settings_row, style='Card.TFrame')
        folder_f.grid(row=0, column=2, sticky='w', padx=10)
        self.save_folder = ""
        ttk.Button(folder_f, text="Select Folder", command=self._select_folder
                   ).pack(side=tk.LEFT)
        self.folder_label = ttk.Label(folder_f, text="No folder selected", style='Muted.TLabel')
        self.folder_label.pack(side=tk.LEFT, padx=(8, 0))

        btn_row = ttk.Frame(f2, style='Card.TFrame')
        btn_row.pack(anchor='w', fill=tk.X, pady=(10, 0))
        self.run_button = ttk.Button(btn_row, text="▶  START FAST FETCH", style='Accent.TButton',
                                     command=self._start_processing)
        self.run_button.pack(side=tk.LEFT)
        self._fetch_pct_var = tk.StringVar(value="")
        ttk.Label(btn_row, textvariable=self._fetch_pct_var,
                  style='Muted.TLabel').pack(side=tk.LEFT, padx=(12, 0))
        self._fetch_progress = ttk.Progressbar(f2, mode='determinate', length=340)
        self._fetch_progress.pack(anchor='w', pady=(6, 0))

        ttk.Label(main, text="Status Log", style='H1.TLabel').pack(anchor='w', pady=(4, 2))
        log_frame = tk.Frame(main, bg='#0f172a', padx=2, pady=2)
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.status_text = tk.Text(
            log_frame, font=FONT_MONO,
            background='#0f172a', foreground='#94e2d5',
            insertbackground='white', relief='flat',
            borderwidth=0, padx=10, pady=8
        )
        sb = tk.Scrollbar(log_frame, command=self.status_text.yview, bg='#1e293b')
        self.status_text.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.status_text.pack(fill=tk.BOTH, expand=True)

    def _sync_date_to_tab2(self):
        val = self.start_date_entry.get()
        if hasattr(self, 'analysis_date_entry'):
            self.analysis_date_entry.delete(0, tk.END)
            self.analysis_date_entry.insert(0, val)

    def _start_processing(self):
        if not self.save_folder:
            messagebox.showerror("Error", "Please select a save folder first.")
            return
        params = {
            'start_cet':           f"{self.start_date_entry.get()}T00:00:00",
            'end_cet':             f"{self.end_date_entry.get()}T23:59:59",
            'output_file':         os.path.join(self.save_folder, self.filename_entry.get()),
            'shadow_price_filter': self.shadow_price_filter_var.get()
        }
        self.run_button.config(state=tk.DISABLED, text="⏳  Fetching…")
        self._fetch_progress['value'] = 0
        self._fetch_pct_var.set("Starting...")
        self._set_status("Fetching data from JAO...")
        threading.Thread(target=self._fetch_thread, args=(params,), daemon=True).start()

    def _fetch_thread(self, params):
        def _on_progress(done, total):
            pct = int(done / total * 100)
            self.root.after(0, self._fetch_progress.config, {'value': pct})
            self.root.after(0, self._fetch_pct_var.set, f"Batch {done} of {total}  ({pct}%)")
            
        try:
            data = run_data_fetching_and_processing(
                params,
                status_cb=lambda m: self.root.after(0, self._update_status, m),
                progress_cb=_on_progress,
            )
            self.raw_filtered_data = data
        except Exception as exc:
            import traceback
            self.root.after(0, self._update_status, f"FETCH ERROR: {exc}\n{traceback.format_exc()}")
            self.raw_filtered_data = []
        self.root.after(0, self._on_fetch_done)

    def _on_fetch_done(self):
        self._fetch_progress['value'] = 100
        self._fetch_pct_var.set("Done")
        self.run_button.config(state=tk.NORMAL, text="▶  START FAST FETCH")
        self._update_data_badge()

    # ------------------------------------------------------------------
    #  TAB 2 – Analysis
    # ------------------------------------------------------------------
    def _create_tab2_widgets(self):
        self._tab2_nb = ttk.Notebook(self.tab2, style='App.TNotebook')
        self._tab2_nb.pack(fill=tk.BOTH, expand=True)

        sub_analysis  = ttk.Frame(self._tab2_nb, style='Card.TFrame')
        sub_briefing  = ttk.Frame(self._tab2_nb, style='Card.TFrame')
        self._tab2_nb.add(sub_analysis, text='  Analysis  ')
        self._tab2_nb.add(sub_briefing, text='  Strategic Briefing  ')

        main = ttk.Frame(sub_analysis, style='Card.TFrame', padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        ctrl = ttk.LabelFrame(main, text=" Filter ", padding=12)
        ctrl.pack(fill=tk.X)

        for col, (lbl, val, attr) in enumerate([
            ("Date (CET):", self.today_str, 'analysis_date_entry'),
            ("Time (CET):", "12:00:00",     'analysis_time_entry'),
            ("Zone From:",  "SE3",           'zone_from_entry'),
            ("Zone To:",    "SE2",           'zone_to_entry'),
        ]):
            ttk.Label(ctrl, text=lbl).grid(row=0, column=col * 2, sticky='w', padx=(0 if col == 0 else 16, 2))
            e = ttk.Entry(ctrl, width=14)
            e.insert(0, val)
            e.grid(row=0, column=col * 2 + 1, sticky='ew')
            setattr(self, attr, e)

        row2 = ttk.Frame(main, style='Card.TFrame')
        row2.pack(fill=tk.X, pady=10)

        sum_f = ttk.LabelFrame(row2, text=" Summary ", padding=10)
        sum_f.pack(side=tk.LEFT)
        ttk.Label(sum_f, text="Total Price Impact (€/MWh):").grid(row=0, column=0, padx=(0, 8))
        self.total_impact_var = tk.StringVar(value="0.0000")
        ttk.Label(sum_f, textvariable=self.total_impact_var, style='Sum.TLabel').grid(row=0, column=1)

        btn_f = ttk.Frame(row2, style='Card.TFrame')
        btn_f.pack(side=tk.LEFT, padx=20)
        ttk.Button(btn_f, text="Run Analysis", command=self._run_analysis, style='Accent.TButton').pack(side=tk.LEFT)
        self._view_graphs_btn = ttk.Button(btn_f, text="View Graphs", command=self._show_all_histories)
        self._view_graphs_btn.pack(side=tk.LEFT, padx=8)
        self._tab2_status = ttk.Label(btn_f, text="", style='Muted.TLabel')
        self._tab2_status.pack(side=tk.LEFT, padx=(0, 4))

        sel_f = ttk.Frame(main, style='Card.TFrame')
        sel_f.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(sel_f, text="Selected CNEC:").pack(side=tk.LEFT)
        self._cnec_sel_var = tk.StringVar()
        self._cnec_sel_combo = ttk.Combobox(sel_f, textvariable=self._cnec_sel_var, width=54, state='readonly')
        self._cnec_sel_combo.pack(side=tk.LEFT, padx=(6, 0))
        self._cnec_sel_var.trace_add('write', lambda *_: self._ma_sync_tab2_cnec())
        ttk.Label(sel_f, text="  (or click a row below)", style='Muted.TLabel').pack(side=tk.LEFT, padx=(6, 0))

        cols       = ('cneName', 'biddingZoneFrom', 'biddingZoneTo', 'shadowPrice', 'ptdf_From', 'ptdf_To', 'price_impact')
        col_labels = ('CNEC Name', 'Zone From', 'Zone To', 'Shadow Price', 'PTDF From', 'PTDF To', 'Price Impact')

        tree_f = ttk.Frame(main, style='Card.TFrame')
        tree_f.pack(fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(tree_f, orient='vertical')
        hsb = ttk.Scrollbar(tree_f, orient='horizontal')
        self.analysis_tree = ttk.Treeview(tree_f, columns=cols, show='headings', yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.config(command=self.analysis_tree.yview)
        hsb.config(command=self.analysis_tree.xview)
        for col, lbl in zip(cols, col_labels):
            self.analysis_tree.heading(col, text=lbl)
            self.analysis_tree.column(col, width=120, anchor='center')
        self.analysis_tree.tag_configure('oddrow',  background=C_PANEL)
        self.analysis_tree.tag_configure('evenrow', background='#f1f5f9')
        self.analysis_tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        vsb.pack(side=tk.RIGHT,  fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self.analysis_tree.pack(fill=tk.BOTH, expand=True)

        self._create_tab2_briefing(sub_briefing)

    # ------------------------------------------------------------------
    #  Briefing Widgets and Logic (Placeholder to keep script valid)
    # ------------------------------------------------------------------
    def _create_tab2_briefing(self, parent):
        toolbar = tk.Frame(parent, bg='#0f2340', height=46)
        toolbar.pack(fill=tk.X, side=tk.TOP)
        btn_frame = tk.Frame(toolbar, bg='#0f2340')
        btn_frame.pack(side=tk.RIGHT, padx=14, pady=8)
        self._brfg_refresh_btn = tk.Button(btn_frame, text="Refresh", command=self._refresh_briefing)
        self._brfg_refresh_btn.pack(side=tk.LEFT)
        body = tk.Frame(parent, bg='#f1f5f9')
        body.pack(fill=tk.BOTH, expand=True)
        self._brfg_text = tk.Text(body, font=('Segoe UI', 9), state='disabled')
        self._brfg_text.pack(fill=tk.BOTH, expand=True)
        self._brfg_kpis = {'corridor': tk.StringVar(), 'mtu': tk.StringVar(), 'n_active': tk.StringVar(),
                          'total_imp': tk.StringVar(), 'top_cnec': tk.StringVar(), 'top_sp': tk.StringVar(),
                          'top_dptdf': tk.StringVar(), 'top_impact': tk.StringVar(), 'regime': tk.StringVar()}

    def _refresh_briefing(self): pass
    def _update_briefing(self, *args): pass
    def _export_briefing_pdf(self): pass

    # ------------------------------------------------------------------
    #  Remaining Create Widgets (Stubs to maintain original structure)
    # ------------------------------------------------------------------
    def _create_tab3_widgets(self):
        f = ttk.Frame(self.tab3, style='Card.TFrame', padding=12)
        f.pack(fill=tk.BOTH, expand=True)
        self.cnec_title_3 = tk.StringVar(value="Select a CNEC row")
        ttk.Label(f, textvariable=self.cnec_title_3, style='H1.TLabel').pack()
        self.fig3 = Figure(figsize=(5, 4), dpi=100); self.canvas3 = FigureCanvasTkAgg(self.fig3, f)
        self.canvas3.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.toolbar_f3 = ttk.Frame(f)

    def _create_tab4_widgets(self):
        f = ttk.Frame(self.tab4, style='Card.TFrame', padding=12)
        f.pack(fill=tk.BOTH, expand=True)
        self.cnec_title_4 = tk.StringVar(value="Select a CNEC row")
        ttk.Label(f, textvariable=self.cnec_title_4, style='H1.TLabel').pack()
        self.fig4 = Figure(figsize=(5, 4), dpi=100); self.canvas4 = FigureCanvasTkAgg(self.fig4, f)
        self.canvas4.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.toolbar_f4 = ttk.Frame(f)

    def _create_tab5_widgets(self):
        f = ttk.Frame(self.tab5, style='Card.TFrame', padding=12); f.pack(fill=tk.BOTH, expand=True)
        self.np_location_entry = ttk.Entry(f); self.gen_type_var = tk.StringVar(); self.np_areas_label = ttk.Label(f)
        self.fig5 = Figure(); self.canvas5 = FigureCanvasTkAgg(self.fig5, f); self.toolbar_f5 = ttk.Frame(f)

    def _create_tab6_widgets(self):
        f = ttk.Frame(self.tab6, style='Card.TFrame', padding=12); f.pack(fill=tk.BOTH, expand=True)
        self._t6_zone_vars = {z: tk.BooleanVar() for z in NORDIC_ZONES}
        self.fig6 = Figure(); self.canvas6 = FigureCanvasTkAgg(self.fig6, f); self.toolbar_f6 = ttk.Frame(f)

    def _create_tab7_widgets(self):
        f = ttk.Frame(self.tab7, style='Card.TFrame', padding=12); f.pack(fill=tk.BOTH, expand=True)
        self._t7_zone = ttk.Entry(f)
        self.fig7 = Figure(); self.canvas7 = FigureCanvasTkAgg(self.fig7, f); self.toolbar_f7 = ttk.Frame(f)

    def _create_tab8_widgets(self):
        f = ttk.Frame(self.tab8, style='Card.TFrame', padding=12); f.pack(fill=tk.BOTH, expand=True)
        self._t8_date = ttk.Entry(f); self._t8_mtu = ttk.Combobox(f)
        self.fig8 = Figure(); self.canvas8 = FigureCanvasTkAgg(self.fig8, f); self.toolbar_f8 = ttk.Frame(f)
        self._t8_diag = tk.Text(f, height=5); self._t8_status = ttk.Label(f)

    def _create_tab9_widgets(self):
        f = ttk.Frame(self.tab9, style='Card.TFrame', padding=12); f.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    #  Logic Helpers
    # ------------------------------------------------------------------
    def _select_folder(self):
        self.save_folder = filedialog.askdirectory()
        if self.save_folder: self.folder_label.config(text=os.path.basename(self.save_folder))

    def _update_status(self, msg):
        self.status_text.config(state='normal')
        self.status_text.insert(tk.END, f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
        self.status_text.see(tk.END); self.status_text.config(state='disabled')

    def _upload_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv")])
        if not path: return
        with open(path, mode='r', encoding='utf-8-sig') as f:
            self.raw_filtered_data = list(csv.DictReader(f))
        self._update_data_badge(); self.upload_label.config(text=f"✓ {os.path.basename(path)}")

    def _update_data_badge(self):
        n = len(self.raw_filtered_data)
        self.data_badge_var.set(f"{n:,} records loaded")

    def _safe_float(self, d, key):
        v = d.get(key)
        try: return float(v)
        except: return 0.0

    def _run_analysis(self):
        d_str = self.analysis_date_entry.get().replace('-', '')
        t_str = self.analysis_time_entry.get()
        z_f, z_t = self.zone_from_entry.get(), self.zone_to_entry.get()
        self.analysis_tree.delete(*self.analysis_tree.get_children())
        total = 0.0
        for d in self.raw_filtered_data:
            if str(d.get('date')) == d_str and d.get('time') == t_str:
                sp = self._safe_float(d, 'shadowPrice')
                pf = self._safe_float(d, f'ptdf_{z_f}')
                pt = self._safe_float(d, f'ptdf_{z_t}')
                impact = sp * (pf - pt); total += impact
                self.analysis_tree.insert('', tk.END, values=(d.get('cneName'), d.get('biddingZoneFrom'), d.get('biddingZoneTo'), f"{sp:.4f}", f"{pf:.4f}", f"{pt:.4f}", f"{impact:.4f}"))
        self.total_impact_var.set(f"{total:.4f}")

    def _on_tree_select(self, e): pass
    def _ma_sync_tab2_cnec(self): pass
    def _show_all_histories(self): pass

if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()