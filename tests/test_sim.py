import copy
import csv
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

sim = load("trader-sim", "sim")

TSLA = os.path.join(FIX, "trader-timemachine", "TSLA-60d5m.json.gz")  # 59 complete sessions to Oct 16 2026
DAY = datetime.date(2026, 10, 14)


def strat(**kw):
    s = copy.deepcopy(sim.DEFAULT_STRATEGY)
    s.update(kw)
    return sim.normalize(s)


def bars(path, start_min=570):
    """[(o, h, l, c, v), ...] -> a full session of 78 bars, padded flat at the last close."""
    out = []
    for i, (o, h, l, c, v) in enumerate(path):
        out.append([start_min + 5 * i, o, h, l, c, v])
    last = out[-1][4]
    while len(out) < 78:
        out.append([start_min + 5 * len(out), last, last, last, last, 1000])
    return out


# opening range 100 to 102 (high 102, low 100); bar 3 closes 102.5 (signal at 9:50), entry at bar 4's open
RANGE = [(101, 102, 100, 101.5, 10000), (101.5, 101.8, 100.5, 101, 9000), (101, 101.9, 100.8, 101.6, 8000)]


def session(after, prev=100.0):
    return {"bars": bars(RANGE + [(101.6, 102.6, 101.5, 102.5, 7000)] + after), "prev_close": prev}


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = sim.main(argv)
    return code, out.getvalue(), err.getvalue()


class Fills(unittest.TestCase):
    def test_target_first(self):
        s = strat(slippage_bps=0)
        row = sim.simulate_symbol("X", session([(102.5, 103.0, 102.4, 102.9, 5000), (102.9, 104.0, 102.8, 103.8, 5000)]),
                                  s, [])
        self.assertEqual(row["signal_time"], "09:50")
        self.assertEqual(row["entry_time"], "09:50")
        self.assertEqual(row["entry"], 102.5)
        self.assertEqual(row["stop"], 101.0)           # range high - 0.5 x range
        self.assertEqual(row["target"], 104.0)         # entry + 1 x risk 1.5
        self.assertEqual(row["exit_reason"], "target")
        self.assertEqual(row["r"], 1.0)
        self.assertEqual(row["shares"], 24)            # min(50 // 1.5 = 33, 2500 // 102.5 = 24)
        self.assertEqual(row["pnl"], 36.0)
        self.assertEqual(row["exit_time"], "10:00")

    def test_stop_and_target_in_one_bar_counts_as_the_stop(self):
        row = sim.simulate_symbol("X", session([(102.5, 104.5, 100.5, 102, 5000)]), strat(slippage_bps=0), [])
        self.assertEqual(row["exit_reason"], "stop_both")
        self.assertEqual(row["r"], -1.0)

    def test_a_gap_through_the_stop_fills_at_the_open(self):
        row = sim.simulate_symbol("X", session([(102.5, 102.6, 102.0, 102.1, 5000), (100.0, 100.2, 99.5, 99.8, 5000)]),
                                  strat(slippage_bps=0), [])
        self.assertEqual(row["exit_reason"], "stop")
        self.assertEqual(row["exit"], 100.0)
        self.assertAlmostEqual(row["r"], -5 / 3, places=3)

    def test_slippage_costs_on_both_sides(self):
        row = sim.simulate_symbol("X", session([(102.5, 102.6, 100.9, 101, 5000)]), strat(slippage_bps=10), [])
        self.assertAlmostEqual(row["entry"], 102.5 * 1.001, places=4)
        self.assertLess(row["r"], -1.0)

    def test_flat_by_sells_at_the_open_of_that_bar(self):
        row = sim.simulate_symbol("X", session([(102.5, 102.8, 102.2, 102.6, 5000)]), strat(slippage_bps=0), [])
        self.assertEqual(row["exit_reason"], "flat")
        self.assertEqual(row["exit_time"], "15:55")
        self.assertEqual(row["exit"], 102.6)

    def test_no_signal_before_the_window_or_after_it(self):
        row = sim.simulate_symbol("X", session([]), strat(no_entries_before="10:30"), [])
        self.assertEqual(row["signal_time"], "10:30")  # the 9:50 breakout is too early; the next close over counts
        late = {"bars": bars(RANGE + [(101.6, 101.9, 101.5, 101.8, 7000)] * 30 + [(101.8, 103, 101.8, 102.9, 7000)])}
        self.assertIsNone(sim.simulate_symbol("X", late, strat(no_entries_after="11:00"), []))

    def test_the_signal_never_uses_a_bar_that_is_not_closed(self):
        # the breakout bar is the 3:50 bar: the next bar is the flat-by bar, so no trade
        b = bars(RANGE + [(101.6, 101.9, 101.5, 101.8, 7000)] * 75)
        b[76] = [950, 101.8, 103, 101.8, 102.9, 7000]
        self.assertIsNone(sim.simulate_symbol("X", {"bars": b}, strat(no_entries_after="15:55"), []))

    def test_rvol_and_gap(self):
        row = sim.simulate_symbol("X", session([], prev=99.0), strat(), [13500, 13500])
        self.assertEqual(row["rvol"], 2.0)
        self.assertAlmostEqual(row["gap_pct"], (101 / 99.0 - 1) * 100, places=2)


class Day(unittest.TestCase):
    def test_filters_and_the_cap_still_simulate_what_they_skip(self):
        win = [(102.5, 104.5, 102.4, 104.4, 5000)]
        bundle = {"A": session(win), "B": session(win), "C": session(win, prev=90.0)}
        rows = sim.simulate_day(DAY, bundle, strat(max_trades_per_day=1, max_gap_pct=5.0, slippage_bps=0), {})
        by = {r["symbol"]: r for r in rows}
        self.assertEqual(by["C"]["skip_reason"], "max_gap_pct")
        self.assertEqual([r["taken"] for r in (by["A"], by["B"])], [1, 0])
        self.assertEqual(by["B"]["skip_reason"], "max_trades")
        self.assertEqual(by["B"]["r"], 1.0)  # simulated anyway

    def test_unknown_strategy_keys_are_refused(self):
        with self.assertRaises(sim.SimError):
            sim.normalize({"stop_fraction": 0.5})


class Archive(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root)

    def test_the_tsla_fixture_archives_59_complete_sessions(self):
        code, out, _ = run(["backfill", "--root", self.root, "--offline", TSLA])
        self.assertEqual(code, 0, out)
        days = sim.archived_days(self.root)
        self.assertEqual(len(days), 59)
        self.assertEqual(days[-1], datetime.date(2026, 10, 16))  # Oct 19 was still trading: left out
        b = sim.load_archive_day(self.root, days[-1])["TSLA"]
        self.assertEqual(len(b["bars"]), 78)
        self.assertEqual(b["prev_close"], sim.load_archive_day(self.root, days[-2])["TSLA"]["bars"][-1][4])

    def test_day_writes_the_log_and_the_review_and_is_idempotent(self):
        run(["backfill", "--root", self.root, "--offline", TSLA])
        for _ in range(2):
            code, out, err = run(["day", "--root", self.root, "--date", "2026-08-03"])
            self.assertEqual(code, 0, err)
        with open(os.path.join(self.root, "trades.csv")) as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["exit_reason"], "target")
        with open(os.path.join(self.root, "reviews", "2026-08-03.md")) as fh:
            text = fh.read()
        for head in ("## Summary", "## Trades", "## What went well", "## Patterns so far", "## Claude's read"):
            self.assertIn(head, text)
        self.assertIn("TSLA hit the +1.00R target", text)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "strategy.json")))

    def test_a_missing_day_is_a_plain_error(self):
        code, _, err = run(["run", "--root", self.root, "--date", "2026-08-03"])
        self.assertEqual(code, 1)
        self.assertIn("no archived bars", err)

    def test_evaluate_splits_train_and_test_by_date(self):
        run(["backfill", "--root", self.root, "--offline", TSLA])
        code, out, _ = run(["evaluate", "--root", self.root, "--json"])
        doc = json.loads(out)
        self.assertEqual(doc["all"]["days"], 59)
        self.assertEqual(doc["train"]["days"] + doc["test"]["days"], 59)
        self.assertLess(doc["train_days"][1], doc["test_days"][0])
        self.assertEqual(doc["all"]["trades"], doc["train"]["trades"] + doc["test"]["trades"])

    def test_propose_only_passes_what_wins_on_the_test_days(self):
        run(["backfill", "--root", self.root, "--offline", TSLA])
        s = sim.ensure_strategy(self.root)
        p = sim.propose(self.root, s, min_test_trades=5)
        self.assertGreater(p["tried"], 5)
        for r in p["shortlist"]:
            self.assertGreater(r["train"]["total_r"], p["baseline"]["train"]["total_r"])  # ranked on train
            if r["passes"]:
                self.assertGreater(r["test"]["avg_r"], 0)
        # on TSLA's 59 days nothing survives the test days: the honest answer
        self.assertIsNone(p["best"])
        code, out, _ = run(["propose", "--root", self.root, "--min-test-trades", "5"])
        self.assertIn("No change proposed", out)
        with open(os.path.join(self.root, "proposals", "2026-10-16.json")) as fh:
            self.assertFalse(json.load(fh)["passes"])

    def test_apply_bumps_the_version_and_refuses_a_stale_proposal(self):
        s = sim.ensure_strategy(self.root)
        c = copy.deepcopy(s)
        c["target_r"] = 2.0
        c["version"] = 2
        prop = {"made": "2026-10-16", "baseline": sim.strategy_id(s), "train": ["a", "b"],
                "test": ["2026-09-21", "2026-10-16"], "tried": 30, "passes": True, "strategy": c,
                "change": {"knob": "target_r", "from": 1.0, "to": 2.0, "train": {}, "test": {"total_r": 3.0, "trades": 20}}}
        path = os.path.join(self.root, "p.json")
        with open(path, "w") as fh:
            json.dump(prop, fh)
        code, out, err = run(["apply", "--root", self.root, path])
        self.assertEqual(code, 0, err)
        now = sim.load_strategy(sim.strategy_path(self.root))
        self.assertEqual((now["version"], now["target_r"]), (2, 2.0))
        with open(os.path.join(self.root, "CHANGELOG.md")) as fh:
            self.assertIn("`target_r` 1.0 -> 2.0", fh.read())
        code, _, err = run(["apply", "--root", self.root, path])
        self.assertEqual(code, 1)
        self.assertIn("Run propose again", err)

    def test_nothing_here_can_place_an_order(self):
        with open(os.path.join(os.path.dirname(sim.__file__), "sim.py")) as fh:
            src = fh.read()
        for word in ("alpaca", "ibkr", "place_order", "submit_order", "POST"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
