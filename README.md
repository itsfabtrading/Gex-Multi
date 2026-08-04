# gex_multi.py

**Own your levels.** Dealer gamma exposure (GEX) computed from public CBOE option chains for **any listed stock, ETF or index** — add a ticker to one list and it gets the full treatment. Ships configured for SPX, NDX, SPY, QQQ, SOXX and VIX, with price levels additionally converted into ES and NQ futures terms.

One command produces the gamma flip, call and put walls, gross-gamma magnet, 0DTE sub-levels and the options-implied 1-day range for every symbol you track — AAPL, NVDA, TSLA, IWM, GLD, whatever you trade — printed as text tables, saved as JSON, appended to a SQLite history, and rendered as charts.

---

## Why this exists

The obvious answer is cost — I was about to start paying for a levels subscription. That's not the real answer, and working out why is what produced this file instead.

**A vendor level is not a number. It's a function.** Every published gamma level is the output of a pipeline: an expiration window, an implied-volatility surface, a smoothing choice, a rule for which strike gets called "the wall," a decision about whether 0DTE is included and how it's weighted. You don't see any of that. You see the last number the pipeline produced.

Which means the vendor can change the function whenever they like — and from where you sit, nothing announces it. Your chart shows a *different level*. It does not show you a *different method*. There's no version number, no changelog, no diff. The series looks continuous because it is continuous; only its meaning changed.

**This is where it stops being an inconvenience and becomes a research problem.** Suppose you log sixty sessions of "price tested the flip and rejected" and build base rates from them. In month three the vendor ships a revision — a wider expiration window, a different flip interpolation, whatever. Your historical sample now describes an indicator that no longer exists. The live signal has quietly decoupled from the backtest that justified trading it. And the failure is invisible from inside the data: your hit rate drifts, and you attribute the drift to regime change, to volatility, to yourself. You are measuring non-stationarity in your own instrument and reading it as non-stationarity in the market. A process that can't distinguish those two things isn't falsifiable, and an unfalsifiable process can't be improved — only abandoned or believed.

There's a live version of the same problem, not just a historical one. Vendor products relabel levels intraday: when spot breaks through a structural strike, the structural maximum gets demoted to a generic rank label and something else inherits the name. If your rule is "fade the call wall," and the call wall moved because a labeling routine reassigned it rather than because the option chain changed, you just traded the vendor's user interface instead of dealer positioning.

**So the argument isn't that vendors are wrong.** They're better resourced, their modeling is probably more sophisticated, and on any given morning their numbers may well sit closer to the truth than these. Accuracy was never the claim. The argument is narrower and harder to dodge: *an input you cannot inspect, freeze, or version cannot be the foundation of a falsifiable process.* Whatever your rules key off has to be something you can reproduce on demand. Which feed sits underneath is an implementation detail — swap the fetcher, upgrade to live data, change the vendor of the raw chain, and the property that matters survives, because the methodology on top of it is still yours.

**That's what ownership actually buys** — and it isn't accuracy:

- **A frozen methodology.** Every assumption is a named constant at the top of one file. Nothing changes unless you change it, and when you do, you know the date.
- **A reproducible history.** Every run appends to SQLite along with the parameters that produced it — conversion mode, basis, expiration window, IV source. A level from three months ago can be recomputed exactly, and you can prove what you were looking at when you took the trade.
- **One variable at a time.** You can move the expiration window from 4 to 8, or flip the basis mode, and measure what it does to your level stability. With a black box you can't run that experiment at all, so you never learn which assumptions carry the signal and which are decoration.
- **Attribution.** When a rule fails you can walk it back to the input that produced it. You can't debug what you can't see.

The practical stance that falls out of this: **own the primary input, use vendors to triangulate.** Cross-referencing your engine against a commercial feed is genuinely useful — when they disagree materially, one of you has a bug or a different assumption, and finding out which is informative either way. What you don't do is let a number you can't reproduce sit inside your decision rule.

---

## How this came about

None of the above was the starting point — it's what I concluded on the way.

I was looking for GEX data on X and came across the **@ESGexLevels** profile. What caught my attention wasn't a level, it was a detail in how they described their setup: they build from CBOE's delayed options data. That reframed the problem. Delayed public data was apparently good enough to produce a usable structural map, which meant this wasn't something only a funded desk could do. So I decided to build the same thing for my own use, pointed at NQ instead of ES. I also took their chart layout as a starting point — horizontal net-gamma bars with the key levels labeled down the left margin — and modified it from there for what I needed: the 1D band, the magnet, the 0DTE splits, multiple symbols per run.

Then I went to GitHub and found that several people had already built pieces of this. Rather than reimplement a CBOE fetcher and a Black-Scholes gamma engine from scratch, I used one that worked and spent my time on the parts nobody had done for my use case — the futures conversion, the expiration window, the level taxonomy, the persistent history. Standing on existing work isn't a shortcut here, it's the sensible move: the plumbing is a solved problem, and the interesting decisions are further up.

What started as "before I pay for this, let me see what it takes to build it" turned into something more useful than a saved subscription: once the whole pipeline was mine, every assumption in it became a variable I could test rather than a number I had to accept.

---

## What it produces

For each symbol, on every run:

```
================================================================
  NQ (from NDX)   (converted: NDX + 210 pts)
  price levels in NQ terms  |  gamma is NDX's, not native NQ
================================================================
  Spot (conv)        : 22,145.30
  exps used          : 4 nearest (front 2026-06-12)
  Gamma flip         : 22,010.00   (positive)
  Gamma flip 0DTE    : 22,060.00
  Call wall (max +g) : 22,400.00
  Call wall 0DTE     : 22,300.00
  Put wall  (max -g) : 21,800.00
  Put wall 0DTE      : 21,900.00
  Magnet (max gross) : 22,100.00   (gross 1.482 bn)
  0DTE share of GEX  : 38.4% of the 4-exp window expires today
  1D Max (settle+EM) : 22,398.11
  1D Min (settle-EM) : 21,861.44
     settle=22,129.78  sigma30=21.40% (VXN)  EM=+/-1.12%
----------------------------------------------------------------
  Top 20 strikes by |net gamma|  ($bn / 1% move):
     ...
================================================================
```

*(Illustrative values — replace with a real run before trusting the shape.)*

Alongside the tables:

- **`summaries/gex_multi_YYYYMMDD_HHMMSS_summary.json`** — full snapshot of every table plus the conversion parameters and expiration window used to build it.
- **`gex_history.db`** — SQLite, one row per symbol per run. The schema auto-migrates when new columns appear, so old rows survive engine changes. This is the file that makes the history reproducible.
- **`charts/gex_*.png`** — horizontal net-gamma-by-strike charts, dark theme, with flip, walls, magnet, spot and the 1D band labeled and de-overlapped.

---

## Level definitions

| Level | Definition |
|---|---|
| **Gamma flip** | Interpolated zero-crossing of the net gamma profile nearest spot. Above it, dealer hedging dampens moves; below it, it amplifies them. |
| **Call wall** | Strike with the most **positive** net dealer gamma (`idxmax`) — typical overhead pin or resistance. |
| **Put wall** | Strike with the most **negative** net dealer gamma (`idxmin`) — support, or an acceleration zone when it fails. |
| **Magnet** | Gross-gamma pin: the strike with the largest `call + \|put\|` gamma. Heavy two-sided open interest on round strikes can net small while grossing enormous, so this often sits where neither wall does. |
| **0DTE flip / walls** | Same engine, front expiration only — plus the share of window gamma expiring today. |
| **1D Max / 1D Min** | `settle × (1 ± EM)` where `EM% = σ₃₀ × √(1/365)`. σ from VIX (SPX/SPY/ES), VXN (NDX/QQQ/NQ), or the symbol's own iv30. |
| **GEX 1..N** | Remaining strikes ranked by absolute net gamma, walls excluded, labeled on the chart. |

Dollar gamma follows the SqueezeMetrics convention: `gamma × OI × 100 × spot² × 0.01`, puts negated, reported in **$bn per 1% move**.

---

## Two design decisions worth reading

**1. Time-to-expiry is floored at 1/262.** Black-Scholes gamma diverges to infinity for an at-the-money option as `T → 0`. Feed a raw 0DTE chain into an unfloored engine near the close and the ATM strike's gamma spike hijacks both walls — the put wall snaps to spot, the gross magnet triples, and the levels become garbage exactly when you need them most. The `1/262` business-day floor caps that spike and puts the strike bars on the same engine as the flip. Production default, not optional.

**2. Index → futures conversion is additive, not multiplicative.** The futures premium is cash-and-carry: a fixed **number of points** across all strikes, not a percentage. So `ES = SPX + basis` by default, where basis is read once each morning as prior ES settle minus prior SPX close at matching 16:00/16:15 ET stamps. A ratio mode exists via `INDEX_BASIS_MODE = "ratio"` for the ETF-derived tables.

Critically: **only price levels convert. Gamma never scales.** A table labeled "NQ (from QQQ)" is QQQ's gamma plotted on the NQ price axis. Every converted table says so in its own header. Moving the price axis is standard practice; pretending the gamma is native is not.

---

## Install

Requires **Python 3.9+** (uses `zoneinfo`).

```bash
pip install -r requirements.txt
```

`matplotlib` is optional — without it charts are skipped and everything else still runs. `scipy` is not optional; the gamma engine uses `scipy.stats.norm`.

**Two modules must sit beside `gex_multi.py`:**

- **`cboe_data.py`** — `get_quotes(symbol)` and `get_ticker_info(symbol)`, hitting CBOE's delayed options JSON endpoint.
- **`gamma_exposure.py`** — `calc_gamma_exposure()` and `calculate_gamma_profile()`.

Both come from **[GMestreM/gex_data](https://github.com/GMestreM/gex_data)** (MIT licensed). They are **not redistributed here**, so that you always get the current upstream version — grab them directly:

```bash
git clone https://github.com/GMestreM/gex_data.git
cp gex_data/cboe_data.py gex_data/gamma_exposure.py .
```

Index symbols (SPX, NDX) must be registered on `cboe_data.py`'s index list to fetch correctly.

---

## Usage

```bash
python gex_multi.py                                  # uses the constants in the file
python gex_multi.py 10.13 41.49                      # ES/SPY ratio, NQ/QQQ ratio
python gex_multi.py 10.13 41.49 21.4                 # ...plus VXN level
python gex_multi.py 10.13 41.49 21.4 51.0 210.0      # ...plus ES-SPX and NQ-NDX basis, in points
```

Positional arguments in order: `ES/SPY ratio`, `NQ/QQQ ratio`, `VXN`, `ES–SPX basis`, `NQ–NDX basis`. Omitted values fall back to the constants at the top of the file — except VXN, which has no constant and instead triggers a CBOE lookup that will not resolve, then falls back to QQQ's iv30 (see below).

**Pass VXN yourself.** CBOE doesn't serve it through this endpoint, so the Nasdaq side falls back to QQQ's iv30 — usable, but off-methodology. The table always tells you which one it used.

---

## Configuration

Everything lives in one block at the top of the file. These are the research variables:

| Constant | Default | What it does |
|---|---|---|
| `SYMBOLS` | `["SPX","NDX","SPY","QQQ","SOXX","VIX"]` | Add any CBOE-listed ticker; IV falls back to its own iv30 automatically. |
| `HIDE_NATIVE` | `["NDX"]` | Fetched for conversion only, no native table. |
| `N_EXPIRATIONS` | `4` | Nearest expirations in the window. `None` = all listed. |
| `INDEX_BASIS_MODE` | `"additive"` | `"additive"` or `"ratio"`. |
| `TABLE_TOP_N` | `20` | Strikes in the table and bars on the chart. |
| `CHART_TOP_N` | `7` | Non-wall strikes labeled GEX 1..N. |
| `CHART_SYMBOLS` | `None` | `None` charts every displayed table. A list restricts it; strings must match table labels exactly. |
| `PLOT_0DTE_LEVELS` | `True` | Also draw the 0DTE walls and flip on the charts, dotted, in their parent's colour. |
| `SUMMARY_DIR` | `"summaries"` | Folder for the per-run JSON snapshots. |
| `SHOW_1D_BAND` / `SHOW_MAGNET` / `SHOW_0DTE` | `True` | Feature toggles. |

**Why near-dated only:** for intraday trading, near-dated gamma drives today's dealer hedging; far-dated LEAPS dilute the read. Every table prints `exps used: N (front DATE)` so you can verify the window and confirm the front expiration is genuinely 0DTE. The 1D band is unaffected by this setting — only the gamma-derived levels change.

### Running any ticker

Nothing in the engine is index-specific. Add a symbol to `SYMBOLS` and it gets a full table, chart, JSON entry and history row like everything else:

```python
SYMBOLS = ["SPX", "NDX", "SPY", "QQQ", "SOXX", "VIX", "AAPL", "NVDA", "IWM", "GLD"]
```

Anything with a listed CBOE option chain works — single names, sector and index ETFs, commodity and bond funds. The engine derives everything it needs from the chain itself, so there's no per-symbol setup, and volatility falls back to the symbol's own `iv30` unless you map it in `IV_SOURCE`.

Three things are symbol-specific and worth knowing:

- **Cash indices** (SPX, NDX, RUT) must be registered on `cboe_data.py`'s index list, since CBOE serves them from a different path than equities. Stocks and ETFs need nothing.
- **The futures conversion is only defined for ES and NQ.** Every other symbol reports in its own native price terms, which is usually what you want anyway.
- **The 1D band uses the symbol's own 30-day implied vol** unless `IV_SOURCE` maps it elsewhere. That's correct for single names, but it means the band on a low-liquidity ticker inherits whatever noise is in its IV surface.

---

## Caveats

- **The data is currently delayed.** CBOE's public endpoint is not a live feed. On expiration days, prior-close chains are stale by the open. The fetcher is a single swappable module, so this is a property of the current data source rather than of the engine.
- **Pre-market "spot" is the prior session's close.** It's the expected-move basis, not a live quote. Only a live spot tells you which side of the flip you're on.
- **Walls are never forced to bracket spot.** If both sit above price, that's information — dealers are positioned entirely overhead. Clamping walls around spot destroys exactly the breakout signal you want.
- **The ±1σ band is an assumption, not a finding.** The 68% containment claim inherited from the expected-move formula is not validated here. Measure your own touch and containment rates from the SQLite history before trusting it.
- **Converted tables are converted.** See design decision #2.
- **This is one methodology among several.** Owning it means you can defend every choice in it — not that every choice is right.

---

## References

The methodology here is assembled from public work, not invented:

- **SqueezeMetrics (Prior Analytics LLC)** — *Gamma Exposure (GEX)™: Quantifying hedge rebalancing in SPX options*, March 2016, revised December 2017. Source of the dollar-gamma convention. [PDF](https://squeezemetrics.com/monitor/download/pdf/white_paper.pdf) *(not redistributed in this repo — the document carries its own restrictions)*.
- **Baltussen, Da, Lammers & Martens (2020)** — *Hedging Demand and Market Intraday Momentum*. SSRN 3760365. Evidence that short-gamma hedging forces trading in the direction of price, producing intraday momentum across 60+ futures markets since 1974.
- **Ni, Pearson & Poteshman (2004)** — *Stock Price Clustering on Option Expiration Dates*. SSRN 519044. Evidence for strike pinning from market-maker hedge rebalancing — the empirical backing for the magnet level.
- **Colin Bennett** — *Trading Volatility: Correlation, Term Structure and Skew* (2014). General reference for gamma mechanics and dealer hedging.

---

## Attribution & license

`gex_multi.py` is licensed under the **Apache License 2.0** — see [`LICENSE`](LICENSE). Use it, fork it, change the constants, tell me what you find.

The two dependency modules are **not mine and not included**: `cboe_data.py` and `gamma_exposure.py` are the work of [**GMestreM**](https://github.com/GMestreM/gex_data), used here under the MIT License. Full credit there — no reason to rebuild a working CBOE fetcher from scratch, and thanks to GMestreM for adding a license so others can build on it.

The chart layout is adapted, with modifications, from the style used by **@ESGexLevels**, whose posts are also what pointed me at CBOE's delayed data as a viable source in the first place. Credit where it's due.

---

## Disclaimer

This is a research tool. It produces numbers, not trade recommendations. Nothing here is financial advice, and none of these levels are a validated edge — whether dealer gamma positioning has measurable predictive value for intraday futures is an open question and the subject of a separate, ongoing, prospective study. Trading futures involves substantial risk of loss. Use at your own risk.

---

## Author

Built and maintained by **[@itsfabtrading](https://x.com/itsfabtrading)** — futures trader focused on NQ, New York session.

Levels, methodology notes and whatever the research actually shows get posted there.

Issues and pull requests welcome. If you find a bug in the gamma math, please open an issue with the chain snapshot that produced it.
