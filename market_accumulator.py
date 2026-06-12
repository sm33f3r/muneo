# Muneo Market Accumulator - A script to accumulate daily market reports into weekly, monthly, and quarterly summaries.

import os
import json
import argparse
import shutil
from datetime import datetime, timezone
from dateutil.parser import parse as parse_date
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

# Accumulation thresholds
MIN_DAYS_FOR_WEEKLY = 4       # Minimum daily reports needed to generate a weekly report
MIN_WEEKS_FOR_MONTHLY = 3     # Minimum weekly reports needed to generate a monthly report
MIN_MONTHS_FOR_QUARTERLY = 1  # Minimum monthly reports needed to generate a partial quarterly report

# Retention thresholds (number of files to keep in reports/ before archiving/deleting)
RETAIN_DAILY = 14             # Keep last 14 daily reports locally
RETAIN_WEEKLY = 7             # Keep last 7 weekly reports locally
RETAIN_MONTHLY = 3            # Keep last 3 monthly reports locally
# Quarterly reports are never archived or deleted

SCRIPT_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"

def parse_args():
    parser = argparse.ArgumentParser(
        description="Muneo Market Accumulator — rolls up daily reports into weekly/monthly/quarterly summaries"
    )
    parser.add_argument(
        "--config",
        default="configs/sui.json",
        help="Path to token config file (default: configs/sui.json)"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Clear all accumulated output and regenerate from scratch"
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Write Markdown summaries alongside JSON output"
    )
    return parser.parse_args()

def load_config(config_path: str) -> dict:
    """Load and validate token config file."""
    with open(config_path, "r") as f:
        config = json.load(f)
    
    required_fields = ["token_name", "output_prefix"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Config missing required field: {field}")
    
    return config

def setup_directories(config: dict) -> dict:
    """Create output directories if they don't exist. Returns path dict."""
    token_dir = config["token_name"].lower()  # e.g. "sui" or "sol"
    
    paths = {
        "reports": Path("./reports"),
        "archive": Path("./reports/archive"),
        "accumulated_base": Path(f"./context/{token_dir}/accumulated"),
        "weekly": Path(f"./context/{token_dir}/accumulated/weekly"),
        "monthly": Path(f"./context/{token_dir}/accumulated/monthly"),
        "quarterly": Path(f"./context/{token_dir}/accumulated/quarterly"),
    }
    
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    
    return paths

def discover_daily_reports(reports_dir: Path, output_prefix: str) -> list[dict]:
    results = []
    for f in reports_dir.glob("*.json"):
        if not f.name.startswith(output_prefix):
            continue
        if "archive" in f.parts:
            continue
        ts_str = f.name.replace(output_prefix + "_", "").replace(".json", "")
        try:
            dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
        except ValueError:
            print(f"  ⚠ Skipping unparseable filename: {f.name}")
            continue
        iso = dt.isocalendar()
        results.append({
            "path": f,
            "filename": f.name,
            "timestamp": dt,
            "date_str": dt.strftime("%Y-%m-%d"),
            "iso_year": iso.year,
            "iso_week": iso.week,
            "iso_weekday": iso.weekday
        })
    return sorted(results, key=lambda x: x["timestamp"])

def group_by_iso_week(daily_reports: list[dict]) -> dict:
    from collections import defaultdict
    groups = defaultdict(list)
    for report in daily_reports:
        key = (report["iso_year"], report["iso_week"])
        groups[key].append(report)
    return dict(sorted(groups.items()))

def load_daily_report(path: Path) -> dict | None:
    """Load a daily report JSON. Returns None on failure."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ Failed to load {path.name}: {e}")
        return None

def deduplicate_by_date(daily_reports: list[dict]) -> list[dict]:
    """
    If multiple reports exist for the same date, keep only the most recent.
    Returns deduplicated list sorted by timestamp ascending.
    """
    by_date = {}
    for r in daily_reports:
        date = r["date_str"]
        if date not in by_date or r["timestamp"] > by_date[date]["timestamp"]:
            by_date[date] = r
    return sorted(by_date.values(), key=lambda x: x["timestamp"])

def aggregate_weekly_price(daily_data: list[dict]) -> dict:
    def vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        return result

    prices = vals("price_data.price_usd")
    volumes = vals("price_data.volume_24h_usd")
    mcaps = vals("price_data.market_cap_usd")
    cex_prices = vals("price_data.cex_price_usd")
    up_vols = vals("price_data.volume_on_up_days_usd")
    down_vols = vals("price_data.volume_on_down_days_usd")
    volume_leans = vals("price_data.volume_lean")

    open_usd = prices[0] if prices else None
    close_usd = prices[-1] if prices else None

    from statistics import mode, mean
    return {
        "open_usd": open_usd,
        "close_usd": close_usd,
        "high_usd": max(prices) if prices else None,
        "low_usd": min(prices) if prices else None,
        "price_change_week_pct": round((close_usd - open_usd) / open_usd * 100, 4) if open_usd and close_usd else None,
        "avg_volume_24h_usd": round(mean(volumes), 2) if volumes else None,
        "total_volume_week_usd": round(sum(volumes), 2) if volumes else None,
        "avg_market_cap_usd": round(mean(mcaps), 2) if mcaps else None,
        "close_market_cap_usd": mcaps[-1] if mcaps else None,
        "ath_usd": vals("price_data.ath_usd")[-1] if vals("price_data.ath_usd") else None,
        "ath_drawdown_pct": vals("price_data.ath_drawdown_pct")[-1] if vals("price_data.ath_drawdown_pct") else None,
        "avg_cex_price_usd": round(mean(cex_prices), 6) if cex_prices else None,
        "volume_on_up_days_usd": round(sum(up_vols), 2) if up_vols else None,
        "volume_on_down_days_usd": round(sum(down_vols), 2) if down_vols else None,
        "volume_lean": mode(volume_leans) if volume_leans else None,
        "days_included": len(daily_data)
    }

def aggregate_weekly_technicals(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    rsi = last_val("technical_indicators.rsi_14")
    sma_50 = last_val("technical_indicators.sma_50")
    ema_200 = last_val("technical_indicators.ema_200")
    close_price = last_val("price_data.price_usd")
    price_vs_sma50_pct = round((close_price - sma_50) / sma_50 * 100, 4) if close_price and sma_50 else None
    price_vs_ema200_pct = round((close_price - ema_200) / ema_200 * 100, 4) if close_price and ema_200 else None
    bb = last_val("technical_indicators.bollinger_bands")

    return {
        "rsi_14_at_close": rsi,
        "rsi_lean": last_val("technical_indicators.rsi_lean"),
        "macd_at_close": {
            "macd_line": last_val("technical_indicators.macd.macd_line"),
            "signal_line": last_val("technical_indicators.macd.signal_line"),
            "histogram": last_val("technical_indicators.macd.histogram"),
        },
        "macd_lean": last_val("technical_indicators.macd_lean"),
        "bollinger_bands_at_close": bb,
        "bb_lean": last_val("technical_indicators.bb_lean"),
        "sma_50_at_close": sma_50,
        "price_vs_sma50_pct": price_vs_sma50_pct,
        "ema_200_at_close": ema_200,
        "price_vs_ema200_pct": price_vs_ema200_pct,
        "ema200_lean": last_val("technical_indicators.ema200_lean"),
    }

def aggregate_weekly_derivatives(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def first_val(field_path):
        keys = field_path.split(".")
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 8)

    oi_open = first_val("derivatives.oi_usd")
    oi_close = last_val("derivatives.oi_usd")
    oi_change_pct = round((oi_close - oi_open) / oi_open * 100, 4) if oi_open and oi_close else None

    return {
        "oi_at_close_usd": oi_close,
        "oi_open_usd": oi_open,
        "oi_change_pct": oi_change_pct,
        "oi_lean": last_val("derivatives.oi_lean"),
        "funding_rate_daily_avg": avg_vals("derivatives.funding_rate_daily_avg"),
        "funding_rate_7d_rolling_avg": last_val("derivatives.funding_rate_7d_rolling_avg"),
        "funding_lean": last_val("derivatives.funding_lean"),
        "btc_long_short_ratio": last_val("derivatives.btc_long_short_ratio"),
        "btc_long_account_pct": last_val("derivatives.btc_long_account_pct"),
        "btc_short_account_pct": last_val("derivatives.btc_short_account_pct"),
        "btc_ls_ratio_lean": last_val("derivatives.btc_ls_ratio_lean"),
    }

def aggregate_weekly_on_chain(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def first_val(field_path):
        keys = field_path.split(".")
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 2)

    def sum_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        return round(sum(result), 2)

    tvl_start = first_val("on_chain.tvl_usd")
    tvl_end = last_val("on_chain.tvl_usd")
    tvl_change_pct = round((tvl_end - tvl_start) / tvl_start * 100, 4) if tvl_start and tvl_end else None

    addr_start = first_val("on_chain.active_addresses")
    addr_end = last_val("on_chain.active_addresses")
    if addr_start and addr_end:
        if addr_end > addr_start * 1.02:
            addr_lean = "bullish"
        elif addr_end < addr_start * 0.98:
            addr_lean = "bearish"
        else:
            addr_lean = "neutral"
    else:
        addr_lean = None

    net_flow = sum_vals("on_chain.exchange_net_flow_usd")
    if net_flow is not None:
        exchange_flow_lean = "bearish" if net_flow > 0 else "bullish" if net_flow < 0 else "neutral"
    else:
        exchange_flow_lean = None

    tvl_lean = None
    if tvl_change_pct is not None:
        if tvl_change_pct > 2:
            tvl_lean = "bullish"
        elif tvl_change_pct < -2:
            tvl_lean = "bearish"
        else:
            tvl_lean = "neutral"

    return {
        "tvl_start_usd": tvl_start,
        "tvl_end_usd": tvl_end,
        "tvl_change_pct": tvl_change_pct,
        "tvl_direction": "rising" if tvl_change_pct and tvl_change_pct > 0 else "falling" if tvl_change_pct and tvl_change_pct < 0 else "flat" if tvl_change_pct == 0 else None,
        "tvl_lean": tvl_lean,
        "active_addresses_avg": avg_vals("on_chain.active_addresses"),
        "active_addresses_start": addr_start,
        "active_addresses_end": addr_end,
        "active_addresses_lean": addr_lean,
        "exchange_inflow_avg_usd": avg_vals("on_chain.exchange_inflow_usd"),
        "exchange_outflow_avg_usd": avg_vals("on_chain.exchange_outflow_usd"),
        "exchange_net_flow_usd": net_flow,
        "exchange_flow_lean": exchange_flow_lean,
    }

def aggregate_weekly_macro(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def first_val(field_path):
        keys = field_path.split(".")
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 4)

    def pct_change(start, end):
        if start and end:
            return round((end - start) / start * 100, 4)
        return None

    def direction(start, end):
        if start is None or end is None:
            return None
        if end > start * 1.001:
            return "rising"
        elif end < start * 0.999:
            return "falling"
        return "flat"

    # BTC
    btc_start = first_val("macro.btc_price_usd")
    btc_end = last_val("macro.btc_price_usd")
    token_start = first_val("price_data.price_usd")
    token_end = last_val("price_data.price_usd")
    btc_return = pct_change(btc_start, btc_end)
    token_return = pct_change(token_start, token_end)
    alpha = round(token_return - btc_return, 4) if token_return is not None and btc_return is not None else None

    btc_direction = direction(btc_start, btc_end)
    btc_dir_lean = "bullish" if btc_direction == "rising" else "bearish" if btc_direction == "falling" else "neutral" if btc_direction == "flat" else None

    # BTC dominance
    dom_start = first_val("macro.btc_dominance")
    dom_end = last_val("macro.btc_dominance")
    dom_dir = direction(dom_start, dom_end)
    dom_lean = "bearish" if dom_dir == "rising" else "bullish" if dom_dir == "falling" else "neutral" if dom_dir == "flat" else None

    # Total crypto mcap
    mcap_start = first_val("macro.total_crypto_mcap_usd")
    mcap_end = last_val("macro.total_crypto_mcap_usd")

    # ETH/BTC ratio
    eth_btc_start = first_val("macro.eth_btc_ratio")
    eth_btc_end = last_val("macro.eth_btc_ratio")
    eth_btc_lean = "bullish" if eth_btc_end and eth_btc_start and eth_btc_end > eth_btc_start else "bearish" if eth_btc_end and eth_btc_start and eth_btc_end < eth_btc_start else "neutral" if eth_btc_start and eth_btc_end else None

    # DXY
    dxy_start = first_val("macro.dxy")
    dxy_end = last_val("macro.dxy")
    dxy_change = pct_change(dxy_start, dxy_end)
    dxy_dir = direction(dxy_start, dxy_end)
    dxy_lean = "bearish" if dxy_dir == "rising" else "bullish" if dxy_dir == "falling" else "neutral" if dxy_dir == "flat" else None

    # SPY
    spy_start = first_val("macro.spy_close")
    spy_end = last_val("macro.spy_close")
    spy_change = pct_change(spy_start, spy_end)
    spy_lean = "bullish" if spy_change and spy_change > 0 else "bearish" if spy_change and spy_change < 0 else "neutral" if spy_change == 0 else None

    # VIX
    vix_start = first_val("macro.vix_close")
    vix_end = last_val("macro.vix_close")
    vix_avg = avg_vals("macro.vix_close")
    if vix_end is not None and vix_avg is not None:
        if vix_end < 20 and (vix_start is None or vix_end <= vix_start):
            vix_lean = "bullish"
        elif vix_end > 25 or (vix_start is not None and vix_end > vix_start):
            vix_lean = "bearish"
        else:
            vix_lean = "neutral"
    else:
        vix_lean = None

    return {
        "btc_period_return_pct": btc_return,
        "btc_direction": btc_direction,
        "token_vs_btc_alpha_pct": alpha,
        "btc_dominance_start": dom_start,
        "btc_dominance_end": dom_end,
        "btc_dominance_direction": dom_dir,
        "btc_dominance_lean": dom_lean,
        "total_crypto_mcap_start_usd": mcap_start,
        "total_crypto_mcap_end_usd": mcap_end,
        "eth_btc_ratio_start": eth_btc_start,
        "eth_btc_ratio_end": eth_btc_end,
        "eth_btc_lean": eth_btc_lean,
        "dxy_start": dxy_start,
        "dxy_end": dxy_end,
        "dxy_change_pct": dxy_change,
        "dxy_lean": dxy_lean,
        "spy_start": spy_start,
        "spy_end": spy_end,
        "spy_change_pct": spy_change,
        "spy_lean": spy_lean,
        "vix_start": vix_start,
        "vix_end": vix_end,
        "vix_avg": vix_avg,
        "vix_lean": vix_lean,
    }

def aggregate_weekly_sentiment(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 4)

    def min_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        return min(result) if result else None

    def max_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        return max(result) if result else None

    fg_at_close = last_val("sentiment.fear_greed_value")
    fg_label = last_val("sentiment.fear_greed_label")

    if fg_at_close is not None:
        if fg_at_close >= 60:
            fg_lean = "bearish"
        elif fg_at_close <= 40:
            fg_lean = "bullish"
        else:
            fg_lean = "neutral"
    else:
        fg_lean = None

    lc_sentiment_close = last_val("sentiment.lc_sentiment")
    if lc_sentiment_close is not None:
        if lc_sentiment_close >= 60:
            sentiment_lean = "bullish"
        elif lc_sentiment_close <= 40:
            sentiment_lean = "bearish"
        else:
            sentiment_lean = "neutral"
    else:
        sentiment_lean = None

    galaxy_score_close = last_val("sentiment.lc_galaxy_score")
    if galaxy_score_close is not None:
        if galaxy_score_close >= 60:
            galaxy_score_lean = "bullish"
        elif galaxy_score_close <= 40:
            galaxy_score_lean = "bearish"
        else:
            galaxy_score_lean = "neutral"
    else:
        galaxy_score_lean = None

    return {
        "fear_greed_avg": avg_vals("sentiment.fear_greed_value"),
        "fear_greed_min": min_vals("sentiment.fear_greed_value"),
        "fear_greed_max": max_vals("sentiment.fear_greed_value"),
        "fear_greed_at_close": fg_at_close,
        "fear_greed_label_at_close": fg_label,
        "fear_greed_lean": fg_lean,
        "lc_interactions_avg": avg_vals("sentiment.lc_interactions"),
        "lc_sentiment_avg": avg_vals("sentiment.lc_sentiment"),
        "lc_sentiment_at_close": lc_sentiment_close,
        "lc_posts_active_avg": avg_vals("sentiment.lc_posts_active"),
        "lc_contributors_active_avg": avg_vals("sentiment.lc_contributors_active"),
        "lc_galaxy_score_avg": avg_vals("sentiment.lc_galaxy_score"),
        "lc_galaxy_score_at_close": galaxy_score_close,
        "galaxy_score_lean": galaxy_score_lean,
        "lc_alt_rank_at_close": last_val("sentiment.lc_alt_rank"),
        "lc_social_dominance_avg": avg_vals("sentiment.lc_social_dominance"),
        "sentiment_lean": sentiment_lean,
    }

def aggregate_weekly_news(daily_data: list[dict]) -> dict:
    """
    Aggregate news headlines from daily reports into a weekly summary.
    Collects all headlines across the week, deduplicates by title,
    and returns the most recent up to a capped limit.
    """
    MAX_HEADLINES = 20

    seen_titles = set()
    all_headlines = []

    for d in daily_data:
        news = (d or {}).get("news", {})
        if not news:
            continue
        for source_key in ("cryptopanic", "finnhub"):
            items = news.get(source_key) or []
            for item in items:
                title = item.get("title") or item.get("headline")
                if not title:
                    continue
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                all_headlines.append({
                    "title": title,
                    "source": source_key,
                    "published_at": item.get("published_at") or item.get("datetime"),
                    "url": item.get("url"),
                    "sentiment": item.get("votes", {}).get("sentiment") if source_key == "cryptopanic" else None,
                })

    # Sort by published_at descending, keep most recent up to cap
    def sort_key(h):
        ts = h.get("published_at")
        return ts if ts else ""

    all_headlines.sort(key=sort_key, reverse=True)
    all_headlines = all_headlines[:MAX_HEADLINES]

    return {
        "headline_count": len(all_headlines),
        "sources_included": list({h["source"] for h in all_headlines}),
        "headlines": all_headlines,
    }

def aggregate_weekly_signal_summary(daily_data: list[dict]) -> dict:
    """
    Aggregate signal summary from daily reports into a weekly summary.
    Period-end lean is taken from the last daily report.
    Daily counts are tallied across all days in the period.
    signal_streaks tracks consecutive lean runs across the week.
    """
    SIGNAL_KEYS = [
        "rsi_lean", "macd_lean", "ema200_lean", "bb_lean", "volume_lean",
        "oi_lean", "funding_lean", "tvl_lean", "exchange_flow_lean",
        "active_addresses_lean", "btc_dominance_lean", "eth_btc_lean",
        "dxy_lean", "spy_lean", "vix_lean", "fear_greed_lean",
        "sentiment_lean", "galaxy_score_lean", "btc_ls_ratio_lean",
    ]

    # Period-end signals from last daily report
    def last_signal(key):
        for d in reversed(daily_data):
            sig = (d or {}).get("signal_summary", {}).get("signals", {})
            val = sig.get(key)
            if val is not None:
                return val
        return None

    period_end_signals = {k: last_signal(k) for k in SIGNAL_KEYS}

    # Count non-null signals
    available = [k for k, v in period_end_signals.items() if v is not None]
    null_signals = [k for k, v in period_end_signals.items() if v is None]
    bullish = sum(1 for v in period_end_signals.values() if v == "bullish")
    bearish = sum(1 for v in period_end_signals.values() if v == "bearish")
    neutral = sum(1 for v in period_end_signals.values() if v == "neutral")

    if len(available) == 0:
        overall_lean = None
    elif bullish > bearish and bullish > neutral:
        overall_lean = "bullish"
    elif bearish > bullish and bearish > neutral:
        overall_lean = "bearish"
    else:
        overall_lean = "neutral"

    # Signal streaks: track overall_lean across each daily report in the week
    daily_leans = []
    for d in daily_data:
        lean = (d or {}).get("signal_summary", {}).get("overall_lean")
        daily_leans.append(lean)

    current_streak_lean = None
    current_streak_length = 0
    longest_bullish = 0
    longest_bearish = 0

    if daily_leans:
        # Current streak: count from end backwards
        current_streak_lean = daily_leans[-1]
        for lean in reversed(daily_leans):
            if lean == current_streak_lean:
                current_streak_length += 1
            else:
                break

        # Longest streaks across period
        streak = 1
        for i in range(1, len(daily_leans)):
            if daily_leans[i] == daily_leans[i - 1] and daily_leans[i] is not None:
                streak += 1
            else:
                streak = 1
            if daily_leans[i] == "bullish":
                longest_bullish = max(longest_bullish, streak)
            elif daily_leans[i] == "bearish":
                longest_bearish = max(longest_bearish, streak)

        # Check first day too
        if daily_leans[0] == "bullish":
            longest_bullish = max(longest_bullish, 1)
        elif daily_leans[0] == "bearish":
            longest_bearish = max(longest_bearish, 1)

    return {
        "signals_evaluated": len(SIGNAL_KEYS),
        "signals_available": len(available),
        "signals_null": len(null_signals),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "overall_lean": overall_lean,
        "signals": period_end_signals,
        "signal_streaks": {
            "current_streak_lean": current_streak_lean,
            "current_streak_length": current_streak_length,
            "longest_bullish_streak_in_period": longest_bullish,
            "longest_bearish_streak_in_period": longest_bearish,
        },
    }

def aggregate_accumulation_metadata(daily_reports: list[dict], daily_data: list[dict]) -> dict:
    """
    Compute accumulation_metadata block for a weekly report.
    daily_reports: list of file metadata dicts (with date_str keys).
    daily_data: list of loaded daily report JSON dicts.
    """
    constituent_periods = 7
    constituent_available = len(daily_data)
    constituent_missing = constituent_periods - constituent_available

    if constituent_missing == 0:
        data_quality = "complete"
    elif constituent_available >= MIN_DAYS_FOR_WEEKLY:
        data_quality = "partial"
    else:
        data_quality = "insufficient"

    return {
        "source": "accumulated",
        "constituent_periods": constituent_periods,
        "constituent_periods_available": constituent_available,
        "constituent_periods_missing": constituent_missing,
        "data_quality": data_quality,
    }


def aggregate_cex_dex_spread(daily_data: list[dict]) -> dict:
    """
    Compute CEX/DEX spread stats across the week.
    Spread = abs((cex_price_usd - price_usd) / price_usd * 100)
    """
    spreads = []
    for d in daily_data:
        price = (d or {}).get("price_data", {}).get("price_usd")
        cex_price = (d or {}).get("price_data", {}).get("cex_price_usd")
        if price and cex_price and price > 0:
            spread_pct = abs((cex_price - price) / price * 100)
            spreads.append(round(spread_pct, 6))

    if not spreads:
        return {
            "avg_spread_pct": None,
            "max_spread_pct": None,
            "min_spread_pct": None,
        }

    from statistics import mean
    return {
        "avg_spread_pct": round(mean(spreads), 6),
        "max_spread_pct": max(spreads),
        "min_spread_pct": min(spreads),
    }

def write_weekly_report(iso_year, iso_week, config, paths, daily_reports, daily_data, price_agg, derivatives_agg, on_chain_agg, macro_agg, sentiment_agg, news_agg, signal_summary_agg, accumulation_metadata, cex_dex_spread):
    from datetime import datetime, timezone
    filename = f"weekly_{iso_year}_W{iso_week:02d}.json"
    output_path = paths["weekly"] / filename

    start_date = daily_reports[0]["date_str"]
    end_date = daily_reports[-1]["date_str"]

    output = {
        "period_metadata": {
            "type": "weekly",
            "period_id": f"{iso_year}-W{iso_week:02d}",
            "iso_year": iso_year,
            "iso_week": iso_week,
            "start_date": start_date,
            "end_date": end_date,
            "token": config["token_name"],
            "days_included": price_agg["days_included"],
            "days_possible": 7,
            "source": "accumulated",
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "data_quality": accumulation_metadata["data_quality"]
        },
        "price": price_agg,
        "technicals": aggregate_weekly_technicals(daily_data),
        "derivatives": derivatives_agg,
        "on_chain": on_chain_agg,
        "macro": macro_agg,
        "sentiment": sentiment_agg,
        "news": news_agg,
        "signal_summary": signal_summary_agg,
        "data_gaps": [],
        "accumulation_metadata": accumulation_metadata,
        "cex_dex_spread": cex_dex_spread,
    }

    try:
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        return output_path
    except Exception as e:
        print(f"  Failed to write {filename}: {e}")
        return None

def clear_accumulated_output(paths: dict) -> None:
    """
    In --rebuild mode: delete all files in weekly/, monthly/, quarterly/
    and the accumulation_index.json. Does not touch reports/ or archive/.
    """
    for subdir in ["weekly", "monthly", "quarterly"]:
        for f in paths[subdir].glob("*.json"):
            f.unlink()
        for f in paths[subdir].glob("*.md"):
            f.unlink()
    
    index_path = paths["accumulated_base"] / "accumulation_index.json"
    if index_path.exists():
        index_path.unlink()
    
    print("  ✓ Cleared accumulated output — rebuilding from scratch")

def main():
    args = parse_args()
    
    print(f"\nMuneo Market Accumulator v{SCRIPT_VERSION}")
    print(f"Config: {args.config}")
    if args.rebuild:
        print("Mode: REBUILD")
    print()
    
    # Load config
    config = load_config(args.config)
    print(f"Token: {config['token_name']}")
    print(f"Output prefix: {config['output_prefix']}")
    
    # Setup directories
    paths = setup_directories(config)
    
    # Rebuild mode: clear existing accumulated output
    if args.rebuild:
        clear_accumulated_output(paths)
    
    # Discover daily reports
    print(f"\nScanning {paths['reports']} for {config['output_prefix']}*.json ...")
    daily_reports = discover_daily_reports(paths["reports"], config["output_prefix"])
    print(f"  Found {len(daily_reports)} daily reports")
    
    if not daily_reports:
        print("  No daily reports found. Nothing to accumulate.")
        return
    
    # Group by ISO week
    weekly_groups = group_by_iso_week(daily_reports)
    print(f"  Grouped into {len(weekly_groups)} ISO weeks:")
    for (yr, wk), reports in sorted(weekly_groups.items()):
        days = len(reports)
        status = "✓ eligible" if days >= MIN_DAYS_FOR_WEEKLY else f"✗ only {days} days (need {MIN_DAYS_FOR_WEEKLY})"
        print(f"    {yr}-W{wk:02d}: {days} days — {status}")

    print("\nGenerating weekly reports...")
    weekly_written = []
    for (yr, wk), reports in sorted(weekly_groups.items()):
        deduped = deduplicate_by_date(reports)
        if len(deduped) < MIN_DAYS_FOR_WEEKLY:
            print(f"  Skipping {yr}-W{wk:02d}: only {len(deduped)} days after dedup")
            continue
        daily_data = [d for d in [load_daily_report(r["path"]) for r in deduped] if d is not None]
        if len(daily_data) < MIN_DAYS_FOR_WEEKLY:
            print(f"  Skipping {yr}-W{wk:02d}: only {len(daily_data)} reports loaded")
            continue
        price_agg = aggregate_weekly_price(daily_data)
        derivatives_agg = aggregate_weekly_derivatives(daily_data)
        on_chain_agg = aggregate_weekly_on_chain(daily_data)
        macro_agg = aggregate_weekly_macro(daily_data)
        sentiment_agg = aggregate_weekly_sentiment(daily_data)
        news_agg = aggregate_weekly_news(daily_data)
        signal_summary_agg = aggregate_weekly_signal_summary(daily_data)
        accumulation_metadata = aggregate_accumulation_metadata(deduped, daily_data)
        cex_dex_spread = aggregate_cex_dex_spread(daily_data)
        output_path = write_weekly_report(yr, wk, config, paths, deduped, daily_data, price_agg, derivatives_agg, on_chain_agg, macro_agg, sentiment_agg, news_agg, signal_summary_agg, accumulation_metadata, cex_dex_spread)
        if output_path:
            weekly_written.append(output_path)
            print(f"  Written: {output_path.name}")
    print(f"\n{len(weekly_written)} weekly reports written.")

    print("Accumulator run complete.")

if __name__ == "__main__":
    main()