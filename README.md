# Arb Finder

Finds arbitrage across betting sites: sets of prices that disagree enough that
backing every outcome pays more than it costs, whatever happens. Desktop UI,
standard library only, no third-party packages.

## Run it

```bash
python arb_app.py                    # opens on the built-in demo feed
python arb_app.py --live --key KEY   # or: export ODDS_API_KEY=... first
python -m unittest test_arb_core -v  # 33 tests over the maths and parsing
```

Python 3.9+. Tk ships with the python.org installers for macOS and Windows. On
Linux: `sudo apt install python3-tk` or `sudo dnf install python3-tkinter`.

The demo feed needs no key and no network — press **Find arbs** and you get
three worked examples (match odds, handicap, over/under). For live prices, get
a key from [the-odds-api.com](https://the-odds-api.com); the free tier is 500
calls a month, and one scan spends one call per region per market.

## The settings

| Setting | What it does |
| --- | --- |
| Total stake | Money split across the legs of one opportunity |
| Round stakes to | Stake granularity — `0.01` for pennies, `1` where a book takes whole units |
| Minimum return % | Hides thin edges; 0.5–1% is a sensible floor once you allow for slippage |
| Ignore prices older than | Drops quotes the feed last touched more than N minutes ago |
| Commission | Per-book rate on winnings, e.g. `betfair:5, smarkets:2` (exchange keys as the feed spells them) |
| Only books quoting every outcome | Keeps a two-way price out of a three-way market — see below |
| At least two bookmakers | Suppresses one-book "arbs", which are pricing errors that get voided |

Click any column to sort, type in **Filter** to narrow by team or bookmaker, and
**Save as CSV** writes one row per bet.

## The maths

For decimal odds `p₁…pₙ` on the outcomes of one market, `S = Σ 1/pᵢ`. An
arbitrage exists when `S < 1`, and the return on total stake is `1/S − 1`.

That is not the same number as the `1 − S` overround, and the difference is
real money: at `S = 0.95` the margin is 5.00% but the return is 5.263%. The app
quotes the return, because that is what turns into profit.

Commission on an exchange is charged on winnings rather than stake, so odds `p`
at rate `c` are worth `1 + (p − 1)(1 − c)` — 3.00 at 5% is really 2.90. Every
comparison and stake uses those adjusted odds.

Stakes are then chosen to maximise the **worst-case** return subject to the
rounding increment. Rounding each leg to its nearest ideal stake is not good
enough: with a coarse increment it leaves one leg short and can turn a real edge
into a loss. Instead the allocator bisects on the target return `T`, using the
fact that covering `T` on a leg priced `p` takes `⌈T / (increment × p)⌉` units,
so `T` is affordable exactly when those unit counts fit the budget. That finds
the true optimum — checked against exhaustive search over 8,000 random markets.
Because the figures shown are the real post-rounding outcome, an edge that
rounding has eaten shows as a loss instead of a phantom win.

## What it refuses to report

- **Two-way prices in a three-way market.** A book quoting only Home/Away on a
  match that can be drawn looks like a huge edge and pays nothing on a draw.
  Any book that does not quote every outcome of a market is dropped from that
  market.
- **Mismatched lines.** Over/under and handicap prices only get compared at the
  same line. Handicaps are grouped by the home team's number, so a home −2.5
  and an away +2.5 are recognised as the same market rather than two.
- **Single-book arbitrage**, stale quotes, and events already under way.
- Anything that looks too good is still shown, but flagged in the Notes column
  and in the amber banner: a 15% edge is almost always a stale or mistyped price.

## Files

- `arb_core.py` — odds maths, feed parsing, detection, CSV export. No UI imports.
- `arb_app.py` — the Tk window. Scans run on a worker thread and return through
  a queue, so the window stays responsive.
- `test_arb_core.py` — unit tests, including the exhaustive stake-allocation check.

## Worth knowing before staking anything

The screen shows candidates, not guarantees. Prices move between the feed's
snapshot and your bet landing, so a 1% edge frequently disappears mid-execution
and leaves you with one leg on. Books void obvious mistakes, limit or close
accounts that only ever take value, and cap stakes well below what a listed
price suggests. Feeds also lag their own bookmakers. Check every price on the
book's own site before you stake, keep the minimum return above your realistic
slippage, and treat the flags as instructions rather than trivia.

Arbitrage betting is legal in most places but breaches many bookmakers' terms.
Check the rules where you live, and the terms of any account you use.
