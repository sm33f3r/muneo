"""
Muneo DIY liquidation heatmap.

Estimates where leveraged Binance USDⓈ-M futures positions would be liquidated,
using only free public data (hourly klines + hourly open interest, last 30 days).
This is a MODEL, not a list of real positions — Binance does not publish them.

Method (same idea as commercial heatmaps such as Glassnode's / Coinglass's):
  1. Each hour where open interest (OI) rises, the new OI is treated as new long
     AND short positions (every contract has both sides) opened at that hour's
     typical price, spread across an assumed leverage mix.
  2. Each position's liquidation price is entry * (1 -/+ 1/leverage +/- maintenance margin).
  3. Each hour where OI falls, all open positions shrink proportionally.
  4. A position is removed once the price path (candle high/low) crosses its
     liquidation price — it has been liquidated.
  5. What remains is the estimated liquidation "fuel" at each price level: long
     liquidations below the current price, short liquidations above it.
"""

import os
import base64
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests

BINANCE_FAPI = "https://fapi.binance.com"
API_TIMEOUT = 10
LOOKBACK_DAYS = 30        # Binance only serves the last 30 days of OI history
INTERVAL = "1h"
HOUR_MS = 60 * 60 * 1000

# Assumed share of new open interest at each leverage — a modelling assumption, not Binance data
LEVERAGE_MIX = {5: 0.15, 10: 0.30, 25: 0.25, 50: 0.20, 100: 0.10}
MAINTENANCE_MARGIN = 0.005  # approximate maintenance margin rate for small/medium position sizes

# Price window reported in the JSON levels, as % either side of the current price
RANGE_PCT = {"BTCUSDT": 15.0, "ETHUSDT": 15.0}
DEFAULT_RANGE_PCT = 25.0
LEVEL_BINS = 60           # JSON level bins across the whole window
BAND_PCT = 5.0            # near-price band used for the pressure lean (matches the old liquidation_map)
TOP_CLUSTERS = 5
IMAGE_PRICE_BINS = 200

IMAGE_DIR = Path("heatmaps")

# Chart colors (sequential single-hue blue ramp, light surface, orange price line)
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"
PRICE_LINE = "#eb6834"
BLUE_RAMP = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]

DISCLAIMER = (
    "Modelled estimate from Binance open-interest changes and an assumed leverage mix "
    "— not actual positions. Use as a rough map of where forced liquidations may cluster."
)


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def fetch_klines(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Hourly futures candles between start_ms and end_ms."""
    out, cursor = [], start_ms
    while cursor < end_ms:
        resp = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={"symbol": symbol, "interval": INTERVAL, "startTime": cursor, "endTime": end_ms, "limit": 1500},
            timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for r in rows:
            out.append({"t": int(r[0]), "high": float(r[2]), "low": float(r[3]), "close": float(r[4])})
        cursor = int(rows[-1][0]) + HOUR_MS
        if len(rows) < 1500:
            break
    return out


def fetch_open_interest(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Hourly open interest value (USD) between start_ms and end_ms, 500 rows per request."""
    out, cursor = [], start_ms
    while cursor < end_ms:
        chunk_end = min(end_ms, cursor + 499 * HOUR_MS)
        resp = requests.get(
            f"{BINANCE_FAPI}/futures/data/openInterestHist",
            params={"symbol": symbol, "period": INTERVAL, "startTime": cursor, "endTime": chunk_end, "limit": 500},
            timeout=API_TIMEOUT,
        )
        resp.raise_for_status()
        rows = resp.json()
        for r in rows:
            out.append({"t": int(r["timestamp"]), "oi_usd": float(r["sumOpenInterestValue"])})
        cursor = chunk_end + HOUR_MS
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

def run_model(candles: list[dict], oi: list[dict], image_grid: np.ndarray | None = None) -> dict:
    """
    Replay the window hour by hour. Returns the surviving positions per side and,
    if image_grid (price bin edges) is given, a [bins x hours] matrix of total
    estimated liquidation USD per price bin after each hour.
    """
    oi_by_t = {row["t"]: row["oi_usd"] for row in oi}
    hours = [c for c in candles if c["t"] in oi_by_t]
    levs = np.array(list(LEVERAGE_MIX.keys()), dtype=float)
    weights = np.array(list(LEVERAGE_MIX.values()), dtype=float)

    long_liq, long_usd = np.empty(0), np.empty(0)
    short_liq, short_usd = np.empty(0), np.empty(0)
    snapshots, times = [], []
    prev_oi = None

    for c in hours:
        # 1. Positions whose liquidation price this candle reached are gone
        keep_long = long_liq < c["low"]
        keep_short = short_liq > c["high"]
        long_liq, long_usd = long_liq[keep_long], long_usd[keep_long]
        short_liq, short_usd = short_liq[keep_short], short_usd[keep_short]

        cur_oi = oi_by_t[c["t"]]
        if prev_oi is not None and prev_oi > 0:
            delta = cur_oi - prev_oi
            if delta > 0:
                # 2. New OI = new longs and shorts at this hour's typical price
                entry = (c["high"] + c["low"] + c["close"]) / 3
                long_liq = np.concatenate([long_liq, entry * (1 - 1 / levs + MAINTENANCE_MARGIN)])
                long_usd = np.concatenate([long_usd, delta * weights])
                short_liq = np.concatenate([short_liq, entry * (1 + 1 / levs - MAINTENANCE_MARGIN)])
                short_usd = np.concatenate([short_usd, delta * weights])
            elif delta < 0:
                # 3. Closed OI shrinks every open position proportionally
                factor = max(0.0, 1 + delta / prev_oi)
                long_usd = long_usd * factor
                short_usd = short_usd * factor
        prev_oi = cur_oi

        if image_grid is not None:
            snap, _ = np.histogram(
                np.concatenate([long_liq, short_liq]),
                bins=image_grid,
                weights=np.concatenate([long_usd, short_usd]),
            )
            snapshots.append(snap)
            times.append(c["t"])

    return {
        "long_liq": long_liq, "long_usd": long_usd,
        "short_liq": short_liq, "short_usd": short_usd,
        "matrix": np.array(snapshots).T if snapshots else None,
        "times": times,
        "hours_modelled": len(hours),
    }


def summarise_levels(state: dict, price: float, range_pct: float) -> dict:
    """Bin surviving positions around the current price and pick out the main clusters."""
    lo, hi = price * (1 - range_pct / 100), price * (1 + range_pct / 100)
    edges = np.linspace(lo, hi, LEVEL_BINS + 1)
    long_bins, _ = np.histogram(state["long_liq"], bins=edges, weights=state["long_usd"])
    short_bins, _ = np.histogram(state["short_liq"], bins=edges, weights=state["short_usd"])

    def cluster(i: int, usd: float) -> dict:
        mid = (edges[i] + edges[i + 1]) / 2
        return {
            "price_low": round(float(edges[i]), 6),
            "price_high": round(float(edges[i + 1]), 6),
            "est_liq_usd": round(float(usd), 2),
            "distance_pct": round(float((mid - price) / price * 100), 2),
        }

    mids = (edges[:-1] + edges[1:]) / 2
    below = [(i, long_bins[i]) for i in range(LEVEL_BINS) if mids[i] < price and long_bins[i] > 0]
    above = [(i, short_bins[i]) for i in range(LEVEL_BINS) if mids[i] > price and short_bins[i] > 0]

    def band_sum(liq: np.ndarray, usd: np.ndarray, a: float, b: float) -> float:
        mask = (liq >= a) & (liq <= b)
        return float(usd[mask].sum())

    band_below = band_sum(state["long_liq"], state["long_usd"], price * (1 - BAND_PCT / 100), price)
    band_above = band_sum(state["short_liq"], state["short_usd"], price, price * (1 + BAND_PCT / 100))

    return {
        "range_pct": range_pct,
        "long_liq_usd_below": round(float(sum(u for _, u in below)), 2),
        "short_liq_usd_above": round(float(sum(u for _, u in above)), 2),
        "band_pct": BAND_PCT,
        "band_long_liq_usd_below": round(band_below, 2),
        "band_short_liq_usd_above": round(band_above, 2),
        "clusters_below": [cluster(i, u) for i, u in sorted(below, key=lambda x: -x[1])[:TOP_CLUSTERS]],
        "clusters_above": [cluster(i, u) for i, u in sorted(above, key=lambda x: -x[1])[:TOP_CLUSTERS]],
        "levels": [
            [round(float(mids[i]), 6), round(float(long_bins[i]), 2), round(float(short_bins[i]), 2)]
            for i in range(LEVEL_BINS)
        ],
    }


def liq_pressure_lean(band_below: float, band_above: float) -> str | None:
    """Same rule as the global report's lean_liq_pressure: 1.5x more fuel on one side sets the lean."""
    if band_below <= 0 and band_above <= 0:
        return None
    if band_below >= band_above * 1.5:
        return "bearish"
    if band_above >= band_below * 1.5:
        return "bullish"
    return "neutral"


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

def format_usd(value: float) -> str:
    """Compact dollar label: $1.2B, $35M, $480K, $950."""
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= divisor:
            return f"${value / divisor:.3g}{suffix}"
    return f"${value:.3g}"


def render_heatmap_png(symbol: str, state: dict, grid: np.ndarray, candles: list[dict], out_path: Path) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, PowerNorm
    import matplotlib.dates as mdates

    # Smooth across neighbouring price bins (display only) so clusters read as zones, not stripes
    kernel = np.array([0.1, 0.2, 0.4, 0.2, 0.1])
    matrix = np.apply_along_axis(lambda col: np.convolve(col, kernel, mode="same"), 0, state["matrix"])
    times = [datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in state["times"]]
    positive = matrix[matrix > 0]
    # Cap at the 99.5th percentile so a few extreme bins don't wash out every other cluster
    vmax = float(np.percentile(positive, 99.5)) if positive.size else 1.0
    vmin = vmax * 0.01

    cmap = LinearSegmentedColormap.from_list("muneo_blue", BLUE_RAMP)
    cmap.set_under(SURFACE)

    fig, ax = plt.subplots(figsize=(12, 6), dpi=110)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    x_edges = mdates.date2num(times + [times[-1]]) if times else [0, 1]
    mesh = ax.pcolormesh(
        x_edges, grid, np.where(matrix >= vmin, matrix, 0),
        cmap=cmap, norm=PowerNorm(gamma=0.5, vmin=vmin, vmax=vmax), shading="flat", rasterized=True,
    )
    modelled = set(state["times"])
    price_t = [datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc) for c in candles if c["t"] in modelled]
    price_c = [c["close"] for c in candles if c["t"] in modelled]
    ax.plot(price_t, price_c, color=PRICE_LINE, linewidth=2)
    if price_t:
        ax.annotate(
            f"Price {price_c[-1]:,.6g}", xy=(price_t[-1], price_c[-1]), xytext=(-6, 10),
            textcoords="offset points", ha="right", color=TEXT_PRIMARY, fontsize=9,
            bbox={"boxstyle": "round,pad=0.25", "fc": SURFACE, "ec": PRICE_LINE, "lw": 1},
        )

    ax.set_ylim(grid[0], grid[-1])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    for spine in ax.spines.values():
        spine.set_color(GRID)
    ax.tick_params(colors=TEXT_SECONDARY, labelsize=9)
    ax.grid(False)
    ax.set_ylabel("Price (USDT)", color=TEXT_SECONDARY, fontsize=10)
    ax.set_title(
        f"{symbol} estimated liquidation heatmap — Binance futures, last {LOOKBACK_DAYS} days",
        color=TEXT_PRIMARY, fontsize=13, loc="left", pad=12,
    )
    cbar = fig.colorbar(mesh, ax=ax, pad=0.01, extend="max")
    cbar.set_label("Estimated liquidation value per price level (USD)", color=TEXT_SECONDARY, fontsize=9)
    cbar.ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: format_usd(v)))
    cbar.ax.tick_params(colors=TEXT_SECONDARY, labelsize=8)
    cbar.outline.set_edgecolor(GRID)

    fig.text(0.01, 0.01, "Model estimate, not actual positions. Muneo — open source.", color=TEXT_SECONDARY, fontsize=8)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, facecolor=SURFACE)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def build_liquidation_heatmap(symbol: str, render_image: bool = True, now_ms: int | None = None) -> dict:
    """Fetch data, run the model, and return the JSON-ready result (image written under heatmaps/)."""
    now_ms = now_ms or int(time.time() * 1000)
    end_ms = now_ms - (now_ms % HOUR_MS)
    start_ms = end_ms - (LOOKBACK_DAYS * 24 - 1) * HOUR_MS

    candles = fetch_klines(symbol, start_ms, end_ms)
    oi = fetch_open_interest(symbol, start_ms, end_ms)
    if not candles or not oi:
        raise ValueError(f"no Binance data for {symbol} (candles={len(candles)}, oi={len(oi)})")

    lows = min(c["low"] for c in candles)
    highs = max(c["high"] for c in candles)
    grid = np.linspace(lows * 0.85, highs * 1.15, IMAGE_PRICE_BINS + 1)
    state = run_model(candles, oi, image_grid=grid if render_image else None)

    price = candles[-1]["close"]
    levels = summarise_levels(state, price, RANGE_PCT.get(symbol, DEFAULT_RANGE_PCT))
    result = {
        "model": "estimated",
        "method": "open_interest_leverage_model",
        "source": "binance_futures",
        "symbol": symbol,
        "interval": INTERVAL,
        "lookback_days": LOOKBACK_DAYS,
        "hours_modelled": state["hours_modelled"],
        "reference_price": price,
        "as_of": datetime.fromtimestamp(candles[-1]["t"] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
        "leverage_mix": {f"{k}x": v for k, v in LEVERAGE_MIX.items()},
        "maintenance_margin": MAINTENANCE_MARGIN,
        **levels,
        "liq_pressure_lean": liq_pressure_lean(levels["band_long_liq_usd_below"], levels["band_short_liq_usd_above"]),
        "levels_format": ["price_mid", "est_long_liq_usd", "est_short_liq_usd"],
        "image_path": None,
        "disclaimer": DISCLAIMER,
    }
    if render_image and state["matrix"] is not None:
        out = IMAGE_DIR / f"liquidation_heatmap_{symbol.lower()}.png"
        result["image_path"] = render_heatmap_png(symbol, state, grid, candles, out).as_posix()
    return result


def push_heatmap_image_to_github(result: dict) -> None:
    """Push the heatmap PNG to the same path every run (overwritten, so no files pile up). Non-blocking."""
    path = result.get("image_path") if result else None
    token, repo = os.getenv("GITHUB_TOKEN"), os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")
    if not path:
        return
    if not token or not repo:
        print("  ⚠ Heatmap image push skipped — GITHUB_TOKEN or GITHUB_REPO not set")
        return
    try:
        api_url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        payload = {
            "message": f"Update heatmap: {Path(path).name}",
            "content": base64.b64encode(Path(path).read_bytes()).decode(),
            "branch": branch,
        }
        check = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=10)
        if check.status_code == 200:
            payload["sha"] = check.json().get("sha")
        resp = requests.put(api_url, json=payload, headers=headers, timeout=20)
        if resp.status_code in (200, 201):
            print(f"  ✓ GitHub push — {path}")
        else:
            print(f"  ✗ Heatmap image push failed — HTTP {resp.status_code}: {resp.text[:100]}")
    except Exception as e:
        print(f"  ✗ Heatmap image push failed — {e}")


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Build a liquidation heatmap for one Binance futures symbol")
    parser.add_argument("symbol", nargs="?", default="BTCUSDT")
    args = parser.parse_args()
    print(json.dumps({k: v for k, v in build_liquidation_heatmap(args.symbol).items() if k != "levels"}, indent=2))
