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
#  JAO DATA FETCHING  — PowerShell Invoke-RestMethod, per-15-min-MTU progress
# ----------------------------------------------------------------------

def fetch_data_for_period(from_dt, to_dt):
    base_url = "https://publicationtool.jao.eu/nordic/api/data/fbDomainShadowPrice"
    from_utc_str = from_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    to_utc_str   = to_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    api_params = {"filter": "{}", "skip": 0, "take": 5000,
                  "fromUtc": from_utc_str, "toUtc": to_utc_str}
    query_string = urllib.parse.urlencode(api_params, safe='{}":,[]')
    api_url = f"{base_url}?{query_string}"
    ps_command = (
        f'try{{$r=Invoke-RestMethod -Uri "{api_url}" -EA Stop -TimeoutSec 10;'
        f'$r|ConvertTo-Json -D 10}}catch{{\'ERR\'}}'
    )
    try:
        result = subprocess.run(
            ["powershell", "-Command", ps_command],
            check=True, capture_output=True, text=True,
            encoding='utf-8', errors='replace'
        )
        output = result.stdout.strip()
        if output == "ERR" or not output:
            return None
        return json.loads(output).get('data', [])
    except Exception:
        return None


def run_data_fetching_and_processing(params, status_cb=None, progress_cb=None):
    start_dt_cet = datetime.fromisoformat(params['start_cet']).replace(tzinfo=_CET)
    end_dt_cet   = datetime.fromisoformat(params['end_cet']).replace(tzinfo=_CET)
    start_dt_utc = start_dt_cet.astimezone(timezone.utc)
    end_dt_utc   = end_dt_cet.astimezone(timezone.utc)

    all_data, current_dt = [], start_dt_utc
    total_steps = max(1, int((end_dt_utc - start_dt_utc).total_seconds() / 900))
    step, failed_ts = 0, []

    while current_dt < end_dt_utc:
        step += 1
        ts_str = current_dt.strftime('%Y-%m-%d %H:%M')
        pct    = int(step / total_steps * 100)

        if status_cb:
            msg = f"Progress: {step}/{total_steps} ({pct}%) | Fetching: {ts_str} UTC"
            if failed_ts:
                msg += f"\nFailed: {len(failed_ts)}"
            status_cb(msg)

        if progress_cb:
            progress_cb(step, total_steps)

        batch = fetch_data_for_period(current_dt, current_dt + timedelta(minutes=15))
        if batch is None:
            failed_ts.append(ts_str)
        else:
            all_data.extend(batch)

        current_dt += timedelta(minutes=15)

    if params.get('shadow_price_filter') == 'positive':
        def _sp_positive(d):
            v = d.get('shadowPrice')
            if v is None:
                return False
            try:
                return float(v) > 0
            except (TypeError, ValueError):
                return False
        all_data = [d for d in all_data if _sp_positive(d)]

    for entry in all_data:
        if entry.get('dateTimeUtc'):
            cet_dt = _utc_to_cet(entry['dateTimeUtc'])
            entry['date'] = cet_dt.strftime('%Y%m%d')
            entry['time'] = cet_dt.strftime('%H:%M:%S')

    summary = f"FETCH COMPLETE. Records: {len(all_data)}"
    if failed_ts:
        summary += f"\nFailed slots: {', '.join(failed_ts)}"
    if status_cb:
        status_cb(summary)

    if all_data:
        output_file = params.get('output_file', '')
        if output_file:
            os.makedirs(os.path.dirname(output_file) or '.', exist_ok=True)
            with open(output_file, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(all_data[0].keys()),
                                        extrasaction='ignore')
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

        # FIX A: Dynamic resolution on zoom — FuncFormatter maps integer x positions
        # back to their MTU label; integer=True ensures we only land on real indices.
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
        """Safely extract a float from a data dict — handles None, '', non-numeric."""
        v = d.get(key)
        if v is None or v == '':
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _np_headers(token: str) -> dict:
        """Standard NordPool API auth headers."""
        return {'Authorization': f'Bearer {token}', 'accept': 'application/json'}

    def _show_chart_loading(self, fig, canvas, msg: str = "Loading, please wait…"):
        """Clear a matplotlib figure and show a centred loading message."""
        fig.clear()
        ax = fig.add_subplot(111)
        ax.text(0.5, 0.5, msg, transform=ax.transAxes,
                ha='center', va='center', fontsize=12, color=C_MUTED)
        ax.axis('off')
        canvas.draw()
        self.root.update_idletasks()

    def _plot_net_pos_twin(self, ax, x_idx, y_np):
        """Add a net-position twin-axis: blue fill above zero, red below."""
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
    #  TAB 1 – Fetch / Upload  (two-column layout)
    # ------------------------------------------------------------------
    def _create_tab1_widgets(self):
        main = ttk.Frame(self.tab1, style='Card.TFrame', padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # ── Top row: Option A (left) | Option B (right) ───────────────
        top = ttk.Frame(main, style='Card.TFrame')
        top.pack(fill=tk.X, pady=(0, 12))
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=2)

        # Option A – load CSV
        f0 = ttk.LabelFrame(top, text=" Option A — Load Previous Data ", padding=14)
        f0.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        ttk.Button(f0, text="Upload CSV File", command=self._upload_csv
                   ).pack(anchor='w', pady=(0, 8))
        self.upload_label = ttk.Label(f0, text="No file loaded.", style='Muted.TLabel')
        self.upload_label.pack(anchor='w')

        # Option B – fetch from JAO
        f1 = ttk.LabelFrame(top, text=" Option B — Fetch New Data from JAO (CET) ", padding=14)
        f1.grid(row=0, column=1, sticky='nsew')

        date_row = ttk.Frame(f1, style='Card.TFrame')
        date_row.pack(fill=tk.X, pady=(0, 4))
        for col, (lbl, val, attr) in enumerate([
            ("Start Date",  self.today_str, 'start_date_entry'),
            ("Start Time",  "00:00:00",     'start_time_entry'),
            ("End Date",    self.today_str, 'end_date_entry'),
            ("End Time",    "23:45:00",     'end_time_entry'),
        ]):
            f = ttk.Frame(date_row, style='Card.TFrame')
            f.grid(row=0, column=col, padx=(0 if col == 0 else 10, 0))
            ttk.Label(f, text=lbl, style='Muted.TLabel').pack(anchor='w')
            e = ttk.Entry(f, width=14)
            e.insert(0, val)
            e.pack()
            setattr(self, attr, e)
        self.start_date_entry.bind("<KeyRelease>", lambda e: self._sync_date_to_tab2())

        # ── Run settings row ──────────────────────────────────────────
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
        self.run_button = ttk.Button(btn_row, text="▶  START FETCH", style='Accent.TButton',
                                     command=self._start_processing)
        self.run_button.pack(side=tk.LEFT)
        self._fetch_pct_var = tk.StringVar(value="")
        ttk.Label(btn_row, textvariable=self._fetch_pct_var,
                  style='Muted.TLabel').pack(side=tk.LEFT, padx=(12, 0))
        self._fetch_progress = ttk.Progressbar(f2, mode='determinate', length=340)
        self._fetch_progress.pack(anchor='w', pady=(6, 0))

        # ── Status log ───────────────────────────────────────────────
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
        self.analysis_date_entry.delete(0, tk.END)
        self.analysis_date_entry.insert(0, val)

    # ------------------------------------------------------------------
    #  TAB 2 – Analysis  (nested: Analysis sub-tab + Strategic Briefing sub-tab)
    # ------------------------------------------------------------------
    def _create_tab2_widgets(self):
        # ── Nested notebook inside Tab 2 ─────────────────────────────
        self._tab2_nb = ttk.Notebook(self.tab2, style='App.TNotebook')
        self._tab2_nb.pack(fill=tk.BOTH, expand=True)

        sub_analysis  = ttk.Frame(self._tab2_nb, style='Card.TFrame')
        sub_briefing  = ttk.Frame(self._tab2_nb, style='Card.TFrame')
        self._tab2_nb.add(sub_analysis, text='  Analysis  ')
        self._tab2_nb.add(sub_briefing, text='  Strategic Briefing  ')

        # ── Build analysis content into sub_analysis ──────────────────
        main = ttk.Frame(sub_analysis, style='Card.TFrame', padding=16)
        main.pack(fill=tk.BOTH, expand=True)

        # ── Filter row ───────────────────────────────────────────────
        ctrl = ttk.LabelFrame(main, text=" Filter ", padding=12)
        ctrl.pack(fill=tk.X)

        for col, (lbl, val, attr) in enumerate([
            ("Date (CET):", self.today_str, 'analysis_date_entry'),
            ("Time (CET):", "12:00:00",     'analysis_time_entry'),
            ("Zone From:",  "SE3",           'zone_from_entry'),
            ("Zone To:",    "SE2",           'zone_to_entry'),
        ]):
            ttk.Label(ctrl, text=lbl).grid(row=0, column=col * 2,
                                           sticky='w', padx=(0 if col == 0 else 16, 2))
            e = ttk.Entry(ctrl, width=14)
            e.insert(0, val)
            e.grid(row=0, column=col * 2 + 1, sticky='ew')
            setattr(self, attr, e)

        # ── Summary + buttons ────────────────────────────────────────
        row2 = ttk.Frame(main, style='Card.TFrame')
        row2.pack(fill=tk.X, pady=10)

        sum_f = ttk.LabelFrame(row2, text=" Summary ", padding=10)
        sum_f.pack(side=tk.LEFT)
        ttk.Label(sum_f, text="Total Price Impact (€/MWh):").grid(row=0, column=0, padx=(0, 8))
        self.total_impact_var = tk.StringVar(value="0.0000")
        ttk.Label(sum_f, textvariable=self.total_impact_var, style='Sum.TLabel').grid(row=0, column=1)

        btn_f = ttk.Frame(row2, style='Card.TFrame')
        btn_f.pack(side=tk.LEFT, padx=20)
        ttk.Button(btn_f, text="Run Analysis",
                   command=self._run_analysis, style='Accent.TButton').pack(side=tk.LEFT)
        self._view_graphs_btn = ttk.Button(btn_f, text="View Graphs",
                                           command=self._show_all_histories)
        self._view_graphs_btn.pack(side=tk.LEFT, padx=8)
        self._tab2_status = ttk.Label(btn_f, text="", style='Muted.TLabel')
        self._tab2_status.pack(side=tk.LEFT, padx=(0, 4))

        # ── Optional CNEC selector ───────────────────────────────────
        sel_f = ttk.Frame(main, style='Card.TFrame')
        sel_f.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(sel_f, text="Selected CNEC:").pack(side=tk.LEFT)
        self._cnec_sel_var = tk.StringVar()
        self._cnec_sel_combo = ttk.Combobox(sel_f, textvariable=self._cnec_sel_var,
                                            width=54, state='readonly')
        self._cnec_sel_combo.pack(side=tk.LEFT, padx=(6, 0))
        # When Tab 2 CNEC selection changes, update Tab 9 default
        self._cnec_sel_var.trace_add('write', lambda *_: self._ma_sync_tab2_cnec())
        ttk.Label(sel_f, text="  (or click a row below)",
                  style='Muted.TLabel').pack(side=tk.LEFT, padx=(6, 0))

        # ── Results treeview ─────────────────────────────────────────
        cols       = ('cneName', 'biddingZoneFrom', 'biddingZoneTo',
                      'shadowPrice', 'ptdf_From', 'ptdf_To', 'price_impact')
        col_labels = ('CNEC Name', 'Zone From', 'Zone To',
                      'Shadow Price', 'PTDF From', 'PTDF To', 'Price Impact')

        tree_f = ttk.Frame(main, style='Card.TFrame')
        tree_f.pack(fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(tree_f, orient='vertical')
        hsb = ttk.Scrollbar(tree_f, orient='horizontal')
        self.analysis_tree = ttk.Treeview(tree_f, columns=cols, show='headings',
                                          yscrollcommand=vsb.set, xscrollcommand=hsb.set)
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

        # ── Build Strategic Briefing into sub_briefing ────────────────
        self._create_tab2_briefing(sub_briefing)

    # ------------------------------------------------------------------
    #  TAB 2 – Strategic Briefing sub-tab
    # ------------------------------------------------------------------
    def _create_tab2_briefing(self, parent):
        """Professional Strategic Market Briefing panel."""

        # ── Toolbar ───────────────────────────────────────────────────
        toolbar = tk.Frame(parent, bg='#0f2340', height=46)
        toolbar.pack(fill=tk.X, side=tk.TOP)
        toolbar.pack_propagate(False)

        title_frame = tk.Frame(toolbar, bg='#0f2340')
        title_frame.pack(side=tk.LEFT, padx=16, pady=0, fill=tk.Y)
        tk.Label(title_frame, text="STRATEGIC MARKET BRIEFING",
                 bg='#0f2340', fg='#f8fafc',
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', pady=(10, 0))
        tk.Label(title_frame, text="Flow-Based Market Coupling  \u2014  Nordic Day-Ahead Domain",
                 bg='#0f2340', fg='#60a5fa',
                 font=('Segoe UI', 7)).pack(anchor='w')

        btn_frame = tk.Frame(toolbar, bg='#0f2340')
        btn_frame.pack(side=tk.RIGHT, padx=14, pady=8)

        self._brfg_refresh_btn = tk.Button(
            btn_frame, text="\u21bb  Refresh",
            bg='#334155', fg='#ffffff',
            font=('Segoe UI', 8, 'bold'),
            relief='flat', padx=12, pady=5,
            cursor='hand2',
            activebackground='#475569', activeforeground='#ffffff',
            command=self._refresh_briefing)
        self._brfg_refresh_btn.pack(side=tk.LEFT, padx=(0, 6))

        self._brfg_export_btn = tk.Button(
            btn_frame, text="\u2193  Export PDF",
            bg='#2563eb', fg='#ffffff',
            font=('Segoe UI', 8, 'bold'),
            relief='flat', padx=14, pady=5,
            cursor='hand2',
            activebackground='#1d4ed8', activeforeground='#ffffff',
            command=self._export_briefing_pdf)
        self._brfg_export_btn.pack(side=tk.LEFT)

        # thin accent line under toolbar
        tk.Frame(parent, bg='#2563eb', height=2).pack(fill=tk.X)

        # ── Body ──────────────────────────────────────────────────────
        body = tk.Frame(parent, bg='#f1f5f9')
        body.pack(fill=tk.BOTH, expand=True)

        # ── KPI strip ─────────────────────────────────────────────────
        kpi_outer = tk.Frame(body, bg='#f1f5f9', width=210)
        kpi_outer.pack(side=tk.LEFT, fill=tk.Y)
        kpi_outer.pack_propagate(False)

        kpi_hdr = tk.Frame(kpi_outer, bg='#e2e8f0', height=30)
        kpi_hdr.pack(fill=tk.X)
        kpi_hdr.pack_propagate(False)
        tk.Label(kpi_hdr, text="SNAPSHOT METRICS", bg='#e2e8f0',
                 fg='#64748b', font=('Segoe UI', 7, 'bold')).pack(
                 side=tk.LEFT, padx=12, pady=7)

        tk.Frame(body, bg='#e2e8f0', width=1).pack(side=tk.LEFT, fill=tk.Y)

        # report
        report_wrap = tk.Frame(body, bg='#ffffff')
        report_wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # KPI cards
        self._brfg_kpis = {}
        _kpi_accent = {
            'corridor':   '#3b82f6', 'mtu':        '#6366f1',
            'n_active':   '#8b5cf6', 'total_imp':  '#0ea5e9',
            'top_cnec':   '#14b8a6', 'top_sp':     '#f59e0b',
            'top_dptdf':  '#6366f1', 'top_impact': '#ef4444',
            'regime':     '#10b981',
        }
        kpi_scroll = tk.Frame(kpi_outer, bg='#f1f5f9')
        kpi_scroll.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        for key, label, default in [
            ('corridor',   'Corridor',          '\u2014 / \u2014'),
            ('mtu',        'MTU (CET)',          '\u2014'),
            ('n_active',   'Active CNECs',       '\u2014'),
            ('total_imp',  'Total \u0394P',      '\u2014'),
            ('top_cnec',   'Dominant CNEC',      '\u2014'),
            ('top_sp',     'Shadow Price \u03bb','\u2014'),
            ('top_dptdf',  'Delta-PTDF',         '\u2014'),
            ('top_impact', 'CNEC Impact PI',     '\u2014'),
            ('regime',     'Regime',             '\u2014'),
        ]:
            card = tk.Frame(kpi_scroll, bg='#ffffff')
            card.pack(fill=tk.X, pady=3)
            # coloured accent border
            tk.Frame(card, bg=_kpi_accent.get(key, C_PRIMARY), width=4
                     ).pack(side=tk.LEFT, fill=tk.Y)
            inner = tk.Frame(card, bg='#ffffff', padx=10, pady=8)
            inner.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            tk.Label(inner, text=label.upper(), bg='#ffffff',
                     fg='#94a3b8', font=('Segoe UI', 6, 'bold')).pack(anchor='w')
            var = tk.StringVar(value=default)
            tk.Label(inner, textvariable=var, bg='#ffffff', fg='#1e293b',
                     font=('Segoe UI', 9, 'bold'),
                     wraplength=158, justify='left').pack(anchor='w')
            self._brfg_kpis[key] = var

        tk.Frame(kpi_scroll, bg='#e2e8f0', height=1).pack(fill=tk.X, pady=6)
        tk.Label(kpi_scroll,
                 text="Run Analysis to refresh.",
                 bg='#f1f5f9', fg='#94a3b8',
                 font=('Segoe UI', 7), justify='left').pack(anchor='w')

        # ── Report text widget ────────────────────────────────────────
        txt_frame = tk.Frame(report_wrap, bg='#ffffff')
        txt_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self._brfg_text = tk.Text(
            txt_frame,
            font=('Segoe UI', 9),
            background='#ffffff',
            foreground='#1e293b',
            insertbackground='#1e293b',
            relief='flat', borderwidth=0,
            padx=28, pady=18,
            wrap=tk.WORD,
            state='disabled',
            cursor='arrow',
            selectbackground='#dbeafe',
            spacing1=0, spacing2=0, spacing3=0,
        )
        _vsb = tk.Scrollbar(txt_frame, command=self._brfg_text.yview,
                            bg='#f1f5f9', troughcolor='#e2e8f0',
                            relief='flat', width=12)
        self._brfg_text.config(yscrollcommand=_vsb.set)
        _vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._brfg_text.pack(fill=tk.BOTH, expand=True)

        # ── Tags ─────────────────────────────────────────────────────
        T = self._brfg_text

        # Document title
        T.tag_configure('doc_title',
            font=('Segoe UI', 14, 'bold'), foreground='#0f2340',
            spacing3=2)
        T.tag_configure('doc_sub',
            font=('Segoe UI', 9), foreground='#64748b', spacing3=6)
        T.tag_configure('doc_rule',
            font=('Segoe UI', 4), foreground='#e2e8f0',
            background='#e2e8f0', spacing1=4, spacing3=8)

        # Section headers — light bg strip, navy bold
        T.tag_configure('h2',
            font=('Segoe UI', 9, 'bold'), foreground='#0f2340',
            background='#f1f5f9',
            lmargin1=0, lmargin2=0,
            spacing1=16, spacing3=6)
        T.tag_configure('h2_mark',
            font=('Segoe UI', 9, 'bold'), foreground='#2563eb',
            background='#f1f5f9')

        # Body text
        T.tag_configure('body',
            font=('Segoe UI', 9), foreground='#334155', spacing3=2)
        T.tag_configure('body_i',
            font=('Segoe UI', 9), foreground='#334155',
            lmargin1=20, lmargin2=20, spacing3=1)
        T.tag_configure('muted',
            font=('Segoe UI', 8), foreground='#94a3b8', spacing3=1)
        T.tag_configure('muted_i',
            font=('Segoe UI', 8), foreground='#94a3b8',
            lmargin1=20, lmargin2=20, spacing1=1)

        # Formula label + block
        T.tag_configure('fl',
            font=('Segoe UI', 8, 'bold'), foreground='#1d4ed8',
            spacing1=12, spacing3=2, lmargin1=20, lmargin2=20)
        T.tag_configure('fm',
            font=('Consolas', 9), foreground='#1d4ed8',
            background='#eff6ff',
            lmargin1=36, lmargin2=36,
            spacing1=1, spacing3=1)
        T.tag_configure('fm_c',
            font=('Consolas', 8), foreground='#64748b',
            background='#eff6ff',
            lmargin1=36, lmargin2=36, spacing3=1)

        # Value colours (Consolas bold for inline numbers)
        T.tag_configure('vn', font=('Consolas', 9, 'bold'), foreground='#1e293b')
        T.tag_configure('vg', font=('Consolas', 9, 'bold'), foreground='#059669')
        T.tag_configure('va', font=('Consolas', 9, 'bold'), foreground='#d97706')
        T.tag_configure('vr', font=('Consolas', 9, 'bold'), foreground='#dc2626')
        T.tag_configure('vb', font=('Consolas', 9, 'bold'), foreground='#2563eb')

        # Regime pills
        T.tag_configure('p_sat',
            font=('Segoe UI', 8, 'bold'), foreground='#7f1d1d',
            background='#fee2e2', relief='flat')
        T.tag_configure('p_str',
            font=('Segoe UI', 8, 'bold'), foreground='#78350f',
            background='#fef3c7', relief='flat')
        T.tag_configure('p_surg',
            font=('Segoe UI', 8, 'bold'), foreground='#14532d',
            background='#dcfce7', relief='flat')

        # Table
        T.tag_configure('th',
            font=('Consolas', 8, 'bold'), foreground='#0f2340',
            background='#e2e8f0', spacing1=6, spacing3=2)
        T.tag_configure('tr_a',
            font=('Consolas', 8), foreground='#334155', background='#ffffff')
        T.tag_configure('tr_b',
            font=('Consolas', 8), foreground='#334155', background='#f8fafc')
        T.tag_configure('rule',
            font=('Consolas', 7), foreground='#e2e8f0')

        self._brfg_write_placeholder()

    # ------------------------------------------------------------------
    def _brfg_write_placeholder(self):
        """Reference sheet shown before any analysis is run."""
        T = self._brfg_text
        T.config(state='normal')
        T.delete('1.0', tk.END)

        def w(text, *tags):
            T.insert(tk.END, text, tags)

        def sec(num, title):
            w(f"  {num}.  {title}  \n", ('h2', 'h2_mark') if num == '' else 'h2')

        w("Strategic Market Briefing\n", 'doc_title')
        w("Flow-Based Market Coupling  \u2014  Nordic Day-Ahead Domain\n", 'doc_sub')
        w("\u2500" * 72 + "\n", 'doc_rule')

        # ── BLOCK 1: FB MC Core Constraint ───────────────────────────
        sec('', "FLOW-BASED MARKET COUPLING \u2014 CORE CONSTRAINT")

        w("\nIn the Single Day-Ahead Coupling (SDAC), EUPHEMIA clears all zones simultaneously\n"
          "subject to the Flow-Based (FB) domain. Every Critical Network Element + Contingency\n"
          "(CNEC) contributes one linear inequality that bounds feasible net positions:\n\n", 'body')

        w("[FB]  F_i  =  \u03a3_z  PTDF_{i,z} \u00b7 NP_z  \u2264  RAM_i       \u2200 CNEC i\n", 'fl')
        w(
            "       F_i          =  power flow on CNEC i  (MW)\n"
            "       PTDF_{i,z}   =  linearised sensitivity: 1 MW of NP_z \u2192 PTDF_{i,z} MW on i\n"
            "       NP_z         =  net position of bidding zone z  (MW, positive = exporter)\n"
            "       RAM_i        =  Remaining Available Margin of CNEC i  (MW)\n"
            "                       RAM = F_max \u2212 F_ref \u2212 FRM \u2212 FAV\n",
            'fm_c')

        w("\nThe FB domain is the intersection of all CNEC half-spaces in NP-space. EUPHEMIA\n"
          "maximises social welfare over bids subject to this polytope \u2014 when a face of the\n"
          "polytope is active (the market hits a CNEC at its RAM limit), a KKT dual variable\n"
          "\u03bb_i \u2265 0 is born. That dual is the Shadow Price reported by JAO.\n\n", 'body')

        # ── BLOCK 2: Step-by-step derivation to price impact ─────────
        sec('', "DERIVATION  \u2014  FROM FB CONSTRAINT TO PRICE IMPACT")

        w("\nEach step below follows directly from the FB constraint and standard LP duality.\n\n",
          'body')

        steps = [
            ("Step 1  \u2014  FB flow constraint (active at optimum)",
             "       F_i*  =  \u03a3_z PTDF_{i,z} \u00b7 NP_z*  =  RAM_i       [CNEC i is binding]\n",
             "       The market clears at the RAM boundary. Adding 1 MW of RAM would allow\n"
             "       NP_z to move further and unlock additional welfare.\n"),
            ("Step 2  \u2014  Shadow price \u03bb_i via KKT stationarity",
             "       \u03bb_i  =  \u2202W* / \u2202RAM_i  \u2265  0       [marginal welfare of relaxing CNEC i]\n",
             "       \u03bb_i EUR/MWh  =  scarcity rent: the market would pay \u03bb_i EUR more per hour\n"
             "       of capacity if CNEC i were relaxed by 1 MW. This is the JAO shadow price.\n"),
            ("Step 3  \u2014  Effect of a bilateral trade ZF \u2192 ZT on flow F_i",
             "       A 1 MW commercial transfer ZF\u2192ZT shifts NP_ZF \u2191 +1 MW, NP_ZT \u2193 \u22121 MW\n"
             "       \u0394F_i  =  PTDF_{i,ZF} \u00b7 (+1)  +  PTDF_{i,ZT} \u00b7 (\u22121)\n"
             "             =  PTDF_{i,ZF}  \u2212  PTDF_{i,ZT}\n"
             "             =  \u0394PTDF_i\n",
             "       \u0394PTDF_i > 0  \u21d4  the trade loads CNEC i toward its RAM limit (flow-limiting)\n"
             "       \u0394PTDF_i < 0  \u21d4  the trade relieves CNEC i (counter-flow)\n"),
            ("Step 4  \u2014  Congestion cost of CNEC i on corridor ZF \u2192 ZT",
             "       PI_i  =  \u03bb_i \u00b7 \u0394PTDF_i\n"
             "             =  \u03bb_i \u00b7 (PTDF_{i,ZF} \u2212 PTDF_{i,ZT})       [EUR/MWh]\n",
             "       \u2192  For every MWh traded ZF\u2192ZT, the binding CNEC i inflicts PI_i EUR of\n"
             "          congestion cost on that commercial flow path.\n"
             "       \u2192  This is precisely the number shown in the \u2018Price Impact\u2019 column of\n"
             "          the Analysis sub-tab and the dominant-CNEC box in this briefing.\n"),
            ("Step 5  \u2014  Total structural zonal price spread",
             "       \u0394P  =  P_ZT \u2212 P_ZF  =  \u03a3_i  PI_i  =  \u03a3_i  \u03bb_i \u00b7 \u0394PTDF_i\n",
             "       The \u2018Total Price Impact\u2019 figure in the Analysis sub-tab is this sum.\n"
             "       It fully decomposes the day-ahead price spread into per-CNEC contributions.\n"
             "       Each binding CNEC is a separable, additive term in the zonal price wedge.\n"),
        ]
        for label, fm, cmt in steps:
            w(label + "\n", 'fl')
            w(fm, 'fm')
            if cmt:
                w(cmt, 'fm_c')
            w("\n", 'fm_c')

        w("\u2500" * 72 + "\n", 'doc_rule')
        w("Set your corridor and MTU in the ", 'body')
        w("Analysis", 'vb')
        w(" sub-tab, then click ", 'body')
        w("Run Analysis", 'vb')
        w(". This panel auto-generates a live intelligence\nreport for that MTU snapshot using the formulas above.\n\n", 'body')

        # ── BLOCK 3: Full equation reference ─────────────────────────
        sec('', "FULL EQUATION REFERENCE")

        w("\nEUPHEMIA is a welfare-maximising MILP over all coupled zones. Each CNEC enters\n"
          "as a linear flow constraint. When binding, its KKT dual variable \u2014 the Shadow\n"
          "Price \u03bb \u2014 measures how much social welfare could be unlocked by relaxing that\n"
          "constraint by 1 MW. The equations below are the analytical backbone of every\n"
          "number the Analysis sub-tab computes.\n\n", 'body')

        eqs = [
            ("[1]  EUPHEMIA welfare objective (LP relaxation)",
             "     max  W  =  \u03a3_z [ CS_z(P_z) + PS_z(P_z) ]\n"
             "     s.t. F_i  =  \u03a3_z  PTDF_{i,z} \u00b7 NP_z      \u2200 i   (DC power flow)\n"
             "          F_i  \u2264  RAM_i                          \u2200 i   (thermal limit)\n"
             "          NP_z \u2208  [NP_z^min, NP_z^max]          \u2200 z   (zone bounds)\n",
             None),
            ("[2]  Shadow Price  \u03bb_i  \u2014  KKT dual of the RAM constraint",
             "     \u03bb_i  =  \u2202W* / \u2202RAM_i  \u2265  0\n",
             "     Relaxing CNEC i by 1 MW raises market welfare by \u03bb_i EUR; equivalently,\n"
             "     the constraint imposes \u03bb_i EUR/MWh of scarcity rent on any flow it limits.\n"),
            ("[3]  Power Transfer Distribution Factor  PTDF_{i,z}",
             "     PTDF_{i,z}  =  \u2202F_i / \u2202NP_z  (linearised around reference point)\n",
             "     One additional MW of net export from zone z loads PTDF_{i,z} MW onto CNEC i.\n"),
            ("[4]  Delta-PTDF for a bilateral corridor  ZF \u2192 ZT",
             "     \u0394PTDF_i  =  PTDF_{i,ZF}  \u2212  PTDF_{i,ZT}\n",
             "     A 1 MW commercial transfer ZF\u2192ZT changes flow on CNEC i by \u0394PTDF_i MW.\n"
             "     Positive \u2194 the transfer loads the constraint toward its RAM limit.\n"),
            ("[5]  Price impact of CNEC i on corridor ZF \u2192 ZT",
             "     PI_i  =  \u03bb_i \u00b7 \u0394PTDF_i  =  \u03bb_i \u00b7 (PTDF_{i,ZF} \u2212 PTDF_{i,ZT})\n",
             "     Units: EUR/MWh.  Per-MWh congestion cost CNEC i imposes on ZF\u2192ZT trade.\n"),
            ("[6]  Total structural price spread  \u0394P  =  P_ZT \u2212 P_ZF",
             "     \u0394P  =  \u03a3_i  PI_i  =  \u03a3_i  \u03bb_i \u00b7 \u0394PTDF_i\n",
             "     The \u2018Total Price Impact\u2019 in the Analysis sub-tab \u2014 complete flow-based\n"
             "     explanation of the zonal price spread.\n"),
        ]
        for label, fm, cmt in eqs:
            w(label + "\n", 'fl')
            w(fm, 'fm')
            if cmt:
                w(cmt, 'fm_c')
            w("\n", 'fm_c')

        w("RAM (Remaining Available Margin) and FALL are charted in Tabs 3 & 4 once a\n"
          "CNEC is selected.  RAM = F_max \u2212 Ref_Flow \u2212 FRM \u2212 FAV; the closer to zero,\n"
          "the more a constraint dominates the flow-based feasible region.\n", 'muted')

        T.config(state='disabled')

    # ------------------------------------------------------------------
    #  Briefing refresh — called by _run_analysis
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def _refresh_briefing(self):
        """Re-run the briefing from stored last-analysis data (Refresh button)."""
        if self._last_briefing:
            b = self._last_briefing
            self._update_briefing(
                b['rows'], b['total'], b['d_str'], b['t_str'],
                b['z_f'], b['z_t'])
        else:
            # nothing run yet — try to trigger analysis
            self._run_analysis()

    def _update_briefing(self, rows, total, d_str, t_str, z_f, z_t):
        """Regenerate the Strategic Briefing from current Analysis results."""
        import traceback as _tb
        try:
            self._update_briefing_inner(rows, total, d_str, t_str, z_f, z_t)
        except Exception as _exc:
            _trace = _tb.format_exc()
            print('[Strategic Briefing] ERROR:\n', _trace)
            try:
                T = self._brfg_text
                T.config(state='normal')
                T.delete('1.0', tk.END)
                T.insert(tk.END, 'BRIEFING ERROR\n',
                         ('doc_title',))
                T.insert(tk.END,
                    'An exception occurred while generating the briefing.\n'
                    'The full traceback is printed to the terminal.\n\n',
                    ('body',))
                T.insert(tk.END, _trace, ('fm_c',))
                T.config(state='disabled')
            except Exception:
                pass

    def _update_briefing_inner(self, rows, total, d_str, t_str, z_f, z_t):
        """Core briefing logic — called via _update_briefing's try/except."""

        n_active = len(rows)
        date_fmt = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}" if len(d_str) == 8 else d_str
        mtu_disp = t_str[:5]
        ts_now   = datetime.now().strftime('%Y-%m-%d %H:%M')

        top        = rows[0] if rows else None
        top_cne    = top[0]            if top else None
        top_sp     = top[3]            if top else 0.0
        top_dptdf  = (top[4] - top[5]) if top else 0.0
        top_impact = top[6]            if top else 0.0

        if   top_sp > 50: regime = 'SYSTEMIC SATURATION'
        elif top_sp > 10: regime = 'STRUCTURAL BOTTLENECK'
        elif top_sp >  0: regime = 'SURGICAL BOTTLENECK'
        else:             regime = 'UNCONGESTED'

        # KPI strip
        self._brfg_kpis['corridor'].set(f"{z_f} \u2192 {z_t}")
        self._brfg_kpis['mtu'].set(f"{date_fmt}  {mtu_disp}")
        self._brfg_kpis['n_active'].set(str(n_active))
        self._brfg_kpis['total_imp'].set(f"{total:+.4f} \u20ac/MWh")
        self._brfg_kpis['top_cnec'].set(
            (top_cne[:26] + '..') if top_cne and len(top_cne) > 26 else (top_cne or '\u2014'))
        self._brfg_kpis['top_sp'].set(f"{top_sp:.4f} \u20ac/MWh" if top else '\u2014')
        self._brfg_kpis['top_dptdf'].set(f"{top_dptdf:+.4f}" if top else '\u2014')
        self._brfg_kpis['top_impact'].set(f"{top_impact:+.4f} \u20ac/MWh" if top else '\u2014')
        self._brfg_kpis['regime'].set(regime)

        def _sp_tag(v):
            if v > 50: return 'vr'
            if v > 10: return 'va'
            if v >  0: return 'vg'
            return 'vn'
        def _pi_tag(v):
            if abs(v) > 5: return 'vr'
            if abs(v) > 1: return 'va'
            return 'vg'

        # Historical stats (same dataset as Tabs 3 & 4)
        hist = {}
        if top_cne and self.raw_filtered_data:
            recs = [d for d in self.raw_filtered_data if d.get('cneName') == top_cne]
            if recs:
                sp_v  = [self._safe_float(d, 'shadowPrice')  for d in recs]
                ram_v = [self._safe_float(d, 'ram')          for d in recs]
                pf_v  = [self._safe_float(d, f'ptdf_{z_f}') for d in recs]
                pt_v  = [self._safe_float(d, f'ptdf_{z_t}') for d in recs]
                pi_v  = [s * (f - t) for s, f, t in zip(sp_v, pf_v, pt_v)]
                n_b   = sum(1 for v in sp_v if v > 0)
                hist  = {
                    'n':        len(recs),
                    'n_bind':   n_b,
                    'bind_pct': n_b / len(recs) * 100,
                    'sp_mean':  sum(sp_v)  / len(sp_v),
                    'sp_max':   max(sp_v),
                    'ram_mean': sum(ram_v) / len(ram_v) if ram_v else 0.0,
                    'ram_min':  min(ram_v) if ram_v else 0.0,
                    'pi_mean':  sum(pi_v)  / len(pi_v),
                    'pi_peak':  max(pi_v,  key=abs),
                }

        # Store for PDF export
        self._last_briefing = dict(
            rows=rows, total=total, d_str=d_str, t_str=t_str,
            z_f=z_f, z_t=z_t, date_fmt=date_fmt, mtu_disp=mtu_disp,
            ts_now=ts_now, n_active=n_active, top=top,
            top_sp=top_sp, top_dptdf=top_dptdf, top_impact=top_impact,
            regime=regime, hist=hist,
        )

        T = self._brfg_text
        T.config(state='normal')
        T.delete('1.0', tk.END)

        def w(text, *tags):
            T.insert(tk.END, text, tags)

        def sec(num, title):
            w(f"  {num}.  {title}  \n", 'h2')

        # ── Document header ───────────────────────────────────────────
        w("Strategic Market Briefing\n", 'doc_title')
        w(f"Nordic FB Domain  \u2014  {date_fmt}  {mtu_disp} CET  \u2014  {z_f} \u2192 {z_t}  \u2014  {ts_now}\n",
          'doc_sub')
        w("\u2500" * 72 + "\n", 'doc_rule')

        # ── 1. Situation Assessment ───────────────────────────────────
        sec(1, "SITUATION ASSESSMENT")
        spread_dir = (f"{z_t} clears at a premium over {z_f}"
                      if total >= 0 else f"{z_f} clears at a premium over {z_t}")
        w("Flow-based domain resolved with ", 'body')
        w(f"{n_active} ", 'va' if n_active > 0 else 'vg')
        w("active CNECs carrying a non-zero shadow rent.\n", 'body')
        w("Total structural spread [eq.\u202f6]:  ", 'body')
        w(f"\u0394P = {total:+.4f} \u20ac/MWh", _pi_tag(total))
        w(f"   ({spread_dir})\n", 'muted')

        if n_active == 0:
            w("\nNo binding constraints in this MTU. The corridor is uncongested \u2014 "
              "prices are expected to converge at the zone boundary.\n", 'body')
            T.config(state='disabled')
            return

        # ── 2. Dominant Constraint ────────────────────────────────────
        sec(2, "DOMINANT CONSTRAINT")
        cne, czf, czt, sp, pf, pt, impact = top
        dptdf     = pf - pt
        share_pct = abs(impact / total * 100) if total else 0.0

        w(f"  CNEC   :  {cne}\n", 'body_i')
        w(f"  Zones  :  {czf} \u2192 {czt}\n", 'body_i')

        ram_now = next((self._safe_float(d, 'ram')
                        for d in self.raw_filtered_data
                        if d.get('cneName') == cne
                        and str(d.get('date')) == d_str
                        and d.get('time') == t_str), None)
        w("  RAM    :  ", 'body_i')
        if ram_now is not None:
            w(f"{ram_now:.1f} MW", _sp_tag(0 if ram_now > 50 else 60))
            w("   (time series \u2192 Tab\u202f3)\n", 'muted')
        else:
            w("\u2014\n", 'vn')

        w("\n  [eq.\u202f5]  PI  =  \u03bb \u00b7 \u0394PTDF\n", 'fl')
        w(f"           =  {sp:.4f}  \u00d7  ({pf:+.4f} \u2212 {pt:+.4f})\n"
          f"           =  {sp:.4f}  \u00d7  {dptdf:+.4f}\n"
          f"           =  ", 'fm')
        w(f"{impact:+.4f} \u20ac/MWh", _pi_tag(impact))
        w(f"   ({share_pct:.1f}% of \u0394P)\n\n", 'muted')

        w(f"  \u03bb = ", 'body_i')
        w(f"{sp:.4f} \u20ac/MWh", _sp_tag(sp))
        w(f"   |   \u0394PTDF = ", 'muted')
        w(f"{dptdf:+.4f}\n", 'va')
        w(f"  A 100\u202fMW trade {z_f}\u2192{z_t} loads ", 'body_i')
        w(f"{dptdf * 100:+.1f}\u202fMW", _sp_tag(abs(dptdf) * sp))
        w(" onto this CNEC  (", 'body_i')
        if dptdf > 0.05:
            w("positively coupled", 'vr')
            w(" \u2014 commercial flow loads the constraint", 'muted')
        elif dptdf < -0.05:
            w("counter-flow", 'vg')
            w(" \u2014 commercial flow relieves the constraint", 'muted')
        else:
            w("electrically decoupled", 'vn')
            w(" \u2014 near-zero \u0394PTDF", 'muted')
        w(").\n", 'body_i')

        # ── 3. Constraint Character ───────────────────────────────────
        sec(3, "CONSTRAINT CHARACTER")
        if sp > 50:
            w("  Regime: ", 'body_i')
            w("  SYSTEMIC SATURATION  ", 'p_sat')
            w(f"   \u03bb = {sp:.4f} \u20ac/MWh\n\n", 'muted')
            w("  \u03bb > 50 signals the unconstrained welfare optimum lies far inside the\n"
              "  infeasible region. The constraint forces the market into a qualitatively\n"
              "  different zonal price formation. Cross-zonal capacity is heavily rationed;\n"
              "  the spread is robust and unlikely to be arbitraged intraday.\n", 'body_i')
        elif sp > 10:
            w("  Regime: ", 'body_i')
            w("  STRUCTURAL BOTTLENECK  ", 'p_str')
            w(f"   \u03bb = {sp:.4f} \u20ac/MWh\n\n", 'muted')
            w("  \u03bb \u2208 (10, 50]: binding and well-defined, but the market retains enough\n"
              "  degrees of freedom to partially compensate. The spread is structurally\n"
              "  justified and will persist across similar dispatch patterns. Intraday\n"
              "  correction requires generation redispatch or significant demand response.\n", 'body_i')
        else:
            w("  Regime: ", 'body_i')
            w("  SURGICAL BOTTLENECK  ", 'p_surg')
            w(f"   \u03bb = {sp:.4f} \u20ac/MWh\n\n", 'muted')
            w("  \u03bb \u2264 10: the optimum sits just outside the feasibility set. A small RAM\n"
              "  relaxation \u2014 or a modest wind/hydro ramp shifting the reference flow \u2014\n"
              "  renders this constraint non-binding. The spread is commercially thin and\n"
              "  sensitive to operational changes.\n", 'body_i')

        # ── 4. Constraint Landscape ───────────────────────────────────
        sec(4, "CONSTRAINT LANDSCAPE")
        top5 = rows[:min(5, len(rows))]
        _lam_hdr   = '\u03bb (EUR/MWh)'
        _dptdf_hdr = '\u0394PTDF'
        w(f"  {'#':<3}  {'CNEC':<32}  {_lam_hdr:>12}  {_dptdf_hdr:>7}  {'PI':>9}  {'share':>6}\n", 'th')
        w("  " + "\u2500" * 70 + "\n", 'rule')
        for rank, (cn, czf2, czt2, sp2, pf2, pt2, pi2) in enumerate(top5, 1):
            dp2  = pf2 - pt2
            shr  = f"{abs(pi2 / total * 100):.1f}%" if total else "\u2014"
            cn_t = (str(cn)[:30] + '..') if len(str(cn)) > 32 else str(cn)
            w(f"  {rank:<3}  {cn_t:<32}  {sp2:>12.4f}  {dp2:>+7.4f}  {pi2:>+9.4f}  {shr:>6}\n",
              'tr_a' if rank % 2 else 'tr_b')
        if len(rows) > 5:
            rest_pi = sum(r[6] for r in rows[5:])
            rest_sh = f"{abs(rest_pi / total * 100):.1f}%" if total else "\u2014"
            w(f"  ...  {len(rows)-5} further CNECs"
              f"{'':>46}  {rest_pi:>+9.4f}  {rest_sh:>6}\n", 'muted')

        # ── 5. Historical Context ─────────────────────────────────────
        if hist:
            sec(5, "HISTORICAL CONTEXT  \u2014  dominant CNEC across loaded period")
            w("  Derived from the same JAO dataset \u2014 Tabs\u202f3\u202f&\u202f4 show the full time series.\n\n",
              'muted_i')
            w(f"  Observations  :  {hist['n']} MTUs"
              f"   |   binding {hist['n_bind']} times ({hist['bind_pct']:.1f}%)\n", 'body_i')
            w(f"  Shadow price  :  current ", 'body_i')
            w(f"{sp:.4f}", _sp_tag(sp))
            w(f"   |   mean {hist['sp_mean']:.4f}"
              f"   |   peak {hist['sp_max']:.4f} \u20ac/MWh\n", 'muted')
            w(f"  Price impact  :  current ", 'body_i')
            w(f"{impact:+.4f}", _pi_tag(impact))
            w(f"   |   mean {hist['pi_mean']:+.4f}"
              f"   |   peak {hist['pi_peak']:+.4f} \u20ac/MWh\n", 'muted')
            if hist['ram_mean'] > 0:
                w(f"  RAM           :  mean {hist['ram_mean']:.1f}\u202fMW"
                  f"   |   tightest {hist['ram_min']:.1f}\u202fMW over period\n", 'body_i')

            if sp > hist['sp_mean'] * 1.5:
                w("\n  Current \u03bb is ", 'body_i')
                w("significantly above", 'vr')
                w(f" the dataset mean ({hist['sp_mean']:.4f}) \u2014"
                  " this MTU is an elevated congestion event.\n", 'body_i')
            elif hist['sp_mean'] > 0 and sp < hist['sp_mean'] * 0.5:
                w("\n  Current \u03bb is ", 'body_i')
                w("below the dataset mean", 'vg')
                w(f" ({hist['sp_mean']:.4f}) \u2014"
                  " lighter congestion than typical for this CNEC.\n", 'body_i')
            else:
                w(f"\n  Current \u03bb is in line with the dataset mean"
                  f" ({hist['sp_mean']:.4f}) \u2014 typical congestion level.\n", 'muted_i')

            if hist['bind_pct'] > 75:
                w("  Binding > 75% of observed MTUs \u2014 "
                  "a persistent structural constraint.\n", 'muted_i')
            elif hist['bind_pct'] < 25:
                w("  Binding < 25% of observed MTUs \u2014 "
                  "episodic; context-sensitive.\n", 'muted_i')

        w("\n" + "\u2500" * 72 + "\n", 'doc_rule')
        w(f"Data: JAO Nordic Flow-Based Shadow Prices  |  Generated: {ts_now}\n", 'muted')
        T.config(state='disabled')

    # ------------------------------------------------------------------
    #  PDF Export
    # ------------------------------------------------------------------
    def _export_briefing_pdf(self):
        """Export the current Strategic Briefing as a professional PDF report."""
        if not self._last_briefing:
            messagebox.showinfo("No Data", "Run Analysis first to generate the briefing.")
            return

        if not REPORTLAB_OK:
            messagebox.showerror(
                "Missing Library",
                "reportlab is not installed.\n\nRun:  pip install reportlab")
            return

        b = self._last_briefing
        path = filedialog.asksaveasfilename(
            title="Export Briefing as PDF",
            defaultextension=".pdf",
            filetypes=[("PDF Document", "*.pdf")],
            initialfile=f"SMB_{b['date_fmt']}_{b['z_f']}_{b['z_t']}.pdf")
        if not path:
            return

        try:
            self._build_briefing_pdf(path, b)
            messagebox.showinfo("Export Complete", f"PDF saved to:\n{path}")
        except Exception as exc:
            messagebox.showerror("Export Failed", str(exc))

    def _build_briefing_pdf(self, path, b):
        """Build the PDF using reportlab Platypus."""
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors as rl
        from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
            HRFlowable, KeepTogether)

        # ── Colour palette ────────────────────────────────────────────
        NAVY   = rl.HexColor('#0f2340')
        BLUE   = rl.HexColor('#2563eb')
        LTBLUE = rl.HexColor('#eff6ff')
        SLATE  = rl.HexColor('#334155')
        MUTED  = rl.HexColor('#94a3b8')
        BG     = rl.HexColor('#f8fafc')
        BGHDR  = rl.HexColor('#f1f5f9')
        RULE   = rl.HexColor('#e2e8f0')
        RED    = rl.HexColor('#dc2626')
        AMBER  = rl.HexColor('#d97706')
        GREEN  = rl.HexColor('#059669')
        PILL_SAT_BG  = rl.HexColor('#fee2e2')
        PILL_SAT_FG  = rl.HexColor('#7f1d1d')
        PILL_STR_BG  = rl.HexColor('#fef3c7')
        PILL_STR_FG  = rl.HexColor('#78350f')
        PILL_SURG_BG = rl.HexColor('#dcfce7')
        PILL_SURG_FG = rl.HexColor('#14532d')

        W, H = A4
        doc = SimpleDocTemplate(
            path,
            pagesize=A4,
            leftMargin=2.2*cm, rightMargin=2.2*cm,
            topMargin=1.5*cm,  bottomMargin=2.2*cm,
        )

        # ── Styles ────────────────────────────────────────────────────
        def sty(name, **kw):
            base = ParagraphStyle(name, fontName='Helvetica',
                                  fontSize=9, textColor=SLATE,
                                  leading=14, **kw)
            return base

        S = {
            'doc_title': sty('doc_title', fontName='Helvetica-Bold',
                             fontSize=16, textColor=NAVY, leading=20),
            'doc_sub':   sty('doc_sub',   fontSize=8,  textColor=MUTED, leading=12),
            'sec_hdr':   sty('sec_hdr',   fontName='Helvetica-Bold',
                             fontSize=9,  textColor=NAVY,
                             backColor=BGHDR, borderPad=5,
                             spaceBefore=14, spaceAfter=4),
            'body':      sty('body',      fontSize=9, textColor=SLATE, leading=13),
            'body_i':    sty('body_i',    fontSize=9, textColor=SLATE,
                             leading=13, leftIndent=14),
            'muted':     sty('muted',     fontSize=7.5, textColor=MUTED, leading=11),
            'muted_i':   sty('muted_i',   fontSize=7.5, textColor=MUTED,
                             leading=11, leftIndent=14),
            'fm_lbl':    sty('fm_lbl',    fontName='Helvetica-Bold',
                             fontSize=8,  textColor=BLUE,
                             leading=12,  leftIndent=14, spaceBefore=8),
            'fm':        sty('fm',        fontName='Courier',
                             fontSize=8,  textColor=BLUE,
                             leading=12,  leftIndent=28, backColor=LTBLUE),
            'fm_c':      sty('fm_c',      fontName='Courier',
                             fontSize=7,  textColor=MUTED,
                             leading=11,  leftIndent=28, backColor=LTBLUE),
            'footer':    sty('footer',    fontSize=7, textColor=MUTED,
                             alignment=TA_CENTER),
        }

        rows  = b['rows']
        total = b['total']
        z_f   = b['z_f']
        z_t   = b['z_t']
        top   = b['top']
        hist  = b['hist']

        def _pi_col(v):
            if abs(v) > 5: return RED
            if abs(v) > 1: return AMBER
            return GREEN
        def _sp_col(v):
            if v > 50: return RED
            if v > 10: return AMBER
            if v > 0:  return GREEN
            return SLATE

        def hr():
            return HRFlowable(width='100%', thickness=0.5,
                              color=RULE, spaceAfter=4, spaceBefore=4)

        def sec(num, title):
            return Paragraph(f"{num}.  {title}", S['sec_hdr'])

        story = []

        # ── Cover header ──────────────────────────────────────────────
        hdr_data = [[
            Paragraph("STRATEGIC MARKET BRIEFING", S['doc_title']),
            Paragraph(f"Generated<br/>{b['ts_now']}", S['muted']),
        ]]
        hdr_tbl = Table(hdr_data, colWidths=[13*cm, 4*cm])
        hdr_tbl.setStyle(TableStyle([
            ('VALIGN',      (0,0), (-1,-1), 'BOTTOM'),
            ('ALIGN',       (1,0), (1,0),   'RIGHT'),
            ('BOTTOMPADDING',(0,0),(-1,-1), 8),
        ]))
        story.append(hdr_tbl)
        story.append(Paragraph(
            f"Nordic Flow-Based Domain  \u2014  {b['date_fmt']}  {b['mtu_disp']} CET"
            f"  \u2014  {z_f} \u2192 {z_t}",
            S['doc_sub']))
        story.append(hr())
        story.append(Spacer(1, 6))

        # ── KPI summary table ─────────────────────────────────────────
        kpi_data = [
            ['Corridor', 'MTU (CET)', 'Active CNECs', 'Total \u0394P', 'Regime'],
            [
                f"{z_f} \u2192 {z_t}",
                f"{b['date_fmt']}  {b['mtu_disp']}",
                str(b['n_active']),
                f"{total:+.4f} \u20ac/MWh",
                b['regime'],
            ],
        ]
        kpi_tbl = Table(kpi_data, colWidths=[3.2*cm, 3.8*cm, 2.8*cm, 4*cm, 5*cm])
        kpi_tbl.setStyle(TableStyle([
            ('BACKGROUND',  (0,0), (-1,0), NAVY),
            ('TEXTCOLOR',   (0,0), (-1,0), rl.white),
            ('FONTNAME',    (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',    (0,0), (-1,-1), 8),
            ('BACKGROUND',  (0,1), (-1,1), BG),
            ('FONTNAME',    (0,1), (-1,1), 'Helvetica-Bold'),
            ('TEXTCOLOR',   (0,1), (-1,1), NAVY),
            ('ALIGN',       (0,0), (-1,-1), 'CENTER'),
            ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
            ('ROWBACKGROUNDS', (0,0), (-1,-1), [NAVY, BG]),
            ('BOX',         (0,0), (-1,-1), 0.5, RULE),
            ('INNERGRID',   (0,0), (-1,-1), 0.25, RULE),
            ('TOPPADDING',  (0,0), (-1,-1), 6),
            ('BOTTOMPADDING',(0,0),(-1,-1), 6),
        ]))
        story.append(kpi_tbl)
        story.append(Spacer(1, 12))

        if b['n_active'] == 0:
            story.append(Paragraph(
                "No binding constraints in this MTU. The corridor is uncongested.",
                S['body']))
            doc.build(story, onFirstPage=self._pdf_page_footer,
                      onLaterPages=self._pdf_page_footer)
            return

        # ── 1. Situation Assessment ───────────────────────────────────
        spread_dir = (f"{z_t} clears at a premium over {z_f}"
                      if total >= 0 else f"{z_f} clears at a premium over {z_t}")
        story.append(sec(1, "SITUATION ASSESSMENT"))
        story.append(Paragraph(
            f"The flow-based domain resolved with <b>{b['n_active']}</b> active CNECs "
            f"carrying a non-zero shadow rent. Total structural price spread [eq.\u202f6]:"
            f"  <b>\u0394P = {total:+.4f} \u20ac/MWh</b>  ({spread_dir}).",
            S['body']))
        story.append(Spacer(1, 6))

        # ── 2. Dominant Constraint ────────────────────────────────────
        if top:
            cne, czf, czt, sp, pf, pt, impact = top
            dptdf     = pf - pt
            share_pct = abs(impact / total * 100) if total else 0.0

            story.append(sec(2, "DOMINANT CONSTRAINT"))

            ram_now = next((self._safe_float(d, 'ram')
                            for d in self.raw_filtered_data
                            if d.get('cneName') == cne
                            and str(d.get('date')) == b['d_str']
                            and d.get('time') == b['t_str']), None)
            ram_str = f"{ram_now:.1f} MW" if ram_now is not None else "\u2014"

            dc_data = [
                ['CNEC', cne],
                ['Bidding Zones', f"{czf} \u2192 {czt}"],
                ['Shadow Price \u03bb', f"{sp:.4f} EUR/MWh"],
                ['PTDF (' + z_f + ')', f"{pf:+.4f}"],
                ['PTDF (' + z_t + ')', f"{pt:+.4f}"],
                ['\u0394PTDF', f"{dptdf:+.4f}"],
                ['Price Impact PI', f"{impact:+.4f} EUR/MWh  ({share_pct:.1f}% of \u0394P)"],
                ['RAM (this MTU)', ram_str],
            ]
            dc_tbl = Table(dc_data, colWidths=[4.5*cm, 13.5*cm])
            dc_tbl.setStyle(TableStyle([
                ('FONTNAME',    (0,0), (0,-1), 'Helvetica-Bold'),
                ('FONTSIZE',    (0,0), (-1,-1), 8),
                ('TEXTCOLOR',   (0,0), (0,-1), NAVY),
                ('TEXTCOLOR',   (1,0), (1,-1), SLATE),
                ('ROWBACKGROUNDS',(0,0),(-1,-1),[BG, rl.white]),
                ('BOX',         (0,0), (-1,-1), 0.5, RULE),
                ('INNERGRID',   (0,0), (-1,-1), 0.25, RULE),
                ('TOPPADDING',  (0,0), (-1,-1), 4),
                ('BOTTOMPADDING',(0,0),(-1,-1), 4),
                ('LEFTPADDING', (0,0), (-1,-1), 8),
            ]))
            story.append(dc_tbl)
            story.append(Spacer(1, 6))

            story.append(Paragraph("[eq.\u202f5]  PI  =  \u03bb \u00b7 \u0394PTDF", S['fm_lbl']))
            story.append(Paragraph(
                f"       =  {sp:.4f}  \u00d7  ({pf:+.4f} \u2212 {pt:+.4f})"
                f"  =  {sp:.4f}  \u00d7  {dptdf:+.4f}  =  {impact:+.4f} EUR/MWh",
                S['fm']))

            coup = ("positively coupled \u2014 commercial flow loads the constraint"
                    if dptdf > 0.05 else
                    "counter-flow \u2014 commercial flow relieves the constraint"
                    if dptdf < -0.05 else
                    "electrically decoupled \u2014 near-zero \u0394PTDF")
            story.append(Paragraph(
                f"A 100\u202fMW trade {z_f}\u2192{z_t} loads {dptdf*100:+.1f}\u202fMW "
                f"onto this CNEC ({coup}).", S['muted_i']))
            story.append(Spacer(1, 8))

        # ── 3. Constraint Character ───────────────────────────────────
        story.append(sec(3, "CONSTRAINT CHARACTER"))
        if sp > 50:
            pill_color, pill_bg = PILL_SAT_FG, PILL_SAT_BG
            pill_label = "SYSTEMIC SATURATION"
            narrative = (
                "\u03bb > 50 signals the unconstrained welfare optimum lies far inside the "
                "infeasible region. The constraint forces the market into a qualitatively "
                "different zonal price formation. Cross-zonal capacity is heavily rationed; "
                "the spread is robust and unlikely to be arbitraged intraday without capacity release.")
        elif sp > 10:
            pill_color, pill_bg = PILL_STR_FG, PILL_STR_BG
            pill_label = "STRUCTURAL BOTTLENECK"
            narrative = (
                "\u03bb \u2208 (10, 50]: binding and well-defined, but the market retains "
                "enough degrees of freedom to partially compensate. The spread persists "
                "across similar dispatch patterns. Intraday correction requires generation "
                "redispatch or significant demand response to shift reference flows.")
        else:
            pill_color, pill_bg = PILL_SURG_FG, PILL_SURG_BG
            pill_label = "SURGICAL BOTTLENECK"
            narrative = (
                "\u03bb \u2264 10: the optimum sits just outside the feasibility set. "
                "A small RAM relaxation \u2014 or a modest wind/hydro ramp \u2014 renders "
                "this constraint non-binding. The spread is commercially thin and "
                "sensitive to operational changes.")

        pill_data = [[f"  {pill_label}  ", f"\u03bb = {sp:.4f} EUR/MWh"]]
        pill_tbl  = Table(pill_data, colWidths=[6*cm, 11.8*cm])
        pill_tbl.setStyle(TableStyle([
            ('BACKGROUND',  (0,0), (0,0), pill_bg),
            ('TEXTCOLOR',   (0,0), (0,0), pill_color),
            ('FONTNAME',    (0,0), (-1,-1), 'Helvetica-Bold'),
            ('FONTSIZE',    (0,0), (-1,-1), 8),
            ('TEXTCOLOR',   (1,0), (1,0), MUTED),
            ('FONTNAME',    (1,0), (1,0), 'Helvetica'),
            ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
            ('TOPPADDING',  (0,0), (-1,-1), 5),
            ('BOTTOMPADDING',(0,0),(-1,-1), 5),
            ('LEFTPADDING', (0,0), (-1,-1), 10),
        ]))
        story.append(pill_tbl)
        story.append(Paragraph(narrative, S['body_i']))
        story.append(Spacer(1, 8))

        # ── 4. Constraint Landscape ───────────────────────────────────
        story.append(sec(4, "CONSTRAINT LANDSCAPE"))
        top5    = rows[:min(5, len(rows))]
        cl_data = [['#', 'CNEC', '\u03bb (EUR/MWh)', '\u0394PTDF', 'PI (EUR/MWh)', 'Share']]
        for rank, (cn, czf2, czt2, sp2, pf2, pt2, pi2) in enumerate(top5, 1):
            dp2  = pf2 - pt2
            shr  = f"{abs(pi2 / total * 100):.1f}%" if total else "\u2014"
            cn_t = (str(cn)[:38] + '..') if len(str(cn)) > 40 else str(cn)
            cl_data.append([str(rank), cn_t, f"{sp2:.4f}", f"{dp2:+.4f}",
                            f"{pi2:+.4f}", shr])
        if len(rows) > 5:
            rest_pi = sum(r[6] for r in rows[5:])
            rest_sh = f"{abs(rest_pi / total * 100):.1f}%" if total else "\u2014"
            cl_data.append([f"…+{len(rows)-5}", "(remaining)", "", "", f"{rest_pi:+.4f}", rest_sh])

        cl_tbl = Table(cl_data, colWidths=[0.8*cm, 6.8*cm, 3*cm, 2.2*cm, 3.2*cm, 1.8*cm])
        cl_style = [
            ('BACKGROUND',  (0,0), (-1,0), NAVY),
            ('TEXTCOLOR',   (0,0), (-1,0), rl.white),
            ('FONTNAME',    (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE',    (0,0), (-1,-1), 7.5),
            ('ALIGN',       (2,0), (-1,-1), 'CENTER'),
            ('ALIGN',       (0,0), (1,-1), 'LEFT'),
            ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[BG, rl.white]),
            ('BOX',         (0,0), (-1,-1), 0.5, RULE),
            ('INNERGRID',   (0,0), (-1,-1), 0.25, RULE),
            ('TOPPADDING',  (0,0), (-1,-1), 4),
            ('BOTTOMPADDING',(0,0),(-1,-1), 4),
            ('LEFTPADDING', (0,0), (-1,-1), 6),
        ]
        cl_tbl.setStyle(TableStyle(cl_style))
        story.append(cl_tbl)
        story.append(Spacer(1, 8))

        # ── 5. Historical Context ─────────────────────────────────────
        if hist:
            story.append(sec(5, "HISTORICAL CONTEXT  \u2014  dominant CNEC across loaded period"))
            story.append(Paragraph(
                "Derived from the same JAO dataset \u2014 Tabs 3 & 4 show the full time series.",
                S['muted_i']))

            hc_data = [
                ['Metric', 'Current MTU', 'Dataset Mean', 'Dataset Peak'],
                ['Shadow Price \u03bb',
                 f"{sp:.4f}",
                 f"{hist['sp_mean']:.4f}",
                 f"{hist['sp_max']:.4f}"],
                ['Price Impact PI',
                 f"{impact:+.4f}",
                 f"{hist['pi_mean']:+.4f}",
                 f"{hist['pi_peak']:+.4f}"],
                ['RAM (MW)',
                 ram_str if top else "\u2014",
                 f"{hist['ram_mean']:.1f}" if hist['ram_mean'] else "\u2014",
                 f"{hist['ram_min']:.1f}" if hist['ram_min'] else "\u2014"],
                ['Binding frequency',
                 f"{hist['bind_pct']:.1f}%",
                 f"{hist['n_bind']} / {hist['n']} MTUs", ""],
            ]
            hc_tbl = Table(hc_data, colWidths=[4.5*cm, 3.5*cm, 3.5*cm, 3.5*cm])
            hc_tbl.setStyle(TableStyle([
                ('BACKGROUND',  (0,0), (-1,0), NAVY),
                ('TEXTCOLOR',   (0,0), (-1,0), rl.white),
                ('FONTNAME',    (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE',    (0,0), (-1,-1), 8),
                ('FONTNAME',    (0,1), (0,-1), 'Helvetica-Bold'),
                ('TEXTCOLOR',   (0,1), (0,-1), NAVY),
                ('ALIGN',       (1,0), (-1,-1), 'CENTER'),
                ('ALIGN',       (0,0), (0,-1), 'LEFT'),
                ('VALIGN',      (0,0), (-1,-1), 'MIDDLE'),
                ('ROWBACKGROUNDS',(0,1),(-1,-1),[BG, rl.white]),
                ('BOX',         (0,0), (-1,-1), 0.5, RULE),
                ('INNERGRID',   (0,0), (-1,-1), 0.25, RULE),
                ('TOPPADDING',  (0,0), (-1,-1), 4),
                ('BOTTOMPADDING',(0,0),(-1,-1), 4),
                ('LEFTPADDING', (0,0), (-1,-1), 8),
            ]))
            story.append(Spacer(1, 4))
            story.append(hc_tbl)

        # ── Footer rule ───────────────────────────────────────────────
        story.append(Spacer(1, 16))
        story.append(hr())
        story.append(Paragraph(
            f"Source: JAO Publication Tool \u2014 Nordic Flow-Based Shadow Prices  "
            f"|  Generated: {b['ts_now']}",
            S['footer']))

        doc.build(story, onFirstPage=self._pdf_page_footer,
                  onLaterPages=self._pdf_page_footer)

    @staticmethod
    def _pdf_page_footer(canvas, doc):
        """Draw page number footer on every PDF page."""
        from reportlab.lib.units import cm
        from reportlab.lib import colors as rl
        canvas.saveState()
        canvas.setFont('Helvetica', 7)
        canvas.setFillColor(rl.HexColor('#94a3b8'))
        canvas.drawString(2.2*cm, 1.2*cm,
                          "JAO Nordic FB Domain  \u2014  Strategic Market Briefing")
        canvas.drawRightString(
            doc.pagesize[0] - 2.2*cm, 1.2*cm,
            f"Page {doc.page}")
        canvas.restoreState()
    # ------------------------------------------------------------------
    #  TAB 3 – Shadow / RAM
    # ------------------------------------------------------------------
    def _create_tab3_widgets(self):
        f = ttk.Frame(self.tab3, style='Card.TFrame', padding=12)
        f.pack(fill=tk.BOTH, expand=True)
        self.cnec_title_3 = tk.StringVar(value="Select a CNEC row in the Analysis tab")
        ttk.Label(f, textvariable=self.cnec_title_3, style='H1.TLabel').pack(anchor='w', pady=(0, 4))
        self.toolbar_f3 = ttk.Frame(f, style='Card.TFrame')
        self.toolbar_f3.pack(fill=tk.X)
        self.fig3 = Figure(figsize=(11, 4.5), dpi=90)
        self._style_figure(self.fig3)
        self.canvas3 = FigureCanvasTkAgg(self.fig3, f)
        self.canvas3.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    #  TAB 4 – Impact / PTDF / FALL
    # ------------------------------------------------------------------
    def _create_tab4_widgets(self):
        f = ttk.Frame(self.tab4, style='Card.TFrame', padding=12)
        f.pack(fill=tk.BOTH, expand=True)
        self.cnec_title_4 = tk.StringVar(value="Select a CNEC row in the Analysis tab")
        ttk.Label(f, textvariable=self.cnec_title_4, style='H1.TLabel').pack(anchor='w', pady=(0, 4))
        self.toolbar_f4 = ttk.Frame(f, style='Card.TFrame')
        self.toolbar_f4.pack(fill=tk.X)
        self.fig4 = Figure(figsize=(11, 8), dpi=90)
        self._style_figure(self.fig4)
        self.canvas4 = FigureCanvasTkAgg(self.fig4, f)
        self.canvas4.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    #  TAB 5 – Gen/Cons vs Shadow/RAM
    # ------------------------------------------------------------------
    def _create_tab5_widgets(self):
        main = ttk.Frame(self.tab5, style='Card.TFrame', padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        ctrl = ttk.Frame(main, style='Card.TFrame')
        ctrl.pack(fill=tk.X, pady=(0, 4))

        ttk.Label(ctrl, text="Location (e.g. NO, SE3):").pack(side=tk.LEFT)
        self.np_location_entry = ttk.Entry(ctrl, width=9)
        self.np_location_entry.insert(0, "NO")
        self.np_location_entry.pack(side=tk.LEFT, padx=(4, 0))

        self.np_areas_label = ttk.Label(ctrl, text="", style='Muted.TLabel')
        self.np_areas_label.pack(side=tk.LEFT, padx=(8, 20))

        ttk.Label(ctrl, text="Type:").pack(side=tk.LEFT)
        self.gen_type_var = tk.StringVar(value="Wind")
        self.gen_combo = ttk.Combobox(ctrl, textvariable=self.gen_type_var, width=14,
                                      values=["Wind", "Solar", "Hydro", "FossilGas", "Total", "Consumption"])
        self.gen_combo.pack(side=tk.LEFT, padx=(4, 16))
        self._tab5_btn = ttk.Button(ctrl, text="Sync Nordpool & Plot", style='Accent.TButton',
                                    command=self._plot_tab5)
        self._tab5_btn.pack(side=tk.LEFT)
        self._tab5_status = ttk.Label(ctrl, text="", style='Muted.TLabel')
        self._tab5_status.pack(side=tk.LEFT, padx=(10, 0))

        self.toolbar_f5 = ttk.Frame(main, style='Card.TFrame')
        self.toolbar_f5.pack(fill=tk.X)
        self.fig5 = Figure(figsize=(11, 8), dpi=90)
        self._style_figure(self.fig5)
        self.canvas5 = FigureCanvasTkAgg(self.fig5, main)
        self.canvas5.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    #  TAB 6 – Price History
    # ------------------------------------------------------------------
    def _create_tab6_widgets(self):
        main = ttk.Frame(self.tab6, style='Card.TFrame', padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        ctrl = ttk.Frame(main, style='Card.TFrame')
        ctrl.pack(fill=tk.X, pady=(0, 4))

        # Zone selector — validated against NORDIC_ZONES to prevent silent empty results.
        # Free-text entry was removed because invalid zone names cause the NordPool API
        # to return an empty response with no error, leaving the chart blank silently.
        ttk.Label(ctrl, text="Zones:").pack(side=tk.LEFT)
        self._t6_zone_vars = {}
        zone_row = ttk.Frame(ctrl, style='Card.TFrame')
        zone_row.pack(side=tk.LEFT, padx=(4, 16))
        self._np_zones_checkboxes = {}
        default_zones = {"SE3", "SE4"}
        for zone in NORDIC_ZONES:
            var = tk.BooleanVar(value=(zone in default_zones))
            cb  = ttk.Checkbutton(zone_row, text=zone, variable=var)
            cb.pack(side=tk.LEFT, padx=2)
            self._t6_zone_vars[zone] = var

        # Keep the entry as a hidden alias for backward compat with _plot_tab6 code
        self.np_zones_entry = None   # replaced by checkboxes

        self._tab6_btn = ttk.Button(ctrl, text="Show Price History", style='Accent.TButton',
                                    command=self._plot_tab6)
        self._tab6_btn.pack(side=tk.LEFT)
        self._tab6_status = ttk.Label(ctrl, text="", style='Muted.TLabel')
        self._tab6_status.pack(side=tk.LEFT, padx=(10, 0))

        self.toolbar_f6 = ttk.Frame(main, style='Card.TFrame')
        self.toolbar_f6.pack(fill=tk.X)
        self.fig6 = Figure(figsize=(11, 6), dpi=90)
        self._style_figure(self.fig6)
        self.canvas6 = FigureCanvasTkAgg(self.fig6, main)
        self.canvas6.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    #  TYPE MAP
    # ------------------------------------------------------------------
    _PROD_TYPE_MAP = {
        "Wind":      "windOnshore",
        "Solar":     "solar",
        "Hydro":     "hydroWaterReservoir",
        "FossilGas": "fossilGas",
        "Nuclear":   "nuclear",
    }

    # ------------------------------------------------------------------
    #  PLOT – Tab 5
    # ------------------------------------------------------------------
    def _plot_tab5(self):
        cnec = self._get_selected_cnec()
        if not cnec:
            messagebox.showinfo("Info", "Select a CNEC in Tab 2 (tree or dropdown).")
            return
        area  = self.np_location_entry.get().strip()
        dtype = self.gen_type_var.get()

        data = sorted([d for d in self.raw_filtered_data if d.get('cneName') == cnec],
                      key=lambda x: (str(x['date']), x['time']))
        if not data:
            return

        self._tab5_btn.config(state=tk.DISABLED, text="⏳  Loading…")
        self._tab5_status.config(text="Fetching Nordpool data…")
        self._show_chart_loading(self.fig5, self.canvas5, "Fetching Nordpool data, please wait…")

        threading.Thread(target=self._plot_tab5_thread,
                         args=(cnec, area, dtype, data), daemon=True).start()

    def _plot_tab5_thread(self, cnec, area, dtype, data):
        country = re.sub(r'\d+$', '', area).upper()
        url     = CONS_URL if dtype == "Consumption" else PROD_URL
        api_key = self._PROD_TYPE_MAP.get(dtype)

        token = get_np_access_token()
        if not token:
            self.root.after(0, self._tab5_done,
                            None, None, None, None, None, None, None,
                            "Could not get NordPool token.")
            return

        np_vals, dates      = {}, sorted(set(d['date'] for d in data))
        delivery_areas_seen = []
        api_errors          = []

        for d_str in dates:
            fmt_date = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}"
            try:
                r = requests.get(url, headers=self._np_headers(token),
                                 params={'date': fmt_date, 'locations': country},
                                 timeout=10)
            except Exception as exc:
                api_errors.append(f"{fmt_date}: request error ({exc})")
                continue
            if r.status_code != 200:
                api_errors.append(f"{fmt_date}: HTTP {r.status_code}")
                continue
            raw = r.json()
            if not raw:
                continue
            loc = raw[0]
            if not delivery_areas_seen:
                delivery_areas_seen = loc.get('deliveryAreas', [])
            entries = (loc.get('consumptions', []) if dtype == "Consumption"
                       else loc.get('productions', []))
            for e in entries:
                ts = e.get('deliveryStart', '')
                if not ts:
                    continue
                try:
                    np_vals[_cet_key(_utc_to_cet(ts))] = (
                        e.get('volume', 0) if dtype == "Consumption"
                        else e.get('total', 0) if dtype == "Total"
                        else e.get('byType', {}).get(api_key, 0)
                    )
                except Exception:
                    continue

        error_msg = (f"{len(api_errors)} date(s) failed: {api_errors[0]}"
                     if api_errors else None)
        self.root.after(0, self._tab5_done,
                        cnec, area, country, dtype, data,
                        np_vals, delivery_areas_seen, error_msg)

    def _tab5_done(self, cnec, area_input, country, dtype, data,
                   np_vals, delivery_areas_seen, error=None):
        self._tab5_btn.config(state=tk.NORMAL, text="Sync Nordpool & Plot")
        if error:
            self._tab5_status.config(text=f"⚠ {error}", foreground=C_AMBER)
            if cnec is None:
                messagebox.showerror("Tab 5 Error", error)
                return
        else:
            self._tab5_status.config(text="")

        if delivery_areas_seen:
            self.np_areas_label.config(text=f"Areas: {', '.join(delivery_areas_seen)}")

        xs     = [f"{str(d['date'])[-4:]} {d['time'][:5]}" for d in data]
        y_gen  = [np_vals.get(f"{d['date']}_{d['time'][:5]}", 0) for d in data]
        y_ram  = [self._safe_float(d, 'ram')         for d in data]
        y_sp   = [self._safe_float(d, 'shadowPrice') for d in data]
        x_idx  = range(len(xs))
        area_label = f"{area_input} ({country})" if area_input != country else country

        self.fig5.clear()
        ax1 = self.fig5.add_subplot(211)
        ax2 = self.fig5.add_subplot(212)

        ax1.fill_between(x_idx, y_gen, alpha=0.12, color=C_GREEN)
        ax1.plot(x_idx, y_gen, color=C_GREEN, linewidth=1.8, label=f"{dtype} (MW)")
        ax1.set_ylabel(f"{dtype} (MW)", color=C_GREEN, fontsize=8.5)
        ax1b = ax1.twinx()
        ax1b.step(x_idx, y_ram, color=C_PRIMARY, where='post', alpha=0.8, linewidth=1.5)
        ax1b.fill_between(x_idx, y_ram, alpha=0.06, color=C_PRIMARY, step='post')
        ax1b.set_ylabel("RAM (MW)", color=C_PRIMARY, fontsize=8.5)
        self._style_twin(ax1b, C_PRIMARY)
        ax1.set_title(f"{cnec}  —  {dtype} ({area_label}) vs RAM")
        ax1.set_xticks(list(x_idx)); ax1.set_xticklabels(xs)
        self._setup_ax(ax1, xs); self._legend(ax1)

        ax2.fill_between(x_idx, y_gen, alpha=0.12, color=C_GREEN)
        ax2.plot(x_idx, y_gen, color=C_GREEN, linewidth=1.8, label=f"{dtype} (MW)")
        ax2.set_ylabel(f"{dtype} (MW)", color=C_GREEN, fontsize=8.5)
        ax2b = ax2.twinx()
        ax2b.plot(x_idx, y_sp, color=C_RED, marker='o', markersize=2.5,
                  linewidth=1.2, label="Shadow Price")
        ax2b.fill_between(x_idx, y_sp, alpha=0.08, color=C_RED)
        ax2b.set_ylabel("Shadow Price (€/MWh)", color=C_RED, fontsize=8.5)
        self._style_twin(ax2b, C_RED)
        ax2.set_title(f"{dtype} ({area_label}) vs Shadow Price")
        ax2.set_xticks(list(x_idx)); ax2.set_xticklabels(xs)
        self._setup_ax(ax2, xs); self._legend(ax2)

        self.fig5.tight_layout(pad=2.0)
        self._add_toolbar(self.canvas5, self.toolbar_f5)

    # ------------------------------------------------------------------
    #  PLOT – Tab 6
    # ------------------------------------------------------------------
    def _plot_tab6(self):
        # Read zones from checkboxes (validated against NORDIC_ZONES)
        zones = [z for z, var in self._t6_zone_vars.items() if var.get()]
        if not zones:
            messagebox.showinfo("Info", "Select at least one zone.")
            return
        if not self.raw_filtered_data:
            messagebox.showinfo("Info", "Load JAO data first.")
            return

        self._tab6_btn.config(state=tk.DISABLED, text="⏳  Loading…")
        self._tab6_status.config(text="Fetching Nordpool prices…")
        self._show_chart_loading(self.fig6, self.canvas6, "Fetching Nordpool prices, please wait…")

        dates = sorted(set(d['date'] for d in self.raw_filtered_data))
        threading.Thread(target=self._plot_tab6_thread,
                         args=(zones, dates), daemon=True).start()

    def _plot_tab6_thread(self, zones, dates):
        token = get_np_access_token()
        if not token:
            self.root.after(0, self._tab6_done, zones, None,
                            "Could not get NordPool token.")
            return

        series         = {}
        api_errors     = []
        unified_labels = []

        for zone in zones:
            p_list, l_list = [], []
            for d_str in dates:
                fmt_date = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}"
                try:
                    r = requests.get(NP_PRICE_URL,
                                     headers=self._np_headers(token),
                                     params={'date': fmt_date, 'areas': zone,
                                             'currency': 'EUR', 'market': 'DayAhead'},
                                     timeout=10)
                except Exception as exc:
                    api_errors.append(f"{zone}/{fmt_date}: request error ({exc})")
                    continue
                if r.status_code != 200:
                    api_errors.append(f"{zone}/{fmt_date}: HTTP {r.status_code}")
                    continue
                area_data = next((x for x in r.json()
                                  if x.get('deliveryArea') == zone), None)
                if not area_data:
                    continue
                for p in area_data.get('prices', []):
                    price_val = p.get('price')
                    if price_val is None:
                        continue
                    p_list.append(price_val)
                    ts = p.get('deliveryStart', '')
                    try:
                        label = (f"{_utc_to_cet(ts).strftime('%m%d')} "
                                 f"{_utc_to_cet(ts).strftime('%H:%M')}")
                    except Exception:
                        label = f"{d_str[-4:]} {ts[11:16]}"
                    l_list.append(label)
            series[zone] = (p_list, l_list)
            if len(l_list) > len(unified_labels):
                unified_labels = l_list

        error_msg = (f"{len(api_errors)} request(s) failed: {api_errors[0]}"
                     if api_errors else None)
        self.root.after(0, self._tab6_done, zones, series, error_msg, unified_labels)

    def _tab6_done(self, zones, series, error=None, unified_labels=None):
        self._tab6_btn.config(state=tk.NORMAL, text="Show Price History")
        if error:
            self._tab6_status.config(text=f"⚠ {error}", foreground=C_AMBER)
            if series is None:
                messagebox.showerror("Tab 6 Error", error)
                return
        else:
            self._tab6_status.config(text="")

        unified_labels = unified_labels or []
        n_labels = len(unified_labels)

        self.fig6.clear()
        ax = self.fig6.add_subplot(111)
        found_any = False

        for i, zone in enumerate(zones):
            p_list, l_list = series.get(zone, ([], []))
            if not p_list:
                continue
            color = CHART_PALETTE[i % len(CHART_PALETTE)]
            n = len(p_list)
            # All zones share the same integer x-axis; shorter series plot on a leading subset
            x_vals = list(range(n))
            ax.plot(x_vals, p_list, label=zone,
                    color=color, marker='.', markersize=3.5, linewidth=1.4)
            ax.fill_between(x_vals, p_list, alpha=0.06, color=color)
            found_any = True

        if found_any:
            self._legend(ax)
            # Show readable date/time labels — subsample to ≤24 ticks
            if unified_labels:
                step = max(1, n_labels // 24)
                tick_pos    = list(range(0, n_labels, step))
                tick_labels = [unified_labels[j] for j in tick_pos]
                ax.set_xticks(tick_pos)
                ax.set_xticklabels(tick_labels, rotation=35, fontsize=7, ha='right')

        ax.set_title("Day-Ahead Market Price History")
        ax.set_ylabel("EUR / MWh", fontsize=8.5)
        # FIX B: pass unified_labels so x-axis date/time labels render on zoom
        self._setup_ax(ax, unified_labels)
        self.fig6.tight_layout(pad=2.0)
        self._add_toolbar(self.canvas6, self.toolbar_f6)

    # ------------------------------------------------------------------
    #  HELPERS
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    #  TAB 7 – Net Position
    # ------------------------------------------------------------------
    def _create_tab7_widgets(self):
        main = ttk.Frame(self.tab7, style='Card.TFrame', padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        ctrl = ttk.Frame(main, style='Card.TFrame')
        ctrl.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(ctrl, text="Zone:").pack(side=tk.LEFT)
        self._t7_zone = ttk.Entry(ctrl, width=8)
        self._t7_zone.insert(0, "SE3")
        self._t7_zone.pack(side=tk.LEFT, padx=(4, 16))

        self._t7_btn = ttk.Button(ctrl, text="Fetch & Plot", style='Accent.TButton',
                                  command=self._plot_tab7)
        self._t7_btn.pack(side=tk.LEFT)
        self._t7_status = ttk.Label(ctrl, text="", style='Muted.TLabel')
        self._t7_status.pack(side=tk.LEFT, padx=(10, 0))

        self.toolbar_f7 = ttk.Frame(main, style='Card.TFrame')
        self.toolbar_f7.pack(fill=tk.X)
        self.fig7 = Figure(figsize=(11, 9), dpi=90)
        self._style_figure(self.fig7)
        self.canvas7 = FigureCanvasTkAgg(self.fig7, main)
        self.canvas7.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _plot_tab7(self):
        zone = self._t7_zone.get().strip()
        if not zone:
            messagebox.showinfo("Info", "Enter a zone.")
            return
        cnec = self._get_selected_cnec()
        if not cnec:
            messagebox.showinfo("Info", "Select a CNEC in Tab 2 (tree or dropdown).")
            return
        if not self.raw_filtered_data:
            messagebox.showinfo("Info", "Load JAO data first.")
            return

        self._t7_btn.config(state=tk.DISABLED, text="Loading...")
        self._t7_status.config(text="Fetching Nordpool volumes & prices...")
        self._show_chart_loading(self.fig7, self.canvas7, "Fetching data, please wait…")

        dates = sorted(set(d['date'] for d in self.raw_filtered_data))
        threading.Thread(target=self._plot_tab7_thread,
                         args=(zone, cnec, dates), daemon=True).start()

    def _plot_tab7_thread(self, zone, cnec, dates):
        token = get_np_access_token()
        if not token:
            self.root.after(0, self._tab7_done, None, None, None, None,
                            "Could not get NordPool token.")
            return

        hdrs       = self._np_headers(token)
        net_pos    = {}
        price_map  = {}
        api_errors = []

        for d_str in dates:
            fmt = f"{d_str[:4]}-{d_str[4:6]}-{d_str[6:]}"

            # Volumes
            try:
                rv = requests.get(NP_VOL_URL, headers=hdrs,
                                  params={'market': 'DayAhead', 'areas': zone,
                                          'date': fmt}, timeout=10)
                if rv.status_code != 200:
                    api_errors.append(f"vol/{fmt}: HTTP {rv.status_code}")
                else:
                    area_v = next((x for x in rv.json()
                                   if x.get('deliveryArea') == zone), None)
                    if area_v:
                        for v in area_v.get('volumes', []):
                            ts = v.get('deliveryStart', '')
                            if not ts:
                                continue
                            try:
                                key  = _cet_key(_utc_to_cet(ts))
                                sell = float(v.get('sell') or 0)
                                buy  = float(v.get('buy')  or 0)
                                net_pos[key] = sell - buy
                            except (TypeError, ValueError, OSError):
                                pass
            except Exception as exc:
                api_errors.append(f"vol/{fmt}: {exc}")

            # Prices
            try:
                rp = requests.get(NP_PRICE_URL, headers=hdrs,
                                  params={'date': fmt, 'areas': zone,
                                          'currency': 'EUR',
                                          'market': 'DayAhead'}, timeout=10)
                if rp.status_code != 200:
                    api_errors.append(f"price/{fmt}: HTTP {rp.status_code}")
                else:
                    area_p = next((x for x in rp.json()
                                   if x.get('deliveryArea') == zone), None)
                    if area_p:
                        for p in area_p.get('prices', []):
                            ts = p.get('deliveryStart', '')
                            if not ts:
                                continue
                            try:
                                price_val = p.get('price')
                                if price_val is not None:
                                    price_map[_cet_key(_utc_to_cet(ts))] = float(price_val)
                            except (TypeError, ValueError, OSError):
                                pass
            except Exception as exc:
                api_errors.append(f"price/{fmt}: {exc}")

        # JAO CNEC data (already CET)
        jao = sorted([d for d in self.raw_filtered_data
                      if d.get('cneName') == cnec],
                     key=lambda x: (str(x['date']), x['time']))

        error_msg = (f"{len(api_errors)} request(s) failed: {api_errors[0]}"
                     if api_errors else None)
        self.root.after(0, self._tab7_done, zone, cnec, jao, net_pos, price_map, error_msg)

    def _tab7_done(self, zone, cnec, jao, net_pos, price_map, error=None):
        self._t7_btn.config(state=tk.NORMAL, text="Fetch & Plot")
        if error:
            self._t7_status.config(text=f"⚠ {error}", foreground=C_AMBER)
            if zone is None:
                messagebox.showerror("Tab 7 Error", error)
                return
        else:
            self._t7_status.config(text="")

        xs    = [f"{str(d['date'])[-4:]} {d['time'][:5]}" for d in jao]
        x_idx = range(len(xs))
        y_ram = [self._safe_float(d, 'ram')         for d in jao]
        y_sp  = [self._safe_float(d, 'shadowPrice') for d in jao]
        y_np  = [net_pos.get(f"{d['date']}_{d['time'][:5]}", 0)   for d in jao]
        y_prc = [price_map.get(f"{d['date']}_{d['time'][:5]}", 0) for d in jao]

        self.fig7.clear()
        self.fig7.suptitle(f"Net Position  ({zone})  —  CNEC: {cnec}",
                           fontsize=10, fontweight='bold', color=C_ACCENT, y=0.995)

        ax1 = self.fig7.add_subplot(311)
        ax2 = self.fig7.add_subplot(312)
        ax3 = self.fig7.add_subplot(313)

        # ── Chart 1: Price vs Net Position ──────────────────────────
        ax1.plot(x_idx, y_prc, color=C_AMBER, linewidth=1.6,
                 marker='o', markersize=2.5, label="Price (€/MWh)")
        ax1.fill_between(x_idx, y_prc, alpha=0.1, color=C_AMBER)
        ax1.set_ylabel("Price (€/MWh)", color=C_AMBER, fontsize=8.5)
        ax1.tick_params(axis='y', colors=C_AMBER, labelsize=7.5)
        self._plot_net_pos_twin(ax1.twinx(), x_idx, y_np)
        ax1.set_title("Day-Ahead Price vs Net Position")
        ax1.set_xticks(list(x_idx)); ax1.set_xticklabels(xs)
        self._setup_ax(ax1, xs); self._legend(ax1)

        # Chart 2: RAM vs Net Position
        ax2.step(x_idx, y_ram, color=C_GREEN, where='post',
                 linewidth=1.6, label="RAM (MW)")
        ax2.fill_between(x_idx, y_ram, alpha=0.1, color=C_GREEN, step='post')
        ax2.set_ylabel("RAM (MW)", color=C_GREEN, fontsize=8.5)
        ax2.tick_params(axis='y', colors=C_GREEN, labelsize=7.5)
        self._plot_net_pos_twin(ax2.twinx(), x_idx, y_np)
        ax2.set_title("RAM vs Net Position")
        ax2.set_xticks(list(x_idx)); ax2.set_xticklabels(xs)
        self._setup_ax(ax2, xs); self._legend(ax2)

        # Chart 3: Shadow Price vs Net Position
        ax3.plot(x_idx, y_sp, color=C_RED, linewidth=1.6,
                 marker='o', markersize=2.5, label="Shadow Price (€/MWh)")
        ax3.fill_between(x_idx, y_sp, alpha=0.08, color=C_RED)
        ax3.set_ylabel("Shadow Price (€/MWh)", color=C_RED, fontsize=8.5)
        ax3.tick_params(axis='y', colors=C_RED, labelsize=7.5)
        self._plot_net_pos_twin(ax3.twinx(), x_idx, y_np)
        ax3.set_title("Shadow Price vs Net Position")
        ax3.set_xticks(list(x_idx)); ax3.set_xticklabels(xs)
        self._setup_ax(ax3, xs); self._legend(ax3)

        self.fig7.tight_layout(pad=2.0)
        self._add_toolbar(self.canvas7, self.toolbar_f7)

    def _apply_cet_conversion(self, data):
        for entry in data:
            if entry.get('dateTimeUtc'):
                cet_dt = _utc_to_cet(entry['dateTimeUtc'])
                entry['date'] = cet_dt.strftime('%Y%m%d')
                entry['time'] = cet_dt.strftime('%H:%M:%S')
        return data

    def _grid_entry(self, parent, txt, row, val):
        ttk.Label(parent, text=txt).grid(row=row, column=0, sticky='w', pady=2)
        e = ttk.Entry(parent, width=26)
        e.grid(row=row, column=1, padx=(8, 0), pady=2)
        e.insert(0, val)
        return e

    def _select_folder(self):
        self.save_folder = filedialog.askdirectory()
        if self.save_folder:
            self.folder_label.config(text=os.path.basename(self.save_folder) or self.save_folder)

    def _update_status(self, msg):
        self.status_text.config(state='normal')
        ts = datetime.now().strftime('%H:%M:%S')
        self.status_text.insert(tk.END, f"[{ts}]  {msg}\n")
        self.status_text.see(tk.END)

    def _update_data_badge(self):
        n = len(self.raw_filtered_data)
        dates = sorted(set(d['date'] for d in self.raw_filtered_data)) if self.raw_filtered_data else []
        span  = f"  •  {dates[0]} → {dates[-1]}" if len(dates) > 1 else (f"  •  {dates[0]}" if dates else "")
        self.data_badge_var.set(f"  {n:,} records{span}  ")
        self._set_status(f"{n:,} records loaded", datetime.now().strftime('%H:%M:%S'))
        self._refresh_cnec_selector()

    def _upload_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV Files", "*.csv")])
        if not path:
            return
        try:
            with open(path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                loaded_data = []
                for row in reader:
                    for key in list(row.keys()):
                        if (key.lower() in ['shadowprice', 'ram', 'fall', 'flowfb', 'fmax']
                                or key.startswith('ptdf_')):
                            try:
                                row[key] = float(row[key]) if row[key] and row[key].strip() != "" else 0.0
                            except (TypeError, ValueError):
                                row[key] = 0.0
                    loaded_data.append(row)
            self.raw_filtered_data = self._apply_cet_conversion(loaded_data)
            name = os.path.basename(path)
            self.upload_label.config(text=f"✓  {name}", foreground=C_GREEN)
            self._update_status(f"Loaded {len(self.raw_filtered_data):,} records  ←  {name}")
            self._update_data_badge()
            self._sync_date_to_tab2()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _on_tree_select(self, event=None):
        sel = self.analysis_tree.selection()
        if sel:
            cnec = self.analysis_tree.item(sel[0])['values'][0]
            self._cnec_sel_var.set(cnec)

    def _refresh_cnec_selector(self):
        names = sorted(set(d.get('cneName', '') for d in self.raw_filtered_data
                           if d.get('cneName')))
        self._cnec_sel_combo['values'] = names
        # Mirror to Tab 9 whenever Tab 1 data changes
        if hasattr(self, '_ma_target_cnec'):
            self._ma_sync_from_raw_data()

    def _get_selected_cnec(self):
        cnec = self._cnec_sel_var.get().strip()
        if cnec:
            return cnec
        sel = self.analysis_tree.selection()
        if sel:
            return self.analysis_tree.item(sel[0])['values'][0]
        return None

    def _run_analysis(self):
        if not self.raw_filtered_data:
            return
        d_str = self.analysis_date_entry.get().replace('-', '')
        t_str = self.analysis_time_entry.get().strip()
        if len(t_str) == 5:          # "HH:MM" → "HH:MM:00"
            t_str += ':00'

        z_f = self.zone_from_entry.get()
        z_t = self.zone_to_entry.get()

        self.analysis_tree.delete(*self.analysis_tree.get_children())
        rows, total = [], 0.0
        for d in self.raw_filtered_data:
            if str(d.get('date')) == d_str and d.get('time') == t_str:
                sp     = self._safe_float(d, 'shadowPrice')
                pf     = self._safe_float(d, f'ptdf_{z_f}')
                pt     = self._safe_float(d, f'ptdf_{z_t}')
                impact = sp * (pf - pt)
                total += impact
                rows.append((d.get('cneName'), d.get('biddingZoneFrom'),
                             d.get('biddingZoneTo'), sp, pf, pt, impact))
        rows.sort(key=lambda r: abs(r[6]), reverse=True)
        for row_idx, (cne, zf, zt, sp, pf, pt, impact) in enumerate(rows):
            tag = 'evenrow' if row_idx % 2 == 0 else 'oddrow'
            self.analysis_tree.insert('', tk.END, tags=(tag,),
                values=(cne, zf, zt,
                        f"{sp:.4f}", f"{pf:.4f}", f"{pt:.4f}", f"{impact:.4f}"))
        self.total_impact_var.set(f"{total:.4f}")
        self._set_status(f"{len(rows)} CNECs shown  |  {d_str}  {t_str}  |  {z_f} → {z_t}")
        self._refresh_cnec_selector()
        self._update_briefing(rows, total, d_str, t_str, z_f, z_t)

    def _show_all_histories(self):
        cnec = self._get_selected_cnec()
        if not cnec:
            return

        data = sorted([d for d in self.raw_filtered_data if d.get('cneName') == cnec],
                      key=lambda x: (str(x['date']), x['time']))
        if not data:
            return

        # Show loading on both canvases, then defer heavy render by 30 ms
        self._view_graphs_btn.config(state=tk.DISABLED, text="⏳  Loading…")
        self._tab2_status.config(text="Rendering graphs…")
        self.cnec_title_3.set(f"CNEC: {cnec}")
        self.cnec_title_4.set(f"CNEC: {cnec}")
        for fig, canvas in ((self.fig3, self.canvas3), (self.fig4, self.canvas4)):
            fig.clear()
            _ax = fig.add_subplot(111)
            _ax.text(0.5, 0.5, "Rendering, please wait...",
                     transform=_ax.transAxes, ha='center', va='center',
                     fontsize=12, color=C_MUTED)
            _ax.axis('off')
            canvas.draw()
        self.root.update_idletasks()

        z_f, z_t = self.zone_from_entry.get(), self.zone_to_entry.get()
        self.root.after(30, lambda: self._render_histories(cnec, z_f, z_t, data))

    def _render_histories(self, cnec, z_f, z_t, data):
        xs        = [f"{str(d['date'])[-4:]} {d['time'][:5]}" for d in data]
        x_idx     = range(len(xs))
        sp        = [self._safe_float(d, 'shadowPrice') for d in data]
        rams      = [self._safe_float(d, 'ram')         for d in data]
        fall_data = [self._safe_float(d, 'fall')        for d in data]
        pf_l      = [self._safe_float(d, f'ptdf_{z_f}') for d in data]
        pt_l      = [self._safe_float(d, f'ptdf_{z_t}') for d in data]
        imps      = [s * (f - t) for s, f, t in zip(sp, pf_l, pt_l)]
        diffs     = [f - t       for f, t     in zip(pf_l, pt_l)]
        fall_missing = all(v == 0.0 for v in fall_data)

        # ── Tab 3 ──────────────────────────────────────────────────
        self.fig3.clear()
        ax3 = self.fig3.add_subplot(111)
        ax3.plot(x_idx, sp, color=C_RED, linewidth=1.8, marker='o',
                 markersize=3.5, label='Shadow Price', zorder=3)
        ax3.fill_between(x_idx, sp, alpha=0.08, color=C_RED)
        ax3.set_ylabel('Shadow Price (€/MWh)', color=C_RED, fontsize=8.5)
        ax3.tick_params(axis='y', colors=C_RED, labelsize=7.5)
        ax3b = ax3.twinx()
        ax3b.bar(x_idx, rams, color=C_PRIMARY, alpha=0.2, label='RAM', width=0.7)
        ax3b.set_ylabel('RAM (MW)', color=C_PRIMARY, fontsize=8.5)
        self._style_twin(ax3b, C_PRIMARY)
        ax3.set_title(f"Shadow Price & RAM  —  {cnec}")
        ax3.set_xticks(list(x_idx))
        ax3.set_xticklabels(xs)
        self._setup_ax(ax3, xs)
        self._legend(ax3)
        self.fig3.tight_layout(pad=2.0)
        self._add_toolbar(self.canvas3, self.toolbar_f3)

        # ── Tab 4 ──────────────────────────────────────────────────
        self.fig4.clear()
        a1 = self.fig4.add_subplot(311)
        a2 = self.fig4.add_subplot(312)
        a3 = self.fig4.add_subplot(313)

        a1.plot(x_idx, imps, color=C_PURPLE, linewidth=1.8, marker='s',
                markersize=3.5, label='Price Impact')
        a1.fill_between(x_idx, imps, alpha=0.1, color=C_PURPLE)
        a1.axhline(0, color=C_BORDER, linewidth=0.9, linestyle='--')
        a1.set_ylabel("€/MWh", fontsize=8.5)
        a1.set_title("Price Impact")
        a1.set_xticks(list(x_idx)); a1.set_xticklabels(xs)
        self._setup_ax(a1, xs)
        self._legend(a1)

        a2.plot(x_idx, pf_l,  color=C_PRIMARY, linewidth=1.5, label=z_f)
        a2.plot(x_idx, pt_l,  color=C_AMBER,   linewidth=1.5, label=z_t)
        a2.plot(x_idx, diffs, color=C_MUTED,   linewidth=1.0, linestyle='--', label='Δ PTDF')
        a2.set_ylabel("PTDF", fontsize=8.5)
        a2.set_title("PTDF")
        a2.set_xticks(list(x_idx)); a2.set_xticklabels(xs)
        self._setup_ax(a2, xs)
        self._legend(a2)

        a3.plot(x_idx, fall_data, color=C_GREEN, linewidth=1.5,
                marker='o', markersize=2.5, label='FALL')
        a3.fill_between(x_idx, fall_data, alpha=0.1, color=C_GREEN)
        a3.axhline(0, color=C_BORDER, linewidth=0.9, linestyle='--')
        a3.set_ylabel("MW", fontsize=8.5)
        fall_title = "FALL" + ("  ⚠ field absent / all-zero in dataset" if fall_missing else "")
        a3.set_title(fall_title, color=(C_AMBER if fall_missing else C_ACCENT))
        a3.set_xticks(list(x_idx)); a3.set_xticklabels(xs)
        self._setup_ax(a3, xs)

        self.fig4.tight_layout(pad=1.8)
        self._add_toolbar(self.canvas4, self.toolbar_f4)

        self._view_graphs_btn.config(state=tk.NORMAL, text="View Graphs")
        self._tab2_status.config(text="")

    def _start_processing(self):
        if not self.save_folder:
            messagebox.showerror("Error", "Please select a save folder first.")
            return
        params = {
            'start_cet':           f"{self.start_date_entry.get()}T{self.start_time_entry.get()}",
            'end_cet':             f"{self.end_date_entry.get()}T{self.end_time_entry.get()}",
            'output_file':         os.path.join(self.save_folder, self.filename_entry.get()),
            'shadow_price_filter': self.shadow_price_filter_var.get()
        }
        self.run_button.config(state=tk.DISABLED, text="⏳  Fetching…")
        self._fetch_progress['value'] = 0
        self._fetch_pct_var.set("0%")
        self._set_status("Fetching data from JAO…")
        threading.Thread(target=self._fetch_thread, args=(params,), daemon=True).start()

    def _fetch_thread(self, params):
        def _on_progress(done, total):
            pct = int(done / total * 100)
            self.root.after(0, self._fetch_progress.config, {'value': pct})
            self.root.after(0, self._fetch_pct_var.set,
                            f"{done} / {total}  ({pct}%)")
        try:
            data = run_data_fetching_and_processing(
                params,
                status_cb=lambda m: self.root.after(0, self._update_status, m),
                progress_cb=_on_progress,
            )
            self.raw_filtered_data = data
        except Exception as exc:
            import traceback
            self.root.after(0, self._update_status,
                            f"FETCH ERROR: {exc}\n{traceback.format_exc()}")
            self.raw_filtered_data = []
        self.root.after(0, self._on_fetch_done)

    def _on_fetch_done(self):
        self._fetch_progress['value'] = 100
        self._fetch_pct_var.set("Done")
        self.run_button.config(state=tk.NORMAL, text="▶  START FETCH")
        self._update_data_badge()


    # ------------------------------------------------------------------
    #  TAB 8 – Nordic Map (Prices + Flows)
    # ------------------------------------------------------------------
    def _create_tab8_widgets(self):
        main = ttk.Frame(self.tab8, style='Card.TFrame', padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        ctrl = ttk.Frame(main, style='Card.TFrame')
        ctrl.pack(fill=tk.X, pady=(0, 6))

        ttk.Label(ctrl, text="Date:").pack(side=tk.LEFT)
        self._t8_date = ttk.Entry(ctrl, width=12)
        self._t8_date.insert(0, self.today_str)
        self._t8_date.pack(side=tk.LEFT, padx=(4, 16))

        _mtu_slots = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
        ttk.Label(ctrl, text="MTU (CET):").pack(side=tk.LEFT)
        self._t8_mtu = ttk.Combobox(ctrl, values=_mtu_slots, width=7, state='readonly')
        self._t8_mtu.set("12:00")
        self._t8_mtu.pack(side=tk.LEFT, padx=(4, 16))

        self._t8_btn = ttk.Button(ctrl, text="Fetch & Plot", style='Accent.TButton',
                                  command=self._plot_tab8)
        self._t8_btn.pack(side=tk.LEFT)
        self._t8_status = ttk.Label(ctrl, text="", style='Muted.TLabel')
        self._t8_status.pack(side=tk.LEFT, padx=(10, 0))

        self.toolbar_f8 = ttk.Frame(main, style='Card.TFrame')
        self.toolbar_f8.pack(fill=tk.X)
        self.fig8 = Figure(figsize=(8, 8), dpi=95)
        self._style_figure(self.fig8)
        self.canvas8 = FigureCanvasTkAgg(self.fig8, main)
        self.canvas8.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # ── Diagnostic log (collapsible, 5 rows) ──────────────────────
        diag_f = tk.Frame(main, bg='#0f172a', padx=2, pady=2)
        diag_f.pack(fill=tk.X, pady=(4, 0))
        self._t8_diag = tk.Text(
            diag_f, font=('Consolas', 8),
            background='#0f172a', foreground='#94e2d5',
            height=5, relief='flat', borderwidth=0, padx=8, pady=4,
            state='disabled'
        )
        sb8 = tk.Scrollbar(diag_f, command=self._t8_diag.yview, bg='#1e293b')
        self._t8_diag.config(yscrollcommand=sb8.set)
        sb8.pack(side=tk.RIGHT, fill=tk.Y)
        self._t8_diag.pack(fill=tk.X)

    def _plot_tab8(self):
        date_str = self._t8_date.get().strip()
        mtu_str  = self._t8_mtu.get().strip()   # "HH:MM"
        try:
            mtu_h, mtu_m = int(mtu_str[:2]), int(mtu_str[3:])
        except Exception:
            messagebox.showinfo("Info", "Select a valid MTU slot.")
            return

        self._t8_btn.config(state=tk.DISABLED, text="Loading...")
        self._t8_status.config(text="Fetching prices & flows...", foreground=C_MUTED)
        # Clear diagnostic pane while loading
        self._t8_diag.config(state='normal')
        self._t8_diag.delete('1.0', tk.END)
        self._t8_diag.config(state='disabled')
        self.fig8.clear()
        _ax = self.fig8.add_subplot(111)
        _ax.text(0.5, 0.5, "Fetching data, please wait...",
                 transform=_ax.transAxes, ha='center', va='center',
                 fontsize=12, color=C_MUTED)
        _ax.axis('off')
        self.canvas8.draw()
        self.root.update_idletasks()

        threading.Thread(target=self._plot_tab8_thread,
                         args=(date_str, mtu_h, mtu_m), daemon=True).start()

    def _plot_tab8_thread(self, date_str, mtu_h, mtu_m):
        """Fetch NordPool DA prices + scheduled physical flows, pass results to the UI thread."""
        mtu_label  = f"{mtu_h:02d}:{mtu_m:02d}"
        diag_lines = []

        def _diag(msg):
            diag_lines.append(msg)

        token = get_np_access_token()
        if not token:
            self.root.after(0, self._tab8_done, None, None, date_str, mtu_label,
                            "AUTH FAILED: could not obtain NordPool access token.",
                            diag_lines)
            return

        hdrs         = self._np_headers(token)
        areas_params = [('areas', z) for z in NORDIC_ZONES]

        # Prices
        prices      = {}
        price_error = ""
        try:
            price_params = [('date', date_str), ('currency', 'EUR'),
                            ('market', 'DayAhead')] + areas_params
            rp = requests.get(NP_PRICE_URL, headers=hdrs,
                              params=price_params, timeout=20)
            _diag(f"[PRICE] status={rp.status_code}  url={rp.url}")
            if rp.status_code != 200:
                price_error = f"Price API HTTP {rp.status_code}: {rp.text[:200]}"
                _diag(f"[PRICE] ERROR body: {rp.text[:400]}")
            else:
                raw_json = rp.json()
                if raw_json and isinstance(raw_json, list):
                    _diag(f"[PRICE] top-level keys: {list(raw_json[0].keys())}")
                    if raw_json[0].get('prices'):
                        _diag(f"[PRICE] prices[0] keys: "
                              f"{list(raw_json[0]['prices'][0].keys())}")
                for area_data in raw_json:
                    zone = area_data.get('deliveryArea')
                    if zone not in NORDIC_ZONES:
                        continue
                    for p in area_data.get('prices', []):
                        ts = p.get('deliveryStart', '')
                        if not ts:
                            continue
                        try:
                            cet_dt = _utc_to_cet(ts)
                            if (cet_dt.strftime('%Y-%m-%d') == date_str
                                    and cet_dt.hour   == mtu_h
                                    and cet_dt.minute == mtu_m):
                                prices[zone] = p.get('price')
                                break
                        except Exception as e:
                            _diag(f"[PRICE] ts-parse error zone={zone} ts={ts!r}: {e}")
                _diag(f"[PRICE] matched zones: {sorted(prices.keys())} "
                      f"(target {mtu_h:02d}:{mtu_m:02d} CET)")
        except Exception as e:
            price_error = f"Price API exception: {e}"
            _diag(f"[PRICE] EXCEPTION: {e}")

        # Scheduled Physical Flows
        flows_raw  = {}
        flow_error = ""
        try:
            flow_params = [('date', date_str), ('market', 'DayAhead')] + areas_params
            rf = requests.get(NP_FLOW_URL, headers=hdrs,
                              params=flow_params, timeout=20)
            _diag(f"[FLOW] status={rf.status_code}  url={rf.url}")
            if rf.status_code != 200:
                flow_error = f"Flow API HTTP {rf.status_code}: {rf.text[:200]}"
                _diag(f"[FLOW] ERROR body: {rf.text[:400]}")
            else:
                raw_flow = rf.json()
                if raw_flow and isinstance(raw_flow, list):
                    _diag(f"[FLOW] top-level keys: {list(raw_flow[0].keys())}")

                def _get_flow_list(ad):
                    for k in ('flows', 'scheduledFlows', 'entries', 'values'):
                        v = ad.get(k)
                        if isinstance(v, list) and v:
                            return k, v
                    return None, []

                conn_key_resolved = None
                for area_data in raw_flow:
                    area = (area_data.get('deliveryArea')
                            or area_data.get('area')
                            or area_data.get('deliveryAreaCode'))
                    if not area:
                        continue
                    fl_key, flow_list = _get_flow_list(area_data)
                    if fl_key and conn_key_resolved is None:
                        _diag(f"[FLOW] flow-list key: '{fl_key}'. "
                              f"Sample slot keys: {list(flow_list[0].keys())}")

                    # Flow data is 15-min MTU
                 
                    matched = None
                    for fl in flow_list:
                        ts = fl.get('deliveryStart', '')
                        if not ts:
                            continue
                        try:
                            cet_dt = _utc_to_cet(ts)
                            if (cet_dt.strftime('%Y-%m-%d') == date_str
                                    and cet_dt.hour   == mtu_h
                                    and cet_dt.minute == mtu_m):
                                matched = fl
                                break
                        except Exception as e:
                            _diag(f"[FLOW] ts-parse error area={area} ts={ts!r}: {e}")

                    if matched is None:
                        continue

                    connections = None
                    for ck in ('byConnections', 'connections',
                               'counterpartAreas', 'scheduledExchanges'):
                        connections = matched.get(ck)
                        if isinstance(connections, list) and connections:
                            if conn_key_resolved != ck:
                                conn_key_resolved = ck
                                _diag(f"[FLOW] connection-list key: '{ck}'. "
                                      f"Sample: {list(connections[0].keys())}")
                            break
                    if not connections:
                        _diag(f"[FLOW] WARNING: no connection list in slot "
                              f"area={area}. Slot keys: {list(matched.keys())}")
                        continue

                    for conn in connections:
                        other = (conn.get('area')
                                 or conn.get('deliveryArea')
                                 or conn.get('counterpart')
                                 or conn.get('toArea')
                                 or conn.get('deliveryAreaCode'))
                        exp_raw = (conn.get('export')
                                   or conn.get('exportFlow')
                                   or conn.get('scheduledExport')
                                   or conn.get('value')
                                   or 0)
                        if not other:
                            continue
                        try:
                            exp = float(exp_raw or 0)
                        except (TypeError, ValueError):
                            exp = 0.0
                        flows_raw[(area, other)] = flows_raw.get((area, other), 0) + exp

                _diag(f"[FLOW] raw pairs: {len(flows_raw)}  "
                      f"e.g. {list(flows_raw.items())[:4]}")
        except Exception as e:
            flow_error = f"Flow API exception: {e}"
            _diag(f"[FLOW] EXCEPTION: {e}")

        # Net flow per canonical border pair (positive = A→B)
        net_flows = {}
        for A, B in ZONE_CONNECTIONS:
            net = flows_raw.get((A, B), 0) - flows_raw.get((B, A), 0)
            if abs(net) >= 1:
                net_flows[(A, B)] = net

        _diag(f"[RESULT] prices={len(prices)} zones  net_flows={len(net_flows)} borders")

        combined_error = "  |  ".join(filter(None, [price_error, flow_error]))
        self.root.after(0, self._tab8_done, prices, net_flows,
                        date_str, mtu_label,
                        combined_error or None, diag_lines)

    def _tab8_done(self, prices, net_flows, date_str, mtu_label,
                   error=None, diag_lines=None):
        self._t8_btn.config(state=tk.NORMAL, text="Fetch & Plot")
        self._t8_status.config(text="")

        # ── Write diagnostic lines to the log pane ────────────────────
        if diag_lines:
            self._t8_diag.config(state='normal')
            self._t8_diag.delete('1.0', tk.END)
            self._t8_diag.insert('1.0', '\n'.join(diag_lines))
            self._t8_diag.see(tk.END)
            self._t8_diag.config(state='disabled')

        if error:
            self._t8_status.config(text=f"⚠ {error}", foreground=C_RED)
            # Still render the map with whatever partial data we have
            if prices is None and net_flows is None:
                messagebox.showerror("Tab 8 Error", error)
                return

        self.fig8.clear()
        ax = self.fig8.add_subplot(111)

        # ── Main canvas: no background image, clean white ────────────
        img_w = img_h = 1000
        ax.set_xlim(0, img_w)
        ax.set_ylim(img_h, 0)
        ax.set_facecolor(C_PANEL)

        # ── Price colour scale ────────────────────────────────────────
        valid_prices = [v for v in prices.values() if v is not None]
        p_min  = min(valid_prices) if valid_prices else 0
        p_max  = max(valid_prices) if valid_prices else 100
        p_span = max(p_max - p_min, 1.0)
        # FIX C: coolwarm gives strong blue/red contrast on white; RdYlGn yellows vanish
        cmap   = mpl.colormaps['coolwarm']

        # ── Zone price boxes ──────────────────────────────────────────
        for zone, (nx, ny) in ZONE_POS_NORM.items():
            px, py = nx * img_w, ny * img_h
            price  = prices.get(zone)
            if price is not None:
                norm_val  = (price - p_min) / p_span
                fc        = cmap(norm_val)
                price_str = f"{price:.1f} EUR"
                # FIX C: white text on dark ends of coolwarm; black text on pale mid-range
                t_color   = 'white' if (norm_val < 0.25 or norm_val > 0.75) else 'black'
            else:
                fc        = (0.45, 0.45, 0.45, 0.88)
                price_str = "N/A"
                t_color   = 'white'
            txt = ax.text(px, py, f"{zone}\n{price_str}",
                    ha='center', va='center', fontsize=7.5, fontweight='bold',
                    color=t_color, zorder=5,
                    bbox=dict(boxstyle='round,pad=0.35', facecolor=fc,
                              edgecolor='white', linewidth=1.3, alpha=0.93))
            # FIX C: halo stroke for legibility on any map background
            txt.set_path_effects([patheffects.withStroke(
                linewidth=2, foreground='black' if t_color == 'white' else 'white',
                alpha=0.3)])

        # ── Flow arrows ───────────────────────────────────────────────
        max_flow = max((abs(v) for v in net_flows.values()), default=1) or 1
        pad_px   = 0.040 * img_w
        has_counter = False

        for (A, B), net_mw in net_flows.items():
            if A not in ZONE_POS_NORM or B not in ZONE_POS_NORM:
                continue
            nx1, ny1 = ZONE_POS_NORM[A];  nx2, ny2 = ZONE_POS_NORM[B]
            x1, y1   = nx1 * img_w, ny1 * img_h
            x2, y2   = nx2 * img_w, ny2 * img_h

            # Determine actual source and destination after direction flip
            src, dst = (A, B) if net_mw >= 0 else (B, A)
            if net_mw < 0:
                x1, y1, x2, y2 = x2, y2, x1, y1
                net_mw = -net_mw

            # Red if flow goes from expensive zone to cheaper zone
            p_src = prices.get(src)
            p_dst = prices.get(dst)
            counter = (p_src is not None and p_dst is not None and p_src > p_dst)
            arrow_color = C_RED if counter else C_PRIMARY
            if counter:
                has_counter = True

            dx, dy = x2 - x1, y2 - y1
            dist   = (dx**2 + dy**2) ** 0.5 or 1
            xs = x1 + dx / dist * pad_px;  ys = y1 + dy / dist * pad_px
            xe = x2 - dx / dist * pad_px;  ye = y2 - dy / dist * pad_px
            lw = 0.8 + 3.4 * (net_mw / max_flow)
            ax.annotate("", xy=(xe, ye), xytext=(xs, ys), zorder=4,
                        arrowprops=dict(arrowstyle='->', color=arrow_color,
                                        lw=lw, mutation_scale=10))
            mx, my = (xs + xe) / 2, (ys + ye) / 2
            flow_val = ax.text(mx, my, f"{net_mw:.0f}",
                    fontsize=5.5, ha='center', va='center', zorder=6,
                    color=arrow_color, fontweight='bold')
            # FIX C: white stroke separates flow numbers from map background
            flow_val.set_path_effects([patheffects.withStroke(linewidth=2, foreground='white')])

        # Flow legend
        legend_handles = [
            Line2D([0], [0], color=C_PRIMARY, lw=2,
                   label="Normal flow (cheap → expensive)"),
        ]
        if has_counter:
            legend_handles.append(
                Line2D([0], [0], color=C_RED, lw=2,
                       label="Counter-intuitive (expensive → cheap)"))
        ax.legend(handles=legend_handles, loc='lower right', fontsize=7,
                  framealpha=0.88, edgecolor=C_BORDER, facecolor=C_PANEL,
                  labelcolor=C_TEXT)

        # ── Colorbar ──────────────────────────────────────────────────
        if valid_prices:
            sm = mpl.cm.ScalarMappable(
                cmap=cmap,
                norm=mpl.colors.Normalize(vmin=p_min, vmax=p_max))
            sm.set_array([])
            cbar = self.fig8.colorbar(sm, ax=ax, fraction=0.025,
                                      pad=0.02, aspect=30)
            cbar.set_label("EUR/MWh", fontsize=8, color=C_MUTED)
            cbar.ax.tick_params(labelsize=7, colors=C_MUTED)
            for lbl in cbar.ax.yaxis.get_ticklabels():
                lbl.set_color(C_MUTED)

        ax.axis('off')
        ax.set_title(
            f"Nordic Map  —  {date_str}  {mtu_label} CET  |  "
            f"Price (DayAhead, EUR/MWh) + Flow (MW)",
            fontsize=9.5, fontweight='bold', color=C_ACCENT, pad=8)

        # ── Geographic overview inset (top-right corner) ──────────────
        ax_ov = ax.inset_axes([0.790, 0.620, 0.195, 0.340])

        ov_h = ov_w = 1000
        map_loaded = False
        if os.path.exists(MAP_PATH):
            try:
                _ov_img = mpl_img.imread(MAP_PATH)
                ov_h, ov_w = _ov_img.shape[:2]
                ax_ov.imshow(_ov_img, extent=[0, ov_w, ov_h, 0],
                             aspect='auto', zorder=0)
                map_loaded = True
            except Exception:
                pass

        ax_ov.set_xlim(0, ov_w)
        ax_ov.set_ylim(ov_h, 0)
        if not map_loaded:
            ax_ov.set_facecolor('#cce8f0')

        # Fetch-info caption above the inset
        n_zones   = len([v for v in prices.values() if v is not None])
        p_rng_str = (f"{p_min:.0f}–{p_max:.0f} €/MWh"
                     if valid_prices else "no prices")
        ax_ov.set_title(
            f"{date_str}  {mtu_label} CET\n"
            f"{n_zones}/{len(NORDIC_ZONES)} zones  |  {p_rng_str}",
            fontsize=5.5, color=C_ACCENT, fontweight='bold',
            pad=3, loc='center')

        ax_ov.axis('off')
        for sp in ax_ov.spines.values():
            sp.set_visible(True)
            sp.set_color(C_BORDER)
            sp.set_linewidth(1.2)

        self.fig8.tight_layout(pad=1.5)
        self._add_toolbar(self.canvas8, self.toolbar_f8)

    # ------------------------------------------------------------------
    #  TAB 9 – Maintenance Analysis  (FB_CODE integration)
    # ------------------------------------------------------------------
    def _create_tab9_widgets(self):
        # lazy-import propagation backend
        try:
            import propagation as _prop
            self._prop = _prop
        except Exception:
            self._prop = None

        # state shared across sub-tabs
        self._ma_jao_df      = None   # pd.DataFrame built from Tab 1 data
        self._ma_outages_df  = None   # outage events DataFrame
        self._ma_results     = {}     # regression results dict
        self._ma_covariates  = None   # merged covariate DataFrame
        self._ma_single_res  = None   # single-event analysis result
        self._ma_verdicts    = []     # hypothesis verdict list

        main = ttk.Frame(self.tab9, style='Card.TFrame', padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        # ── inner notebook ─────────────────────────────────────────────
        nb = ttk.Notebook(main, style='App.TNotebook')
        nb.pack(fill=tk.BOTH, expand=True)
        self._ma_nb = nb

        sub_frames = {}
        for key, label in [
            ('setup',    '  Setup  '),
            ('outages',  '  Outage Sources  '),
            ('run',      '  Run Analysis  '),
            ('results',  '  Results  '),
            ('plots',    '  Plots  '),
            ('single',   '  Single Event  '),
            ('explain',  '  Explain  '),
            ('export',   '  Export  '),
        ]:
            f = ttk.Frame(nb, style='Card.TFrame')
            nb.add(f, text=label)
            sub_frames[key] = f

        self._ma_build_setup(sub_frames['setup'])
        self._ma_build_outages(sub_frames['outages'])
        self._ma_build_run(sub_frames['run'])
        self._ma_build_results(sub_frames['results'])
        self._ma_build_plots(sub_frames['plots'])
        self._ma_build_single(sub_frames['single'])
        self._ma_build_explain(sub_frames['explain'])
        self._ma_build_export(sub_frames['export'])
        # Pre-fill CNEC list and ENTSO-E dates from any data already in Tab 1
        self._ma_sync_from_raw_data()

    # ── Sub-tab 1: Setup ──────────────────────────────────────────────
    def _ma_build_setup(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Maintenance Analysis — Setup", style='H1.TLabel').pack(anchor='w', pady=(0,8))

        # Data source
        src_f = ttk.LabelFrame(f, text=" Data Source ", padding=12)
        src_f.pack(fill=tk.X, pady=(0,10))

        self._ma_data_mode = tk.StringVar(value="tab1")
        ttk.Radiobutton(src_f, text="Use data loaded in Tab 1  (Fetch / Upload)",
                        variable=self._ma_data_mode, value="tab1",
                        command=self._ma_refresh_setup_status).pack(anchor='w')
        ttk.Radiobutton(src_f, text="Generate synthetic demo dataset  (90-day FI→NO3)",
                        variable=self._ma_data_mode, value="synthetic",
                        command=self._ma_refresh_setup_status).pack(anchor='w', pady=(4,0))

        self._ma_setup_status = ttk.Label(src_f, text="", style='Muted.TLabel')
        self._ma_setup_status.pack(anchor='w', pady=(6,0))

        # Analysis scope
        scope_f = ttk.LabelFrame(f, text=" Analysis Scope ", padding=12)
        scope_f.pack(fill=tk.X, pady=(0,10))

        row1 = ttk.Frame(scope_f, style='Card.TFrame')
        row1.pack(fill=tk.X)
        ttk.Label(row1, text="Outage source country:").pack(side=tk.LEFT)
        self._ma_src_country = ttk.Combobox(row1,
            values=["FI","NO","SE","DK","EE","LV","LT"], width=6, state='readonly')
        self._ma_src_country.set("FI")
        self._ma_src_country.pack(side=tk.LEFT, padx=(4,20))

        ttk.Label(row1, text="Target CNEC:").pack(side=tk.LEFT)
        self._ma_target_cnec = ttk.Combobox(row1, values=[], width=36, state='readonly')
        self._ma_target_cnec.set("")
        self._ma_target_cnec.pack(side=tk.LEFT, padx=(4,6))
        ttk.Label(row1, text="(auto-filled from Tab 1 data)",
                  style='Muted.TLabel').pack(side=tk.LEFT)

        row2 = ttk.Frame(scope_f, style='Card.TFrame')
        row2.pack(fill=tk.X, pady=(8,0))
        ttk.Label(row2, text="Output directory:").pack(side=tk.LEFT)
        self._ma_out_dir = ttk.Entry(row2, width=36)
        self._ma_out_dir.insert(0, os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "ma_output"))
        self._ma_out_dir.pack(side=tk.LEFT, padx=(4,6))
        ttk.Button(row2, text="Browse…",
                   command=lambda: self._ma_browse_dir()).pack(side=tk.LEFT)

        btn_row = ttk.Frame(f, style='Card.TFrame')
        btn_row.pack(anchor='w', pady=(4,0))
        self._ma_apply_btn = ttk.Button(btn_row, text="Apply Setup", style='Accent.TButton',
                                         command=self._ma_apply_setup)
        self._ma_apply_btn.pack(side=tk.LEFT, padx=(0,8))
        ttk.Button(btn_row, text="Reset All",
                   command=self._ma_reset_all).pack(side=tk.LEFT)
        self._ma_apply_wait_lbl = ttk.Label(btn_row, text="", style='Muted.TLabel')
        self._ma_apply_wait_lbl.pack(side=tk.LEFT, padx=(12, 0))

        self._ma_refresh_setup_status()

    def _ma_reset_all(self):
        """Clear all Tab 9 loaded data, outages, and results for a completely fresh start."""
        self._ma_jao_df      = None
        self._ma_outages_df  = None
        self._ma_results     = {}
        self._ma_verdicts    = []
        self._ma_covariates  = None
        self._ma_single_res  = None
        # Clear outage treeview
        self._ma_out_tree.delete(*self._ma_out_tree.get_children())
        self._ma_outage_status.config(text="Outages cleared.", foreground=C_MUTED)
        # Clear results treeview and detail
        self._ma_res_tree.delete(*self._ma_res_tree.get_children())
        self._ma_res_detail.config(state='normal')
        self._ma_res_detail.delete('1.0', tk.END)
        self._ma_res_detail.config(state='disabled')
        # Clear pipeline log
        self._ma_log.config(state='normal')
        self._ma_log.delete('1.0', tk.END)
        self._ma_log.config(state='disabled')
        # Clear single-event outage list and sub-tab content
        self._ma_single_id['values'] = []
        self._ma_single_id.set('')
        self._ma_single_summary_txt.config(state='normal')
        self._ma_single_summary_txt.delete('1.0', tk.END)
        self._ma_single_summary_txt.config(state='disabled')
        self._ma_cnec_tree.delete(*self._ma_cnec_tree.get_children())
        if self._ma_fig_single is not None and self._ma_canvas_single is not None:
            self._ma_fig_single.clear(); self._ma_canvas_single.draw()
        self._ma_fig_decomp.clear(); self._ma_canvas_decomp.draw()
        self._ma_setup_status.config(text="All data cleared. Apply Setup to reload.",
                                     foreground=C_AMBER)

    def _ma_refresh_setup_status(self):
        mode = self._ma_data_mode.get()
        if mode == "tab1":
            n = len(self.raw_filtered_data)
            if n:
                self._ma_setup_status.config(
                    text=f"Tab 1 has {n:,} records loaded — ready to use.",
                    foreground=C_GREEN)
                self._ma_sync_from_raw_data()
            else:
                self._ma_setup_status.config(
                    text="Tab 1 has no data yet. Load a JAO CSV or fetch data first.",
                    foreground=C_AMBER)
        else:
            self._ma_setup_status.config(
                text="Synthetic 90-day dataset will be generated on Apply.",
                foreground=C_PRIMARY)

    def _ma_sync_from_raw_data(self):
        """Pre-fill CNEC dropdown and ENTSO-E dates from raw_filtered_data
        (Tab 1 data). Safe to call any time — silently skips if no data."""
        data = self.raw_filtered_data
        if not data:
            return
        # --- CNEC list ---
        cnecs = sorted(set(d.get('cneName', '') for d in data
                           if d.get('cneName', '').strip()))
        if cnecs:
            tab2_cnec = self._get_selected_cnec() or ''
            self._ma_target_cnec.config(values=cnecs)
            if tab2_cnec and tab2_cnec in cnecs:
                self._ma_target_cnec.set(tab2_cnec)
            elif not self._ma_target_cnec.get() or \
                    self._ma_target_cnec.get() not in cnecs:
                self._ma_target_cnec.set(cnecs[0])
        # --- ENTSO-E date range ---
        dates = sorted(set(str(d.get('date', '')) for d in data
                           if d.get('date', '')))
        if dates:
            def _fmt(d): return f"{d[:4]}-{d[4:6]}-{d[6:8]} 00:00"
            self._ma_ent_start.delete(0, tk.END)
            self._ma_ent_start.insert(0, _fmt(dates[0]))
            self._ma_ent_end.delete(0, tk.END)
            self._ma_ent_end.insert(0, _fmt(dates[-1]))

    def _ma_sync_tab2_cnec(self):
        """When the user changes the selected CNEC in Tab 2, mirror it to Tab 9."""
        if not hasattr(self, '_ma_target_cnec'):
            return
        cnec = self._get_selected_cnec() or ''
        if not cnec:
            return
        current_values = list(self._ma_target_cnec['values'])
        if cnec in current_values:
            self._ma_target_cnec.set(cnec)

    def _ma_browse_dir(self):
        d = filedialog.askdirectory()
        if d:
            self._ma_out_dir.delete(0, tk.END)
            self._ma_out_dir.insert(0, d)

    def _ma_refresh_cnec_list(self, df):
        """Populate _ma_target_cnec combobox from CNEC names in loaded JAO data,
        defaulting to whichever CNEC is currently selected in Tab 2."""
        if 'cneName' not in df.columns:
            return
        cnecs = sorted(str(c) for c in df['cneName'].dropna().unique() if str(c).strip())
        if not cnecs:
            return
        # Default: use Tab 2's currently selected CNEC if it exists in the data
        tab2_cnec = self._get_selected_cnec() or ""
        self._ma_target_cnec.config(values=cnecs)
        if tab2_cnec and tab2_cnec in cnecs:
            self._ma_target_cnec.set(tab2_cnec)
        elif self._ma_target_cnec.get() not in cnecs:
            self._ma_target_cnec.set(cnecs[0])

    def _ma_apply_setup(self):
        # Guard: don't start a second run while one is in progress
        if getattr(self, '_ma_setup_running', False):
            return
        import pandas as pd
        # Clear all previous analysis results for a fresh run (on main thread)
        self._ma_jao_df      = None
        self._ma_outages_df  = None
        self._ma_results     = {}
        self._ma_verdicts    = []
        self._ma_covariates  = None
        self._ma_single_res  = None
        self._ma_res_tree.delete(*self._ma_res_tree.get_children())
        self._ma_res_detail.config(state='normal')
        self._ma_res_detail.delete('1.0', tk.END)
        self._ma_res_detail.config(state='disabled')
        self._ma_log.config(state='normal')
        self._ma_log.delete('1.0', tk.END)
        self._ma_log.config(state='disabled')
        self._ma_out_tree.delete(*self._ma_out_tree.get_children())
        self._ma_single_id['values'] = []
        self._ma_single_id.set('')

        mode    = self._ma_data_mode.get()
        out_dir = self._ma_out_dir.get().strip()

        if mode == "tab1" and not self.raw_filtered_data:
            messagebox.showwarning("Setup", "No data in Tab 1. Load data first.")
            return

        # Show waiting indicator and disable button
        self._ma_setup_running = True
        self._ma_apply_btn.config(state='disabled')
        self._ma_apply_wait_lbl.config(text="⏳  Please wait…")
        self.root.update_idletasks()

        threading.Thread(target=self._ma_apply_setup_thread,
                         args=(mode, out_dir), daemon=True).start()

    def _ma_apply_setup_thread(self, mode, out_dir):
        """Worker: runs setup in background, posts result to main thread."""
        import pandas as pd
        result = {"ok": False, "msg": "", "kind": "info",
                  "jao_df": None, "outages_df": None,
                  "cnec_dates": None}
        try:
            os.makedirs(out_dir, exist_ok=True)
            if mode == "synthetic":
                import synthetic as _syn
                info = _syn.generate_demo_dataset(out_dir, days=90)
                jao_df = self._prop.load_jao_csv(info['jao_path']) \
                    if self._prop else pd.read_csv(info['jao_path'])
                result.update(ok=True, kind="info",
                              msg=(f"Synthetic dataset generated.\n"
                                   f"JAO rows: {info['jao_rows']:,}  |  "
                                   f"Outages: {info['outage_events']}"),
                              jao_df=jao_df,
                              outages_df=pd.read_csv(info['outages_path']))
            else:
                df = pd.DataFrame(self.raw_filtered_data)
                if 'dateTimeUtc' not in df.columns:
                    from zoneinfo import ZoneInfo as _ZI
                    _cet = _ZI("Europe/Oslo")
                    df['dateTimeUtc'] = (
                        pd.to_datetime(df['date'].astype(str) + ' ' + df['time'],
                                       format='%Y%m%d %H:%M:%S')
                        .dt.tz_localize(_cet, ambiguous='infer',
                                        nonexistent='shift_forward')
                        .dt.tz_convert('UTC')
                    )
                if 'flowFb' in df.columns and 'flowFB' not in df.columns:
                    df['flowFB'] = df['flowFb']
                for col in ['shadowPrice', 'ram', 'fall', 'flowFB', 'fmax', 'f0',
                            'frm', 'fra', 'amr', 'faac', 'iva']:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                if self._prop:
                    tmp_path = os.path.join(out_dir, '_tab1_tmp.csv')
                    df.to_csv(tmp_path, index=False)
                    jao_df = self._prop.load_jao_csv(tmp_path)
                else:
                    jao_df = df
                cnec_dates = None
                if 'date' in jao_df.columns:
                    _dates = sorted(jao_df['date'].dropna().astype(str).unique())
                    if _dates:
                        cnec_dates = _dates
                result.update(ok=True, kind="info",
                              msg=(f"Tab 1 data loaded: {len(jao_df):,} rows, "
                                   f"{jao_df['cneName'].nunique()} CNECs."),
                              jao_df=jao_df,
                              cnec_dates=cnec_dates)
        except Exception as ex:
            import traceback
            result.update(ok=False, kind="error",
                          msg=f"{ex}\n\n{traceback.format_exc()}")
        self.root.after(0, self._ma_apply_setup_done, result)

    def _ma_apply_setup_done(self, result):
        """Called on main thread after setup thread completes."""
        self._ma_setup_running = False
        self._ma_apply_btn.config(state='normal')
        self._ma_apply_wait_lbl.config(text="")

        if not result["ok"]:
            messagebox.showerror("Setup Error", result["msg"])
            return

        self._ma_jao_df     = result["jao_df"]
        self._ma_outages_df = result.get("outages_df")

        if self._ma_jao_df is not None:
            self._ma_refresh_cnec_list(self._ma_jao_df)

        dates = result.get("cnec_dates")
        if dates:
            def _fmt(d): return f"{d[:4]}-{d[4:6]}-{d[6:8]} 00:00"
            self._ma_ent_start.delete(0, tk.END)
            self._ma_ent_start.insert(0, _fmt(dates[0]))
            self._ma_ent_end.delete(0, tk.END)
            self._ma_ent_end.insert(0, _fmt(dates[-1]))

        messagebox.showinfo("Setup", result["msg"])

    # ── Sub-tab 2: Outage Sources ──────────────────────────────────────
    def _ma_build_outages(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Outage Sources", style='H1.TLabel').pack(anchor='w', pady=(0,8))

        # Manual CSV
        man_f = ttk.LabelFrame(f, text=" Manual Outage CSV ", padding=12)
        man_f.pack(fill=tk.X, pady=(0,8))
        row = ttk.Frame(man_f, style='Card.TFrame')
        row.pack(fill=tk.X)
        self._ma_manual_path = ttk.Entry(row, width=50)
        default_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "manual_outages.csv")
        self._ma_manual_path.insert(0, default_csv)
        self._ma_manual_path.pack(side=tk.LEFT, padx=(0,6))
        ttk.Button(row, text="Browse…",
                   command=self._ma_browse_manual).pack(side=tk.LEFT)
        ttk.Button(man_f, text="Load Manual CSV", style='Accent.TButton',
                   command=self._ma_load_manual).pack(anchor='w', pady=(8,0))

        # ENTSO-E
        ent_f = ttk.LabelFrame(f, text=" ENTSO-E Transparency (A77 + A78) ", padding=12)
        ent_f.pack(fill=tk.X, pady=(0,8))
        ttk.Label(ent_f, text="API token is pre-configured. Enter dates in CET local time.",
                  style='Muted.TLabel').pack(anchor='w', pady=(0,6))

        row2 = ttk.Frame(ent_f, style='Card.TFrame')
        row2.pack(fill=tk.X)
        ttk.Label(row2, text="Start (CET):").pack(side=tk.LEFT)
        self._ma_ent_start = ttk.Entry(row2, width=20)
        self._ma_ent_start.insert(0, "2024-01-01 00:00")
        self._ma_ent_start.pack(side=tk.LEFT, padx=(4,16))
        ttk.Label(row2, text="End (CET):").pack(side=tk.LEFT)
        self._ma_ent_end = ttk.Entry(row2, width=20)
        self._ma_ent_end.insert(0, "2025-01-01 00:00")
        self._ma_ent_end.pack(side=tk.LEFT, padx=(4,0))
        ttk.Label(row2, text="  format: YYYY-MM-DD HH:MM", style='Muted.TLabel').pack(side=tk.LEFT)

        ttk.Button(ent_f, text="Fetch from ENTSO-E", style='Accent.TButton',
                   command=self._ma_fetch_entsoe).pack(anchor='w', pady=(8,0))

        self._ma_outage_status = ttk.Label(f, text="No outages loaded.",
                                            style='Muted.TLabel')
        self._ma_outage_status.pack(anchor='w', pady=(10,0))

        # Outage list treeview
        cols = ('outage_id','asset_name','asset_type','start_utc','end_utc',
                'capacity_mw','planned_or_forced')
        tree_f = ttk.Frame(f, style='Card.TFrame')
        tree_f.pack(fill=tk.BOTH, expand=True, pady=(6,0))
        vsb = ttk.Scrollbar(tree_f, orient='vertical')
        hsb = ttk.Scrollbar(tree_f, orient='horizontal')
        self._ma_out_tree = ttk.Treeview(tree_f, columns=cols, show='headings',
                                          yscrollcommand=vsb.set,
                                          xscrollcommand=hsb.set, height=8)
        vsb.config(command=self._ma_out_tree.yview)
        hsb.config(command=self._ma_out_tree.xview)
        widths = [160,220,90,160,160,90,90]
        for col, w in zip(cols, widths):
            self._ma_out_tree.heading(col, text=col.replace('_',' ').title())
            self._ma_out_tree.column(col, width=w, anchor='w')
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._ma_out_tree.pack(fill=tk.BOTH, expand=True)

    def _ma_browse_manual(self):
        p = filedialog.askopenfilename(filetypes=[("CSV","*.csv")])
        if p:
            self._ma_manual_path.delete(0, tk.END)
            self._ma_manual_path.insert(0, p)

    def _ma_load_manual(self):
        p = self._ma_manual_path.get().strip()
        if not os.path.exists(p):
            messagebox.showerror("Error", f"File not found: {p}")
            return
        try:
            import pandas as pd
            df = pd.read_csv(p)
            self._ma_outages_df = df
            self._ma_populate_outage_tree(df)
            self._ma_refresh_single_list()
            msg = f"{len(df)} outage events loaded from manual CSV."
            self._ma_outage_status.config(text=msg, foreground=C_GREEN)
            self._update_status(f"[Tab9] {msg}")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _ma_fetch_entsoe(self):
        if not self._prop:
            messagebox.showerror("Error", "propagation module not loaded.")
            return
        start_raw = self._ma_ent_start.get().strip()
        end_raw   = self._ma_ent_end.get().strip()
        src       = self._ma_src_country.get()
        try:
            s_cet = datetime.strptime(start_raw, "%Y-%m-%d %H:%M").replace(tzinfo=_CET)
            e_cet = datetime.strptime(end_raw,   "%Y-%m-%d %H:%M").replace(tzinfo=_CET)
        except ValueError:
            messagebox.showerror("Date Error",
                "Enter dates as  YYYY-MM-DD HH:MM  (CET local time).")
            return
        start_utc = s_cet.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_utc   = e_cet.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._ma_outage_status.config(text="Fetching from ENTSO-E…", foreground=C_AMBER)
        self.root.update_idletasks()

        def _run():
            try:
                def _log(m):
                    self.root.after(0, self._update_status, m)
                    self.root.after(0, self._ma_outage_status.config, {'text': m[:120]})
                df = self._prop.fetch_entsoe_outages(
                    start_utc, end_utc,
                    log_cb=_log,
                    country_code=src)
                self.root.after(0, self._ma_entsoe_done, df)
            except Exception as ex:
                self.root.after(0, messagebox.showerror, "ENTSO-E Error", str(ex))
        threading.Thread(target=_run, daemon=True).start()

    def _ma_entsoe_done(self, df):
        import pandas as pd
        if self._ma_outages_df is not None and not self._ma_outages_df.empty:
            df = self._prop.deduplicate_outages(
                pd.concat([self._ma_outages_df, df], ignore_index=True))
        self._ma_outages_df = df
        self._ma_populate_outage_tree(df)
        self._ma_refresh_single_list()
        msg = f"{len(df)} outage events after deduplication."
        self._ma_outage_status.config(text=msg, foreground=C_GREEN)
        self._update_status(f"[Tab9] ENTSO-E fetch done — {msg}")

    def _ma_populate_outage_tree(self, df):
        self._ma_out_tree.delete(*self._ma_out_tree.get_children())
        for _, r in df.head(200).iterrows():
            vals = tuple(str(r.get(c,''))[:80] for c in
                         ('outage_id','asset_name','asset_type','start_utc',
                          'end_utc','capacity_mw','planned_or_forced'))
            self._ma_out_tree.insert('', tk.END, values=vals)

    # ── Sub-tab 3: Run Analysis ────────────────────────────────────────
    def _ma_build_run(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Run Analysis", style='H1.TLabel').pack(anchor='w', pady=(0,8))
        ttk.Label(f, text="Requires: Setup applied  +  Outages loaded.",
                  style='Muted.TLabel').pack(anchor='w', pady=(0,10))

        self._ma_run_btn = ttk.Button(f, text="▶  Run Full Pipeline",
                                       style='Accent.TButton',
                                       command=self._ma_run_pipeline)
        self._ma_run_btn.pack(anchor='w')

        self._ma_progress = ttk.Progressbar(f, mode='indeterminate', length=400)
        self._ma_progress.pack(anchor='w', pady=(8,0))

        log_frame = tk.Frame(f, bg='#0f172a', padx=2, pady=2)
        log_frame.pack(fill=tk.BOTH, expand=True, pady=(8,0))
        self._ma_log = tk.Text(log_frame, font=FONT_MONO, background='#0f172a',
                               foreground='#94e2d5', insertbackground='white',
                               relief='flat', borderwidth=0, padx=10, pady=8)
        sb = tk.Scrollbar(log_frame, command=self._ma_log.yview, bg='#1e293b')
        self._ma_log.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._ma_log.pack(fill=tk.BOTH, expand=True)

    def _ma_log_append(self, msg):
        ts = datetime.now().strftime('%H:%M:%S')
        self._ma_log.config(state='normal')
        self._ma_log.insert(tk.END, f"[{ts}] {msg}\n")
        self._ma_log.see(tk.END)
        self._ma_log.config(state='disabled')
        # Mirror to Tab 1 status log so all activity is visible in one place
        self._update_status(f"[Tab9] {msg}")

    def _ma_run_pipeline(self):
        if self._ma_jao_df is None:
            messagebox.showwarning("Run", "Apply Setup first.")
            return
        if self._ma_outages_df is None:
            messagebox.showwarning("Run", "Load outages first (Outage Sources tab).")
            return
        if not self._prop:
            messagebox.showerror("Run", "propagation module not available.")
            return
        self._ma_run_btn.config(state=tk.DISABLED, text="Running…")
        self._ma_progress.start(10)
        self._ma_log.config(state='normal')
        self._ma_log.delete('1.0', tk.END)
        threading.Thread(target=self._ma_pipeline_thread, daemon=True).start()

    def _ma_pipeline_thread(self):
        self._ma_results  = {}
        self._ma_verdicts = []
        def log(m): self.root.after(0, self._ma_log_append, m)
        try:
            src  = self._ma_src_country.get()
            cnec = self._ma_target_cnec.get().strip()
            if not cnec:
                log("ERROR: Select a target CNEC in the Setup tab.")
                self.root.after(0, self._ma_pipeline_done, False)
                return

            # Use the FULL dataset for panel regressions — PanelOLS requires
            # ≥2 entities (CNECs) for entity fixed effects. The target CNEC
            # is used only as the analysis label passed to summarize_hypotheses.
            no3_df = self._ma_jao_df.copy()
            n_cnecs = no3_df['cneName'].nunique() if 'cneName' in no3_df.columns else 0
            log(f"Dataset: {len(no3_df):,} rows across {n_cnecs} CNECs (target: {cnec})")
            if no3_df.empty:
                log("ERROR: No rows in dataset.")
                self.root.after(0, self._ma_pipeline_done, False)
                return

            log("Building covariates…")
            cov_df = self._prop.build_covariates(no3_df, self._ma_outages_df,
                                                  log_cb=log)
            self._ma_covariates = cov_df
            log(f"  Covariates built: {len(cov_df):,} rows")

            log("Running panel regressions (per-hypothesis specs)…")
            _hy  = self._prop._indep_for_hypothesis
            _reg = self._prop.run_panel_regression

            # H1: fall_signed (or fall) — HVDC-first spec
            h1_dep = "fall_signed" if "fall_signed" in cov_df.columns else "fall"
            log(f"  H1 [{h1_dep}]…")
            res_fall = _reg(cov_df, h1_dep, indep=_hy("H1", src.lower()),
                            log_cb=log, cluster="time", src=src.lower())

            # H2: |PTDF_SRC| — AC-first spec
            h2_dep = (f"ptdf_{src.upper()}_abs" if f"ptdf_{src.upper()}_abs" in cov_df.columns
                      else "ptdf_FI_abs" if "ptdf_FI_abs" in cov_df.columns else "ptdf_FI")
            log(f"  H2 [{h2_dep}]…")
            res_ptdf = _reg(cov_df, h2_dep, indep=_hy("H2", src.lower()),
                            log_cb=log, cluster="time", src=src.lower())

            # H3: RAM
            log("  H3 [ram]…")
            res_ram = _reg(cov_df, "ram", indep=_hy("H3", src.lower()),
                           log_cb=log, cluster="time", src=src.lower())

            # H4: shadow price (binding rows only)
            sp_col = "shadowPrice_clean" if "shadowPrice_clean" in cov_df.columns else "shadowPrice"
            sp_mask = cov_df[sp_col].fillna(0) > 0
            log(f"  H4 [{sp_col}]  binding={sp_mask.sum():,}/{len(cov_df):,}…")
            if sp_mask.sum() >= 50:
                res_sp = _reg(cov_df[sp_mask], sp_col, indep=_hy("H4", src.lower()),
                              log_cb=log, cluster="time", src=src.lower())
            else:
                log("  Too few binding MTUs; skipping H4")
                res_sp = {}

            # H6: FRM placebo
            log("  H6 [frm]…")
            res_frm = (_reg(cov_df, "frm", indep=_hy("H6", src.lower()),
                            log_cb=log, cluster="time", src=src.lower())
                       if "frm" in cov_df.columns else {})

            results = {
                h1_dep:          res_fall,
                "fall_signed":   res_fall,
                "fall":          res_fall,
                "f0":            res_fall,
                h2_dep:          res_ptdf,
                "ptdf_FI_abs":   res_ptdf,
                "ptdf_FI":       res_ptdf,
                "ram":           res_ram,
                sp_col:          res_sp,
                "shadowPrice":   res_sp,
                "frm":           res_frm,
            }

            log("Running logit IVA model…")
            logit_res = self._prop.run_logit_iva(cov_df)

            log("Summarising hypotheses…")
            self._ma_results = results
            verdicts = self._prop.summarize_hypotheses(
                results, logit_res, src=src, tgt=cnec)
            self._ma_verdicts = verdicts
            for v in verdicts:
                m = re.search(r',\s*p=([0-9.eE+\-]+)', v.get('verdict',''))
                p_str = m.group(1) if m else '—'
                log(f"  {v.get('id','?')}: {v.get('verdict','?')[:80]}  p={p_str}")

            self.root.after(0, self._ma_pipeline_done, True)
        except Exception as ex:
            log(f"ERROR: {ex}")
            self.root.after(0, self._ma_pipeline_done, False)

    def _ma_pipeline_done(self, ok):
        self._ma_run_btn.config(state=tk.NORMAL, text="▶  Run Full Pipeline")
        self._ma_progress.stop()
        if ok:
            self._ma_log_append("Pipeline complete. Check Results and Plots tabs.")
            self._ma_refresh_results()
            self._ma_refresh_plots()

    # ── Sub-tab 4: Results ─────────────────────────────────────────────
    def _ma_build_results(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Hypothesis Results", style='H1.TLabel').pack(anchor='w', pady=(0,8))

        cols = ('hypothesis','verdict','dep_var','coefficient','p_value','stars','note')
        tree_f = ttk.Frame(f, style='Card.TFrame')
        tree_f.pack(fill=tk.BOTH, expand=True)
        vsb = ttk.Scrollbar(tree_f, orient='vertical')
        self._ma_res_tree = ttk.Treeview(tree_f, columns=cols, show='headings',
                                          yscrollcommand=vsb.set)
        vsb.config(command=self._ma_res_tree.yview)
        widths = [80, 100, 90, 100, 80, 60, 300]
        for col, w in zip(cols, widths):
            self._ma_res_tree.heading(col, text=col.replace('_',' ').title())
            self._ma_res_tree.column(col, width=w, anchor='center')
        self._ma_res_tree.tag_configure('support',  background='#dcfce7')
        self._ma_res_tree.tag_configure('reject',   background='#fee2e2')
        self._ma_res_tree.tag_configure('inconc',   background='#fef9c3')
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._ma_res_tree.pack(fill=tk.BOTH, expand=True)

        # Regression detail text
        ttk.Label(f, text="Regression Detail", style='H1.TLabel').pack(anchor='w', pady=(10,4))
        det_f = tk.Frame(f, bg='#0f172a')
        det_f.pack(fill=tk.BOTH, expand=True)
        self._ma_res_detail = tk.Text(det_f, font=FONT_MONO, background='#0f172a',
                                       foreground='#94e2d5', relief='flat',
                                       borderwidth=0, padx=10, pady=8, height=10)
        sb2 = tk.Scrollbar(det_f, command=self._ma_res_detail.yview, bg='#1e293b')
        self._ma_res_detail.config(yscrollcommand=sb2.set)
        sb2.pack(side=tk.RIGHT, fill=tk.Y)
        self._ma_res_detail.pack(fill=tk.BOTH, expand=True)
        self._ma_res_tree.bind('<<TreeviewSelect>>', self._ma_on_result_select)

    def _ma_refresh_results(self):
        self._ma_res_tree.delete(*self._ma_res_tree.get_children())
        verdicts = getattr(self, '_ma_verdicts', [])
        for v in verdicts:
            hyp     = v.get('id', '')
            verdict = v.get('verdict', '')
            note    = v.get('text', '')[:80]
            m_coef = re.search(r'β=([+-]?[0-9.eE+\-]+)', verdict)
            m_pval = re.search(r',\s*p=([0-9.eE+\-]+)', verdict)
            coef  = m_coef.group(1) if m_coef else 'N/A'
            pval  = 'N/A'
            stars = ''
            if m_pval:
                try:
                    p = float(m_pval.group(1))
                    pval  = f"{p:.4f}"
                    stars = '***' if p < 0.001 else '**' if p < 0.01 \
                            else '*' if p < 0.05 else ''
                except ValueError:
                    pass
            # Map hypothesis ID to the dep-var key used in self._ma_results
            _hyp_to_dep = {
                'H1': 'fall_signed', 'H2': 'ptdf_FI_abs',
                'H3': 'ram',         'H4': 'shadowPrice',
                'H5': '',            'H6': 'frm',
            }
            dep = _hyp_to_dep.get(hyp, '')
            # Fall back: try to parse from "regression for <dep>" in verdict
            if not dep:
                m_dep = re.search(r'regression for (\S+)', verdict)
                dep = m_dep.group(1) if m_dep else ''
            tag = ('support' if 'SUPPORTED' in verdict or 'SIGNIFICANT' in verdict
                   else 'reject' if 'inconclusive' in verdict.lower()
                   else 'inconc')
            self._ma_res_tree.insert('', tk.END,
                                     values=(hyp, verdict[:100], dep, coef, pval, stars, note),
                                     tags=(tag,))

    def _ma_on_result_select(self, event=None):
        sel = self._ma_res_tree.selection()
        if not sel:
            return
        vals = self._ma_res_tree.item(sel[0])['values']
        hyp = vals[0] if len(vals) > 0 else ''
        dep = vals[2] if len(vals) > 2 else ''
        # Try dep var first; fall back through hypothesis→dep map
        _hyp_to_dep = {
            'H1': 'fall_signed', 'H2': 'ptdf_FI_abs',
            'H3': 'ram',         'H4': 'shadowPrice',
            'H6': 'frm',
        }
        detail = (self._ma_results.get(dep)
                  or self._ma_results.get(_hyp_to_dep.get(hyp, ''), {}))
        # Render the coefs table in a readable format
        lines = []
        if detail and 'coefs' in detail:
            try:
                import pandas as _pd
                cf = detail['coefs']
                lines.append(f"n_obs={detail.get('n_obs','?')}  "
                             f"R²_within={detail.get('rsquared_within',float('nan')):.4f}  "
                             f"R²_overall={detail.get('rsquared_overall',float('nan')):.4f}")
                lines.append('')
                lines.append(f"{'Parameter':40s}  {'β':>10}  {'SE':>8}  {'p':>8}")
                lines.append('-' * 72)
                for _, r in cf.iterrows():
                    lines.append(f"{str(r.get('param',''))[:40]:40s}"
                                 f"  {r.get('coef',0):>10.4f}"
                                 f"  {r.get('std_err',0):>8.4f}"
                                 f"  {r.get('p',1):>8.4f}")
            except Exception:
                lines = [json.dumps(
                    {k: (round(v,4) if isinstance(v,float) else v)
                     for k,v in detail.items() if k not in ('raw_result','coefs')},
                    indent=2, default=str)]
        elif detail:
            lines = [json.dumps(
                {k: (round(v,4) if isinstance(v,float) else v)
                 for k,v in detail.items() if k not in ('raw_result','coefs')},
                indent=2, default=str)]
        else:
            lines = ['(no regression detail — model did not converge or was skipped)']
        txt = '\n'.join(lines)
        self._ma_res_detail.config(state='normal')
        self._ma_res_detail.delete('1.0', tk.END)
        self._ma_res_detail.insert('1.0', txt)
        self._ma_res_detail.config(state='disabled')

    # ── Sub-tab 5: Plots ───────────────────────────────────────────────
    def _ma_build_plots(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=8)
        f.pack(fill=tk.BOTH, expand=True)

        ctrl = ttk.Frame(f, style='Card.TFrame')
        ctrl.pack(fill=tk.X, pady=(0,4))
        ttk.Label(ctrl, text="Plot:").pack(side=tk.LEFT)
        self._ma_plot_type = ttk.Combobox(ctrl, state='readonly', width=28,
            values=["Time Series (RAM, Shadow Price, F0)",
                    "PTDF_FI Distribution (violin)",
                    "Shadow Price vs RAM (scatter)",
                    "CNEC Binding Frequency (bar)"])
        self._ma_plot_type.set("Time Series (RAM, Shadow Price, F0)")
        self._ma_plot_type.pack(side=tk.LEFT, padx=(4,12))
        ttk.Button(ctrl, text="Draw", style='Accent.TButton',
                   command=self._ma_draw_plot).pack(side=tk.LEFT)

        self._ma_toolbar_f5 = ttk.Frame(f, style='Card.TFrame')
        self._ma_toolbar_f5.pack(fill=tk.X)
        self._ma_fig_plots = Figure(figsize=(11, 7), dpi=90)
        self._style_figure(self._ma_fig_plots)
        self._ma_canvas_plots = FigureCanvasTkAgg(self._ma_fig_plots, f)
        self._ma_canvas_plots.get_tk_widget().pack(fill=tk.BOTH, expand=True)

    def _ma_refresh_plots(self):
        self._ma_draw_plot()

    def _ma_draw_plot(self):
        if self._ma_covariates is None:
            return
        import numpy as np
        cov = self._ma_covariates
        ptype = self._ma_plot_type.get()
        self._ma_fig_plots.clear()

        if "Time Series" in ptype:
            axes = [self._ma_fig_plots.add_subplot(3,1,i+1) for i in range(3)]
            for ax, col, color, label in zip(
                    axes,
                    ["ram","shadowPrice","f0"],
                    [C_GREEN, C_RED, C_PRIMARY],
                    ["RAM (MW)","Shadow Price (EUR/MWh)","F0 (MW)"]):
                if col not in cov.columns:
                    ax.set_title(f"{label} — not available"); continue
                agg = cov.groupby("dateTimeUtc")[col].mean().reset_index()
                agg = agg.sort_values("dateTimeUtc")
                ax.plot(range(len(agg)), agg[col], color=color, linewidth=0.9)
                ax.fill_between(range(len(agg)), agg[col], alpha=0.08, color=color)
                ax.set_title(label, fontsize=8.5, color=C_ACCENT)
                self._setup_ax(ax, [])

        elif "violin" in ptype.lower() or "PTDF" in ptype:
            ax = self._ma_fig_plots.add_subplot(111)
            ptdf_col = f"ptdf_{self._ma_src_country.get().upper()}"
            if ptdf_col in cov.columns:
                # FIX D: filter out CNECs with zero valid data to prevent matplotlib crash
                unique_cnecs = sorted(cov['cneName'].dropna().unique())
                data_by_cnec, labels = [], []
                for cnec in unique_cnecs:
                    vals = cov[cov['cneName'] == cnec][ptdf_col].dropna().values
                    if len(vals) > 0:
                        data_by_cnec.append(vals)
                        labels.append(str(cnec)[:18])
                if data_by_cnec:
                    ax.violinplot(data_by_cnec, positions=range(len(data_by_cnec)))
                    ax.set_xticks(range(len(labels)))
                    ax.set_xticklabels(labels, rotation=30, ha='right', fontsize=6)
                    ax.set_title(f"{ptdf_col} distribution by CNEC", color=C_ACCENT)
                    ax.axhline(0, color=C_BORDER, linewidth=0.8, linestyle='--')
                    self._setup_ax(ax, labels)
            else:
                ax.text(0.5, 0.5, f"{ptdf_col} not in data",
                        transform=ax.transAxes, ha='center', va='center', color=C_MUTED)

        elif "scatter" in ptype.lower():
            ax = self._ma_fig_plots.add_subplot(111)
            if 'ram' in cov.columns and 'shadowPrice' in cov.columns:
                sub = cov[['ram','shadowPrice']].dropna().sample(min(3000,len(cov)))
                ax.scatter(sub['ram'], sub['shadowPrice'],
                           alpha=0.25, s=6, color=C_PRIMARY)
                ax.set_xlabel("RAM (MW)", fontsize=8.5)
                ax.set_ylabel("Shadow Price (EUR/MWh)", fontsize=8.5)
                ax.set_title("Shadow Price vs RAM", color=C_ACCENT)
                self._setup_ax(ax, [])

        elif "Binding" in ptype:
            ax = self._ma_fig_plots.add_subplot(111)
            if 'shadowPrice' in cov.columns:
                freq = (cov.groupby("cneName")['shadowPrice']
                        .apply(lambda s: (s > 0.01).mean() * 100)
                        .sort_values(ascending=False).head(20))
                bars = ax.bar(range(len(freq)), freq.values, color=C_PRIMARY, alpha=0.8)
                ax.set_xticks(range(len(freq)))
                ax.set_xticklabels([c[:16] for c in freq.index],
                                   rotation=40, ha='right', fontsize=6)
                ax.set_ylabel("Binding frequency (%)", fontsize=8.5)
                ax.set_title("CNEC Binding Frequency (shadow price > 0)",
                             color=C_ACCENT)
                self._setup_ax(ax, [])

        self._ma_fig_plots.tight_layout(pad=2.0)
        self._add_toolbar(self._ma_canvas_plots, self._ma_toolbar_f5)

    # ── Sub-tab 6: Single Event ────────────────────────────────────────
    def _ma_build_single(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=8)
        f.pack(fill=tk.BOTH, expand=True)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)

        ttk.Label(f, text="Single Event Deep-Dive",
                  style='H1.TLabel').grid(row=0, column=0, sticky='w', pady=(0, 6))

        # ── Control panel ─────────────────────────────────────────────
        ctrl = ttk.LabelFrame(f, text="Select event", padding=8)
        ctrl.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        ctrl.columnconfigure(1, weight=1)

        # Row 0: Outage ID
        ttk.Label(ctrl, text="Outage ID:").grid(row=0, column=0, sticky='w')
        self._ma_single_id = ttk.Combobox(ctrl, width=50, state='readonly')
        self._ma_single_id.grid(row=0, column=1, columnspan=3, sticky='ew', padx=6)
        ttk.Button(ctrl, text="Refresh List",
                   command=self._ma_refresh_single_list).grid(row=0, column=4, padx=(6, 0))

        # Row 1: Baseline days + Post-event days
        ttk.Label(ctrl, text="Baseline (days before):").grid(row=1, column=0, sticky='w', pady=(6, 0))
        self._ma_baseline_days = ttk.Spinbox(ctrl, from_=2, to=365, width=6)
        self._ma_baseline_days.set(14)
        self._ma_baseline_days.grid(row=1, column=1, sticky='w', padx=6, pady=(6, 0))
        ttk.Label(ctrl, text="(2–365)", style='Hint.TLabel').grid(
            row=1, column=1, sticky='e', padx=(0, 4), pady=(6, 0))

        ttk.Label(ctrl, text="Post-event (days after):").grid(
            row=1, column=2, sticky='w', padx=(20, 0), pady=(6, 0))
        self._ma_post_days = ttk.Spinbox(ctrl, from_=1, to=90, width=6)
        self._ma_post_days.set(3)
        self._ma_post_days.grid(row=1, column=3, sticky='w', padx=6, pady=(6, 0))
        ttk.Label(ctrl, text="(1–90)", style='Hint.TLabel').grid(
            row=1, column=3, sticky='e', padx=(0, 4), pady=(6, 0))

        # Row 2: Counterfactual model selector
        ttk.Label(ctrl, text="Counterfactual model:").grid(
            row=2, column=0, sticky='w', pady=(8, 0))
        self._ma_its_method_var = tk.StringVar()
        _method_labels = {
            "seasonal_naive": "Seasonal Naive           [min 2 days]",
            "fourier_trend":  "Fourier + Linear Trend   [min 7 days]",
            "stl":            "STL Decomposition        [min 7 days]",
            "arima":          "ARIMA on residuals       [min 14 days, rec. 30]",
            "sarima":         "SARIMA hourly [m=24]     [min 14 days, rec. 30–90]",
            "all":            "All five  [run & compare side-by-side]",
        }
        _prop = getattr(self, '_prop', None)
        _method_keys = (list(_prop.ITS_METHOD_NAMES) + ["all"]) if _prop else list(_method_labels)
        _method_display = [_method_labels.get(k, k) for k in _method_keys]
        self._ma_its_method_map = {_method_labels.get(k, k): k for k in _method_keys}
        _method_cb = ttk.Combobox(ctrl, textvariable=self._ma_its_method_var,
                                   values=_method_display, state='readonly', width=55)
        _method_cb.grid(row=2, column=1, columnspan=3, sticky='ew', padx=6, pady=(8, 0))
        self._ma_its_method_var.set(_method_labels["seasonal_naive"])

        # Row 3: Dynamic hint label
        self._ma_its_hint_var = tk.StringVar()
        ttk.Label(ctrl, textvariable=self._ma_its_hint_var,
                  style='Hint.TLabel', wraplength=720).grid(
            row=3, column=0, columnspan=5, sticky='w', padx=4, pady=(3, 0))

        def _update_hint(*_):
            label = self._ma_its_method_var.get()
            key   = self._ma_its_method_map.get(label, "seasonal_naive")
            try:
                cur_bl = int(self._ma_baseline_days.get())
            except Exception:
                cur_bl = 14
            _p = getattr(self, '_prop', None)
            if key == "all":
                hint = ("Runs all five models and overlays their projected counterfactuals "
                        "on the ITS plot so you can compare them directly. "
                        "Summary metrics use Seasonal Naive as primary. "
                        "Tip: set baseline ≥30 days to get the best out of ARIMA and SARIMA.")
            elif _p and hasattr(_p, '_ITS_METHODS'):
                meta     = _p._ITS_METHODS.get(key, {})
                desc     = meta.get("description", "")
                min_days = int(meta.get("min_days", 2))
                warning  = ""
                if cur_bl < min_days:
                    warning = (f"  ⚠  Current baseline ({cur_bl} days) is below the minimum "
                               f"for this method ({min_days} days). "
                               f"Increase the baseline or results may degrade.")
                hint = desc + warning
            else:
                hint = ""
            self._ma_its_hint_var.set(hint)

        self._ma_its_method_var.trace_add("write", _update_hint)
        self._ma_baseline_days.configure(command=_update_hint)
        _update_hint()

        # Run button + waiting label
        self._ma_analyse_btn = ttk.Button(ctrl, text="Analyse Event",
                                           style='Accent.TButton',
                                           command=self._ma_run_single)
        self._ma_analyse_btn.grid(row=2, column=4, rowspan=2,
                                   padx=(12, 0), pady=(8, 0))
        self._ma_analyse_wait_lbl = ttk.Label(ctrl, text="", style='Muted.TLabel')
        self._ma_analyse_wait_lbl.grid(row=4, column=0, columnspan=5,
                                        sticky='w', padx=4, pady=(4, 0))

        # ── Inner notebook with analysis sub-tabs ─────────────────────
        snb = ttk.Notebook(f, style='App.TNotebook')
        snb.grid(row=2, column=0, sticky='nsew')
        self._ma_single_nb = snb

        # Summary tab
        sf0 = ttk.Frame(snb, style='Card.TFrame')
        snb.add(sf0, text='  Summary  ')
        sf0.columnconfigure(0, weight=1); sf0.rowconfigure(0, weight=1)
        self._ma_single_summary_txt = tk.Text(
            sf0, font=('Segoe UI', 9), wrap='word',
            background=C_PANEL, foreground=C_TEXT,
            relief='flat', padx=12, pady=8, state='disabled')
        sb0 = ttk.Scrollbar(sf0, command=self._ma_single_summary_txt.yview)
        self._ma_single_summary_txt.config(yscrollcommand=sb0.set)
        sb0.grid(row=0, column=1, sticky='ns')
        self._ma_single_summary_txt.grid(row=0, column=0, sticky='nsew')

        # ITS tab — tall scrollable figure
        sf1 = ttk.Frame(snb, style='Card.TFrame')
        snb.add(sf1, text='  ITS — before/after trend  ')
        sf1.columnconfigure(0, weight=1); sf1.rowconfigure(2, weight=1)
        self._ma_toolbar_its = ttk.Frame(sf1, style='Card.TFrame')
        self._ma_toolbar_its.grid(row=0, column=0, sticky='ew')
        # Legend row — populated dynamically in _ma_single_done
        self._ma_its_legend_bar = ttk.Frame(sf1, style='Card.TFrame', padding=(8, 3))
        self._ma_its_legend_bar.grid(row=1, column=0, sticky='ew')
        # Scrollable container for the matplotlib figure
        _its_scroll_frame = ttk.Frame(sf1, style='Card.TFrame')
        _its_scroll_frame.grid(row=2, column=0, sticky='nsew')
        _its_scroll_frame.columnconfigure(0, weight=1)
        _its_scroll_frame.rowconfigure(0, weight=1)
        _its_vsb = ttk.Scrollbar(_its_scroll_frame, orient='vertical')
        _its_vsb.grid(row=0, column=1, sticky='ns')
        self._ma_its_viewport = tk.Canvas(_its_scroll_frame, bg=C_PANEL,
                                           yscrollcommand=_its_vsb.set,
                                           highlightthickness=0)
        self._ma_its_viewport.grid(row=0, column=0, sticky='nsew')
        _its_vsb.config(command=self._ma_its_viewport.yview)
        # Bind mousewheel to scroll
        def _its_scroll(event):
            self._ma_its_viewport.yview_scroll(
                int(-1 * (event.delta / 120)), "units")
        self._ma_its_viewport.bind("<MouseWheel>", _its_scroll)
        # Figure and canvas created fresh on each analysis run
        self._ma_fig_single    = None
        self._ma_canvas_single = None
        self._ma_its_win_id    = None

        # RAM Decomposition tab
        sf2 = ttk.Frame(snb, style='Card.TFrame')
        snb.add(sf2, text='  ΔRAM decomposition  ')
        sf2.columnconfigure(0, weight=1); sf2.rowconfigure(1, weight=1)
        self._ma_toolbar_decomp = ttk.Frame(sf2, style='Card.TFrame')
        self._ma_toolbar_decomp.grid(row=0, column=0, sticky='ew')
        self._ma_fig_decomp = Figure(figsize=(11, 5), dpi=90)
        self._style_figure(self._ma_fig_decomp)
        self._ma_canvas_decomp = FigureCanvasTkAgg(self._ma_fig_decomp, sf2)
        self._ma_canvas_decomp.get_tk_widget().grid(row=1, column=0, sticky='nsew')

        # DiD text tab
        sf3 = ttk.Frame(snb, style='Card.TFrame')
        snb.add(sf3, text='  DiD (high vs low PTDF_FI)  ')
        sf3.columnconfigure(0, weight=1); sf3.rowconfigure(0, weight=1)
        self._ma_single_did_txt = tk.Text(
            sf3, font=('Segoe UI', 9), wrap='word',
            background=C_PANEL, foreground=C_TEXT,
            relief='flat', padx=12, pady=8, state='disabled')
        sb3 = ttk.Scrollbar(sf3, command=self._ma_single_did_txt.yview)
        self._ma_single_did_txt.config(yscrollcommand=sb3.set)
        sb3.grid(row=0, column=1, sticky='ns')
        self._ma_single_did_txt.grid(row=0, column=0, sticky='nsew')

        # CNEC Table tab
        sf4 = ttk.Frame(snb, style='Card.TFrame')
        snb.add(sf4, text='  Per-CNEC table  ')
        sf4.columnconfigure(0, weight=1); sf4.rowconfigure(0, weight=1)
        cnec_cols = ('cnec', 'pre_f0', 'during_f0', 'delta_f0',
                     'pre_ram', 'during_ram', 'delta_ram',
                     'pre_shadowPrice', 'during_shadowPrice', 'delta_shadowPrice')
        ct_vsb = ttk.Scrollbar(sf4, orient='vertical')
        ct_hsb = ttk.Scrollbar(sf4, orient='horizontal')
        self._ma_cnec_tree = ttk.Treeview(
            sf4, columns=cnec_cols, show='headings',
            yscrollcommand=ct_vsb.set, xscrollcommand=ct_hsb.set)
        ct_vsb.config(command=self._ma_cnec_tree.yview)
        ct_hsb.config(command=self._ma_cnec_tree.xview)
        for col in cnec_cols:
            lbl = col.replace('_', ' ').replace('during ', 'dur ').replace('shadowPrice', 'SP')
            self._ma_cnec_tree.heading(col, text=lbl)
            self._ma_cnec_tree.column(col, width=110 if col == 'cnec' else 80, anchor='center')
        ct_vsb.grid(row=0, column=1, sticky='ns')
        ct_hsb.grid(row=1, column=0, sticky='ew')
        self._ma_cnec_tree.grid(row=0, column=0, sticky='nsew')

    def _ma_refresh_single_list(self):
        if self._ma_outages_df is not None and not self._ma_outages_df.empty:
            ids = self._ma_outages_df['outage_id'].dropna().unique().tolist()
            self._ma_single_id['values'] = ids
            if ids:
                self._ma_single_id.set(ids[0])

    def _ma_run_single(self):
        if getattr(self, '_ma_single_running', False):
            return
        if self._ma_jao_df is None:
            messagebox.showwarning("Single Event", "Apply Setup first.")
            return
        if self._ma_outages_df is None:
            messagebox.showwarning("Single Event", "Load outages first.")
            return
        oid = self._ma_single_id.get().strip()
        if not oid:
            messagebox.showwarning("Single Event", "Select an outage ID.")
            return
        bd = int(self._ma_baseline_days.get())
        pd_days = int(self._ma_post_days.get())
        its_label = self._ma_its_method_var.get()
        its_key   = self._ma_its_method_map.get(its_label, "seasonal_naive")
        _prop = getattr(self, '_prop', None)
        if _prop and hasattr(_prop, 'ITS_METHOD_NAMES') and \
                its_key not in list(_prop.ITS_METHOD_NAMES) + ["all"]:
            its_key = getattr(_prop, 'ITS_DEFAULT_METHOD', 'seasonal_naive')
        self._ma_single_running = True
        self._ma_analyse_btn.config(state='disabled')
        self._ma_analyse_wait_lbl.config(text="⏳  Calculating, please wait…")
        threading.Thread(target=self._ma_single_thread,
                         args=(oid, bd, pd_days, its_key), daemon=True).start()

    def _ma_single_thread(self, outage_id, baseline_days,
                          post_days=3, its_method="seasonal_naive"):
        try:
            import pandas as pd
            row = self._ma_outages_df[
                self._ma_outages_df['outage_id'] == outage_id].iloc[0]
            src = self._ma_src_country.get()
            # Full dataset for multi-CNEC DiD. Drop rows with null cneName to
            # prevent sorted(cneName.unique()) crashing on mixed float/str.
            no3_df = self._ma_jao_df.copy()
            if 'cneName' in no3_df.columns:
                no3_df = no3_df[no3_df['cneName'].notna() &
                                (no3_df['cneName'].astype(str).str.strip() != '')]
            res = self._prop.single_event_analysis(
                no3_df, row,
                baseline_days=baseline_days,
                post_days=post_days,
                its_method=its_method,
                src=src.lower(),
                log_cb=lambda m: self.root.after(0, self._ma_log_append, m))
            self.root.after(0, self._ma_single_done, res)
        except Exception as ex:
            import traceback
            err = f"{ex}\n\n{traceback.format_exc()}"
            def _err_done():
                self._ma_single_running = False
                self._ma_analyse_btn.config(state='normal')
                self._ma_analyse_wait_lbl.config(text="")
                messagebox.showerror("Single Event Error", err)
            self.root.after(0, _err_done)

    def _ma_single_done(self, res):
        import numpy as np
        # Clear waiting indicator
        self._ma_single_running = False
        self._ma_analyse_btn.config(state='normal')
        self._ma_analyse_wait_lbl.config(text="")

        self._ma_single_res = res
        its       = res.get('its',        None)
        decomp    = res.get('decomp',     None)
        summary   = res.get('summary',    {})
        cnec_df   = res.get('cnec_table', None)
        did_est   = summary.get('did_estimates', {})
        its_summ  = summary.get('its_summary', {})

        # ── Summary tab ───────────────────────────────────────────────
        lines = [
            f"Asset:    {summary.get('asset_name','')}  ({summary.get('asset_type','')})",
            f"Status:   {summary.get('planned_or_forced','')}",
            f"Period:   {summary.get('start_utc','')} → {summary.get('end_utc','')}",
            f"Duration: {summary.get('duration_h','?')} h",
            f"CNECs:    {summary.get('n_cnecs',0)} | "
            f"Pre rows: {summary.get('n_pre_rows',0)} | "
            f"During: {summary.get('n_during_rows',0)} | "
            f"Post: {summary.get('n_post_rows',0)}",
            "",
            "── Parameter Shifts (during − pre mean) ──",
        ]
        for col in ['fall_signed', 'f0', 'ram', 'shadowPrice', 'frm', 'iva']:
            d = summary.get(f'delta_{col}')
            if d is None or (isinstance(d, float) and np.isnan(d)):
                continue
            pre_v  = summary.get(f'pre_mean_{col}', float('nan'))
            dur_v  = summary.get(f'during_mean_{col}', float('nan'))
            rec_v  = summary.get(f'recovery_raw_{col}')
            cf_imp = summary.get(f'impact_cf_{col}')
            cf_rec = summary.get(f'recovery_frac_{col}')
            row_str = f"  {col:16s}  pre={pre_v:+.2f}  during={dur_v:+.2f}  Δ={d:+.2f}"
            if rec_v is not None and not (isinstance(rec_v, float) and np.isnan(rec_v)):
                row_str += f"  post_Δ={rec_v:+.2f}"
            if cf_imp is not None and not (isinstance(cf_imp, float) and np.isnan(cf_imp)):
                row_str += f"  cf_impact={cf_imp:+.2f}"
            if cf_rec is not None and not (isinstance(cf_rec, float) and np.isnan(cf_rec)):
                row_str += f"  recovery={cf_rec:.0%}"
            lines.append(row_str)
        if its_summ:
            lines += ["", "── ITS Counterfactual Impacts ──"]
            for col, s in its_summ.items():
                imp = s.get('impact', float('nan'))
                frac = s.get('recovery_frac', float('nan'))
                lines.append(f"  {col:16s}  impact={imp:+.2f} MW  recovery={frac:.0%}")
        if did_est:
            lines += ["", "── DiD Estimates (PRE ∪ POST, DURING excluded) ──"]
            for col, est in did_est.items():
                b_int = est.get('beta_interaction', est.get('beta', float('nan')))
                p_int = est.get('p_interaction',    est.get('p',    float('nan')))
                b_post= est.get('beta_post', float('nan'))
                p_post= est.get('p_post', float('nan'))
                lines.append(f"  {col:16s}  "
                              f"β_post={b_post:+.4f}(p={p_post:.3f})  "
                              f"β_PTDF×post={b_int:+.4f}(p={p_int:.3f})")
                lines.append(f"    {est.get('interpretation','')}")
        txt = "\n".join(lines)
        self._ma_single_summary_txt.config(state='normal')
        self._ma_single_summary_txt.delete('1.0', tk.END)
        self._ma_single_summary_txt.insert(tk.END, txt)
        self._ma_single_summary_txt.config(state='disabled')

        # ── ITS tab ───────────────────────────────────────────────────
        import matplotlib.dates as mdates
        its_all = res.get('its_all', {})
        # Compute height based on number of params so each panel is readable
        n_params = len(its['param'].unique()) if its is not None and not its.empty else 1
        fig_h = max(5, 4.5 * n_params)
        # Destroy old canvas and create a fresh Figure — guarantees stale state never persists
        if self._ma_canvas_single is not None:
            try:
                self._ma_canvas_single.get_tk_widget().destroy()
            except Exception:
                pass
        self._ma_fig_single = Figure(figsize=(11, fig_h), dpi=90)
        self._style_figure(self._ma_fig_single)
        self._ma_canvas_single = FigureCanvasTkAgg(self._ma_fig_single,
                                                    self._ma_its_viewport)
        if self._ma_its_win_id is not None:
            self._ma_its_viewport.delete(self._ma_its_win_id)
        self._ma_its_win_id = self._ma_its_viewport.create_window(
            0, 0, anchor='nw', window=self._ma_canvas_single.get_tk_widget())
        _method_colors = {
            "seasonal_naive": (C_RED,    "--"),
            "fourier_trend":  (C_PRIMARY, "-."),
            "stl":            (C_GREEN,   ":"),
        }
        # Clear and rebuild the tkinter legend bar above the canvas
        for w in self._ma_its_legend_bar.winfo_children():
            w.destroy()
        legend_entries = []   # list of (hex_color, label_text)

        if its is not None and not its.empty:
            params = list(its['param'].unique())
            n = len(params)
            for i, param in enumerate(params):
                ax = self._ma_fig_single.add_subplot(n, 1, i + 1)
                # Actual values
                ref_df = list(its_all.values())[0] if its_all else its
                sub_actual = ref_df[ref_df['param'] == param].sort_values('dateTimeUtc')
                if param in sub_actual.columns:
                    ax.plot(sub_actual['dateTimeUtc'], sub_actual[param],
                            lw=1.2, color=C_ACCENT, zorder=5)
                    if i == 0:
                        legend_entries.append((C_ACCENT, 'Actual'))
                # Projected counterfactual(s)
                if its_all and len(its_all) > 1:
                    for mkey, mdf in its_all.items():
                        if mdf.empty:
                            continue
                        msub = mdf[mdf['param'] == param].sort_values('dateTimeUtc')
                        col_h, ls = _method_colors.get(mkey, (C_MUTED, "--"))
                        _p = getattr(self, '_prop', None)
                        mlabel = (_p._ITS_METHODS.get(mkey, {}).get("label", mkey)
                                  if _p and hasattr(_p, '_ITS_METHODS') else mkey)
                        ax.plot(msub['dateTimeUtc'], msub['projected'],
                                lw=1.0, ls=ls, color=col_h, alpha=0.85)
                        if i == 0:
                            legend_entries.append((col_h, f"Y(0) {mlabel}"))
                else:
                    sub = its[its['param'] == param].sort_values('dateTimeUtc')
                    ax.plot(sub['dateTimeUtc'], sub['projected'],
                            lw=1.0, ls='--', color=C_RED)
                    # ax.fill_between(sub['dateTimeUtc'],
                    #                 sub[param] if param in sub.columns else sub['projected'],
                    #                 sub['projected'], alpha=0.12, color=C_AMBER)
                    if i == 0:
                        legend_entries.append((C_RED, 'Y(0) counterfactual'))
                # Outage shading
                try:
                    s_ts = summary.get('start_utc', '')
                    e_ts = summary.get('end_utc', '')
                    if s_ts and e_ts:
                        import pandas as pd
                        ax.axvspan(pd.Timestamp(s_ts), pd.Timestamp(e_ts),
                                   alpha=0.12, color=C_AMBER)
                        if i == 0:
                            legend_entries.append((C_AMBER, 'Outage window'))
                except Exception:
                    pass
                ax.set_ylabel(param, fontsize=8)
                ax.grid(True, ls=':', alpha=0.4)
                if i == 0:
                    n_models = len(its_all)
                    suffix = f" — all {n_models} models" if n_models > 1 else ""
                    ax.set_title(
                        f"ITS counterfactual{suffix}: {summary.get('asset_name','')[:45]}",
                        fontsize=9)
                try:
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H"))
                    for lbl in ax.get_xticklabels():
                        lbl.set_rotation(15); lbl.set_fontsize(7)
                except Exception:
                    pass
        else:
            ax = self._ma_fig_single.add_subplot(1, 1, 1)
            ax.text(0.5, 0.5, 'No ITS data', ha='center', va='center',
                    transform=ax.transAxes, color=C_MUTED)

        # Build tkinter legend bar (coloured swatch + label for each entry)
        for color, label in legend_entries:
            swatch = tk.Frame(self._ma_its_legend_bar, bg=color,
                              width=16, height=16, relief='flat')
            swatch.pack(side=tk.LEFT, padx=(0, 4))
            swatch.pack_propagate(False)
            tk.Label(self._ma_its_legend_bar, text=label,
                     font=('Segoe UI', 9), bg=C_PANEL,
                     fg=C_TEXT).pack(side=tk.LEFT, padx=(0, 16))

        self._ma_fig_single.tight_layout(pad=1.5)
        self._ma_canvas_single.draw()
        # Update toolbar to point at new canvas
        self._add_toolbar(self._ma_canvas_single, self._ma_toolbar_its)
        # Expand scroll region to fit the full figure height
        self._ma_canvas_single.get_tk_widget().update_idletasks()
        pw = self._ma_canvas_single.get_tk_widget().winfo_reqwidth()
        ph = self._ma_canvas_single.get_tk_widget().winfo_reqheight()
        self._ma_its_viewport.configure(scrollregion=(0, 0, pw, ph))
        self._ma_its_viewport.yview_moveto(0)   # scroll back to top

        # ── RAM Decomp tab ────────────────────────────────────────────
        self._ma_fig_decomp.clear()
        if decomp is not None and not decomp.empty and 'component' in decomp.columns:
            # Support both 'MW' (propagation.py) and 'delta_mw' column names
            mw_col = 'MW' if 'MW' in decomp.columns else 'delta_mw' \
                if 'delta_mw' in decomp.columns else None
            if mw_col:
                ax = self._ma_fig_decomp.add_subplot(1, 1, 1)
                agg = decomp[~decomp['component'].isin(['Δram observed'])].groupby(
                    'component')[mw_col].mean().sort_values()
                colors = [C_GREEN if v >= 0 else C_RED for v in agg.values]
                ax.barh(agg.index, agg.values, color=colors, alpha=0.8)
                ax.axvline(0, color=C_BORDER, linewidth=0.8)
                ax.set_xlabel("Average MW contribution to ΔRAM", color=C_TEXT)
                ax.set_title(f"ΔRAM Decomposition — {summary.get('asset_name','')[:50]}",
                             color=C_ACCENT, fontsize=9)
                ax.grid(True, axis='x', ls=':', alpha=0.4)
                self._setup_ax(ax, [])
            else:
                ax = self._ma_fig_decomp.add_subplot(1, 1, 1)
                ax.text(0.5, 0.5, 'No decomposition data', ha='center', va='center',
                        transform=ax.transAxes, color=C_MUTED)
        else:
            ax = self._ma_fig_decomp.add_subplot(1, 1, 1)
            ax.text(0.5, 0.5, 'No decomposition data', ha='center', va='center',
                    transform=ax.transAxes, color=C_MUTED)
        self._ma_fig_decomp.tight_layout(pad=1.5)
        self._add_toolbar(self._ma_canvas_decomp, self._ma_toolbar_decomp)
        self._ma_canvas_decomp.draw()

        # ── DiD text tab ─────────────────────────────────────────────
        self._ma_single_did_txt.config(state='normal')
        self._ma_single_did_txt.delete('1.0', tk.END)
        did = res.get('did', None)
        if did is not None and not (hasattr(did, 'empty') and did.empty):
            self._ma_single_did_txt.insert(tk.END,
                "Difference-in-Differences: CNECs with high |PTDF_FI| (treatment)\n"
                "vs CNECs with low |PTDF_FI| (control).\n"
                "ATT = (high_during − high_pre) − (low_during − low_pre)\n"
                "Positive ATT means the outage loaded the high-PTDF CNECs more.\n\n")
            try:
                import pandas as pd
                if isinstance(did, pd.DataFrame) and not did.empty:
                    pivot = did.pivot_table(index=['group', 'param'],
                                            columns='period', values='mean')
                    self._ma_single_did_txt.insert(tk.END, pivot.to_string())
            except Exception:
                pass
            if did_est:
                self._ma_single_did_txt.insert(tk.END, "\n\n── ATT estimates ──\n")
                for col, est in did_est.items():
                    if isinstance(est, dict):
                        b_int  = est.get('beta_interaction', est.get('beta', float('nan')))
                        p_int  = est.get('p_interaction',    est.get('p',    float('nan')))
                        b_post = est.get('beta_post', float('nan'))
                        p_post = est.get('p_post',    float('nan'))
                        interp = est.get('interpretation', '')
                        self._ma_single_did_txt.insert(tk.END,
                            f"  {col}: β_post={b_post:+.4f}(p={p_post:.3f})  "
                            f"β_PTDF×post={b_int:+.4f}(p={p_int:.3f})  {interp}\n")
                    else:
                        self._ma_single_did_txt.insert(tk.END,
                            f"  {col}: ATT={float(est):+.2f} MW\n")
        else:
            self._ma_single_did_txt.insert(tk.END, "No DiD data available.")
        self._ma_single_did_txt.config(state='disabled')

        # ── CNEC Table tab ────────────────────────────────────────────
        self._ma_cnec_tree.delete(*self._ma_cnec_tree.get_children())
        if cnec_df is not None and not cnec_df.empty:
            for _, r in cnec_df.iterrows():
                vals = []
                for c in ('cnec', 'pre_f0', 'during_f0', 'delta_f0',
                          'pre_ram', 'during_ram', 'delta_ram',
                          'pre_shadowPrice', 'during_shadowPrice', 'delta_shadowPrice'):
                    v = r.get(c, '')
                    if isinstance(v, float):
                        vals.append('—' if np.isnan(v) else f'{v:.2f}')
                    else:
                        vals.append(str(v)[:50])
                self._ma_cnec_tree.insert('', tk.END, values=vals)

        # Switch to Summary sub-tab automatically
        self._ma_single_nb.select(0)

    # ── Sub-tab 7: Explain ────────────────────────────────────────────
    def _ma_build_explain(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Plain-Language Findings", style='H1.TLabel').pack(anchor='w', pady=(0,8))
        ttk.Button(f, text="Generate Report", style='Accent.TButton',
                   command=self._ma_generate_explain).pack(anchor='w', pady=(0,8))

        txt_f = tk.Frame(f, bg=C_PANEL)
        txt_f.pack(fill=tk.BOTH, expand=True)
        self._ma_explain_txt = tk.Text(txt_f, font=('Segoe UI', 10), wrap='word',
                                        background=C_PANEL, foreground=C_TEXT,
                                        relief='flat', padx=12, pady=8)
        sb = ttk.Scrollbar(txt_f, command=self._ma_explain_txt.yview)
        self._ma_explain_txt.config(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._ma_explain_txt.pack(fill=tk.BOTH, expand=True)

    def _ma_generate_explain(self):
        verdicts = getattr(self, '_ma_verdicts', [])
        src  = self._ma_src_country.get()
        cnec = self._ma_target_cnec.get()
        n_cov = len(self._ma_covariates) if self._ma_covariates is not None else 0
        n_out = len(self._ma_outages_df) if self._ma_outages_df is not None else 0

        lines = [
            f"MAINTENANCE ANALYSIS REPORT",
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"Source country: {src}  |  Target CNEC: {cnec}",
            f"Dataset: {n_cov:,} observations  |  Outage events: {n_out}",
            "",
            "HYPOTHESIS SUMMARY",
            "=" * 60,
        ]
        if not verdicts:
            lines.append("No results yet. Run the pipeline first.")
        else:
            for v in verdicts:
                hyp     = v.get('id','')
                verdict = v.get('verdict','')
                note    = v.get('text','')
                m_c = re.search(r'β=([+-]?[0-9.eE+\-]+)', verdict)
                m_p = re.search(r',\s*p=([0-9.eE+\-]+)', verdict)
                lines.append(f"\n{hyp}: {verdict}")
                if m_c and m_p:
                    lines.append(f"  Coefficient: {m_c.group(1)}  |  p-value: {m_p.group(1)}")
                if note:
                    lines.append(f"  Hypothesis: {note}")
                # Plain-language interpretation
                if 'SUPPORTED' in verdict or 'SIGNIFICANT' in verdict:
                    lines.append(f"  Interpretation: Evidence found that {src} outages "
                                 f"propagate to CNEC '{cnec}' parameters.")
                elif 'reject' in verdict.lower() or 'inconclusive' in verdict.lower():
                    lines.append(f"  Interpretation: No statistically significant "
                                 f"propagation detected for this hypothesis.")
                else:
                    lines.append(f"  Interpretation: Results are inconclusive "
                                 f"— more data may be needed.")

        lines += [
            "",
            "=" * 60,
            "METHODOLOGY",
            "Panel OLS regression with CNEC entity fixed effects and clustered",
            "standard errors by date. Holm-Bonferroni correction applied.",
            "Interrupted time series uses seasonal-naive (hour x weekday) baseline.",
        ]
        report = "\n".join(lines)
        self._ma_explain_txt.config(state='normal')
        self._ma_explain_txt.delete('1.0', tk.END)
        self._ma_explain_txt.insert('1.0', report)

    # ── Sub-tab 8: Export ──────────────────────────────────────────────
    def _ma_build_export(self, parent):
        f = ttk.Frame(parent, style='Card.TFrame', padding=16)
        f.pack(fill=tk.BOTH, expand=True)

        ttk.Label(f, text="Export Results", style='H1.TLabel').pack(anchor='w', pady=(0,8))

        out_f = ttk.LabelFrame(f, text=" Output Directory ", padding=10)
        out_f.pack(fill=tk.X, pady=(0,10))
        row = ttk.Frame(out_f, style='Card.TFrame')
        row.pack(fill=tk.X)
        self._ma_export_dir = ttk.Entry(row, width=50)
        self._ma_export_dir.insert(0, os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "ma_output"))
        self._ma_export_dir.pack(side=tk.LEFT, padx=(0,6))
        ttk.Button(row, text="Browse…",
                   command=lambda: (
                       self._ma_export_dir.delete(0, tk.END),
                       self._ma_export_dir.insert(0, filedialog.askdirectory() or
                                                   self._ma_export_dir.get()))
                   ).pack(side=tk.LEFT)

        btn_f = ttk.Frame(f, style='Card.TFrame')
        btn_f.pack(fill=tk.X, pady=(0,8))
        ttk.Button(btn_f, text="Export Covariates CSV",
                   command=lambda: self._ma_export_csv('covariates')).pack(side=tk.LEFT, padx=(0,8))
        ttk.Button(btn_f, text="Export Outages CSV",
                   command=lambda: self._ma_export_csv('outages')).pack(side=tk.LEFT, padx=(0,8))
        ttk.Button(btn_f, text="Export Hypothesis Verdicts CSV",
                   command=lambda: self._ma_export_csv('verdicts')).pack(side=tk.LEFT, padx=(0,8))
        ttk.Button(btn_f, text="Export HTML Report",
                   command=self._ma_export_html).pack(side=tk.LEFT)

        self._ma_export_status = ttk.Label(f, text="", style='Muted.TLabel')
        self._ma_export_status.pack(anchor='w', pady=(4,0))

    def _ma_export_csv(self, what):
        import pandas as pd
        out_dir = self._ma_export_dir.get().strip()
        os.makedirs(out_dir, exist_ok=True)
        try:
            if what == 'covariates' and self._ma_covariates is not None:
                path = os.path.join(out_dir, 'ma_covariates.csv')
                self._ma_covariates.to_csv(path, index=False)
            elif what == 'outages' and self._ma_outages_df is not None:
                path = os.path.join(out_dir, 'ma_outages.csv')
                self._ma_outages_df.to_csv(path, index=False)
            elif what == 'verdicts':
                verdicts = getattr(self, '_ma_verdicts', [])
                if not verdicts:
                    messagebox.showinfo("Export", "No results yet. Run pipeline first.")
                    return
                path = os.path.join(out_dir, 'ma_verdicts.csv')
                pd.DataFrame(verdicts).to_csv(path, index=False)
            else:
                messagebox.showinfo("Export", "Nothing to export yet.")
                return
            self._ma_export_status.config(
                text=f"Saved: {os.path.basename(path)}", foreground=C_GREEN)
        except Exception as e:
            messagebox.showerror("Export Error", str(e))

    def _ma_export_html(self):
        import base64, io
        out_dir  = self._ma_export_dir.get().strip()
        os.makedirs(out_dir, exist_ok=True)
        verdicts     = getattr(self, '_ma_verdicts', [])
        explain_txt  = self._ma_explain_txt.get('1.0', tk.END)

        # Capture current plots figure as embedded PNG
        figs_html = ""
        for fig, title in [(self._ma_fig_plots, "Population Plots"),
                           (self._ma_fig_single, "Single Event")] \
                          if self._ma_fig_single is not None else \
                          [(self._ma_fig_plots, "Population Plots")]:
            try:
                buf = io.BytesIO()
                fig.savefig(buf, format='png', dpi=90, bbox_inches='tight')
                buf.seek(0)
                b64 = base64.b64encode(buf.read()).decode()
                figs_html += (f'<h3>{title}</h3>'
                              f'<img src="data:image/png;base64,{b64}" '
                              f'style="max-width:100%">')
            except Exception:
                pass

        def _html_row(v):
            hyp     = v.get('id', '')
            verdict = v.get('verdict', '')
            note    = v.get('text', '')
            m_dep = re.search(r'regression for (\S+)', verdict)
            dep   = m_dep.group(1) if m_dep else ''
            m_p   = re.search(r',\s*p=([0-9.eE+\-]+)', verdict)
            pval  = m_p.group(1) if m_p else ''
            return (f"<tr><td>{hyp}</td>"
                    f"<td><b>{verdict}</b></td>"
                    f"<td>{dep}</td>"
                    f"<td>{pval}</td>"
                    f"<td>{note}</td></tr>")
        rows_html = "".join(_html_row(v) for v in verdicts)

        html = f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<title>Maintenance Analysis Report</title>
<style>
body{{font-family:Segoe UI,sans-serif;margin:40px;color:#1e293b;}}
h1{{color:#1e3a5f;}} h2{{color:#2563eb;border-bottom:1px solid #d1d9e6;padding-bottom:4px;}}
table{{border-collapse:collapse;width:100%;margin-bottom:24px;}}
th{{background:#1e3a5f;color:white;padding:8px;text-align:left;}}
td{{padding:6px 8px;border-bottom:1px solid #e2e8f0;}}
pre{{background:#f1f5f9;padding:16px;border-radius:4px;overflow:auto;}}
</style></head><body>
<h1>Maintenance Analysis Report</h1>
<p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
<h2>Hypothesis Results</h2>
<table><tr><th>Hypothesis</th><th>Verdict</th><th>Dep. Var</th>
<th>p-value</th><th>Note</th></tr>{rows_html}</table>
<h2>Findings</h2><pre>{explain_txt}</pre>
<h2>Charts</h2>{figs_html}
</body></html>"""

        path = os.path.join(out_dir, 'ma_report.html')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(html)
        self._ma_export_status.config(
            text=f"HTML report saved: {os.path.basename(path)}", foreground=C_GREEN)


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()