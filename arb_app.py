"""
Arb Finder -- desktop UI for spotting arbitrage across betting sites.

Run:
    python arb_app.py                 # starts on the built-in demo feed
    python arb_app.py --key YOUR_KEY  # or set ODDS_API_KEY in the environment

Requires only the Python standard library (Tk ships with most Python builds;
on Debian/Ubuntu install it with: sudo apt install python3-tk).
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
from datetime import datetime
from typing import Dict, List, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk
except ImportError:  # pragma: no cover - depends on the local Python build
    sys.exit(
        "Tk is not available in this Python build.\n"
        "Debian/Ubuntu: sudo apt install python3-tk\n"
        "Fedora:        sudo dnf install python3-tkinter\n"
        "macOS/Windows: use the installer from python.org, which includes Tk."
    )

import arb_core as core

POLL_MS = 120
CURRENCIES = ("£", "$", "€", "A$", "C$")

# Muted slate palette with a single blue accent; flagged rows go amber.
BG = "#f4f5f7"
PANEL = "#ffffff"
INK = "#1d2430"
MUTED = "#6b7480"
ACCENT = "#1f5fa8"
GOOD = "#12683f"
WARN = "#8a5300"
WARN_BG = "#fdf4e3"
STRIPE = "#f8f9fb"


def _mono_family(root: tk.Misc) -> str:
    available = set(tkfont.families(root))
    for name in ("SF Mono", "Menlo", "Consolas", "DejaVu Sans Mono",
                 "Liberation Mono", "Courier New"):
        if name in available:
            return name
    return "TkFixedFont"


class ArbFinderApp:
    """The whole window. All Tk calls happen on the main thread; scans run in a
    worker and hand results back through a queue."""

    def __init__(self, root: tk.Tk, api_key: str = "", demo: bool = True):
        self.root = root
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.opportunities: List[core.Opportunity] = []
        self.row_map: Dict[str, core.Opportunity] = {}
        self.sort_column = "roi"
        self.sort_desc = True
        self.scanning = False
        self.auto_job: Optional[str] = None
        self.sports: List[tuple] = list(core.DEFAULT_SPORTS)

        root.title("Arb Finder")
        root.geometry("1320x820")
        root.minsize(1040, 660)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)

        self._build_style()
        self._build_variables(api_key, demo)
        self._build_layout()
        self._sync_source_state()
        self.root.after(POLL_MS, self._poll)
        self._set_status("Ready. The demo feed needs no key -- press Find arbs to see how results look.")

    # ---------------------------------------------------------------- setup

    def _build_style(self) -> None:
        self.mono = _mono_family(self.root)
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=("TkDefaultFont", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BG, foreground=INK)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Status.TLabel", background=BG, foreground=MUTED)
        style.configure("Head.TLabel", background=BG, foreground=MUTED,
                        font=("TkDefaultFont", 9, "bold"))
        style.configure("Title.TLabel", background=BG, foreground=INK,
                        font=("TkDefaultFont", 15, "bold"))
        style.configure("Good.TLabel", background=BG, foreground=GOOD,
                        font=("TkDefaultFont", 11, "bold"))
        style.configure("Warn.TLabel", background=WARN_BG, foreground=WARN)
        style.configure("TLabelframe", background=BG, bordercolor="#d8dce2")
        style.configure("TLabelframe.Label", background=BG, foreground=MUTED,
                        font=("TkDefaultFont", 9, "bold"))
        style.configure("TCheckbutton", background=BG)
        style.configure("TRadiobutton", background=BG)
        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                        font=("TkDefaultFont", 10, "bold"), padding=(14, 7))
        style.map("Accent.TButton",
                  background=[("active", "#1a4f8c"), ("disabled", "#a9b6c6")])
        style.configure("TButton", padding=(10, 6))
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=INK, rowheight=25, font=(self.mono, 10),
                        bordercolor="#d8dce2")
        style.configure("Treeview.Heading", font=("TkDefaultFont", 9, "bold"),
                        background="#e9ecf1", foreground=INK, padding=(6, 5))
        style.map("Treeview", background=[("selected", "#d7e6f7")],
                  foreground=[("selected", INK)])

    def _build_variables(self, api_key: str, demo: bool) -> None:
        self.source_var = tk.StringVar(value="demo" if demo else "live")
        self.key_var = tk.StringVar(value=api_key)
        self.sport_var = tk.StringVar(value=self.sports[0][1])
        self.region_vars = {r: tk.BooleanVar(value=(r == "uk")) for r in core.REGIONS}
        self.market_vars = {m: tk.BooleanVar(value=True) for m in core.MARKETS}
        self.stake_var = tk.StringVar(value="100")
        self.increment_var = tk.StringVar(value="0.01")
        self.min_roi_var = tk.StringVar(value="0.5")
        self.max_age_var = tk.StringVar(value="15")
        self.commission_var = tk.StringVar(value="")
        self.currency_var = tk.StringVar(value="£")
        self.coverage_var = tk.BooleanVar(value=True)
        self.distinct_var = tk.BooleanVar(value=True)
        self.auto_var = tk.BooleanVar(value=False)
        self.interval_var = tk.StringVar(value="60")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._render_rows())
        self.status_var = tk.StringVar()
        self.quota_var = tk.StringVar(value="")
        self.summary_var = tk.StringVar(value="Select an opportunity to see the stakes.")

    def _build_layout(self) -> None:
        outer = ttk.Frame(self.root, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="Arb Finder", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="  Prices that disagree enough to pay whatever happens",
                  style="Muted.TLabel").pack(side="left", padx=(8, 0))
        ttk.Label(header, textvariable=self.quota_var, style="Muted.TLabel").pack(side="right")

        self._build_source_box(outer, row=1)
        self._build_settings_box(outer, row=2)
        self._build_results(outer, row=3)

        status = ttk.Frame(outer)
        status.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel").pack(side="left")

    def _build_source_box(self, parent: ttk.Frame, row: int) -> None:
        box = ttk.Labelframe(parent, text="WHERE THE PRICES COME FROM", padding=(12, 8))
        box.grid(row=row, column=0, sticky="ew", pady=(0, 8))

        line1 = ttk.Frame(box)
        line1.pack(fill="x")
        ttk.Radiobutton(line1, text="Demo feed", value="demo", variable=self.source_var,
                        command=self._sync_source_state).pack(side="left")
        ttk.Radiobutton(line1, text="Live feed (the-odds-api.com)", value="live",
                        variable=self.source_var,
                        command=self._sync_source_state).pack(side="left", padx=(14, 18))
        ttk.Label(line1, text="API key").pack(side="left")
        self.key_entry = ttk.Entry(line1, textvariable=self.key_var, width=34, show="*")
        self.key_entry.pack(side="left", padx=(6, 10))
        self.sports_button = ttk.Button(line1, text="Load sports", command=self.load_sports)
        self.sports_button.pack(side="left")
        ttk.Label(line1, text="   A live scan spends one call per region per market.",
                  style="Muted.TLabel").pack(side="left")

        line2 = ttk.Frame(box)
        line2.pack(fill="x", pady=(10, 0))
        ttk.Label(line2, text="Sport").pack(side="left")
        self.sport_box = ttk.Combobox(line2, textvariable=self.sport_var, state="readonly",
                                      width=30, values=[title for _, title in self.sports])
        self.sport_box.pack(side="left", padx=(6, 20))
        ttk.Label(line2, text="Regions", style="Head.TLabel").pack(side="left", padx=(0, 6))
        for region in core.REGIONS:
            ttk.Checkbutton(line2, text=region.upper(),
                            variable=self.region_vars[region]).pack(side="left", padx=(0, 6))
        ttk.Label(line2, text="   Markets", style="Head.TLabel").pack(side="left", padx=(10, 6))
        labels = {"h2h": "Match odds", "spreads": "Handicaps", "totals": "Over/under"}
        for market in core.MARKETS:
            ttk.Checkbutton(line2, text=labels[market],
                            variable=self.market_vars[market]).pack(side="left", padx=(0, 8))


    def _build_settings_box(self, parent: ttk.Frame, row: int) -> None:
        box = ttk.Labelframe(parent, text="HOW TO STAKE AND WHAT TO KEEP", padding=(12, 8))
        box.grid(row=row, column=0, sticky="ew", pady=(0, 10))

        line1 = ttk.Frame(box)
        line1.pack(fill="x")

        def field(parent_frame, label, var, width):
            ttk.Label(parent_frame, text=label).pack(side="left")
            entry = ttk.Entry(parent_frame, textvariable=var, width=width)
            entry.pack(side="left", padx=(6, 16))
            return entry

        ttk.Label(line1, text="Currency").pack(side="left")
        ttk.Combobox(line1, textvariable=self.currency_var, values=CURRENCIES,
                     state="readonly", width=4).pack(side="left", padx=(6, 16))
        field(line1, "Total stake", self.stake_var, 9)
        field(line1, "Round stakes to", self.increment_var, 7)
        field(line1, "Minimum return %", self.min_roi_var, 6)
        field(line1, "Ignore prices older than (min)", self.max_age_var, 6)

        line2 = ttk.Frame(box)
        line2.pack(fill="x", pady=(10, 0))
        ttk.Label(line2, text="Commission, e.g. betfair:5, smarkets:2").pack(side="left")
        ttk.Entry(line2, textvariable=self.commission_var, width=30).pack(side="left", padx=(6, 18))
        ttk.Checkbutton(line2, text="Only books quoting every outcome",
                        variable=self.coverage_var).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(line2, text="At least two bookmakers",
                        variable=self.distinct_var).pack(side="left")

        line3 = ttk.Frame(box)
        line3.pack(fill="x", pady=(10, 0))
        self.scan_button = ttk.Button(line3, text="Find arbs", style="Accent.TButton",
                                      command=self.start_scan)
        self.scan_button.pack(side="left")
        ttk.Checkbutton(line3, text="Repeat every", variable=self.auto_var,
                        command=self._sync_auto).pack(side="left", padx=(14, 4))
        ttk.Entry(line3, textvariable=self.interval_var, width=5).pack(side="left")
        ttk.Label(line3, text="seconds").pack(side="left", padx=(4, 18))
        self.export_button = ttk.Button(line3, text="Save as CSV", command=self.export_csv,
                                        state="disabled")
        self.export_button.pack(side="left")
        ttk.Label(line3, text="Filter").pack(side="left", padx=(18, 4))
        ttk.Entry(line3, textvariable=self.filter_var, width=26).pack(side="left")

    def _build_results(self, parent: ttk.Frame, row: int) -> None:
        panes = ttk.PanedWindow(parent, orient="vertical")
        panes.grid(row=row, column=0, sticky="nsew")

        top = ttk.Frame(panes)
        panes.add(top, weight=3)
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)

        self.columns = (
            ("roi", "Return %", 80, "e"),
            ("profit", "Profit", 88, "e"),
            ("stake", "Stake", 88, "e"),
            ("sport", "Sport", 84, "w"),
            ("event", "Event", 244, "w"),
            ("starts", "Starts", 152, "w"),
            ("market", "Market", 96, "w"),
            ("line", "Line", 54, "e"),
            ("books", "Bookmakers", 196, "w"),
            ("flags", "Notes", 148, "w"),
        )
        self.tree = ttk.Treeview(top, columns=[c[0] for c in self.columns],
                                 show="headings", selectmode="browse")
        for key, title, width, anchor in self.columns:
            self.tree.heading(key, text=title, command=lambda k=key: self._sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "event"))
        self.tree.tag_configure("odd", background=STRIPE)
        self.tree.tag_configure("flagged", background=WARN_BG, foreground=WARN)
        self.tree.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        bottom = ttk.Frame(panes, padding=(0, 8, 0, 0))
        panes.add(bottom, weight=2)
        bottom.rowconfigure(2, weight=1)
        bottom.columnconfigure(0, weight=1)

        ttk.Label(bottom, textvariable=self.summary_var,
                  style="Good.TLabel").grid(row=0, column=0, sticky="w")
        self.warning_label = ttk.Label(bottom, text="", style="Warn.TLabel", padding=(6, 3))
        self.warning_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.warning_label.grid_remove()

        leg_columns = (
            ("outcome", "Bet on", 220, "w"),
            ("book", "Bookmaker", 190, "w"),
            ("odds", "Odds", 80, "e"),
            ("net", "After commission", 152, "e"),
            ("stake", "Stake", 110, "e"),
            ("payout", "Pays", 110, "e"),
            ("updated", "Priced at", 120, "w"),
        )
        self.legs = ttk.Treeview(bottom, columns=[c[0] for c in leg_columns],
                                 show="headings", height=5, selectmode="none")
        for key, title, width, anchor in leg_columns:
            self.legs.heading(key, text=title)
            self.legs.column(key, width=width, anchor=anchor, stretch=(key == "outcome"))
        self.legs.tag_configure("odd", background=STRIPE)
        self.legs.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        leg_bar = ttk.Scrollbar(bottom, orient="vertical", command=self.legs.yview)
        leg_bar.grid(row=2, column=1, sticky="ns", pady=(6, 0))
        self.legs.configure(yscrollcommand=leg_bar.set)

    # ------------------------------------------------------------- helpers

    def _sync_source_state(self) -> None:
        live = self.source_var.get() == "live"
        state = "normal" if live else "disabled"
        self.key_entry.configure(state=state)
        self.sports_button.configure(state=state)
        self.sport_box.configure(state="readonly" if live else "disabled")

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def money(self, value: float) -> str:
        return f"{self.currency_var.get()}{value:,.2f}"

    @staticmethod
    def _local(stamp: Optional[datetime], fmt: str = "%a %d %b %H:%M") -> str:
        return stamp.astimezone().strftime(fmt) if stamp else "-"

    def _selected_sport_key(self) -> str:
        title = self.sport_var.get()
        for key, label in self.sports:
            if label == title:
                return key
        return self.sports[0][0]

    def _parse_commissions(self, text: str) -> Dict[str, float]:
        result: Dict[str, float] = {}
        for chunk in text.replace(";", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if ":" not in chunk:
                raise ValueError(
                    f"Commission needs a bookmaker and a percentage, like betfair:5 -- got '{chunk}'."
                )
            key, _, value = chunk.partition(":")
            key = key.strip().lower()
            if not key:
                raise ValueError(f"Commission entry '{chunk}' is missing a bookmaker key.")
            try:
                percent = float(value.strip())
            except ValueError:
                raise ValueError(f"'{value.strip()}' is not a percentage in '{chunk}'.") from None
            if not 0.0 <= percent < 100.0:
                raise ValueError(f"Commission must be between 0 and 100, got {percent}.")
            result[key] = percent / 100.0
        return result

    def _read_config(self) -> dict:
        def number(var, label, minimum=None, allow_blank=False):
            raw = var.get().strip()
            if not raw and allow_blank:
                return None
            try:
                value = float(raw)
            except ValueError:
                raise ValueError(f"{label} must be a number, got '{raw}'.") from None
            if minimum is not None and value < minimum:
                raise ValueError(f"{label} must be at least {minimum}.")
            return value

        stake = number(self.stake_var, "Total stake", 0.01)
        increment = number(self.increment_var, "Round stakes to", 0.01)
        if increment > stake:
            raise ValueError("Round stakes to cannot be larger than the total stake.")
        min_roi = number(self.min_roi_var, "Minimum return %", 0.0)
        max_age = number(self.max_age_var, "Ignore prices older than", 0.0, allow_blank=True)
        markets = [m for m, var in self.market_vars.items() if var.get()]
        if not markets:
            raise ValueError("Pick at least one market.")
        live = self.source_var.get() == "live"
        regions = [r for r, var in self.region_vars.items() if var.get()]
        if live and not regions:
            raise ValueError("Pick at least one region.")
        if live and not self.key_var.get().strip():
            raise ValueError("The live feed needs an API key. Get a free one at the-odds-api.com.")
        return {
            "live": live,
            "api_key": self.key_var.get().strip(),
            "sport": self._selected_sport_key(),
            "regions": regions,
            "markets": markets,
            "stake": stake,
            "increment": increment,
            "min_roi": min_roi / 100.0,
            "max_age": max_age if max_age else None,
            "commissions": self._parse_commissions(self.commission_var.get()),
            "coverage": self.coverage_var.get(),
            "distinct": self.distinct_var.get(),
        }

    # ---------------------------------------------------------------- scan

    def start_scan(self) -> None:
        if self.scanning:
            return
        try:
            config = self._read_config()
        except ValueError as exc:
            messagebox.showwarning("Check the settings", str(exc), parent=self.root)
            self.auto_var.set(False)
            self._sync_auto()
            return
        self.scanning = True
        self.scan_button.configure(state="disabled", text="Scanning...")
        self._set_status("Reading prices...")
        threading.Thread(target=self._scan_worker, args=(config,), daemon=True).start()

    def _scan_worker(self, config: dict) -> None:
        try:
            if config["live"]:
                client = core.OddsAPIClient(config["api_key"])
                payload = client.fetch_odds(config["sport"], config["regions"], config["markets"])
                quota = (client.requests_remaining, client.requests_used)
            else:
                payload = core.demo_payload()
                quota = None
            groups = core.parse_events(payload, config["markets"])
            opportunities = core.find_opportunities(
                groups,
                total_stake=config["stake"],
                min_roi=config["min_roi"],
                increment=config["increment"],
                commissions=config["commissions"],
                max_age_minutes=config["max_age"],
                require_full_coverage=config["coverage"],
                require_distinct_books=config["distinct"],
            )
            self.queue.put(("results", opportunities, len(payload), len(groups), quota))
        except core.OddsFeedError as exc:
            self.queue.put(("error", str(exc)))
        except Exception as exc:  # noqa: BLE001 - the UI must survive anything
            self.queue.put(("error", f"Something went wrong while scanning: {exc}"))

    def load_sports(self) -> None:
        key = self.key_var.get().strip()
        if not key:
            messagebox.showwarning("API key needed",
                                   "Enter your API key first, then load the sport list.",
                                   parent=self.root)
            return
        self.sports_button.configure(state="disabled")
        self._set_status("Loading sports...")

        def worker():
            try:
                self.queue.put(("sports", core.OddsAPIClient(key).list_sports()))
            except core.OddsFeedError as exc:
                self.queue.put(("error", str(exc)))
            except Exception as exc:  # noqa: BLE001
                self.queue.put(("error", f"Could not load sports: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "results":
                    self._finish_scan(*message[1:])
                elif kind == "sports":
                    self._apply_sports(message[1])
                elif kind == "error":
                    self._fail(message[1])
        except queue.Empty:
            pass
        finally:
            self.root.after(POLL_MS, self._poll)

    def _finish_scan(self, opportunities, event_count, group_count, quota) -> None:
        self.scanning = False
        self.scan_button.configure(state="normal", text="Find arbs")
        self.opportunities = opportunities
        self.export_button.configure(state="normal" if opportunities else "disabled")
        if quota and quota[0] is not None:
            self.quota_var.set(f"API calls left: {quota[0]}   used: {quota[1]}")
        stamp = datetime.now().strftime("%H:%M:%S")
        if opportunities:
            best = opportunities[0]
            self._set_status(
                f"{len(opportunities)} found in {group_count} markets across {event_count} events. "
                f"Best pays {best.roi * 100:.2f}%. Updated {stamp}."
            )
        else:
            self._set_status(
                f"No arbs in {group_count} markets across {event_count} events at {stamp}. "
                "Try more regions or markets, or lower the minimum return."
            )
        self._render_rows()
        self._schedule_auto()

    def _apply_sports(self, sports) -> None:
        self.sports_button.configure(state="normal")
        if not sports:
            return
        self.sports = sports
        self.sport_box.configure(values=[title for _, title in sports])
        if self.sport_var.get() not in [title for _, title in sports]:
            self.sport_var.set(sports[0][1])
        self._set_status(f"Loaded {len(sports)} sports.")

    def _fail(self, message: str) -> None:
        self.scanning = False
        self.scan_button.configure(state="normal", text="Find arbs")
        self.sports_button.configure(state="normal" if self.source_var.get() == "live" else "disabled")
        self.auto_var.set(False)
        self._sync_auto()
        self._set_status(message)
        messagebox.showerror("Cannot read the odds feed", message, parent=self.root)

    # ------------------------------------------------------------- display

    def _sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_column = column
            self.sort_desc = column in ("roi", "profit", "stake")
        self._render_rows()

    def _sort_key(self, opp: core.Opportunity):
        column = self.sort_column
        if column == "roi":
            return opp.roi
        if column == "profit":
            return opp.guaranteed_profit
        if column == "stake":
            return opp.total_stake
        if column == "starts":
            return opp.commence_time.timestamp() if opp.commence_time else float("inf")
        if column == "line":
            return opp.line if opp.line is not None else 0.0
        return {
            "sport": opp.sport_title,
            "event": opp.event_name,
            "market": opp.market_key,
            "books": ", ".join(opp.bookmakers),
            "flags": "; ".join(opp.warnings),
        }.get(column, "").lower()

    def _render_rows(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.row_map.clear()
        self.legs.delete(*self.legs.get_children())
        self.summary_var.set("Select an opportunity to see the stakes.")
        self.warning_label.grid_remove()

        needle = self.filter_var.get().strip().lower()
        rows = [
            o for o in self.opportunities
            if not needle or needle in (
                f"{o.event_name} {o.sport_title} {o.market_key} {' '.join(o.bookmakers)}"
            ).lower()
        ]
        rows.sort(key=self._sort_key, reverse=self.sort_desc)

        for index, opp in enumerate(rows):
            tags = ["odd" if index % 2 else "even"]
            if opp.warnings:
                tags = ["flagged"]
            iid = self.tree.insert("", "end", tags=tags, values=(
                f"{opp.roi * 100:.2f}",
                self.money(opp.guaranteed_profit),
                self.money(opp.total_stake),
                opp.sport_title,
                opp.event_name,
                self._local(opp.commence_time),
                {"h2h": "Match odds", "spreads": "Handicap", "totals": "Over/under"}
                .get(opp.market_key, opp.market_key),
                opp.line_label,
                ", ".join(opp.bookmakers),
                "; ".join(opp.warnings),
            ))
            self.row_map[iid] = opp

        for key, title, _, _ in self.columns:
            arrow = " v" if self.sort_desc else " ^"
            self.tree.heading(key, text=title + (arrow if key == self.sort_column else ""))

        children = self.tree.get_children()
        if children:
            self.tree.selection_set(children[0])
            self.tree.focus(children[0])

    def _on_select(self, _event=None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        opp = self.row_map.get(selection[0])
        if not opp:
            return
        self.legs.delete(*self.legs.get_children())
        for index, leg in enumerate(opp.legs):
            self.legs.insert("", "end", tags=("odd" if index % 2 else "even"), values=(
                leg.outcome,
                leg.bookmaker_title,
                f"{leg.price:.2f}",
                f"{leg.net_price:.3f}" if leg.commission else "-",
                self.money(leg.stake),
                self.money(leg.payout),
                self._local(leg.last_update, "%H:%M:%S"),
            ))
        worst = min(leg.payout for leg in opp.legs)
        self.summary_var.set(
            f"Stake {self.money(opp.total_stake)} across {len(opp.legs)} bets, "
            f"collect at least {self.money(worst)} however it ends "
            f"-- {self.money(opp.guaranteed_profit)} profit, {opp.roi * 100:.2f}%."
        )
        if opp.warnings:
            self.warning_label.configure(text="  ".join(opp.warnings))
            self.warning_label.grid()
        else:
            self.warning_label.grid_remove()

    # --------------------------------------------------------------- extras

    def _sync_auto(self) -> None:
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None
        if self.auto_var.get():
            if self.source_var.get() == "live":
                self._set_status("Repeating scans. Each one spends an API call.")
            self._schedule_auto()

    def _schedule_auto(self) -> None:
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
            self.auto_job = None
        if not self.auto_var.get():
            return
        try:
            seconds = max(10.0, float(self.interval_var.get()))
        except ValueError:
            seconds = 60.0
            self.interval_var.set("60")
        self.auto_job = self.root.after(int(seconds * 1000), self.start_scan)

    def export_csv(self) -> None:
        if not self.opportunities:
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save opportunities",
            defaultextension=".csv",
            initialfile=f"arbs-{datetime.now():%Y%m%d-%H%M}.csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            core.opportunities_to_csv(self.opportunities, path)
        except OSError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self.root)
            return
        self._set_status(f"Saved {len(self.opportunities)} opportunities to {path}")

    def close(self) -> None:
        if self.auto_job:
            self.root.after_cancel(self.auto_job)
        self.root.destroy()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Find arbitrage across betting sites.")
    parser.add_argument("--key", default=os.environ.get("ODDS_API_KEY", ""),
                        help="the-odds-api.com key (or set ODDS_API_KEY)")
    parser.add_argument("--live", action="store_true",
                        help="start on the live feed instead of the demo feed")
    args = parser.parse_args(argv)

    root = tk.Tk()
    ArbFinderApp(root, api_key=args.key, demo=not (args.live and args.key))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
