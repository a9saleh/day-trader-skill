#!/usr/bin/env python3
"""Paper Simulator for trader-sim: replay each session, log every trade, write the review,
and propose strategy changes that hold up out of sample. Standard library only. No orders.

    python3 sim.py backfill                          # archive the last 60 days of 5-minute bars
    python3 sim.py day                               # after the close: fetch + run + review today
    python3 sim.py day --date 2026-10-19             # or any archived / fetchable day
    python3 sim.py run --date 2026-10-19             # simulate one day -> trader/sim/trades.csv
    python3 sim.py review --date 2026-10-19          # -> trader/sim/reviews/2026-10-19.md
    python3 sim.py evaluate --strategy cand.json     # any strategy on the whole archive, train / test
    python3 sim.py propose                           # one-change candidates, kept only if they win out of sample
    python3 sim.py apply trader/sim/proposals/2026-10-23.json   # adopt a proposal: a new strategy version

The archive. Yahoo keeps 60 days of 5-minute bars, so every run saves each complete
session it sees to trader/sim/bars/<day>.json.gz (every symbol, regular hours only).
The archive grows by a day each trading day, and that is the data every review and
every proposal is judged on.

The strategy. trader/sim/strategy.json (copied from strategy.default.json on first use):
a long opening-range breakout, the pack's one end-to-end setup, with every knob in
the file. The simulation is bar by bar and never looks ahead:
  signal   the first 5-minute bar after the opening range to CLOSE above the range
           high, between no_entries_before and no_entries_after
  entry    the NEXT bar's open, plus slippage (the signal is only known at the close)
  stop     range high - stop_frac x range (0.5 = the midpoint), a stop order: it fills
           at the stop, or at the open if the bar gaps through it, less slippage
  target   entry + target_r x risk, a limit: it fills at the target
  both     a bar that touches the stop and the target is counted as the stop
  flat     the open of the flat_by bar (15:55), less slippage, or the last close
  filters  min_price, opening range % (min_range_pct / max_range_pct), gap from the
           prior close (min_gap_pct / max_gap_pct), relative opening-range volume
           against up to 20 earlier archived sessions (min_rvol), and how far the
           signal close is over the range (max_pct_over)
  size     floor(risk_per_trade / risk a share), capped at max_position_frac of account
  cap      max_trades_per_day, earliest signal first, then the most traded range
Signals a filter or the cap kept out are still simulated and logged with taken=0 and
the reason, so the review can say what each rule cost or saved.

propose. Splits the archive by date: the first 2/3 to train, the last 1/3 to test.
Each candidate changes ONE knob. Candidates are ranked on train only; the best few
are then scored on test, and a candidate is proposed only if, on the test days, it
beats the current strategy's total R by --min-gain, its average R is above zero and
it has at least --min-test-trades trades. It never edits strategy.json: apply does,
when a person (or a PR they merge) says so. Every version is then forward-tested on
the days after it was adopted, and the review shows each version's record.

A simulation is not a trade. Real fills are worse: queue position, spreads, halts.
Not financial advice. Exit code 0 on success, 1 when it could not run.
"""

import argparse
import copy
import csv
import datetime
import gzip
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"
YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")
PAUSE = 0.4         # seconds between requests: be polite to free endpoints
BACKOFF = 5.0       # seconds to wait before the one retry after a 429
SLEEP = time.sleep  # tests swap this out
BAR_MIN = 5
RVOL_DAYS = 20
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = "trader/sim"
NOTE = "A simulation is not a trade: real fills are worse. Not financial advice."

NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day",
    "2026-01-19": "Martin Luther King, Jr. Day",
    "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday",
    "2026-05-25": "Memorial Day",
    "2026-06-19": "Juneteenth National Independence Day",
    "2026-07-03": "Independence Day (observed)",
    "2026-09-07": "Labor Day",
    "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day",
    "2027-01-18": "Martin Luther King, Jr. Day",
    "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday",
    "2027-05-31": "Memorial Day",
    "2027-06-18": "Juneteenth National Independence Day (observed)",
    "2027-07-05": "Independence Day (observed)",
    "2027-09-06": "Labor Day",
    "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {  # 1:00 p.m. ET
    "2026-11-27": "day after Thanksgiving",
    "2026-12-24": "Christmas Eve",
    "2027-11-26": "day after Thanksgiving",
}
OPEN = datetime.time(9, 30)
CLOSE = datetime.time(16, 0)
EARLY = datetime.time(13, 0)

DEFAULT_STRATEGY = {
    "version": 1,
    "setup": "orb_long",
    "or_minutes": 15,
    "no_entries_before": "09:45",
    "no_entries_after": "11:00",
    "stop_frac": 0.5,
    "target_r": 1.0,
    "flat_by": "15:55",
    "min_price": 5.0,
    "min_range_pct": 0.2,
    "max_range_pct": 5.0,
    "min_gap_pct": -100.0,
    "max_gap_pct": 100.0,
    "min_rvol": 0.0,
    "max_pct_over": 100.0,
    "max_trades_per_day": 5,
    "account": 5000.0,
    "risk_per_trade": 50.0,
    "max_position_frac": 0.5,
    "slippage_bps": 5.0,
    "commission_per_share": 0.0,
}
TUNABLE = {
    "or_minutes": [5, 15, 30],
    "no_entries_after": ["10:15", "10:30", "11:00", "12:00", "15:30"],
    "stop_frac": [0.25, 0.5, 0.75, 1.0],
    "target_r": [0.75, 1.0, 1.5, 2.0, 3.0],
    "min_range_pct": [0.0, 0.2, 0.5, 1.0],
    "max_range_pct": [1.5, 2.0, 3.0, 5.0, 100.0],
    "min_gap_pct": [-100.0, -2.0, 0.0, 1.0],
    "max_gap_pct": [1.0, 2.0, 4.0, 100.0],
    "min_rvol": [0.0, 1.0, 1.5, 2.0],
    "max_pct_over": [0.25, 0.5, 1.0, 100.0],
}
FIELDS = ["date", "strategy", "symbol", "taken", "skip_reason", "signal_time", "entry_time", "entry",
          "stop", "target", "shares", "exit_time", "exit", "exit_reason", "pnl", "r", "mfe_r", "mae_r",
          "bars_held", "gap_pct", "range_pct", "rvol", "pct_over", "entry_min", "or_dollar_vol",
          "day_change_pct"]
NUM = {"entry", "stop", "target", "pnl", "r", "mfe_r", "mae_r", "gap_pct", "range_pct", "rvol",
       "pct_over", "day_change_pct"}
INT = {"taken", "shares", "bars_held", "entry_min", "or_dollar_vol"}
BUCKETS = [  # feature, edges, label
    ("gap_pct", [-1.0, 0.0, 1.0, 3.0], "gap from prior close %"),
    ("range_pct", [0.5, 1.0, 2.0, 3.0], "opening range % of price"),
    ("rvol", [0.8, 1.2, 2.0, 3.0], "opening-range volume vs normal"),
    ("pct_over", [0.1, 0.25, 0.5, 1.0], "signal close % over the range"),
    ("entry_min", [30, 45, 60, 90], "minutes after the open at entry"),
    ("day_change_pct", [-1.0, -0.25, 0.25, 1.0], "market (SPY) day %"),
]


class SimError(Exception):
    pass


# ---------------------------------------------------------------- time

def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def eastern_offset(utc):
    """Hours from UTC for New York: -4 in daylight time, -5 otherwise (US rule since 2007)."""
    y = utc.year
    start = datetime.datetime.combine(_nth_sunday(y, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(y, 11, 1), datetime.time(6, 0))
    naive = utc.replace(tzinfo=None)
    return -4 if start <= naive < end else -5


def to_eastern(utc):
    return utc.replace(tzinfo=None) + datetime.timedelta(hours=eastern_offset(utc))


def ts_to_eastern(ts):
    """Unix seconds -> naive New York time. Never uses the machine's time zone."""
    return to_eastern(datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=int(ts)))


def now_eastern():
    return to_eastern(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None))


def trading_day(d):
    if d.weekday() >= 5:
        return False, "weekend"
    name = NYSE_HOLIDAYS.get(d.isoformat())
    if name:
        return False, "NYSE holiday: " + name
    return True, ""


def session_close(d):
    return EARLY if d.isoformat() in NYSE_EARLY_CLOSE else CLOSE


def parse_day(text):
    try:
        return datetime.date.fromisoformat(text)
    except (TypeError, ValueError):
        raise SimError("--date wants a day like 2026-10-19")


def hhmm_min(text):
    """'10:15' -> minutes after midnight."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(text))
    if not m:
        raise SimError("a time like 10:15 was expected, not %r" % text)
    return int(m.group(1)) * 60 + int(m.group(2))


def min_hhmm(m):
    return "%02d:%02d" % (m // 60, m % 60)


OPEN_MIN = OPEN.hour * 60 + OPEN.minute


# ---------------------------------------------------------------- network

def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def yahoo_get(path):
    """query2 first, query1 on a 429 or 5xx, one more round after BACKOFF on a 429."""
    last = "no answer"
    for attempt in (0, 1):
        saw_429 = False
        for host in YAHOO_HOSTS:
            try:
                return http_get(host + path)
            except urllib.error.HTTPError as e:
                last = "HTTP %d" % e.code
                e.close()
                if e.code == 429:
                    saw_429 = True
                    continue
                if e.code >= 500:
                    continue
                raise SimError("Yahoo answered %s for %s" % (last, path.split("?")[0]))
            except (urllib.error.URLError, OSError) as e:
                last = str(getattr(e, "reason", e))
                continue
        if attempt == 0 and saw_429:
            SLEEP(BACKOFF)
            continue
        break
    raise SimError("Yahoo answered %s for %s" % (last, path.split("?")[0]))


def chart_path(sym, rng):
    return "/v8/finance/chart/%s?range=%s&interval=5m" % (sym.upper().replace(".", "-"), rng)


# ---------------------------------------------------------------- bars

def chart_result(doc, sym=""):
    try:
        res = doc["chart"]["result"]
    except (KeyError, TypeError):
        raise SimError("not a Yahoo chart answer for %s" % sym)
    if not res:
        raise SimError("no data for %s" % sym)
    return res[0]


def sessions_from_chart(doc, sym=""):
    """A Yahoo v8 chart (5-minute bars) -> {day: {"bars": [[min, o, h, l, c, v], ...], "prev_close": x}}.
    Regular hours only, on the 5-minute grid, bars with every price. A day counts only when
    it is complete: every slot from the open to the close is there (a missing bar is a halt)."""
    res = chart_result(doc, sym)
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    t = res.get("timestamp") or []
    cols = [q.get(k) or [] for k in ("open", "high", "low", "close", "volume")]
    days = {}
    for i, ts in enumerate(t):
        row = [c[i] if i < len(c) else None for c in cols]
        if any(x is None for x in row[:4]):
            continue
        et = ts_to_eastern(ts)
        m = et.hour * 60 + et.minute
        close_m = session_close(et.date()).hour * 60 + session_close(et.date()).minute
        if et.second or et.minute % BAR_MIN or m < OPEN_MIN or m >= close_m:
            continue
        days.setdefault(et.date(), {})[m] = [m] + [round(float(x), 4) for x in row[:4]] + [int(row[4] or 0)]
    out = {}
    prev = (res.get("meta") or {}).get("chartPreviousClose")
    for d in sorted(days):
        bars = [days[d][m] for m in sorted(days[d])]
        close_m = session_close(d).hour * 60 + session_close(d).minute
        want = (close_m - OPEN_MIN) // BAR_MIN
        complete = len(bars) == want and trading_day(d)[0]
        if complete:
            out[d] = {"bars": bars, "prev_close": prev}
        prev = bars[-1][4]
    return out


def bundle_from_file(path):
    """A fixture: one v8 chart, or {"SYM": <v8 chart>, ...}. .json.gz is fine."""
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict) and "chart" in doc:
        sym = ((chart_result(doc).get("meta") or {}).get("symbol") or "").upper()
        return {sym: doc}
    if isinstance(doc, dict):
        return {k.upper(): v for k, v in doc.items() if isinstance(v, dict) and "chart" in v}
    raise SimError("%s is not a Yahoo chart or a bundle of them" % path)


# ---------------------------------------------------------------- archive

def bars_dir(root):
    return os.path.join(root, "bars")


def archive_path(root, day):
    return os.path.join(bars_dir(root), "%s.json.gz" % day.isoformat())


def archived_days(root):
    d = bars_dir(root)
    if not os.path.isdir(d):
        return []
    out = []
    for f in os.listdir(d):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.json\.gz$", f)
        if m:
            out.append(datetime.date.fromisoformat(m.group(1)))
    return sorted(out)


def load_archive_day(root, day):
    path = archive_path(root, day)
    if not os.path.isfile(path):
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def save_archive(root, by_day):
    """by_day = {day: {SYM: {"bars", "prev_close"}}}. Merges into what is there. -> days written."""
    os.makedirs(bars_dir(root), exist_ok=True)
    written = []
    for day in sorted(by_day):
        have = load_archive_day(root, day) or {}
        have.update(by_day[day])
        path = archive_path(root, day)
        tmp = path + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(have, fh, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, path)
        written.append(day)
    return written


def absorb(charts, by_day=None):
    """{SYM: v8 chart} -> {day: {SYM: session}}."""
    by_day = by_day if by_day is not None else {}
    for sym, doc in charts.items():
        try:
            for d, s in sessions_from_chart(doc, sym).items():
                by_day.setdefault(d, {})[sym] = s
        except SimError:
            continue
    return by_day


def fetch(symbols, rng, pause=PAUSE, log=None):
    charts, failed = {}, {}
    for i, sym in enumerate(symbols):
        try:
            charts[sym] = json.loads(yahoo_get(chart_path(sym, rng)))
        except (SimError, ValueError) as e:
            failed[sym] = str(e)
        if log and (i + 1) % 10 == 0:
            log("fetched %d of %d" % (i + 1, len(symbols)))
        if pause:
            SLEEP(pause)
    return charts, failed


def read_universe(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            s = line.split("#")[0].split("|")[0].strip().upper()
            if s:
                out.append(s)
    return out


# ---------------------------------------------------------------- strategy

def strategy_path(root):
    return os.path.join(root, "strategy.json")


def load_strategy(path):
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    return normalize(doc)


def normalize(doc):
    s = copy.deepcopy(DEFAULT_STRATEGY)
    unknown = sorted(set(doc) - set(s) - {"notes", "adopted", "evidence"})
    if unknown:
        raise SimError("unknown strategy keys: %s" % ", ".join(unknown))
    s.update(doc)
    if s["setup"] != "orb_long":
        raise SimError("the only setup is orb_long")
    if s["or_minutes"] % BAR_MIN or not 5 <= s["or_minutes"] <= 120:
        raise SimError("or_minutes wants a multiple of 5 from 5 to 120")
    for k in ("no_entries_before", "no_entries_after", "flat_by"):
        hhmm_min(s[k])
    if s["stop_frac"] <= 0 or s["target_r"] <= 0:
        raise SimError("stop_frac and target_r must be above zero")
    return s


def ensure_strategy(root):
    path = strategy_path(root)
    if not os.path.isfile(path):
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(HERE, "strategy.default.json"), encoding="utf-8") as fh:
            doc = json.load(fh)
        write_json(path, doc)
    return load_strategy(path)


def strategy_id(s):
    knobs = {k: s[k] for k in DEFAULT_STRATEGY if k != "version"}
    h = hashlib.sha1(json.dumps(knobs, sort_keys=True).encode()).hexdigest()[:6]
    return "v%s-%s" % (s["version"], h)


# ---------------------------------------------------------------- simulation

def or_stats(bars, n):
    rng = bars[:n]
    return max(b[2] for b in rng), min(b[3] for b in rng), sum(b[4] * b[5] for b in rng)


def day_change(bundle, sym="SPY"):
    s = bundle.get(sym)
    if not s or not s.get("prev_close"):
        return None
    return round((s["bars"][-1][4] / s["prev_close"] - 1) * 100, 2)


def simulate_symbol(sym, sess, s, hist_or_vol):
    """One symbol, one day -> a trade row (taken decided later) or None (no signal)."""
    bars = sess["bars"]
    n = s["or_minutes"] // BAR_MIN
    if len(bars) <= n + 1:
        return None
    oh, ol, or_dv = or_stats(bars, n)
    first = OPEN_MIN + BAR_MIN * n
    lo_t, hi_t = hhmm_min(s["no_entries_before"]), hhmm_min(s["no_entries_after"])
    flat_t = hhmm_min(s["flat_by"])
    k = None
    for i in range(n, len(bars) - 1):
        closes_at = bars[i][0] + BAR_MIN
        if closes_at > hi_t or bars[i + 1][0] >= flat_t:
            break
        if bars[i][4] > oh and closes_at >= max(lo_t, first + BAR_MIN):
            k = i
            break
    if k is None:
        return None
    slip = s["slippage_bps"] / 10000.0
    sig = bars[k]
    nxt = bars[k + 1]
    entry = nxt[1] * (1 + slip)
    stop = oh - s["stop_frac"] * (oh - ol)
    prev = sess.get("prev_close")
    or_vol = sum(b[5] for b in bars[:n])
    rvol = round(or_vol / (sum(hist_or_vol) / len(hist_or_vol)), 2) if hist_or_vol and sum(hist_or_vol) else None
    row = {
        "symbol": sym, "signal_time": min_hhmm(sig[0] + BAR_MIN), "entry_time": min_hhmm(nxt[0]),
        "gap_pct": round((bars[0][1] / prev - 1) * 100, 2) if prev else None,
        "range_pct": round((oh - ol) / oh * 100, 3),
        "rvol": rvol, "pct_over": round((sig[4] / oh - 1) * 100, 3),
        "entry_min": nxt[0] - OPEN_MIN, "or_dollar_vol": round(or_dv),
        "_t": sig[0], "skip_reason": "",
    }
    if entry <= stop:
        row.update({"entry": round(entry, 4), "stop": round(stop, 4), "skip_reason": "opened_below_stop",
                    "taken": 0})
        return row
    risk = entry - stop
    target = entry + s["target_r"] * risk
    exit_px, why, held = None, "", 0
    hi, lo = entry, entry
    for j in range(k + 1, len(bars)):
        b = bars[j]
        if b[0] >= flat_t:
            exit_px, why = b[1] * (1 - slip), "flat"
            break
        held += 1
        hi, lo = max(hi, b[2]), min(lo, b[3])
        if b[3] <= stop:
            exit_px, why = min(stop, b[1]) * (1 - slip), "stop" if b[2] < target else "stop_both"
            break
        if b[2] >= target:
            exit_px, why = target, "target"
            break
        if j == len(bars) - 1:
            exit_px, why = b[4] * (1 - slip), "close"
    if exit_px is None:
        exit_px, why = bars[-1][4] * (1 - slip), "close"
    shares = int(s["risk_per_trade"] // risk)
    shares = min(shares, int(s["account"] * s["max_position_frac"] // entry))
    pnl = shares * (exit_px - entry) - 2 * shares * s["commission_per_share"]
    exit_t = bars[min(j, len(bars) - 1)][0]
    row.update({
        "entry": round(entry, 4), "stop": round(stop, 4), "target": round(target, 4), "shares": shares,
        "exit_time": min_hhmm(exit_t + (0 if why == "flat" else BAR_MIN)), "exit": round(exit_px, 4),
        "exit_reason": why, "pnl": round(pnl, 2), "r": round((exit_px - entry) / risk, 3),
        "mfe_r": round((hi - entry) / risk, 3), "mae_r": round((entry - lo) / risk, 3), "bars_held": held,
    })
    if shares < 1:
        row["skip_reason"] = "size_zero"
    return row


def filter_reason(row, s, price):
    if price < s["min_price"]:
        return "min_price"
    if row["range_pct"] < s["min_range_pct"]:
        return "min_range_pct"
    if row["range_pct"] > s["max_range_pct"]:
        return "max_range_pct"
    if row["gap_pct"] is not None and row["gap_pct"] < s["min_gap_pct"]:
        return "min_gap_pct"
    if row["gap_pct"] is not None and row["gap_pct"] > s["max_gap_pct"]:
        return "max_gap_pct"
    if s["min_rvol"] > 0 and (row["rvol"] is None or row["rvol"] < s["min_rvol"]):
        return "min_rvol"
    if row["pct_over"] > s["max_pct_over"]:
        return "max_pct_over"
    return ""


def simulate_day(day, bundle, s, history):
    """bundle = {SYM: session}; history = {SYM: [or volume of earlier days]} -> rows."""
    n = s["or_minutes"] // BAR_MIN
    spy = day_change(bundle)
    rows = []
    for sym in sorted(bundle):
        sess = bundle[sym]
        row = simulate_symbol(sym, sess, s, history.get(sym, [])[-RVOL_DAYS:])
        if row is None:
            continue
        if not row["skip_reason"]:
            row["skip_reason"] = filter_reason(row, s, sess["bars"][n - 1][4])
        rows.append(row)
    rows.sort(key=lambda r: (r["_t"], -r["or_dollar_vol"], r["symbol"]))
    taken = 0
    for r in rows:
        if not r["skip_reason"] and taken >= s["max_trades_per_day"]:
            r["skip_reason"] = "max_trades"
        r["taken"] = 0 if r["skip_reason"] else 1
        taken += r["taken"]
        r["date"] = day.isoformat()
        r["strategy"] = strategy_id(s)
        r["day_change_pct"] = spy
        r.pop("_t", None)
    for sym, sess in bundle.items():
        history.setdefault(sym, []).append(sum(b[5] for b in sess["bars"][:n]))
    return rows


def history_before(root, day, n):
    """OR volume of up to RVOL_DAYS archived days before `day`, per symbol."""
    hist = {}
    prior = [d for d in archived_days(root) if d < day][-RVOL_DAYS:]
    for d in prior:
        for sym, sess in (load_archive_day(root, d) or {}).items():
            hist.setdefault(sym, []).append(sum(b[5] for b in sess["bars"][:n]))
    return hist


def run_days(days, bundles, s):
    """Simulate a run of days in order, carrying the volume history. -> rows."""
    hist, rows = {}, []
    for d in days:
        rows.extend(simulate_day(d, bundles[d], s, hist))
    return rows


# ---------------------------------------------------------------- log

def log_path(root):
    return os.path.join(root, "trades.csv")


def read_log(root):
    path = log_path(root)
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out.append(typed(r))
    return out


def typed(r):
    out = {}
    for k in FIELDS:
        v = r.get(k, "")
        if v in ("", None):
            out[k] = None if (k in NUM or k in INT) else ""
        elif k in NUM:
            out[k] = float(v)
        elif k in INT:
            out[k] = int(float(v))
        else:
            out[k] = v
    return out


def write_log(root, day, sid, rows):
    """Replace this day's rows for this strategy, keep everything else."""
    keep = [r for r in read_log(root) if not (r["date"] == day.isoformat() and r["strategy"] == sid)]
    allrows = sorted(keep + [typed({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS}) for r in rows],
                     key=lambda r: (r["date"], r["strategy"], r["signal_time"] or "", r["symbol"]))
    os.makedirs(root, exist_ok=True)
    tmp = log_path(root) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in allrows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})
    os.replace(tmp, log_path(root))
    return allrows


# ---------------------------------------------------------------- stats

def stats(rows, days=None):
    """days = how many sessions the rows cover (default: the days that had a signal)."""
    t = [r for r in rows if r["taken"] and r["r"] is not None]
    n = len(t)
    ndays = len({r["date"] for r in rows}) if days is None else days
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": None, "total_r": 0.0, "avg_r": None, "se_r": None,
                "pnl": 0.0, "max_dd_r": 0.0, "days": ndays}
    rs = [r["r"] for r in t]
    avg = sum(rs) / n
    se = math.sqrt(sum((x - avg) ** 2 for x in rs) / (n - 1) / n) if n > 1 else None
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"trades": n, "wins": sum(1 for x in rs if x > 0), "win_rate": round(sum(1 for x in rs if x > 0) / n, 3),
            "total_r": round(sum(rs), 2), "avg_r": round(avg, 3), "se_r": round(se, 3) if se else None,
            "pnl": round(sum(r["pnl"] or 0 for r in t), 2), "max_dd_r": round(dd, 2), "days": ndays}


def bucket_label(edges, x):
    if x is None:
        return "n/a"
    for i, e in enumerate(edges):
        if x < e:
            return ("< %g" % e) if i == 0 else ("%g to %g" % (edges[i - 1], e))
    return ">= %g" % edges[-1]


def bucket_table(rows, include_skipped=True):
    """Every signal (taken or not, it was simulated either way) by feature bucket."""
    pool = [r for r in rows if r["r"] is not None and (include_skipped or r["taken"])]
    out = []
    for feat, edges, label in BUCKETS:
        groups = {}
        for r in pool:
            groups.setdefault(bucket_label(edges, r[feat]), []).append(r["r"])
        order = ["< %g" % edges[0]] + ["%g to %g" % (edges[i - 1], edges[i]) for i in range(1, len(edges))] + \
                [">= %g" % edges[-1], "n/a"]
        cells = [(b, len(groups[b]), sum(groups[b]) / len(groups[b]), sum(1 for x in groups[b] if x > 0) / len(groups[b]))
                 for b in order if b in groups]
        out.append((feat, label, cells))
    return out


def fmt_r(x):
    return "n/a" if x is None else "%+.2fR" % x


def money(x):
    return "-$%s" % format(abs(x), ",.2f") if x < 0 else "$%s" % format(x, ",.2f")


# ---------------------------------------------------------------- review

def review_md(day, rows, alllog, s):
    sid = strategy_id(s)
    today = [r for r in rows if r["date"] == day.isoformat() and r["strategy"] == sid]
    taken = [r for r in today if r["taken"]]
    st = stats(today)
    lines = ["# Session review: %s %s" % (day.strftime("%a"), day.isoformat()), "",
             "Strategy %s (long opening-range breakout, %d-minute range, stop at %g of the range below the high, "
             "target %gR, entries %s to %s, flat by %s). Simulated on free Yahoo 5-minute bars; no orders."
             % (sid, s["or_minutes"], s["stop_frac"], s["target_r"], s["no_entries_before"], s["no_entries_after"],
                s["flat_by"]), ""]
    spy = today[0]["day_change_pct"] if today else None
    lines.append("## Summary")
    lines.append("")
    if not today:
        lines += ["No symbol closed a 5-minute bar above its opening range inside the entry window. "
                  "No signal, no trade.", ""]
    else:
        lines.append("- Signals: %d. Taken: %d. Kept out by a rule: %d." % (len(today), len(taken), len(today) - len(taken)))
        if taken:
            lines.append("- Result: %d won, %d lost, net %s, %s total, %s a trade on average."
                         % (st["wins"], st["trades"] - st["wins"], money(st["pnl"]), fmt_r(st["total_r"]), fmt_r(st["avg_r"])))
        if spy is not None:
            lines.append("- Market: SPY %+.2f%% on the day." % spy)
        lines.append("")
    if taken:
        lines += ["## Trades", "", "| symbol | signal | entry | stop | target | exit | why | R | P&L | best | worst |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in taken:
            lines.append("| %s | %s | %.2f @ %s | %.2f | %.2f | %.2f @ %s | %s | %s | %s | %s | %s |" % (
                r["symbol"], r["signal_time"], r["entry"], r["entry_time"], r["stop"], r["target"], r["exit"],
                r["exit_time"], r["exit_reason"], fmt_r(r["r"]), money(r["pnl"]), fmt_r(r["mfe_r"]),
                fmt_r(-r["mae_r"])))
        lines.append("")
        lines += ["## What went well", ""] + (went_well(taken) or ["- Nothing: no trade made money."]) + [""]
        lines += ["## What went wrong", ""] + (went_wrong(taken) or ["- Nothing: no trade lost money."]) + [""]
    skipped = [r for r in today if not r["taken"] and r["r"] is not None]
    if skipped:
        lines += ["## What the rules kept you out of", "",
                  "Each of these signals was simulated anyway, as if it had been taken."]
        by = {}
        for r in skipped:
            by.setdefault(r["skip_reason"], []).append(r)
        for reason in sorted(by):
            g = by[reason]
            tot = sum(r["r"] for r in g)
            lines.append("- `%s`: %d signal%s, %s together (%s). %s" % (
                reason, len(g), "" if len(g) == 1 else "s", fmt_r(tot),
                ", ".join("%s %s" % (r["symbol"], fmt_r(r["r"])) for r in g[:8]) + (" ..." if len(g) > 8 else ""),
                "The rule saved you." if tot < 0 else "The rule cost you." if tot > 0 else ""))
        lines.append("")
    mine = [r for r in alllog if r["strategy"] == sid]
    lines += ["## Patterns so far (%s, %d days, %d signals)" % (sid, len({r["date"] for r in mine}),
                                                               len([r for r in mine if r["r"] is not None])), "",
              "Every simulated signal, taken or not. A bucket with fewer than 20 signals is an anecdote, "
              "not evidence; treat a spread there as a question for `propose`, not an answer.", ""]
    for feat, label, cells in bucket_table(mine):
        if not cells:
            continue
        lines.append("- **%s**: %s" % (label, "; ".join(
            "%s: %d, %s avg, %d%% won%s" % (b, n, fmt_r(avg), round(w * 100), "" if n >= 20 else " (thin)")
            for b, n, avg, w in cells)))
    lines += ["", "## Record by strategy version", "", "| version | days | trades | win rate | total R | avg R | P&L | worst drawdown |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for v in sorted({r["strategy"] for r in alllog}):
        sv = stats([r for r in alllog if r["strategy"] == v])
        lines.append("| %s | %d | %d | %s | %s | %s | %s | %s |" % (
            v, sv["days"], sv["trades"], "n/a" if sv["win_rate"] is None else "%d%%" % round(sv["win_rate"] * 100),
            fmt_r(sv["total_r"]), fmt_r(sv["avg_r"]), money(sv["pnl"]), fmt_r(-sv["max_dd_r"])))
    lines += ["", "## Claude's read", "", "_(Claude adds two or three sentences here: what today says, "
              "what it does not, and any idea worth testing with `sim.py evaluate`.)_", "", "---", "", NOTE, ""]
    return "\n".join(lines)


def went_well(taken):
    out = []
    for r in sorted(taken, key=lambda r: -r["r"]):
        if r["r"] <= 0:
            continue
        if r["exit_reason"] == "target":
            out.append("- %s hit the %s target at %s, %d bar%s after entry (it ran to %s at best)."
                       % (r["symbol"], fmt_r(r["r"]), r["exit_time"], r["bars_held"], "" if r["bars_held"] == 1 else "s",
                          fmt_r(r["mfe_r"])))
        else:
            out.append("- %s closed %s at the %s exit; it had been %s at best, so the target was %s."
                       % (r["symbol"], fmt_r(r["r"]), r["exit_reason"], fmt_r(r["mfe_r"]),
                          "never reached" if r["mfe_r"] < 1 else "within reach"))
    return out


def went_wrong(taken):
    out = []
    for r in sorted(taken, key=lambda r: r["r"]):
        if r["r"] > 0:
            continue
        bits = []
        if r["exit_reason"].startswith("stop"):
            bits.append("stopped at %s after %d bar%s" % (r["exit_time"], r["bars_held"], "" if r["bars_held"] == 1 else "s"))
            if r["r"] < -1.05:
                bits.append("filled %s past the stop (a gap or slippage)" % fmt_r(-1 - r["r"]).lstrip("+"))
            if r["exit_reason"] == "stop_both":
                bits.append("the bar touched both the stop and the target; counted as the stop")
        else:
            bits.append("never reached the target, out at the %s at %s" % (r["exit_reason"], r["exit_time"]))
        if r["mfe_r"] >= 0.5:
            bits.append("it was %s in the money first" % fmt_r(r["mfe_r"]))
        elif r["mfe_r"] < 0.15:
            bits.append("it never went anywhere after the entry (best %s)" % fmt_r(r["mfe_r"]))
        if r["pct_over"] is not None and r["pct_over"] > 0.5:
            bits.append("the signal bar closed %.2f%% over the range (a chase)" % r["pct_over"])
        if r["gap_pct"] is not None and abs(r["gap_pct"]) >= 2:
            bits.append("it gapped %+.1f%% at the open" % r["gap_pct"])
        out.append("- %s %s: %s." % (r["symbol"], fmt_r(r["r"]), "; ".join(bits)))
    return out


# ---------------------------------------------------------------- evaluate / propose

def load_bundles(root, days=None):
    days = days or archived_days(root)
    return days, {d: load_archive_day(root, d) for d in days}


def split(days, train_frac=2.0 / 3):
    cut = max(1, int(round(len(days) * train_frac)))
    return days[:cut], days[cut:]


def score(rows, days):
    ds = {d.isoformat() for d in days}
    return stats([r for r in rows if r["date"] in ds], len(ds))


def candidates(s):
    out = []
    for k, values in TUNABLE.items():
        for v in values:
            if v == s[k]:
                continue
            c = copy.deepcopy(s)
            c[k] = v
            out.append((k, v, c))
    return out


def propose(root, s, min_test_trades=15, min_gain=2.0, top=5, min_train_trades=20, days=None):
    days, bundles = load_bundles(root, days)
    if len(days) < 9:
        raise SimError("only %d archived days; propose wants at least 9 (run backfill)" % len(days))
    train, test = split(days)
    base_rows = run_days(days, bundles, s)
    base_train, base_test = score(base_rows, train), score(base_rows, test)
    ranked = []
    for k, v, c in candidates(s):
        rows = run_days(days, bundles, c)
        tr = score(rows, train)
        if tr["trades"] < min_train_trades:
            continue
        ranked.append((tr["total_r"] - base_train["total_r"], k, v, c, rows, tr))
    ranked.sort(key=lambda x: -x[0])
    tried = len(ranked)
    shortlist = [x for x in ranked if x[0] > 0][:top]
    results = []
    for gain, k, v, c, rows, tr in shortlist:
        te = score(rows, test)
        ok = (te["trades"] >= min_test_trades and te["avg_r"] is not None and te["avg_r"] > 0
              and te["total_r"] - base_test["total_r"] >= min_gain)
        why = "passes" if ok else (
            "too few test trades (%d < %d)" % (te["trades"], min_test_trades) if te["trades"] < min_test_trades else
            "loses on the test days (avg %s)" % fmt_r(te["avg_r"]) if not te["avg_r"] or te["avg_r"] <= 0 else
            "test gain %s is under %s" % (fmt_r(te["total_r"] - base_test["total_r"]), fmt_r(min_gain)))
        results.append({"knob": k, "from": s[k], "to": v, "train": tr, "test": te, "passes": ok, "why": why,
                        "strategy": c})
    best = next((r for r in results if r["passes"]), None)
    return {"days": [d.isoformat() for d in days], "train": [train[0].isoformat(), train[-1].isoformat()],
            "test": [test[0].isoformat(), test[-1].isoformat()] if test else None, "test_days": len(test),
            "baseline": {"train": base_train, "test": base_test}, "tried": tried, "shortlist": results,
            "best": best}


def proposal_md(p, s, notes):
    lines = ["# Strategy proposal", "",
             "Archive: %d days. Train %s to %s, test %s to %s. %d one-knob candidates had enough train trades; "
             "the best %d on train were scored on test." % (len(p["days"]), p["train"][0], p["train"][1],
                                                           p["test"][0], p["test"][1], p["tried"], len(p["shortlist"])), "",
             "| | train trades | train total | train avg | test trades | test total | test avg | verdict |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    b = p["baseline"]
    lines.append("| current %s | %d | %s | %s | %d | %s | %s | baseline |" % (
        strategy_id(s), b["train"]["trades"], fmt_r(b["train"]["total_r"]), fmt_r(b["train"]["avg_r"]),
        b["test"]["trades"], fmt_r(b["test"]["total_r"]), fmt_r(b["test"]["avg_r"])))
    for r in p["shortlist"]:
        lines.append("| `%s` %s -> %s | %d | %s | %s | %d | %s | %s | %s |" % (
            r["knob"], r["from"], r["to"], r["train"]["trades"], fmt_r(r["train"]["total_r"]),
            fmt_r(r["train"]["avg_r"]), r["test"]["trades"], fmt_r(r["test"]["total_r"]), fmt_r(r["test"]["avg_r"]),
            r["why"]))
    lines.append("")
    if p["best"]:
        r = p["best"]
        lines += ["**Proposed:** change `%s` from %s to %s. On the %d test days it never saw while being chosen it "
                  "made %s over %d trades against %s for the current strategy." % (
                      r["knob"], r["from"], r["to"], p["test_days"],
                      fmt_r(r["test"]["total_r"]), r["test"]["trades"], fmt_r(b["test"]["total_r"])), "",
                  "This is still a small sample and %d candidates were tried, so one of them can look good by "
                  "luck. Adopting it starts a new version; the daily reviews then forward-test it on days nobody "
                  "has seen yet. Drop it if the forward record turns negative." % p["tried"]]
    else:
        lines += ["**No change proposed.** Nothing that won on the train days also held up on the test days. "
                  "That is the honest answer more often than not: keep the current strategy and keep collecting days."]
    if notes:
        lines += ["", "## What the write-ups pointed at", ""] + notes
    lines += ["", "---", "", NOTE, ""]
    return "\n".join(lines)


def writeup_notes(rows):
    """The widest bucket spreads with enough signals: the hypotheses the reviews raise."""
    out = []
    for feat, label, cells in bucket_table(rows):
        cells = [c for c in cells if c[1] >= 15 and c[0] != "n/a"]
        if len(cells) < 2:
            continue
        lo, hi = min(cells, key=lambda c: c[2]), max(cells, key=lambda c: c[2])
        out.append((hi[2] - lo[2], "- %s: best %s (%d signals, %s avg), worst %s (%d, %s avg)." % (
            label, hi[0], hi[1], fmt_r(hi[2]), lo[0], lo[1], fmt_r(lo[2]))))
    return [x[1] for x in sorted(out, reverse=True)[:4]]


# ---------------------------------------------------------------- commands

def write_json(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def write_text(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def symbols_of(a):
    if a.symbols:
        return [x.strip().upper() for x in a.symbols.split(",") if x.strip()]
    return read_universe(a.universe or os.path.join(HERE, "universe.txt"))


def say(a, msg):
    if not getattr(a, "quiet", False):
        print(msg)


def gather(a, rng):
    """Bars from --offline or Yahoo, into the archive. -> days written."""
    if a.offline:
        by_day = absorb(bundle_from_file(a.offline))
        failed = {}
    else:
        charts, failed = fetch(symbols_of(a), rng, a.pause, log=None if a.quiet else (lambda m: print(m, file=sys.stderr)))
        if not charts:
            raise SimError("Yahoo returned nothing (%s)" % (next(iter(failed.values())) if failed else "no symbols"))
        by_day = absorb(charts)
    written = save_archive(a.root, by_day)
    if failed:
        say(a, "%d symbol%s failed: %s" % (len(failed), "" if len(failed) == 1 else "s", ", ".join(sorted(failed)[:12])))
    return written


def cmd_backfill(a):
    written = gather(a, "60d")
    say(a, "archived %d complete session%s%s" % (len(written), "" if len(written) == 1 else "s",
                                                ": %s to %s" % (written[0], written[-1]) if written else ""))
    return 0 if written else 1


def run_one(a, day, s):
    bundle = load_archive_day(a.root, day)
    if bundle is None:
        raise SimError("no archived bars for %s; run `sim.py day --date %s` after the close" % (day, day))
    rows = simulate_day(day, bundle, s, history_before(a.root, day, s["or_minutes"] // BAR_MIN))
    alllog = write_log(a.root, day, strategy_id(s), rows)
    return rows, alllog


def cmd_run(a):
    s = ensure_strategy(a.root) if not a.strategy else load_strategy(a.strategy)
    day = parse_day(a.date) if a.date else archived_days(a.root)[-1]
    rows, _ = run_one(a, day, s)
    t = [r for r in rows if r["taken"]]
    st = stats(rows)
    say(a, "%s %s: %d signals, %d taken, %s, %s" % (day, strategy_id(s), len(rows), len(t), money(st["pnl"]),
                                                   fmt_r(st["total_r"])))
    return 0


def cmd_review(a):
    s = ensure_strategy(a.root)
    day = parse_day(a.date) if a.date else archived_days(a.root)[-1]
    alllog = read_log(a.root)
    text = review_md(day, alllog, alllog, s)
    path = os.path.join(a.root, "reviews", "%s.md" % day.isoformat())
    write_text(path, text)
    say(a, "wrote %s" % path)
    return 0


def cmd_day(a):
    s = ensure_strategy(a.root)
    day = parse_day(a.date) if a.date else now_eastern().date()
    ok, why = trading_day(day)
    if not ok:
        say(a, "%s: no session (%s). Nothing to simulate." % (day, why))
        return 0
    if load_archive_day(a.root, day) is None or a.refetch:
        if not a.date and not a.offline:
            now = now_eastern()
            close_m = session_close(day).hour * 60 + session_close(day).minute
            if now.hour * 60 + now.minute < close_m + 10:
                raise SimError("it is %s ET: run `day` after %s ET, once the session's bars are final"
                               % (now.strftime("%H:%M"), min_hhmm(close_m + 10)))
        gather(a, "5d")
    if load_archive_day(a.root, day) is None:
        raise SimError("Yahoo had no complete session for %s" % day)
    rows, alllog = run_one(a, day, s)
    path = os.path.join(a.root, "reviews", "%s.md" % day.isoformat())
    write_text(path, review_md(day, alllog, alllog, s))
    st = stats(rows)
    say(a, "%s %s: %d signals, %d taken, %s, %s. Review: %s" % (
        day, strategy_id(s), len(rows), st["trades"], money(st["pnl"]), fmt_r(st["total_r"]), path))
    return 0


def cmd_evaluate(a):
    s = load_strategy(a.strategy) if a.strategy else ensure_strategy(a.root)
    days, bundles = load_bundles(a.root)
    if not days:
        raise SimError("the archive is empty; run backfill first")
    train, test = split(days)
    rows = run_days(days, bundles, s)
    out = {"strategy": strategy_id(s), "all": score(rows, days), "train": score(rows, train),
           "test": score(rows, test), "train_days": [train[0].isoformat(), train[-1].isoformat()],
           "test_days": [test[0].isoformat(), test[-1].isoformat()] if test else None}
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    for k in ("all", "train", "test"):
        st = out[k]
        say(a, "%-5s %3d days %4d trades  win %s  total %s  avg %s (se %s)  drawdown %s" % (
            k, st["days"], st["trades"], "n/a" if st["win_rate"] is None else "%3d%%" % round(st["win_rate"] * 100),
            fmt_r(st["total_r"]), fmt_r(st["avg_r"]), "n/a" if st["se_r"] is None else "%.2f" % st["se_r"],
            fmt_r(-st["max_dd_r"])))
    return 0


def cmd_propose(a):
    s = ensure_strategy(a.root)
    p = propose(a.root, s, a.min_test_trades, a.min_gain)
    stamp = archived_days(a.root)[-1].isoformat()
    md = proposal_md(p, s, writeup_notes(read_log(a.root)))
    write_text(os.path.join(a.root, "proposals", "%s.md" % stamp), md)
    doc = {"made": stamp, "baseline": strategy_id(s), "train": p["train"], "test": p["test"], "tried": p["tried"],
           "passes": bool(p["best"])}
    if p["best"]:
        c = copy.deepcopy(p["best"]["strategy"])
        c["version"] = int(s["version"]) + 1
        doc["change"] = {"knob": p["best"]["knob"], "from": p["best"]["from"], "to": p["best"]["to"],
                         "train": p["best"]["train"], "test": p["best"]["test"]}
        doc["strategy"] = c
    write_json(os.path.join(a.root, "proposals", "%s.json" % stamp), doc)
    say(a, md if not a.quiet else "")
    return 0


def cmd_apply(a):
    with open(a.proposal, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not doc.get("passes") or "strategy" not in doc:
        raise SimError("that proposal changes nothing (no candidate passed the test days)")
    cur = ensure_strategy(a.root)
    if strategy_id(cur) != doc["baseline"]:
        raise SimError("that proposal was made against %s; the strategy is now %s. Run propose again."
                       % (doc["baseline"], strategy_id(cur)))
    new = normalize(doc["strategy"])
    new["adopted"] = doc["made"]
    new["evidence"] = doc["change"]
    write_json(strategy_path(a.root), new)
    ch = doc["change"]
    entry = "- %s: %s -> %s. `%s` %s -> %s. Test days %s to %s: %s over %d trades.\n" % (
        doc["made"], doc["baseline"], strategy_id(new), ch["knob"], ch["from"], ch["to"], doc["test"][0],
        doc["test"][1], fmt_r(ch["test"]["total_r"]), ch["test"]["trades"])
    path = os.path.join(a.root, "CHANGELOG.md")
    head = "" if os.path.isfile(path) else "# Strategy changelog\n\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(head + entry)
    say(a, "adopted %s" % strategy_id(new))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Paper Simulator (trader-sim): replay, log, review, improve. No orders.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--root", default=DEFAULT_ROOT, help="where the archive, log and reviews live (%s)" % DEFAULT_ROOT)
        p.add_argument("--quiet", action="store_true")

    def source(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers instead of the universe")
        g.add_argument("--universe", help="one ticker a line (default: universe.txt next to this script)")
        p.add_argument("--offline", metavar="FILE", help="a saved Yahoo chart or {SYM: chart} bundle")
        p.add_argument("--pause", type=float, default=PAUSE)

    p = sub.add_parser("backfill", help="archive the last 60 days of 5-minute bars")
    common(p)
    source(p)
    p = sub.add_parser("day", help="after the close: fetch, simulate and review one day")
    common(p)
    source(p)
    p.add_argument("--date")
    p.add_argument("--refetch", action="store_true", help="fetch even if the day is archived")
    p = sub.add_parser("run", help="simulate one archived day into the trade log")
    common(p)
    p.add_argument("--date")
    p.add_argument("--strategy", help="a strategy file instead of the current one")
    p = sub.add_parser("review", help="write the day's review from the trade log")
    common(p)
    p.add_argument("--date")
    p = sub.add_parser("evaluate", help="score a strategy on the whole archive, train and test")
    common(p)
    p.add_argument("--strategy")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("propose", help="one-knob candidates, kept only if they win out of sample")
    common(p)
    p.add_argument("--min-test-trades", type=int, default=15)
    p.add_argument("--min-gain", type=float, default=2.0, help="total R a candidate must add on the test days")
    p = sub.add_parser("apply", help="adopt a proposal as the next strategy version")
    common(p)
    p.add_argument("proposal")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"backfill": cmd_backfill, "day": cmd_day, "run": cmd_run, "review": cmd_review,
                "evaluate": cmd_evaluate, "propose": cmd_propose, "apply": cmd_apply}[a.cmd](a)
    except SimError as e:
        print("sim.py: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("sim.py: stopped", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
