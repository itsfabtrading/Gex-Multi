"""
gex_multi.py  -  Multi-symbol GEX + 1D band + SQLite history
                 (N-nearest-expiration window, VXN override)

    @itsfabtrading            https://x.com/itsfabtrading

    Copyright 2026 @itsfabtrading

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
    implied. See the License for the specific language governing
    permissions and limitations under the License.

    Requires cboe_data.py + gamma_exposure.py from GMestreM/gex_data
        https://github.com/GMestreM/gex_data
    Gamma methodology per SqueezeMetrics / Prior Analytics LLC,
        "Gamma Exposure (GEX)", March 2016, revised December 2017.

WHY THIS EXISTS
  A vendor level is not a number, it's a function - and it's a function you
  can't see, don't version, and can't diff. When the vendor changes it, your
  chart shows a different level; it does not show you a different method. If
  your rules key off that number, your edge changed without your consent and
  without your knowledge. Everything below is frozen, parameterised and
  reproducible: same chain plus same constants always yields the same levels.
  That is the entire point of the file.

Tables: NQ, ES (converted) ; SPY, SPX, QQQ, SOXX, VIX (native).
Each carries the flip, walls, top strikes, and the 1D Max/Min band, and
every run is appended to gex_history.db.

------------------------------------------------------------------
EXPIRATION WINDOW  (new)
  By default the levels are computed over the N NEAREST expirations,
  front expiration included (= 0DTE when run live / nearest listed when
  run pre-market on the prior-settle chain).
      N_EXPIRATIONS = 4     # change this number
      N_EXPIRATIONS = None  # use ALL listed expirations (old behaviour;
                            #   equivalent to an "all expirations" view)
  Why near-term: for intraday trading, near-dated gamma drives dealer
  hedging today; far-dated LEAPS just dilute the read. Each table prints
  "exps used: N (front DATE)" so you can verify the window and that 0DTE
  is the front. The 1D Max/Min band is NOT affected by this - only the
  gamma-derived levels (flip, walls, strikes) change.

------------------------------------------------------------------
1D MAX / 1D MIN
  EM% = sigma * sqrt(1/365) ; 1D Max/Min = settle * (1 +/- EM%)
  sigma source: SPX/SPY/ES -> VIX ; QQQ/NQ -> VXN ; SOXX -> own iv30.
  VXN isn't served by CBOE, so by default the Nasdaq side falls back to
  QQQ iv30. Pass VXN yourself as the 3rd argument to stay on-methodology.

CONVERSION: only PRICE LEVELS scale by the ratio. GAMMA stays the source
ETF's - converted tables are QQQ/SPY gamma on the NQ/ES price axis.

USAGE
    python gex_multi.py                  # ratios from constants below
    python gex_multi.py 10.13 41.49      # ES/SPY then NQ/QQQ
    python gex_multi.py 10.13 41.49 21.4 # ...and VXN level (Nasdaq band)

Run in the `gex` conda env, beside cboe_data.py + gamma_exposure.py.
"""

import sys
import os
import json
import math
import sqlite3
import datetime
import numpy as np
import pandas as pd

from cboe_data import get_quotes, get_ticker_info
from gamma_exposure import calculate_gamma_profile, calc_gamma_exposure

# ----------------------------------------------------------------------
# EDIT EACH MORNING (or pass on the command line).
ES_SPY_RATIO = 10.13     # live ES / live SPY        (CLI arg 1)
NQ_QQQ_RATIO = 41.49     # live NQ / live QQQ        (CLI arg 2)

# Index -> future conversion. "additive" (default) is physically correct:
# the futures premium is cash-and-carry, a fixed NUMBER OF POINTS across all
# strikes, not a percentage. basis = prior ES settle - prior SPX close
# (same 16:00/16:15 ET stamps), read once each morning.
INDEX_BASIS_MODE = "additive"    # "additive": ES = SPX + basis | "ratio": ES = SPX x ratio
ES_SPX_BASIS = 51.0              # points  (CLI arg 4)
NQ_NDX_BASIS = 210.0             # points  (CLI arg 5)
ES_SPX_RATIO = 1.0076            # fallback used only when INDEX_BASIS_MODE = "ratio"
NQ_NDX_RATIO = 1.0076            # fallback used only when INDEX_BASIS_MODE = "ratio"

# Number of NEAREST expirations to include (front = 0DTE when live).
# Set to None to use ALL listed expirations ("all expirations" view).
N_EXPIRATIONS = 4

# Every symbol here is fetched from CBOE and gets a native table + chart.
# To add a ticker (e.g. "MU"), just append it: IV falls back to its own
# iv30 automatically; add its label to CHART_SYMBOLS if you want the PNG.
# Indexes (SPX/NDX-style) must be on cboe_data.py's index list to fetch.
SYMBOLS = ["SPX", "NDX", "SPY", "QQQ", "SOXX", "VIX"]
HIDE_NATIVE = ["NDX"]    # fetched for conversion only - no native table/chart
IV_SOURCE = {"SPX": "VIX", "SPY": "VIX", "QQQ": "VXN", "NDX": "VXN", "SOXX": "SELF"}
SETTLE_FIELD = "previousClose"   # prior-settle reference; flip to "close" if needed
CALENDAR_DAYS = 365

TABLE_TOP_N = 20          # strikes shown in the text table AND plotted as bars on the chart
GENERATE_CHARTS = True    # write one PNG per charted symbol into ./charts/
CHART_TOP_N = 7           # how many NON-WALL strikes to label GEX 1..N on the chart
# Which displayed tables to chart each morning.
#   None  = chart EVERY displayed table (anything in SYMBOLS, incl. VIX)  <- default
#   list  = restrict to these labels; strings must match table labels exactly, e.g.
#           CHART_SYMBOLS = ["NQ (from QQQ)", "ES (from SPX)"]
CHART_SYMBOLS = None

SHOW_1D_BAND = True    # draw 1D Max/Min (settle +/- EM) lines on charts
SHOW_MAGNET = True     # gross-gamma pin (max call+|put| strike) in table + chart
SHOW_0DTE = True       # 0DTE walls/flip + share of window GEX expiring today (table)
PLOT_0DTE_LEVELS = True  # also draw the 0DTE call/put wall + flip on the charts (dotted)

PCT_FROM, PCT_TO = 0.8, 1.2
DB_PATH = "gex_history.db"
SUMMARY_DIR = "summaries"   # folder for the gex_multi_<timestamp>_summary.json snapshots
# ----------------------------------------------------------------------


def _norm_vol(x):
    """Vol as a decimal. 21.4 -> 0.214 ; 0.214 stays 0.214."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if x <= 0:
        return None
    return x / 100.0 if x > 1 else x


def fetch_info(symbol):
    info_df, _ = get_ticker_info(symbol=symbol)
    if info_df is None or info_df.empty:
        return None
    s = info_df.iloc[:, 0]

    def first_positive(*fields):
        for f in fields:
            if f in s.index:
                try:
                    v = float(s[f])
                except (TypeError, ValueError):
                    continue
                if pd.notna(v) and v > 0:
                    return v
        return None

    return {
        "spot": first_positive("close", "price", "previousClose"),
        "settle": first_positive(SETTLE_FIELD, "close", "previousClose"),
        "iv30": s["ivThirty"] if "ivThirty" in s.index else None,
        "last_trade": pd.to_datetime(s.get("lastTradeTimestamp"))
        if s.get("lastTradeTimestamp") is not None else pd.Timestamp.now(),
    }


def get_vol_index_decimal(symbol):
    try:
        info = fetch_info(symbol)
    except Exception:
        return None
    return _norm_vol(info["spot"]) if info else None


def expected_move_pct(sigma):
    return sigma * math.sqrt(1.0 / CALENDAR_DAYS)


def _session_date():
    """Current trading-session date on the US-market wall clock (ET).
    Premarket, CBOE delayed quotes still carry YESTERDAY's last-trade
    timestamp, so expiry filtering must use the real clock, not the data."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo("America/New_York")).date()
    except Exception:  # no tz database -> UTC-4 approximation (EDT)
        return (datetime.datetime.utcnow() - datetime.timedelta(hours=4)).date()


def cap_expirations(chain, asof, n):
    """Drop expired rows, then keep the N nearest expirations (None = all).
    `asof` must be the true session reference (max of last_trade and the ET
    wall-clock date) so a stale premarket last_trade can't keep a dead
    expiration alive. Returns (chain, n_expirations_used, front_expiry_date)."""
    chain = chain[chain["expiration"].dt.date >= asof.date()]
    exps = sorted(pd.Series(chain["expiration"].dt.normalize().unique()).tolist())
    front = exps[0].date() if exps else None
    if n is not None and exps:
        keep = set(exps[:n])
        chain = chain[chain["expiration"].dt.normalize().isin(keep)]
    n_used = int(chain["expiration"].dt.normalize().nunique())
    return chain, n_used, front


def _gamma_rows(chain, spot, last_trade):
    """Per-row floored BS dollar gamma ($bn / 1% move), sign NOT yet applied.
    Same 1/262 0DTE floor as calculate_gamma_profile; expired rows dropped."""
    df = chain.copy()
    df["_bd"] = [np.busday_count(last_trade.date(), x.date()) for x in df["expiration"]]
    df = df[df["_bd"] >= 0]          # never let an expired row into the sums
    df["_dte_yr"] = np.where(df["_bd"] == 0, 1.0 / 262, df["_bd"] / 262.0)
    df["_gex"] = df.apply(
        lambda r: calc_gamma_exposure(
            spot, r["strike"], r["impliedVolatility"], r["_dte_yr"],
            0, 0, r["optionType"], r["openInterest"]
        ), axis=1) / 1e9
    return df


def _aggregate_strikes(rows):
    """Per-strike call/put/net dealer gamma from _gamma_rows output (puts negated)."""
    d = rows.copy()
    is_put = d["optionType"] == "put"
    d.loc[is_put, "_gex"] *= -1
    calls = d.loc[~is_put].groupby("strike")["_gex"].sum()
    puts = d.loc[is_put].groupby("strike")["_gex"].sum()
    out = pd.DataFrame({"Total Gamma Call": calls, "Total Gamma Put": puts}).fillna(0.0)
    out["Total Gamma"] = out["Total Gamma Call"] + out["Total Gamma Put"]
    return out


def total_gamma_by_strike(chain, spot, last_trade):
    """Net dealer gamma per strike ($bn / 1% move), floored-BS engine.

    Why not CBOE's reported gamma: Black-Scholes gamma -> infinity for an
    at-the-money option as time-to-expiry -> 0, so raw values spike on the
    ATM strike and hijack both walls. The 1/262 floor caps that spike and
    puts the bars on the same engine as the flip. Calls add, puts subtract.
    """
    return _aggregate_strikes(_gamma_rows(chain, spot, last_trade))


def compute_one(symbol, vol_map):
    print(f"  fetching {symbol} ...", flush=True)
    info = fetch_info(symbol)
    if not info or not info["spot"]:
        print(f"    !! no usable info for {symbol}")
        return None
    spot, settle, last_trade = info["spot"], info["settle"], info["last_trade"]

    chain = get_quotes(symbol=symbol)
    if chain is None or chain.empty:
        print(f"    !! no option chain for {symbol}")
        return None
    chain["expiration"] = pd.to_datetime(chain["expiration"])

    # True session reference: premarket, last_trade is still yesterday's close,
    # which would keep a dead expiration alive AND hand it full 0DTE weight.
    asof = max(pd.Timestamp(last_trade), pd.Timestamp(_session_date()))

    chain, n_exp, front_exp = cap_expirations(chain, asof, N_EXPIRATIONS)
    if chain.empty:
        print(f"    !! no contracts left for {symbol} after expiration filter")
        return None

    cp = chain.dropna(subset=["impliedVolatility"]).copy()
    cp = cp[cp["impliedVolatility"] > 0]

    try:
        _profile, flip = calculate_gamma_profile(cp.copy(), spot, asof, PCT_FROM, PCT_TO)
        flip = float(flip)
    except IndexError:
        flip = None

    rows = _gamma_rows(cp, spot, asof)
    strikes = _aggregate_strikes(rows)
    tg = strikes["Total Gamma"]
    call_wall = float(tg.idxmax())    # most POSITIVE net gamma (resistance / pin)
    put_wall = float(tg.idxmin())     # most NEGATIVE net gamma (support / acceleration)
    call_wall_gex = float(tg.max())
    put_wall_gex = float(tg.min())
    order = tg.abs().sort_values(ascending=False).index
    top = [(float(k), float(tg[k])) for k in order[:TABLE_TOP_N]]

    # Magnet: gross-gamma pin = strike with the biggest call + |put| gamma.
    # Huge two-sided OI (round strikes) can net small but gross enormous, so
    # this can coincide with a wall or sit where neither wall is.
    gross = strikes["Total Gamma Call"] + strikes["Total Gamma Put"].abs()
    magnet = float(gross.idxmax())
    magnet_gross = float(gross.max())
    magnet_net = float(tg[magnet])

    # 0DTE view: same engine, front expiration only.
    z_cw = z_pw = z_flip = z_pct = None
    if front_exp is not None:
        fmask = rows["expiration"].dt.date == front_exp
        tot_abs = float(rows["_gex"].abs().sum())
        if fmask.any() and tot_abs > 0:
            z_pct = float(rows.loc[fmask, "_gex"].abs().sum()) / tot_abs
            tg0 = _aggregate_strikes(rows.loc[fmask])["Total Gamma"]
            z_cw = float(tg0.idxmax())
            z_pw = float(tg0.idxmin())
            try:
                _p0, f0 = calculate_gamma_profile(
                    cp[cp["expiration"].dt.date == front_exp].copy(),
                    spot, asof, PCT_FROM, PCT_TO)
                z_flip = float(f0)
            except Exception:
                z_flip = None

    src = IV_SOURCE.get(symbol, "SELF")
    sigma, iv_label = None, None
    if src != "SELF":
        sigma = vol_map.get(src)
        iv_label = src
    if sigma is None:
        sigma = _norm_vol(info["iv30"])
        if sigma is not None:
            iv_label = f"{symbol} iv30" + ("" if src == "SELF" else f" (fallback; {src} N/A)")

    if sigma is not None and settle:
        em = expected_move_pct(sigma)
        one_d_max, one_d_min = settle * (1 + em), settle * (1 - em)
    else:
        em, one_d_max, one_d_min = None, None, None

    return {
        "symbol": symbol, "spot": float(spot),
        "settle": (float(settle) if settle else None),
        "flip": flip, "call_wall": call_wall, "put_wall": put_wall, "top": top,
        "call_wall_gex": call_wall_gex, "put_wall_gex": put_wall_gex,
        "magnet": magnet, "magnet_gross": magnet_gross, "magnet_net": magnet_net,
        "zdte_call_wall": z_cw, "zdte_put_wall": z_pw,
        "zdte_flip": z_flip, "zdte_pct": z_pct,
        "sigma": sigma, "iv_label": iv_label, "em_pct": em,
        "one_d_max": one_d_max, "one_d_min": one_d_min,
        "n_exp": n_exp, "front_exp": (str(front_exp) if front_exp else None),
    }


def table_levels(res, scale=1.0, offset=0.0):
    """Price transform: level*scale + offset. Ratio conversions use scale
    (offset 0); additive basis uses offset (scale 1). Gamma is never scaled."""
    flip = res["flip"]
    sc = lambda v: (v * scale + offset if v is not None else None)
    spot = sc(res["spot"])
    fl = sc(flip)
    regime = "positive" if (fl is not None and spot > fl) else ("negative" if fl is not None else "unknown")
    return {
        "spot": spot, "flip": fl, "regime": regime,
        "call_wall": sc(res["call_wall"]), "put_wall": sc(res["put_wall"]),
        "call_wall_gex": res["call_wall_gex"], "put_wall_gex": res["put_wall_gex"],
        "magnet": sc(res.get("magnet")), "magnet_gross": res.get("magnet_gross"),
        "magnet_net": res.get("magnet_net"),
        "zdte_call_wall": sc(res.get("zdte_call_wall")),
        "zdte_put_wall": sc(res.get("zdte_put_wall")),
        "zdte_flip": sc(res.get("zdte_flip")), "zdte_pct": res.get("zdte_pct"),
        "one_d_max": sc(res["one_d_max"]), "one_d_min": sc(res["one_d_min"]),
        "settle": sc(res["settle"]), "sigma30": res["sigma"], "em_pct": res["em_pct"],
        "iv_label": res["iv_label"], "n_exp": res["n_exp"], "front_exp": res["front_exp"],
        "top_strikes": [[k * scale + offset, g] for k, g in res["top"]],
    }


def _safe(name):
    return name.replace(" ", "_").replace("(", "").replace(")", "")


def _darken(hexc, f=0.5):
    """Return an RGB tuple of a hex color darkened by factor f (0-1)."""
    h = hexc.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (r * f / 255.0, g * f / 255.0, b * f / 255.0)


def make_chart(d, out_dir, stamp_disp, chart_top_n):
    """Render a horizontal net-gamma-by-strike chart for one displayed table.
    Bar length = |net gamma|, color = sign (cyan +, red -). Labels CALL/PUT
    wall, GAMMA FLIP, SPOT, and the top `chart_top_n` NON-wall strikes as
    GEX 1..N (ranked by |net|, walls excluded)."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.ticker import FuncFormatter
    except ImportError:
        print("  !! matplotlib not installed -> run:  pip install matplotlib   (charts skipped)")
        return None

    lv = d["levels"]
    label, converted, source, target = d["label"], d["converted"], d["source"], d["name"]
    strikes = list(lv["top_strikes"])
    if not strikes:
        print(f"  !! {label}: no strikes to chart")
        return None
    cw, pw, flip, spot = lv["call_wall"], lv["put_wall"], lv["flip"], lv["spot"]
    magnet = lv.get("magnet") if SHOW_MAGNET else None
    band_hi = lv.get("one_d_max") if SHOW_1D_BAND else None
    band_lo = lv.get("one_d_min") if SHOW_1D_BAND else None
    # 0DTE levels (front expiration only) -- drawn dotted, same hue as their parents.
    z = SHOW_0DTE and PLOT_0DTE_LEVELS
    cw0 = lv.get("zdte_call_wall") if z else None
    pw0 = lv.get("zdte_put_wall") if z else None
    flip0 = lv.get("zdte_flip") if z else None

    # Ensure walls (and a non-top-N magnet) are present as bars.
    sset = {s for s, _ in strikes}
    if cw not in sset and lv.get("call_wall_gex") is not None:
        strikes.append([cw, lv["call_wall_gex"]])
    if pw not in sset and lv.get("put_wall_gex") is not None:
        strikes.append([pw, lv["put_wall_gex"]])
    sset = {s for s, _ in strikes}
    if magnet is not None and magnet not in sset and lv.get("magnet_net") is not None:
        strikes.append([magnet, lv["magnet_net"]])
    net_by = {s: n for s, n in strikes}

    # GEX ranking: exclude the wall strikes, rank the rest by |net|, take top N.
    nonwall = [(s, n) for s, n in strikes if s != cw and s != pw]
    ranked = sorted(nonwall, key=lambda x: abs(x[1]), reverse=True)[:chart_top_n]
    gex_num = {s: i + 1 for i, (s, _) in enumerate(ranked)}

    ys = sorted(net_by)
    env = list(ys) + [spot] + ([flip] if flip is not None else [])
    env += [v for v in (cw0, pw0, flip0) if v is not None]
    if band_hi is not None and band_lo is not None:
        env += [band_hi, band_lo]
    span = (max(env) - min(env)) or 1.0
    pad = span * 0.04
    # Bar thickness as a fixed fraction of the y-range, so every chart in a run
    # looks identical regardless of how its strikes are spaced (the median-gap
    # approach made sparse-strike symbols like QQQ come out fat). The second
    # term only bites at very high strike counts, to avoid overlap.
    n_bars = max(len(ys), 1)
    bar_h = min(span * 0.014, span / n_bars * 0.7)
    maxw = max(abs(n) for n in net_by.values()) or 1.0

    CYAN, CYAN_DIM = "#4FC3E8", "#235a6e"
    RED, RED_DIM = "#FF4B5C", "#5e2730"
    CALL, PUT, YEL = "#CDEEF8", "#FF2A3D", "#F2C744"
    LAV, MINT = "#C3A6E8", "#BFE9CF"
    BG, GRID, FG = "#0a0e1a", "#1b2436", "#90a2b8"
    labeled = set(gex_num) | {cw, pw}

    fig, ax = plt.subplots(figsize=(6.4, 9.6), dpi=140)
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)

    for s, n in net_by.items():
        pos = n >= 0
        is_lbl = (s == cw) or (s == pw) or (s in gex_num) or (s == magnet)
        if s == cw:      c = CALL
        elif s == pw:    c = PUT
        elif is_lbl:     c = CYAN if pos else RED
        else:            c = CYAN_DIM if pos else RED_DIM
        ax.barh(s, abs(n), height=bar_h, color=c,
                edgecolor=_darken(c, 0.5), linewidth=1.2,
                zorder=(5 if is_lbl else 2))   # labeled levels drawn in front

    if flip is not None:
        ax.axhline(flip, color=YEL, lw=1.1, ls="--", zorder=3, alpha=0.9)
    # 0DTE levels: dotted, thinner, same hue as the all-expiration parent.
    if cw0 is not None:
        ax.axhline(cw0, color=CALL, lw=0.9, ls=":", zorder=3, alpha=0.8)
    if pw0 is not None:
        ax.axhline(pw0, color=PUT, lw=0.9, ls=":", zorder=3, alpha=0.8)
    if flip0 is not None:
        ax.axhline(flip0, color=YEL, lw=0.9, ls=":", zorder=3, alpha=0.8)
    ax.axhline(spot, color="#dfe7f0", lw=1.0, ls=":", zorder=3, alpha=0.65)
    if band_hi is not None and band_lo is not None:
        for b in (band_hi, band_lo):
            ax.axhline(b, color=LAV, lw=1.0, ls=(0, (4, 2, 1, 2)), zorder=3, alpha=0.85)

    tr = ax.get_yaxis_transform()  # x in axes fraction, y in data units

    # Collect all left-margin labels, then spread them so they never overwrite.
    specs = [(cw, f"CALL WALL  {cw:,.0f}", CALL, "bold"),
             (pw, f"PUT WALL  {pw:,.0f}", PUT, "bold"),
             (spot, f"SPOT  {spot:,.0f}", "#dfe7f0", "normal")]
    if flip is not None:
        specs.append((flip, f"GAMMA FLIP  {flip:,.0f}", YEL, "bold"))
    if magnet is not None:
        specs.append((magnet, f"MAGNET  {magnet:,.0f}", MINT, "bold"))
    if cw0 is not None:
        specs.append((cw0, f"CALL WALL 0DTE  {cw0:,.0f}", CALL, "normal"))
    if pw0 is not None:
        specs.append((pw0, f"PUT WALL 0DTE  {pw0:,.0f}", PUT, "normal"))
    if flip0 is not None:
        specs.append((flip0, f"GAMMA FLIP 0DTE  {flip0:,.0f}", YEL, "normal"))
    if band_hi is not None and band_lo is not None:
        specs.append((band_hi, f"1D MAX  {band_hi:,.0f}", LAV, "normal"))
        specs.append((band_lo, f"1D MIN  {band_lo:,.0f}", LAV, "normal"))
    for s, num in gex_num.items():
        specs.append((s, f"GEX {num}  {s:,.0f}", CYAN if net_by[s] >= 0 else RED, "bold"))

    specs.sort(key=lambda t: t[0])
    lo, hi = min(env) - pad, max(env) + pad
    gap = (hi - lo) * 0.020
    adj = [t[0] for t in specs]
    for i in range(1, len(adj)):                       # push overlaps upward
        if adj[i] - adj[i - 1] < gap:
            adj[i] = adj[i - 1] + gap
    for i in range(len(adj) - 2, -1, -1):              # pull back into bounds
        if adj[i + 1] - adj[i] < gap:
            adj[i] = adj[i + 1] - gap

    for (oy, text, color, weight), ly in zip(specs, adj):
        ax.text(-0.02, ly, text, transform=tr, ha="right", va="center",
                color=color, fontsize=8.5, fontweight=weight, fontfamily="monospace")

    ax.set_xlim(0, maxw * 1.15)
    ax.set_ylim(min(env) - pad, max(env) + pad)
    from matplotlib.ticker import MultipleLocator
    step = (0.1 if maxw <= 0.5 else 0.2 if maxw <= 1 else 0.5 if maxw <= 2
            else 1.0 if maxw <= 5 else 2.0 if maxw <= 12 else 5.0)
    ax.xaxis.set_major_locator(MultipleLocator(step))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"${x:g}B"))
    ax.tick_params(colors=FG, labelsize=8)
    ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.grid(axis="x", color=GRID, lw=0.6, zorder=0)
    ax.set_axisbelow(True)

    # Title + subtitle, centred across the FULL figure width. Centring on the
    # axes alone would sit them at ~64% of the image, because the left label
    # margin (31%) is outside the axes but very much inside the picture. The
    # old loc="left" also let the long converted-table subtitle run off the
    # right edge; centring keeps it inside the canvas.
    title_txt = f"{label}    NET GEX BY STRIKE"
    sub = f"{lv['regime']}   |   {stamp_disp}"
    if converted:
        sub += f"   |   gamma: {source} (levels in {target} terms)"
    fig.text(0.5, 0.952, title_txt, color="#e8eef5", fontsize=12,
             fontweight="bold", fontfamily="monospace", ha="center", va="center")
    fig.text(0.5, 0.931, sub, color=FG, fontsize=8.5,
             fontfamily="monospace", ha="center", va="center")

    fig.subplots_adjust(left=0.31, right=0.96, top=0.92, bottom=0.06)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"gex_{_safe(label)}.png")
    fig.savefig(path, facecolor=BG)
    plt.close(fig)
    return path


def render_table(title, target, lv, converted=False, source=None, conv_desc=None):
    bar = "=" * 64
    L = [bar]
    if converted:
        L.append(f"  {title}   (converted: {conv_desc})")
        L.append(f"  price levels in {target} terms  |  gamma is {source}'s, not native {target}")
    else:
        L.append(f"  {title}   GEX  (native, delayed)")
    L.append(bar)
    L.append(f"  Spot{' (conv)' if converted else ' (delayed)'}       : {lv['spot']:,.2f}")
    L.append(f"  exps used          : {lv['n_exp']} nearest (front {lv['front_exp']})")
    z = SHOW_0DTE and lv.get("zdte_call_wall") is not None
    # Flips together
    if lv["flip"] is not None:
        L.append(f"  Gamma flip         : {lv['flip']:,.2f}   ({lv['regime']})")
    else:
        L.append(f"  Gamma flip         : not found in window")
    if z:
        fl0 = f"{lv['zdte_flip']:,.2f}" if lv.get("zdte_flip") is not None else "n/a"
        L.append(f"  Gamma flip 0DTE    : {fl0}")
    # Call walls together
    L.append(f"  Call wall (max +g) : {lv['call_wall']:,.2f}")
    if z:
        L.append(f"  Call wall 0DTE     : {lv['zdte_call_wall']:,.2f}")
    # Put walls together
    L.append(f"  Put wall  (max -g) : {lv['put_wall']:,.2f}")
    if z:
        L.append(f"  Put wall 0DTE      : {lv['zdte_put_wall']:,.2f}")
    # Pin / expiry share
    if SHOW_MAGNET and lv.get("magnet") is not None:
        L.append(f"  Magnet (max gross) : {lv['magnet']:,.2f}   "
                 f"(gross {lv['magnet_gross']:.3f} bn)")
    if z and lv.get("zdte_pct") is not None:
        L.append(f"  0DTE share of GEX  : {lv['zdte_pct']*100:.1f}% of the "
                 f"{lv['n_exp']}-exp window expires today")
    if lv["one_d_max"] is not None:
        L.append(f"  1D Max (settle+EM) : {lv['one_d_max']:,.2f}")
        L.append(f"  1D Min (settle-EM) : {lv['one_d_min']:,.2f}")
        L.append(f"     settle={lv['settle']:,.2f}  sigma30={lv['sigma30']*100:.2f}% "
                 f"({lv['iv_label']})  EM=+/-{lv['em_pct']*100:.2f}%")
    else:
        L.append(f"  1D Max/Min         : skipped (no IV/settle)")
    L.append("-" * 64)
    L.append(f"  Top {len(lv['top_strikes'])} strikes by |net gamma|  ($bn / 1% move):")
    for strike, g in lv["top_strikes"]:
        L.append(f"     {strike:>13,.2f}    {g:+.3f}")
    L.append(bar)
    return "\n".join(L)


COLUMNS = [
    ("run_ts", "TEXT"), ("run_date", "TEXT"), ("symbol", "TEXT"),
    ("is_converted", "INTEGER"), ("gamma_source", "TEXT"), ("ratio", "REAL"),
    ("conv_mode", "TEXT"), ("basis", "REAL"),
    ("spot", "REAL"), ("flip", "REAL"), ("regime", "TEXT"),
    ("call_wall", "REAL"), ("put_wall", "REAL"),
    ("magnet", "REAL"), ("magnet_gross", "REAL"),
    ("zdte_call_wall", "REAL"), ("zdte_put_wall", "REAL"),
    ("zdte_flip", "REAL"), ("zdte_pct", "REAL"),
    ("one_d_max", "REAL"), ("one_d_min", "REAL"), ("settle", "REAL"),
    ("sigma30", "REAL"), ("em_pct", "REAL"), ("iv_label", "TEXT"),
    ("n_exp", "INTEGER"), ("front_exp", "TEXT"),
    ("top_strikes_json", "TEXT"),
]


def ensure_schema(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS gex_levels (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    existing = {row[1] for row in cur.execute("PRAGMA table_info(gex_levels)")}
    for col, typ in COLUMNS:
        if col not in existing:
            cur.execute(f"ALTER TABLE gex_levels ADD COLUMN {col} {typ}")


def write_sqlite(db_path, run_ts, displayed):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    ensure_schema(cur)
    run_date = run_ts[:10]
    cols = [c for c, _ in COLUMNS]
    placeholders = ",".join("?" * len(cols))
    for d in displayed:
        lv = d["levels"]
        row = {
            "run_ts": run_ts, "run_date": run_date, "symbol": d["name"],
            "is_converted": 1 if d["converted"] else 0, "gamma_source": d["source"],
            "ratio": d.get("ratio"), "conv_mode": d.get("conv_mode"), "basis": d.get("basis"),
            "spot": lv["spot"], "flip": lv["flip"],
            "regime": lv["regime"], "call_wall": lv["call_wall"], "put_wall": lv["put_wall"],
            "magnet": lv.get("magnet"), "magnet_gross": lv.get("magnet_gross"),
            "zdte_call_wall": lv.get("zdte_call_wall"), "zdte_put_wall": lv.get("zdte_put_wall"),
            "zdte_flip": lv.get("zdte_flip"), "zdte_pct": lv.get("zdte_pct"),
            "one_d_max": lv["one_d_max"], "one_d_min": lv["one_d_min"], "settle": lv["settle"],
            "sigma30": lv["sigma30"], "em_pct": lv["em_pct"], "iv_label": lv["iv_label"],
            "n_exp": lv["n_exp"], "front_exp": lv["front_exp"],
            "top_strikes_json": json.dumps(lv["top_strikes"]),
        }
        cur.execute(f"INSERT INTO gex_levels ({','.join(cols)}) VALUES ({placeholders})",
                    [row[c] for c in cols])
    con.commit()
    con.close()


def main(es_spy, nq_qqq, vxn_override=None, es_spx_basis=None, nq_ndx_basis=None):
    # Resolve index->future conversion per INDEX_BASIS_MODE.
    if INDEX_BASIS_MODE == "additive":
        es_b = ES_SPX_BASIS if es_spx_basis is None else es_spx_basis
        nq_b = NQ_NDX_BASIS if nq_ndx_basis is None else nq_ndx_basis
        es_conv = {"scale": 1.0, "offset": es_b, "desc": f"SPX + {es_b:g} pts",
                   "mode": "additive", "ratio": None, "basis": es_b}
        nq_conv = {"scale": 1.0, "offset": nq_b, "desc": f"NDX + {nq_b:g} pts",
                   "mode": "additive", "ratio": None, "basis": nq_b}
        print(f"Index basis (additive):  ES = SPX + {es_b:g}   NQ = NDX + {nq_b:g}")
    else:
        es_conv = {"scale": ES_SPX_RATIO, "offset": 0.0, "desc": f"SPX x {ES_SPX_RATIO:g}",
                   "mode": "ratio", "ratio": ES_SPX_RATIO, "basis": None}
        nq_conv = {"scale": NQ_NDX_RATIO, "offset": 0.0, "desc": f"NDX x {NQ_NDX_RATIO:g}",
                   "mode": "ratio", "ratio": NQ_NDX_RATIO, "basis": None}
        print(f"Index ratios:  ES/SPX = {ES_SPX_RATIO:g}   NQ/NDX = {NQ_NDX_RATIO:g}")
    print(f"ETF ratios:  ES/SPY = {es_spy:.3f}   NQ/QQQ = {nq_qqq:.3f}")
    win = "ALL" if N_EXPIRATIONS is None else f"{N_EXPIRATIONS} nearest"
    print(f"Expiration window: {win}")
    print("Fetching vol indices (VIX, VXN) ...", flush=True)
    vxn_val = _norm_vol(vxn_override) if vxn_override is not None else get_vol_index_decimal("VXN")
    if vxn_override is not None:
        print(f"  VXN = {vxn_val*100:.2f}% (manual override)")
    vol_map = {"VIX": get_vol_index_decimal("VIX"), "VXN": vxn_val}
    for k in ("VIX", "VXN"):
        v = vol_map[k]
        if v and not (k == "VXN" and vxn_override is not None):
            print(f"  {k} = {v*100:.2f}%")
        elif not v:
            print(f"  {k} = unavailable (Nasdaq band falls back to QQQ iv30)")
    print(f"Fetching {len(SYMBOLS)} underlyings from CBOE (delayed ~15m). "
          f"Index chains (SPX, NDX) are the slow steps.\n")

    res = {}
    for s in SYMBOLS:
        try:
            res[s] = compute_one(s, vol_map)
        except Exception as e:
            print(f"    !! {s} failed: {e}")
            res[s] = None

    displayed = []
    if res.get("NDX"):
        displayed.append({"name": "NQ", "label": "NQ (from NDX)", "converted": True,
                          "source": "NDX", "conv_desc": nq_conv["desc"],
                          "conv_mode": nq_conv["mode"], "ratio": nq_conv["ratio"],
                          "basis": nq_conv["basis"],
                          "levels": table_levels(res["NDX"], nq_conv["scale"], nq_conv["offset"])})
    if res.get("QQQ"):
        displayed.append({"name": "NQ", "label": "NQ (from QQQ)", "converted": True,
                          "source": "QQQ", "conv_desc": f"QQQ x {nq_qqq:.3f}",
                          "conv_mode": "ratio", "ratio": nq_qqq, "basis": None,
                          "levels": table_levels(res["QQQ"], nq_qqq)})
    if res.get("SPX"):
        displayed.append({"name": "ES", "label": "ES (from SPX)", "converted": True,
                          "source": "SPX", "conv_desc": es_conv["desc"],
                          "conv_mode": es_conv["mode"], "ratio": es_conv["ratio"],
                          "basis": es_conv["basis"],
                          "levels": table_levels(res["SPX"], es_conv["scale"], es_conv["offset"])})
    if res.get("SPY"):
        displayed.append({"name": "ES", "label": "ES (from SPY)", "converted": True,
                          "source": "SPY", "conv_desc": f"SPY x {es_spy:.3f}",
                          "conv_mode": "ratio", "ratio": es_spy, "basis": None,
                          "levels": table_levels(res["SPY"], es_spy)})
    for s in SYMBOLS:
        if s in HIDE_NATIVE or not res.get(s):
            continue
        displayed.append({"name": s, "label": s, "converted": False, "source": s,
                          "conv_desc": None, "conv_mode": None, "ratio": 1.0, "basis": None,
                          "levels": table_levels(res[s], 1.0)})

    print("\n" + "\n\n".join(
        render_table(d["label"], d["name"], d["levels"], d["converted"], d["source"], d["conv_desc"])
        for d in displayed))

    stamp = datetime.datetime.now(datetime.timezone.utc).astimezone()
    run_ts = stamp.isoformat()
    summary = {"run_timestamp": run_ts,
               "etf_ratios": {"ES_SPY": es_spy, "NQ_QQQ": nq_qqq},
               "index_conversion": {"mode": INDEX_BASIS_MODE,
                                    "ES_SPX": es_conv["basis"] if es_conv["basis"] is not None else es_conv["ratio"],
                                    "NQ_NDX": nq_conv["basis"] if nq_conv["basis"] is not None else nq_conv["ratio"]},
               "n_expirations": N_EXPIRATIONS,
               "tables": {d["label"]: d["levels"] for d in displayed}}
    os.makedirs(SUMMARY_DIR, exist_ok=True)
    jname = os.path.join(SUMMARY_DIR, f"gex_multi_{stamp.strftime('%Y%m%d_%H%M%S')}_summary.json")
    with open(jname, "w") as fh:
        json.dump(summary, fh, indent=2)
    write_sqlite(DB_PATH, run_ts, displayed)

    print(f"\nSaved snapshot: {jname}")
    print(f"Appended {len(displayed)} rows to {DB_PATH}")

    if GENERATE_CHARTS:
        made = []
        for d in displayed:
            if CHART_SYMBOLS is None or d["label"] in CHART_SYMBOLS:
                p = make_chart(d, "charts", stamp.strftime("%Y-%m-%d %H:%M"), CHART_TOP_N)
                if p:
                    made.append(p)
        if made:
            print("\nCharts written to ./charts/ :")
            for p in made:
                print(f"   {os.path.basename(p)}")

    print("\nQC: each 'Spot (conv)' should match live ES/NQ; 'settle' should be the "
          "right prior settle; 'exps used' confirms the window (front should be 0DTE).")


if __name__ == "__main__":
    # args: 1=ES/SPY ratio  2=NQ/QQQ ratio  3=VXN override  4=ES-SPX basis pts  5=NQ-NDX basis pts
    es = float(sys.argv[1]) if len(sys.argv) > 1 else ES_SPY_RATIO
    nq = float(sys.argv[2]) if len(sys.argv) > 2 else NQ_QQQ_RATIO
    vxn = float(sys.argv[3]) if len(sys.argv) > 3 else None
    es_b = float(sys.argv[4]) if len(sys.argv) > 4 else None
    nq_b = float(sys.argv[5]) if len(sys.argv) > 5 else None
    main(es, nq, vxn, es_b, nq_b)
