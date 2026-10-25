---
name: trader-sim
description: >-
  Paper Simulator. Replays every trading day on free 5-minute bars, logs
  every simulated trade, writes a review of what went well and what went
  wrong, and proposes strategy changes only when they win on days they were
  not tuned on. Archives each session to trader/sim/bars, simulates the long
  opening-range breakout bar by bar with slippage and no look-ahead, appends
  trader/sim/trades.csv, writes trader/sim/reviews/<day>.md, and turns the
  reviews into a train / test checked proposal that a person adopts. Never
  places an order. Use when the user says "simulate today", "paper trade
  the day", "what would the strategy have done", "review today's trades",
  "what went wrong today", "improve the strategy", "propose a change",
  "backtest on the archive", "run the daily loop", or on the daily
  scheduled run after the close.
argument-hint: "[backfill | day | run | review | evaluate | propose | apply] [--date YYYY-MM-DD]"
---

# trader-sim

The Paper Simulator is the pack's learning loop. Each trading day, after the
close, it replays the session on that day's free 5-minute bars as if the
strategy had traded it, writes every trade to a log, and writes a review.
Once enough days pile up, it tries small changes to the strategy and keeps
one only if it also wins on days it never saw while being chosen. It never
places, sends or sizes a real order. It is a simulation, not a trading
record, and a good simulated week does not mean the strategy makes money.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/sim.py" backfill                      # once: archive the last 60 days
python3 "${CLAUDE_SKILL_DIR}/sim.py" day                           # after 4:10 PM ET: fetch, simulate, review today
python3 "${CLAUDE_SKILL_DIR}/sim.py" day --date 2026-10-16         # a past day (archived, or within Yahoo's 5 days)
python3 "${CLAUDE_SKILL_DIR}/sim.py" evaluate                      # the current strategy on the whole archive
python3 "${CLAUDE_SKILL_DIR}/sim.py" evaluate --strategy idea.json # your own idea, same train / test split
python3 "${CLAUDE_SKILL_DIR}/sim.py" propose                       # -> trader/sim/proposals/<day>.md and .json
python3 "${CLAUDE_SKILL_DIR}/sim.py" apply trader/sim/proposals/2026-10-23.json   # adopt: a new version
```

Flags: `--root trader/sim`, `--symbols TSLA,NVDA` or `--universe FILE`
(default: `universe.txt` here, 60 liquid names with SPY as the market
reference), `--offline FILE` (a saved Yahoo chart or a `{SYM: chart}`
bundle), `--refetch`, `--pause 0.4`, `--quiet`. propose takes
`--min-test-trades 15` and `--min-gain 2` (R).

## What it reads and writes

| file | what |
| --- | --- |
| Yahoo chart API | one request a symbol: `range=5d` (day) or `60d` (backfill), `interval=5m`. Free, unofficial, can lag or rate-limit |
| `trader/sim/bars/<day>.json.gz` | the archive: every complete regular session, every symbol, `[minute, o, h, l, c, v]` bars and the prior close. Yahoo forgets 5-minute bars after 60 days; the archive does not |
| `trader/sim/strategy.json` | the current strategy, copied from `strategy.default.json` on first use. Changed only by `apply` |
| `trader/sim/trades.csv` | the trade log: one row a signal, taken or not, with entry, stop, target, exit, why, R, P&L, best and worst excursion, and the features below |
| `trader/sim/reviews/<day>.md` | the day's review |
| `trader/sim/proposals/<day>.md` / `.json` | propose's table and, when one passes, the candidate strategy |
| `trader/sim/CHANGELOG.md` | every adopted change, with its test-day evidence |

## How a day is simulated

The rules sit in `sim.py`'s docstring and in `strategy.json`. In short: the
signal is the first 5-minute bar after the opening range to close above the
range high, inside the entry window. The entry is the next bar's open plus
slippage, because nobody knows a bar closed above the range until it has. The
stop is a stop order (it fills past its price when a bar gaps through it).
The target is a limit. A bar that touches both counts as the stop. Anything
open at the flat-by bar is sold at its open. Filters (price, range width, gap,
relative volume, how far the signal is over the range) and the daily trade
cap decide which signals are taken. Signals a rule kept out are simulated
anyway and logged with `taken=0`, so the review can say what each rule cost
or saved.

Features logged for every signal: `gap_pct`, `range_pct`, `rvol` (opening-range
volume against up to 20 earlier archived sessions, so it fills in as the
archive grows), `pct_over`, `entry_min`, `day_change_pct` (SPY).

## The review

`reviews/<day>.md` has: the summary, the trades table, **What went well**,
**What went wrong** (a stop hit fast, a fill past the stop, a chase, a gap,
a trade that was in the money and gave it back), **What the rules kept you
out of**, **Patterns so far** (every signal of this version by feature
bucket, buckets under 20 signals marked thin), **Record by strategy
version**, and **Claude's read**.

After `day` writes the review, Claude fills in **Claude's read** in two or
three sentences: what today says, what it does not say, and at most one
idea worth testing. Rules for that section:

- One day is one sample. Never say a rule "works" or "is broken" from one day.
- Name a pattern only from the Patterns section, and quote its count.
- An idea is a hypothesis for `evaluate`, written as a knob and a value,
  never a change to make now.

## The improvement loop

The loop has two speeds, and both go through a person.

1. **Parameters, weekly.** `propose` splits the archive by date, the first
   two thirds to train and the last third to test. Each candidate changes
   one knob in `strategy.json` (the grid is `TUNABLE` in `sim.py`). The
   candidates are ranked on train only. The best five are scored on test. A
   candidate passes when, on the test days, it adds at least `--min-gain` R
   over the current strategy, its average R is above zero, and it has at
   least `--min-test-trades` trades. The proposal says how many candidates
   were tried, because with 30-odd tries one can pass by luck. Most weeks the
   answer is "no change", and that is the right answer.
2. **Ideas, as they come.** When the reviews keep pointing at something the
   grid does not cover, Claude writes the idea as a strategy file (a copy of
   `strategy.json` with the change), runs `evaluate --strategy` on it, and
   reports train and test side by side. A change to `sim.py` itself (a new
   filter, a new exit) is a code change: a branch, tests, and a pull request
   with the evaluate numbers before and after.

`apply` is the only thing that changes `strategy.json`. It refuses a
proposal made against an older version, bumps the version, and writes the
evidence to `CHANGELOG.md`. From then on every review shows the new version's
own forward record on days nobody had seen when it was chosen. If that
record turns negative over 20 or more trades, say so and propose going back.

## What the numbers can and cannot tell you

- 60 days of one setup on 60 symbols is a few hundred signals at most. A
  bucket's average R moves a lot with 20 trades in it.
- Free bars are 5-minute bars from an unofficial feed. Real fills are worse
  than these: spreads, queue position, halts, partial fills.
- Tuning on the same days you score on finds noise. That is why nothing
  passes without the test days, and why every version is forward-tested.

## Rules

- Say "simulated", every time. Never call the log trades, profits or returns
  without the word.
- Quote the sample size with every win rate or average.
- Never edit `strategy.json` by hand or skip `apply`. Never adopt a proposal
  without the user's yes (merging the proposal's pull request is a yes).
- Never relax `--min-test-trades` or `--min-gain` to get a change through.

## What it never does

It never places, sizes or sends an order, never calls trader-broker, never
reads broker keys and never calls its results real.

## Safety

- Education and tooling, not financial advice. A simulated profit is not a recommendation.
- Nothing here can trade. Going from a simulation to real money is the user's own decision, through
  trader-risk and trader-broker, and every order there still needs the user's explicit yes.
- No options, margin, short selling or leverage: the simulator only models long stock.
