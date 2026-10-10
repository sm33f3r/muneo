# Muneo Market Accumulator - A script to accumulate daily market reports into weekly, monthly, and quarterly summaries.

import os
import json
import base64
import argparse
import shutil
from datetime import datetime, timezone
from dateutil.parser import parse as parse_date
from dotenv import load_dotenv
from pathlib import Path
import requests

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

GLOBAL_CONFIG = {"token_name": "global", "output_prefix": "market_report_global"}
GLOBAL_ROLLUP_SCHEMA_VERSION = "1.1.0"
GLOBAL_SIGNAL_KEYS = [
    "market_cap_direction_lean", "btc_dominance_lean", "altcoin_season_lean", "btc_direction_lean",
    "btc_funding_lean", "btc_ls_lean", "eth_btc_lean", "dxy_lean", "broad_dollar_index_lean",
    "spy_lean", "vix_lean", "fear_greed_lean", "news_volume_lean", "tech_sector_lean",
    "coinbase_premium_lean", "etf_flow_lean", "liq_pressure_lean",
]

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
    parser.add_argument(
        "--global-weekly",
        dest="global_weekly",
        action="store_true",
        help="Generate global weekly rollups from reports/market_report_global_*.json (local only)"
    )
    parser.add_argument(
        "--global-rollups",
        dest="global_rollups",
        action="store_true",
        help="Generate global monthly and quarterly rollups from existing global weekly files (local only)"
    )
    parser.add_argument(
        "--global-prune-dry-run",
        dest="global_prune_dry_run",
        action="store_true",
        help="Preview global rollup pruning (coverage-gated); deletes nothing"
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

def _get_nested(report: dict, *keys, default=None):
    """Safely traverse nested dict keys. Returns default if any key is missing."""
    val = report
    for key in keys:
        if not isinstance(val, dict):
            return default
        val = val.get(key, default)
        if val is default:
            return default
    return val


def setup_directories(config: dict) -> dict:
    """Create output directories if they don't exist. Returns path dict."""
    token_dir = config["token_name"].lower()  # e.g. "sui" or "sol"
    
    paths = {
        "reports": Path("./reports"),
        "archive": Path("./reports/archive"),
        "context_root": Path(f"./context/{token_dir}"),
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

def select_daily_reports_to_delete(daily_reports: list[dict], rolled_up_weeks: set, retain_days: int = RETAIN_DAILY, today: datetime | None = None) -> list[dict]:
    """
    Return the subset of daily_reports eligible for deletion.
    A report is eligible only if BOTH hold:
      1. The report's entire ISO week ended >= retain_days ago
         (age measured from the week's Sunday at midnight)
      2. Its (iso_year, iso_week) is present in rolled_up_weeks
         (i.e. that week already produced a written weekly report — never
         delete data from a week that was skipped for insufficient days)
    """
    if today is None:
        today = datetime.now(timezone.utc).replace(tzinfo=None)

    eligible = []
    for r in daily_reports:
        week_end = datetime.fromisocalendar(r["iso_year"], r["iso_week"], 7)
        age_days = (today - week_end).days
        week_key = (r["iso_year"], r["iso_week"])
        if age_days >= retain_days and week_key in rolled_up_weeks:
            eligible.append(r)
    return eligible

def fetch_existing_weekly(paths: dict, config: dict, iso_year: int, iso_week: int) -> tuple[dict | None, str | None]:
    """
    Look up an already-written weekly rollup: local disk first, then GitHub.
    Returns (parsed_json, source) with source in {"local", "github", None}. Never raises.
    """
    filename = f"weekly_{iso_year}_W{iso_week:02d}.json"
    try:
        local_path = paths["weekly"] / filename
        if local_path.exists():
            with open(local_path, "r", encoding="utf-8") as f:
                return json.load(f), "local"
    except Exception:
        pass
    try:
        token = os.getenv("GITHUB_TOKEN")
        repo = os.getenv("GITHUB_REPO")
        if not token or not repo:
            return None, None
        branch = os.getenv("GITHUB_BRANCH") or "main"
        url = f"https://api.github.com/repos/{repo}/contents/context/{config['token_name'].lower()}/accumulated/weekly/{filename}"
        resp = requests.get(
            url,
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"},
            params={"ref": branch},
            timeout=10,
        )
        if resp.status_code != 200:
            return None, None
        content = resp.json()["content"].replace("\n", "")
        return json.loads(base64.b64decode(content).decode("utf-8")), "github"
    except Exception:
        return None, None

def should_overwrite_weekly(new_days: int, existing_days: int | None) -> bool:
    """True if there is no existing weekly or the regenerated one has at least as many days."""
    return existing_days is None or new_days >= existing_days

def cleanup_old_daily_reports(daily_reports: list[dict], rolled_up_weeks: set) -> dict:
    """
    Delete daily report files that are >= RETAIN_DAILY days old and whose
    ISO week already has a written weekly report. Deletes both the local
    copy and the GitHub copy under reports/{filename}. Non-blocking —
    any single file failure logs a warning and continues.
    Returns {"local": [...], "remote": [...], "skipped_no_sha": [...]}.
    """
    to_delete = select_daily_reports_to_delete(daily_reports, rolled_up_weeks)
    result = {"local": [], "remote": [], "skipped_no_sha": []}

    if not to_delete:
        return result

    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    } if token else None

    for r in to_delete:
        filename = r["filename"]
        local_path = r["path"]

        # Local delete
        try:
            if local_path.exists():
                local_path.unlink()
                result["local"].append(filename)
        except Exception as e:
            print(f"  ⚠ Failed to delete local {filename}: {e}")

        # Remote delete
        if not token or not repo:
            continue

        github_path = f"reports/{filename}"
        api_url = f"https://api.github.com/repos/{repo}/contents/{github_path}"
        try:
            check = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=10)
            if check.status_code != 200:
                result["skipped_no_sha"].append(filename)
                continue
            sha = check.json().get("sha")
            if not sha:
                result["skipped_no_sha"].append(filename)
                continue

            payload = {
                "message": f"chore: prune daily report {filename} (retention {RETAIN_DAILY}d)",
                "sha": sha,
                "branch": branch
            }
            response = requests.delete(api_url, json=payload, headers=headers, timeout=15)
            if response.status_code == 200:
                result["remote"].append(filename)
                print(f"  ✓ Deleted from GitHub — {github_path}")
            else:
                print(f"  ✗ GitHub delete failed — {github_path} HTTP {response.status_code}: {response.text[:100]}")
        except Exception as e:
            print(f"  ✗ GitHub delete failed — {github_path}: {e}")

    return result

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
        "schema_version_input": "2.0.0",
        "pre_upgrade_reports_skipped": 0,
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

def discover_weekly_reports(weekly_dir: Path) -> list[dict]:
    """
    Scan the weekly output directory and return a sorted list of weekly report metadata dicts.
    Each dict contains: path, filename, period_id, iso_year, iso_week, start_date, end_date, report.
    """
    results = []
    for f in weekly_dir.glob("weekly_*.json"):
        try:
            report = json.loads(f.read_text())
            pm = report.get("period_metadata", {})
            iso_year = pm.get("iso_year")
            iso_week = pm.get("iso_week")
            start_date = pm.get("start_date")
            end_date = pm.get("end_date")
            if iso_year is None or iso_week is None:
                print(f"  ⚠ Skipping weekly file missing metadata: {f.name}")
                continue
            results.append({
                "path": f,
                "filename": f.name,
                "period_id": pm.get("period_id"),
                "iso_year": iso_year,
                "iso_week": iso_week,
                "start_date": start_date,
                "end_date": end_date,
                "report": report,
            })
        except Exception as e:
            print(f"  ⚠ Failed to load weekly file {f.name}: {e}")
            continue
    return sorted(results, key=lambda x: (x["iso_year"], x["iso_week"]))

def group_by_calendar_month(weekly_reports: list[dict]) -> dict:
    """
    Group weekly reports by the calendar month where the majority of their days fall.
    A week spanning Mon Jan 27 – Sun Feb 2 has 5 days in January — it belongs to January.
    Returns dict keyed by (year, month) tuples, values are lists of weekly report dicts,
    sorted by (iso_year, iso_week) within each month.
    """
    from collections import defaultdict
    from datetime import date, timedelta

    groups = defaultdict(list)

    for wr in weekly_reports:
        start_str = wr.get("start_date")
        end_str = wr.get("end_date")

        if not start_str or not end_str:
            print(f"  ⚠ Skipping weekly report missing start/end date: {wr['filename']}")
            continue

        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)

        # Count days per calendar month across this week's span
        month_day_counts = defaultdict(int)
        current = start
        while current <= end:
            month_day_counts[(current.year, current.month)] += 1
            current += timedelta(days=1)

        # Assign to the month with the most days
        majority_month = max(month_day_counts, key=month_day_counts.get)
        groups[majority_month].append(wr)

    return dict(sorted(groups.items()))

def aggregate_monthly_price(weekly_reports: list[dict]) -> dict:
    """
    Aggregate price data from weekly reports into a monthly summary.
    Open: from first week's price.open_usd
    Close: from last week's price.close_usd
    High/Low: max/min across all weekly high/low values
    Averages: weighted by days_included where available, otherwise simple mean
    """
    from statistics import mean, mode

    def get_price(wr, field):
        return (wr["report"].get("price") or {}).get(field)

    weekly_data = [wr["report"].get("price") or {} for wr in weekly_reports]

    opens = [w.get("open_usd") for w in weekly_data if w.get("open_usd") is not None]
    closes = [w.get("close_usd") for w in weekly_data if w.get("close_usd") is not None]
    highs = [w.get("high_usd") for w in weekly_data if w.get("high_usd") is not None]
    lows = [w.get("low_usd") for w in weekly_data if w.get("low_usd") is not None]
    avg_vols = [w.get("avg_volume_24h_usd") for w in weekly_data if w.get("avg_volume_24h_usd") is not None]
    total_vols = [w.get("total_volume_week_usd") for w in weekly_data if w.get("total_volume_week_usd") is not None]
    avg_mcaps = [w.get("avg_market_cap_usd") for w in weekly_data if w.get("avg_market_cap_usd") is not None]
    close_mcaps = [w.get("close_market_cap_usd") for w in weekly_data if w.get("close_market_cap_usd") is not None]
    ath_vals = [w.get("ath_usd") for w in weekly_data if w.get("ath_usd") is not None]
    ath_dd_vals = [w.get("ath_drawdown_pct") for w in weekly_data if w.get("ath_drawdown_pct") is not None]
    cex_prices = [w.get("avg_cex_price_usd") for w in weekly_data if w.get("avg_cex_price_usd") is not None]
    up_vols = [w.get("volume_on_up_days_usd") for w in weekly_data if w.get("volume_on_up_days_usd") is not None]
    down_vols = [w.get("volume_on_down_days_usd") for w in weekly_data if w.get("volume_on_down_days_usd") is not None]
    volume_leans = [w.get("volume_lean") for w in weekly_data if w.get("volume_lean") is not None]
    days_list = [w.get("days_included") for w in weekly_data if w.get("days_included") is not None]

    open_usd = opens[0] if opens else None
    close_usd = closes[-1] if closes else None
    price_change_pct = round((close_usd - open_usd) / open_usd * 100, 4) if open_usd and close_usd else None
    high_usd = max(highs) if highs else None
    drawdown_from_high = round((close_usd - high_usd) / high_usd * 100, 4) if close_usd and high_usd else None

    return {
        "open_usd": open_usd,
        "close_usd": close_usd,
        "high_usd": high_usd,
        "low_usd": min(lows) if lows else None,
        "price_change_month_pct": price_change_pct,
        "drawdown_from_period_high_pct": drawdown_from_high,
        "ath_usd": ath_vals[-1] if ath_vals else None,
        "ath_drawdown_pct": ath_dd_vals[-1] if ath_dd_vals else None,
        "avg_volume_24h_usd": round(mean(avg_vols), 2) if avg_vols else None,
        "total_volume_month_usd": round(sum(total_vols), 2) if total_vols else None,
        "avg_market_cap_usd": round(mean(avg_mcaps), 2) if avg_mcaps else None,
        "close_market_cap_usd": close_mcaps[-1] if close_mcaps else None,
        "avg_cex_price_usd": round(mean(cex_prices), 6) if cex_prices else None,
        "volume_on_up_days_usd": round(sum(up_vols), 2) if up_vols else None,
        "volume_on_down_days_usd": round(sum(down_vols), 2) if down_vols else None,
        "volume_lean": mode(volume_leans) if volume_leans else None,
        "weeks_included": len(weekly_reports),
    }

def aggregate_monthly_technicals(weekly_reports: list[dict]) -> dict:
    """
    Aggregate technical indicators from weekly reports into a monthly summary.
    All values taken from the last weekly report (period-end).
    """
    def last_val(field):
        for wr in reversed(weekly_reports):
            tech = (wr["report"].get("technicals") or {})
            v = tech.get(field)
            if v is not None:
                return v
        return None

    def last_nested(outer, inner):
        for wr in reversed(weekly_reports):
            tech = (wr["report"].get("technicals") or {})
            obj = tech.get(outer)
            if obj and obj.get(inner) is not None:
                return obj.get(inner)
        return None

    last_tech = None
    for wr in reversed(weekly_reports):
        t = wr["report"].get("technicals")
        if t is not None:
            last_tech = t
            break

    close_price = None
    for wr in reversed(weekly_reports):
        p = (wr["report"].get("price") or {}).get("close_usd")
        if p is not None:
            close_price = p
            break

    sma_50 = last_val("sma_50_at_close")
    ema_200 = last_val("ema_200_at_close")
    price_vs_sma50_pct = round((close_price - sma_50) / sma_50 * 100, 4) if close_price and sma_50 else None
    price_vs_ema200_pct = round((close_price - ema_200) / ema_200 * 100, 4) if close_price and ema_200 else None

    macd_at_close = None
    for wr in reversed(weekly_reports):
        m = (wr["report"].get("technicals") or {}).get("macd_at_close")
        if m is not None:
            macd_at_close = m
            break

    bb_at_close = None
    for wr in reversed(weekly_reports):
        b = (wr["report"].get("technicals") or {}).get("bollinger_bands_at_close")
        if b is not None:
            bb_at_close = b
            break

    return {
        "rsi_14_at_close": last_val("rsi_14_at_close"),
        "rsi_lean": last_val("rsi_lean"),
        "macd_at_close": macd_at_close,
        "macd_lean": last_val("macd_lean"),
        "bollinger_bands_at_close": bb_at_close,
        "bb_lean": last_val("bb_lean"),
        "sma_50_at_close": sma_50,
        "price_vs_sma50_pct": price_vs_sma50_pct,
        "ema_200_at_close": ema_200,
        "price_vs_ema200_pct": price_vs_ema200_pct,
        "ema200_lean": last_val("ema200_lean"),
    }

def aggregate_monthly_derivatives(weekly_reports: list[dict]) -> dict:
    """
    Aggregate derivatives data from weekly reports into a monthly summary.
    OI: open from first week, close from last week, change computed across period.
    Funding rate: mean of weekly averages across the month.
    BTC L/S ratio: period-end from last weekly report.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            d = (wr["report"].get("derivatives") or {})
            v = d.get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for wr in weekly_reports:
            d = (wr["report"].get("derivatives") or {})
            v = d.get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 8) if result else None

    oi_open = first_val("oi_open_usd")
    oi_close = last_val("oi_at_close_usd")
    oi_change_pct = round((oi_close - oi_open) / oi_open * 100, 4) if oi_open and oi_close else None

    return {
        "oi_at_close_usd": oi_close,
        "oi_open_usd": oi_open,
        "oi_change_pct": oi_change_pct,
        "oi_lean": last_val("oi_lean"),
        "funding_rate_daily_avg": avg_vals("funding_rate_daily_avg"),
        "funding_rate_7d_rolling_avg": last_val("funding_rate_7d_rolling_avg"),
        "funding_lean": last_val("funding_lean"),
        "btc_long_short_ratio": last_val("btc_long_short_ratio"),
        "btc_long_account_pct": last_val("btc_long_account_pct"),
        "btc_short_account_pct": last_val("btc_short_account_pct"),
        "btc_ls_ratio_lean": last_val("btc_ls_ratio_lean"),
    }

def aggregate_monthly_on_chain(weekly_reports: list[dict]) -> dict:
    """
    Aggregate on-chain data from weekly reports into a monthly summary.
    TVL: start from first week, end from last week, change computed across period.
    Address and flow data: averages across available weekly values.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for wr in weekly_reports:
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 2) if result else None

    def sum_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(sum(result), 2) if result else None

    tvl_start = first_val("tvl_start_usd")
    tvl_end = last_val("tvl_end_usd")
    tvl_change_pct = round((tvl_end - tvl_start) / tvl_start * 100, 4) if tvl_start and tvl_end else None

    if tvl_change_pct is not None:
        if tvl_change_pct > 2:
            tvl_lean = "bullish"
        elif tvl_change_pct < -2:
            tvl_lean = "bearish"
        else:
            tvl_lean = "neutral"
    else:
        tvl_lean = None

    tvl_direction = None
    if tvl_change_pct is not None:
        if tvl_change_pct > 0:
            tvl_direction = "rising"
        elif tvl_change_pct < 0:
            tvl_direction = "falling"
        else:
            tvl_direction = "flat"

    addr_start = first_val("active_addresses_start")
    addr_end = last_val("active_addresses_end")
    if addr_start and addr_end:
        if addr_end > addr_start * 1.02:
            addr_lean = "bullish"
        elif addr_end < addr_start * 0.98:
            addr_lean = "bearish"
        else:
            addr_lean = "neutral"
    else:
        addr_lean = None

    net_flow = sum_vals("exchange_net_flow_usd")
    if net_flow is not None:
        exchange_flow_lean = "bearish" if net_flow > 0 else "bullish" if net_flow < 0 else "neutral"
    else:
        exchange_flow_lean = None

    return {
        "tvl_start_usd": tvl_start,
        "tvl_end_usd": tvl_end,
        "tvl_change_pct": tvl_change_pct,
        "tvl_direction": tvl_direction,
        "tvl_lean": tvl_lean,
        "active_addresses_avg": avg_vals("active_addresses_avg"),
        "active_addresses_start": addr_start,
        "active_addresses_end": addr_end,
        "active_addresses_lean": addr_lean,
        "exchange_inflow_avg_usd": avg_vals("exchange_inflow_avg_usd"),
        "exchange_outflow_avg_usd": avg_vals("exchange_outflow_avg_usd"),
        "exchange_net_flow_usd": net_flow,
        "exchange_flow_lean": exchange_flow_lean,
    }

def aggregate_monthly_macro(weekly_reports: list[dict]) -> dict:
    """
    Aggregate macro data from weekly reports into a monthly summary.
    BTC return: computed from first week open to last week close.
    Dominance, ETH/BTC, DXY, SPY, VIX: start from first week, end from last week.
    Alpha: token monthly return minus BTC monthly return.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            v = (wr["report"].get("macro") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for wr in weekly_reports:
            v = (wr["report"].get("macro") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("macro") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 4) if result else None

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

    # BTC return across month
    btc_return = None
    btc_returns = []
    for wr in weekly_reports:
        v = (wr["report"].get("macro") or {}).get("btc_period_return_pct")
        if v is not None:
            btc_returns.append(v)
    if btc_returns:
        # Compound weekly returns into monthly return
        from functools import reduce
        compound = reduce(lambda acc, r: acc * (1 + r / 100), btc_returns, 1.0)
        btc_return = round((compound - 1) * 100, 4)

    btc_direction = direction(
        first_val("btc_dominance_start") and None,  # use return direction instead
        None
    )
    if btc_return is not None:
        if btc_return > 0.1:
            btc_direction = "rising"
        elif btc_return < -0.1:
            btc_direction = "falling"
        else:
            btc_direction = "flat"

    # Token monthly return for alpha
    token_return = None
    token_returns = []
    for wr in weekly_reports:
        price = wr["report"].get("price") or {}
        o = price.get("open_usd")
        c = price.get("close_usd")
        if o and c:
            token_returns.append((c - o) / o * 100)
    if token_returns:
        from functools import reduce
        compound = reduce(lambda acc, r: acc * (1 + r / 100), token_returns, 1.0)
        token_return = round((compound - 1) * 100, 4)

    alpha = round(token_return - btc_return, 4) if token_return is not None and btc_return is not None else None

    # Dominance
    dom_start = first_val("btc_dominance_start")
    dom_end = last_val("btc_dominance_end")
    dom_dir = direction(dom_start, dom_end)
    dom_lean = "bearish" if dom_dir == "rising" else "bullish" if dom_dir == "falling" else "neutral" if dom_dir == "flat" else None

    # ETH/BTC
    eth_btc_start = first_val("eth_btc_ratio_start")
    eth_btc_end = last_val("eth_btc_ratio_end")
    eth_btc_lean = "bullish" if eth_btc_end and eth_btc_start and eth_btc_end > eth_btc_start else "bearish" if eth_btc_end and eth_btc_start and eth_btc_end < eth_btc_start else "neutral" if eth_btc_start and eth_btc_end else None

    # DXY
    dxy_start = first_val("dxy_start")
    dxy_end = last_val("dxy_end")
    dxy_change = pct_change(dxy_start, dxy_end)
    dxy_dir = direction(dxy_start, dxy_end)
    dxy_lean = "bearish" if dxy_dir == "rising" else "bullish" if dxy_dir == "falling" else "neutral" if dxy_dir == "flat" else None

    # SPY
    spy_start = first_val("spy_start")
    spy_end = last_val("spy_end")
    spy_change = pct_change(spy_start, spy_end)
    spy_lean = "bullish" if spy_change and spy_change > 0 else "bearish" if spy_change and spy_change < 0 else "neutral" if spy_change == 0 else None

    # VIX
    vix_start = first_val("vix_start")
    vix_end = last_val("vix_end")
    vix_avg = avg_vals("vix_avg")
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
        "total_crypto_mcap_start_usd": first_val("total_crypto_mcap_start_usd"),
        "total_crypto_mcap_end_usd": last_val("total_crypto_mcap_end_usd"),
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

def aggregate_monthly_sentiment(weekly_reports: list[dict]) -> dict:
    """
    Aggregate sentiment data from weekly reports into a monthly summary.
    Fear & Greed: avg/min/max across all weekly averages, period-end from last week.
    LunarCrush fields: averages across weekly averages, period-end from last week.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 4) if result else None

    def min_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return min(result) if result else None

    def max_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return max(result) if result else None

    fg_at_close = last_val("fear_greed_at_close")
    if fg_at_close is not None:
        if fg_at_close >= 60:
            fg_lean = "bearish"
        elif fg_at_close <= 40:
            fg_lean = "bullish"
        else:
            fg_lean = "neutral"
    else:
        fg_lean = None

    lc_sentiment_close = last_val("lc_sentiment_at_close")
    if lc_sentiment_close is not None:
        if lc_sentiment_close >= 60:
            sentiment_lean = "bullish"
        elif lc_sentiment_close <= 40:
            sentiment_lean = "bearish"
        else:
            sentiment_lean = "neutral"
    else:
        sentiment_lean = None

    galaxy_score_close = last_val("lc_galaxy_score_at_close")
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
        "fear_greed_avg": avg_vals("fear_greed_avg"),
        "fear_greed_min": min_vals("fear_greed_min"),
        "fear_greed_max": max_vals("fear_greed_max"),
        "fear_greed_at_close": fg_at_close,
        "fear_greed_label_at_close": last_val("fear_greed_label_at_close"),
        "fear_greed_lean": fg_lean,
        "lc_interactions_avg": avg_vals("lc_interactions_avg"),
        "lc_sentiment_avg": avg_vals("lc_sentiment_avg"),
        "lc_sentiment_at_close": lc_sentiment_close,
        "lc_posts_active_avg": avg_vals("lc_posts_active_avg"),
        "lc_contributors_active_avg": avg_vals("lc_contributors_active_avg"),
        "lc_galaxy_score_avg": avg_vals("lc_galaxy_score_avg"),
        "lc_galaxy_score_at_close": galaxy_score_close,
        "galaxy_score_lean": galaxy_score_lean,
        "lc_alt_rank_at_close": last_val("lc_alt_rank_at_close"),
        "lc_social_dominance_avg": avg_vals("lc_social_dominance_avg"),
        "sentiment_lean": sentiment_lean,
    }

def aggregate_monthly_news(weekly_reports: list[dict]) -> dict:
    """
    Aggregate news headlines from weekly reports into a monthly summary.
    Collects all headlines across all weeks, deduplicates by title,
    sorts by published_at descending, caps at 30 headlines.
    """
    MAX_HEADLINES = 30

    seen_titles = set()
    all_headlines = []

    for wr in weekly_reports:
        news = (wr["report"].get("news") or {})
        items = news.get("headlines") or []
        for item in items:
            title = item.get("title")
            if not title:
                continue
            if title in seen_titles:
                continue
            seen_titles.add(title)
            all_headlines.append(item)

    def sort_key(h):
        ts = h.get("published_at")
        return ts if ts else ""

    all_headlines.sort(key=sort_key, reverse=True)
    all_headlines = all_headlines[:MAX_HEADLINES]

    return {
        "headline_count": len(all_headlines),
        "sources_included": list({h.get("source") for h in all_headlines if h.get("source")}),
        "headlines": all_headlines,
    }

def aggregate_monthly_signal_summary(weekly_reports: list[dict]) -> dict:
    """
    Aggregate signal summary from weekly reports into a monthly summary.
    Period-end signals taken from last weekly report.
    Signal streaks computed across weekly overall_lean values.
    """
    SIGNAL_KEYS = [
        "rsi_lean", "macd_lean", "ema200_lean", "bb_lean", "volume_lean",
        "oi_lean", "funding_lean", "tvl_lean", "exchange_flow_lean",
        "active_addresses_lean", "btc_dominance_lean", "eth_btc_lean",
        "dxy_lean", "spy_lean", "vix_lean", "fear_greed_lean",
        "sentiment_lean", "galaxy_score_lean", "btc_ls_ratio_lean",
    ]

    def last_signal(key):
        for wr in reversed(weekly_reports):
            sig = (wr["report"].get("signal_summary") or {}).get("signals") or {}
            val = sig.get(key)
            if val is not None:
                return val
        return None

    period_end_signals = {k: last_signal(k) for k in SIGNAL_KEYS}

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

    # Signal streaks across weekly overall_lean values
    weekly_leans = []
    for wr in weekly_reports:
        lean = (wr["report"].get("signal_summary") or {}).get("overall_lean")
        weekly_leans.append(lean)

    current_streak_lean = None
    current_streak_length = 0
    longest_bullish = 0
    longest_bearish = 0

    if weekly_leans:
        current_streak_lean = weekly_leans[-1]
        for lean in reversed(weekly_leans):
            if lean == current_streak_lean:
                current_streak_length += 1
            else:
                break

        streak = 1
        for i in range(1, len(weekly_leans)):
            if weekly_leans[i] == weekly_leans[i - 1] and weekly_leans[i] is not None:
                streak += 1
            else:
                streak = 1
            if weekly_leans[i] == "bullish":
                longest_bullish = max(longest_bullish, streak)
            elif weekly_leans[i] == "bearish":
                longest_bearish = max(longest_bearish, streak)

        if weekly_leans[0] == "bullish":
            longest_bullish = max(longest_bullish, 1)
        elif weekly_leans[0] == "bearish":
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

def build_weekly_breakdown(weekly_reports: list[dict]) -> list[dict]:
    """
    Build a compact weekly breakdown array for inclusion in monthly reports.
    One entry per constituent weekly report.
    """
    breakdown = []
    for wr in weekly_reports:
        report = wr["report"]
        pm = report.get("period_metadata") or {}
        price = report.get("price") or {}
        ss = report.get("signal_summary") or {}
        breakdown.append({
            "period_id": pm.get("period_id"),
            "start_date": pm.get("start_date"),
            "end_date": pm.get("end_date"),
            "overall_lean": ss.get("overall_lean"),
            "close_usd": price.get("close_usd"),
            "price_change_week_pct": price.get("price_change_week_pct"),
            "signals_available": ss.get("signals_available"),
            "data_quality": pm.get("data_quality"),
        })
    return breakdown


def write_monthly_report(cal_year, cal_month, config, paths, weekly_reports,
                          price_agg, technicals_agg, derivatives_agg, on_chain_agg,
                          macro_agg, sentiment_agg, news_agg, signal_summary_agg,
                          accumulation_metadata, cex_dex_spread, weekly_breakdown):
    from datetime import datetime, timezone
    filename = f"monthly_{cal_year}_M{cal_month:02d}.json"
    output_path = paths["monthly"] / filename

    start_date = weekly_reports[0].get("start_date")
    end_date = weekly_reports[-1].get("end_date")
    weeks_included = len(weekly_reports)

    output = {
        "period_metadata": {
            "type": "monthly",
            "period_id": f"{cal_year}-M{cal_month:02d}",
            "cal_year": cal_year,
            "cal_month": cal_month,
            "start_date": start_date,
            "end_date": end_date,
            "token": config["token_name"],
            "weeks_included": weeks_included,
            "weeks_possible": 4,
            "source": "accumulated",
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "data_quality": accumulation_metadata["data_quality"],
        },
        "price": price_agg,
        "technicals": technicals_agg,
        "derivatives": derivatives_agg,
        "on_chain": on_chain_agg,
        "macro": macro_agg,
        "sentiment": sentiment_agg,
        "news": news_agg,
        "signal_summary": signal_summary_agg,
        "weekly_breakdown": weekly_breakdown,
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

def aggregate_monthly_accumulation_metadata(weekly_reports: list[dict]) -> dict:
    """
    Compute accumulation_metadata block for a monthly report.
    Constituent periods are weeks.
    """
    weeks_possible = 4
    weeks_available = len(weekly_reports)
    weeks_missing = max(0, weeks_possible - weeks_available)

    if weeks_missing == 0:
        data_quality = "complete"
    elif weeks_available >= MIN_WEEKS_FOR_MONTHLY:
        data_quality = "partial"
    else:
        data_quality = "insufficient"

    return {
        "source": "accumulated",
        "constituent_periods": weeks_possible,
        "constituent_periods_available": weeks_available,
        "constituent_periods_missing": weeks_missing,
        "data_quality": data_quality,
    }

def aggregate_cex_dex_spread_monthly(weekly_reports: list[dict]) -> dict:
    """
    Aggregate CEX/DEX spread from weekly reports into a monthly summary.
    """
    from statistics import mean

    avgs = [w["report"].get("cex_dex_spread", {}).get("avg_spread_pct")
            for w in weekly_reports
            if (w["report"].get("cex_dex_spread") or {}).get("avg_spread_pct") is not None]
    maxes = [w["report"].get("cex_dex_spread", {}).get("max_spread_pct")
             for w in weekly_reports
             if (w["report"].get("cex_dex_spread") or {}).get("max_spread_pct") is not None]
    mins = [w["report"].get("cex_dex_spread", {}).get("min_spread_pct")
            for w in weekly_reports
            if (w["report"].get("cex_dex_spread") or {}).get("min_spread_pct") is not None]

    return {
        "avg_spread_pct": round(mean(avgs), 6) if avgs else None,
        "max_spread_pct": max(maxes) if maxes else None,
        "min_spread_pct": min(mins) if mins else None,
    }

def discover_monthly_reports(monthly_dir: Path) -> list[dict]:
    """
    Scan the monthly output directory and return a sorted list of monthly report metadata dicts.
    Each dict contains: path, filename, period_id, cal_year, cal_month, start_date, end_date, report.
    """
    results = []
    for f in monthly_dir.glob("monthly_*.json"):
        try:
            report = json.loads(f.read_text())
            pm = report.get("period_metadata", {})
            cal_year = pm.get("cal_year")
            cal_month = pm.get("cal_month")
            start_date = pm.get("start_date")
            end_date = pm.get("end_date")
            if cal_year is None or cal_month is None:
                print(f"  ⚠ Skipping monthly file missing metadata: {f.name}")
                continue
            results.append({
                "path": f,
                "filename": f.name,
                "period_id": pm.get("period_id"),
                "cal_year": cal_year,
                "cal_month": cal_month,
                "start_date": start_date,
                "end_date": end_date,
                "report": report,
            })
        except Exception as e:
            print(f"  ⚠ Failed to load monthly file {f.name}: {e}")
            continue
    return sorted(results, key=lambda x: (x["cal_year"], x["cal_month"]))

def group_by_quarter(monthly_reports: list[dict]) -> dict:
    """
    Group monthly reports by calendar quarter.
    Returns dict keyed by (year, quarter) tuples (e.g. (2026, 2)),
    values are lists of monthly report dicts sorted by cal_month ascending.
    """
    from collections import defaultdict

    groups = defaultdict(list)

    for mr in monthly_reports:
        cal_year = mr.get("cal_year")
        cal_month = mr.get("cal_month")
        if cal_year is None or cal_month is None:
            print(f"  ⚠ Skipping monthly report missing year/month: {mr['filename']}")
            continue
        quarter = (cal_month - 1) // 3 + 1
        groups[(cal_year, quarter)].append(mr)

    # Sort months within each quarter
    for key in groups:
        groups[key].sort(key=lambda x: x["cal_month"])

    return dict(sorted(groups.items()))

def aggregate_quarterly_price(monthly_reports: list[dict]) -> dict:
    """
    Aggregate price data from monthly reports into a quarterly summary.
    Open: from first month's price.open_usd
    Close: from last month's price.close_usd
    High/Low: max/min across all monthly high/low values
    Totals: summed across months. Averages: mean across months.
    """
    from statistics import mean, mode

    def get_price(mr, field):
        return (mr["report"].get("price") or {}).get(field)

    monthly_data = [mr["report"].get("price") or {} for mr in monthly_reports]

    opens = [m.get("open_usd") for m in monthly_data if m.get("open_usd") is not None]
    closes = [m.get("close_usd") for m in monthly_data if m.get("close_usd") is not None]
    highs = [m.get("high_usd") for m in monthly_data if m.get("high_usd") is not None]
    lows = [m.get("low_usd") for m in monthly_data if m.get("low_usd") is not None]
    avg_vols = [m.get("avg_volume_24h_usd") for m in monthly_data if m.get("avg_volume_24h_usd") is not None]
    total_vols = [m.get("total_volume_month_usd") for m in monthly_data if m.get("total_volume_month_usd") is not None]
    avg_mcaps = [m.get("avg_market_cap_usd") for m in monthly_data if m.get("avg_market_cap_usd") is not None]
    close_mcaps = [m.get("close_market_cap_usd") for m in monthly_data if m.get("close_market_cap_usd") is not None]
    ath_vals = [m.get("ath_usd") for m in monthly_data if m.get("ath_usd") is not None]
    ath_dd_vals = [m.get("ath_drawdown_pct") for m in monthly_data if m.get("ath_drawdown_pct") is not None]
    cex_prices = [m.get("avg_cex_price_usd") for m in monthly_data if m.get("avg_cex_price_usd") is not None]
    up_vols = [m.get("volume_on_up_days_usd") for m in monthly_data if m.get("volume_on_up_days_usd") is not None]
    down_vols = [m.get("volume_on_down_days_usd") for m in monthly_data if m.get("volume_on_down_days_usd") is not None]
    volume_leans = [m.get("volume_lean") for m in monthly_data if m.get("volume_lean") is not None]

    open_usd = opens[0] if opens else None
    close_usd = closes[-1] if closes else None
    high_usd = max(highs) if highs else None
    price_change_pct = round((close_usd - open_usd) / open_usd * 100, 4) if open_usd and close_usd else None
    drawdown_from_high = round((close_usd - high_usd) / high_usd * 100, 4) if close_usd and high_usd else None

    return {
        "open_usd": open_usd,
        "close_usd": close_usd,
        "high_usd": high_usd,
        "low_usd": min(lows) if lows else None,
        "price_change_quarter_pct": price_change_pct,
        "drawdown_from_period_high_pct": drawdown_from_high,
        "ath_usd": ath_vals[-1] if ath_vals else None,
        "ath_drawdown_pct": ath_dd_vals[-1] if ath_dd_vals else None,
        "avg_volume_24h_usd": round(mean(avg_vols), 2) if avg_vols else None,
        "total_volume_quarter_usd": round(sum(total_vols), 2) if total_vols else None,
        "avg_market_cap_usd": round(mean(avg_mcaps), 2) if avg_mcaps else None,
        "close_market_cap_usd": close_mcaps[-1] if close_mcaps else None,
        "avg_cex_price_usd": round(mean(cex_prices), 6) if cex_prices else None,
        "volume_on_up_days_usd": round(sum(up_vols), 2) if up_vols else None,
        "volume_on_down_days_usd": round(sum(down_vols), 2) if down_vols else None,
        "volume_lean": mode(volume_leans) if volume_leans else None,
        "months_included": len(monthly_reports),
    }

def aggregate_quarterly_technicals(monthly_reports: list[dict]) -> dict:
    """
    Aggregate technical indicators from monthly reports into a quarterly summary.
    All values taken from the last monthly report (period-end).
    """
    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("technicals") or {}).get(field)
            if v is not None:
                return v
        return None

    close_price = None
    for mr in reversed(monthly_reports):
        p = (mr["report"].get("price") or {}).get("close_usd")
        if p is not None:
            close_price = p
            break

    sma_50 = last_val("sma_50_at_close")
    ema_200 = last_val("ema_200_at_close")
    price_vs_sma50_pct = round((close_price - sma_50) / sma_50 * 100, 4) if close_price and sma_50 else None
    price_vs_ema200_pct = round((close_price - ema_200) / ema_200 * 100, 4) if close_price and ema_200 else None

    macd_at_close = None
    for mr in reversed(monthly_reports):
        m = (mr["report"].get("technicals") or {}).get("macd_at_close")
        if m is not None:
            macd_at_close = m
            break

    bb_at_close = None
    for mr in reversed(monthly_reports):
        b = (mr["report"].get("technicals") or {}).get("bollinger_bands_at_close")
        if b is not None:
            bb_at_close = b
            break

    return {
        "rsi_14_at_close": last_val("rsi_14_at_close"),
        "rsi_lean": last_val("rsi_lean"),
        "macd_at_close": macd_at_close,
        "macd_lean": last_val("macd_lean"),
        "bollinger_bands_at_close": bb_at_close,
        "bb_lean": last_val("bb_lean"),
        "sma_50_at_close": sma_50,
        "price_vs_sma50_pct": price_vs_sma50_pct,
        "ema_200_at_close": ema_200,
        "price_vs_ema200_pct": price_vs_ema200_pct,
        "ema200_lean": last_val("ema200_lean"),
    }

def aggregate_quarterly_derivatives(monthly_reports: list[dict]) -> dict:
    """
    Aggregate derivatives data from monthly reports into a quarterly summary.
    OI: open from first month, close from last month.
    Funding: mean of monthly averages.
    BTC L/S: period-end from last month.
    """
    from statistics import mean

    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for mr in monthly_reports:
            v = (mr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 8) if result else None

    oi_open = first_val("oi_open_usd")
    oi_close = last_val("oi_at_close_usd")
    oi_change_pct = round((oi_close - oi_open) / oi_open * 100, 4) if oi_open and oi_close else None

    return {
        "oi_at_close_usd": oi_close,
        "oi_open_usd": oi_open,
        "oi_change_pct": oi_change_pct,
        "oi_lean": last_val("oi_lean"),
        "funding_rate_daily_avg": avg_vals("funding_rate_daily_avg"),
        "funding_rate_7d_rolling_avg": last_val("funding_rate_7d_rolling_avg"),
        "funding_lean": last_val("funding_lean"),
        "btc_long_short_ratio": last_val("btc_long_short_ratio"),
        "btc_long_account_pct": last_val("btc_long_account_pct"),
        "btc_short_account_pct": last_val("btc_short_account_pct"),
        "btc_ls_ratio_lean": last_val("btc_ls_ratio_lean"),
    }

def aggregate_quarterly_on_chain(monthly_reports: list[dict]) -> dict:
    """
    Aggregate on-chain data from monthly reports into a quarterly summary.
    TVL: start from first month, end from last month.
    Address and flow data: averages across monthly values.
    """
    from statistics import mean

    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for mr in monthly_reports:
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 2) if result else None

    def sum_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(sum(result), 2) if result else None

    tvl_start = first_val("tvl_start_usd")
    tvl_end = last_val("tvl_end_usd")
    tvl_change_pct = round((tvl_end - tvl_start) / tvl_start * 100, 4) if tvl_start and tvl_end else None

    if tvl_change_pct is not None:
        tvl_lean = "bullish" if tvl_change_pct > 2 else "bearish" if tvl_change_pct < -2 else "neutral"
        tvl_direction = "rising" if tvl_change_pct > 0 else "falling" if tvl_change_pct < 0 else "flat"
    else:
        tvl_lean = None
        tvl_direction = None

    addr_start = first_val("active_addresses_start")
    addr_end = last_val("active_addresses_end")
    if addr_start and addr_end:
        addr_lean = "bullish" if addr_end > addr_start * 1.02 else "bearish" if addr_end < addr_start * 0.98 else "neutral"
    else:
        addr_lean = None

    net_flow = sum_vals("exchange_net_flow_usd")
    exchange_flow_lean = "bearish" if net_flow and net_flow > 0 else "bullish" if net_flow and net_flow < 0 else "neutral" if net_flow == 0 else None

    return {
        "tvl_start_usd": tvl_start,
        "tvl_end_usd": tvl_end,
        "tvl_change_pct": tvl_change_pct,
        "tvl_direction": tvl_direction,
        "tvl_lean": tvl_lean,
        "active_addresses_avg": avg_vals("active_addresses_avg"),
        "active_addresses_start": addr_start,
        "active_addresses_end": addr_end,
        "active_addresses_lean": addr_lean,
        "exchange_inflow_avg_usd": avg_vals("exchange_inflow_avg_usd"),
        "exchange_outflow_avg_usd": avg_vals("exchange_outflow_avg_usd"),
        "exchange_net_flow_usd": net_flow,
        "exchange_flow_lean": exchange_flow_lean,
    }


def aggregate_quarterly_macro(monthly_reports: list[dict]) -> dict:
    """
    Aggregate macro data from monthly reports into a quarterly summary.
    BTC and token returns: compounded across monthly returns.
    Dominance, ETH/BTC, DXY, SPY, VIX: start from first month, end from last month.
    """
    from statistics import mean
    from functools import reduce

    if not monthly_reports:
        return {}

    sorted_months = sorted(monthly_reports, key=lambda mr: (mr.get("cal_year", 0), mr.get("cal_month", 0)))

    def macro(mr):
        return (mr["report"].get("macro") or {})

    def last_val(field):
        for mr in reversed(sorted_months):
            v = macro(mr).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for mr in sorted_months:
            v = macro(mr).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        vals = [macro(mr).get(field) for mr in sorted_months]
        vals = [v for v in vals if v is not None]
        return round(mean(vals), 4) if vals else None

    def pct_change(start, end):
        if start is None or end is None or start == 0:
            return None
        return round((end - start) / abs(start) * 100, 2)

    def direction(val):
        if val is None:
            return None
        if val > 0.1:
            return "rising"
        if val < -0.1:
            return "falling"
        return "flat"

    # Compound BTC returns
    btc_returns = [macro(mr).get("btc_period_return_pct") for mr in sorted_months]
    btc_returns = [v for v in btc_returns if v is not None]
    if btc_returns:
        btc_compound = reduce(lambda acc, r: acc * (1 + r / 100), btc_returns, 1.0)
        btc_period_return_pct = round((btc_compound - 1) * 100, 2)
        btc_direction = direction(btc_period_return_pct)
    else:
        btc_period_return_pct = None
        btc_direction = None

    # Compound token returns (vs BTC alpha)
    token_returns = [macro(mr).get("token_vs_btc_alpha_pct") for mr in sorted_months]
    token_returns_clean = [v for v in token_returns if v is not None]
    if token_returns_clean and btc_returns:
        token_compound = reduce(lambda acc, r: acc * (1 + r / 100), token_returns_clean, 1.0)
        token_vs_btc_alpha_pct = round((token_compound - 1) * 100, 2)
    else:
        token_vs_btc_alpha_pct = None

    # BTC dominance
    btc_dom_start = first_val("btc_dominance_start")
    btc_dom_end = last_val("btc_dominance_end")
    btc_dom_change = pct_change(btc_dom_start, btc_dom_end)
    btc_dom_lean_end = last_val("btc_dominance_lean")

    # Total crypto market cap
    mcap_start = first_val("total_crypto_mcap_start_usd")
    mcap_end = last_val("total_crypto_mcap_end_usd")
    mcap_change_pct = pct_change(mcap_start, mcap_end)
    mcap_lean = last_val("total_crypto_mcap_lean")

    # ETH/BTC
    eth_btc_start = first_val("eth_btc_start")
    eth_btc_end = last_val("eth_btc_end")
    eth_btc_change_pct = pct_change(eth_btc_start, eth_btc_end)
    eth_btc_lean = last_val("eth_btc_lean")

    # DXY
    dxy_start = first_val("dxy_start")
    dxy_end = last_val("dxy_end")
    dxy_change_pct = pct_change(dxy_start, dxy_end)
    dxy_lean = last_val("dxy_lean")

    # SPY
    spy_start = first_val("spy_start")
    spy_end = last_val("spy_end")
    spy_change_pct = pct_change(spy_start, spy_end)
    spy_lean = last_val("spy_lean")

    # VIX
    vix_start = first_val("vix_start")
    vix_end = last_val("vix_end")
    vix_change_pct = pct_change(vix_start, vix_end)
    vix_avg = avg_vals("vix_avg")
    vix_lean = last_val("vix_lean")

    return {
        "btc_period_return_pct": btc_period_return_pct,
        "btc_direction": btc_direction,
        "token_vs_btc_alpha_pct": token_vs_btc_alpha_pct,
        "btc_dominance_start": btc_dom_start,
        "btc_dominance_end": btc_dom_end,
        "btc_dominance_change_pct": btc_dom_change,
        "btc_dominance_lean": btc_dom_lean_end,
        "total_crypto_mcap_start_usd": mcap_start,
        "total_crypto_mcap_end_usd": mcap_end,
        "total_crypto_mcap_change_pct": mcap_change_pct,
        "total_crypto_mcap_lean": mcap_lean,
        "eth_btc_start": eth_btc_start,
        "eth_btc_end": eth_btc_end,
        "eth_btc_change_pct": eth_btc_change_pct,
        "eth_btc_lean": eth_btc_lean,
        "dxy_start": dxy_start,
        "dxy_end": dxy_end,
        "dxy_change_pct": dxy_change_pct,
        "dxy_lean": dxy_lean,
        "spy_start": spy_start,
        "spy_end": spy_end,
        "spy_change_pct": spy_change_pct,
        "spy_lean": spy_lean,
        "vix_start": vix_start,
        "vix_end": vix_end,
        "vix_change_pct": vix_change_pct,
        "vix_avg": vix_avg,
        "vix_lean": vix_lean,
    }


def aggregate_quarterly_sentiment(monthly_reports: list[dict]) -> dict:
    """
    Aggregate sentiment data from monthly reports into a quarterly summary.
    Fear & Greed: avg/min/max across monthly averages, period-end from last month.
    LunarCrush: averages across monthly averages, period-end from last month.
    """
    from statistics import mean

    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 4) if result else None

    def min_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return min(result) if result else None

    def max_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return max(result) if result else None

    fg_at_close = last_val("fear_greed_at_close")
    if fg_at_close is not None:
        fg_lean = "bearish" if fg_at_close >= 60 else "bullish" if fg_at_close <= 40 else "neutral"
    else:
        fg_lean = None

    lc_sentiment_close = last_val("lc_sentiment_at_close")
    if lc_sentiment_close is not None:
        sentiment_lean = "bullish" if lc_sentiment_close >= 60 else "bearish" if lc_sentiment_close <= 40 else "neutral"
    else:
        sentiment_lean = None

    galaxy_score_close = last_val("lc_galaxy_score_at_close")
    if galaxy_score_close is not None:
        galaxy_score_lean = "bullish" if galaxy_score_close >= 60 else "bearish" if galaxy_score_close <= 40 else "neutral"
    else:
        galaxy_score_lean = None

    return {
        "fear_greed_avg": avg_vals("fear_greed_avg"),
        "fear_greed_min": min_vals("fear_greed_min"),
        "fear_greed_max": max_vals("fear_greed_max"),
        "fear_greed_at_close": fg_at_close,
        "fear_greed_label_at_close": last_val("fear_greed_label_at_close"),
        "fear_greed_lean": fg_lean,
        "lc_interactions_avg": avg_vals("lc_interactions_avg"),
        "lc_sentiment_avg": avg_vals("lc_sentiment_avg"),
        "lc_sentiment_at_close": lc_sentiment_close,
        "lc_posts_active_avg": avg_vals("lc_posts_active_avg"),
        "lc_contributors_active_avg": avg_vals("lc_contributors_active_avg"),
        "lc_galaxy_score_avg": avg_vals("lc_galaxy_score_avg"),
        "lc_galaxy_score_at_close": galaxy_score_close,
        "galaxy_score_lean": galaxy_score_lean,
        "lc_alt_rank_at_close": last_val("lc_alt_rank_at_close"),
        "lc_social_dominance_avg": avg_vals("lc_social_dominance_avg"),
        "sentiment_lean": sentiment_lean,
    }


def aggregate_quarterly_news(monthly_reports: list[dict]) -> dict:
    """
    Aggregate news headlines from monthly reports into a quarterly summary.
    Collects all headlines across all months, deduplicates by title,
    sorts by published_at descending, caps at 40 headlines.
    """
    MAX_HEADLINES = 40

    seen_titles = set()
    all_headlines = []

    for mr in monthly_reports:
        news = (mr["report"].get("news") or {})
        items = news.get("headlines") or []
        for item in items:
            title = item.get("title")
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            all_headlines.append(item)

    all_headlines.sort(key=lambda h: h.get("published_at") or "", reverse=True)
    all_headlines = all_headlines[:MAX_HEADLINES]

    return {
        "headline_count": len(all_headlines),
        "sources_included": list({h.get("source") for h in all_headlines if h.get("source")}),
        "headlines": all_headlines,
    }


def aggregate_quarterly_signal_summary(monthly_reports: list[dict]) -> dict:
    """
    Aggregate signal summary from monthly reports into a quarterly summary.
    Period-end signals taken from last monthly report.
    Signal streaks computed across monthly overall_lean values.
    """
    SIGNAL_KEYS = [
        "rsi_lean", "macd_lean", "ema200_lean", "bb_lean", "volume_lean",
        "oi_lean", "funding_lean", "tvl_lean", "exchange_flow_lean",
        "active_addresses_lean", "btc_dominance_lean", "eth_btc_lean",
        "dxy_lean", "spy_lean", "vix_lean", "fear_greed_lean",
        "sentiment_lean", "galaxy_score_lean", "btc_ls_ratio_lean",
    ]

    def last_signal(key):
        for mr in reversed(monthly_reports):
            sig = (mr["report"].get("signal_summary") or {}).get("signals") or {}
            val = sig.get(key)
            if val is not None:
                return val
        return None

    period_end_signals = {k: last_signal(k) for k in SIGNAL_KEYS}
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

    monthly_leans = [(mr["report"].get("signal_summary") or {}).get("overall_lean") for mr in monthly_reports]

    current_streak_lean = monthly_leans[-1] if monthly_leans else None
    current_streak_length = 0
    longest_bullish = 0
    longest_bearish = 0

    if monthly_leans:
        for lean in reversed(monthly_leans):
            if lean == current_streak_lean:
                current_streak_length += 1
            else:
                break

        streak = 1
        for i in range(1, len(monthly_leans)):
            if monthly_leans[i] == monthly_leans[i - 1] and monthly_leans[i] is not None:
                streak += 1
            else:
                streak = 1
            if monthly_leans[i] == "bullish":
                longest_bullish = max(longest_bullish, streak)
            elif monthly_leans[i] == "bearish":
                longest_bearish = max(longest_bearish, streak)

        if monthly_leans[0] == "bullish":
            longest_bullish = max(longest_bullish, 1)
        elif monthly_leans[0] == "bearish":
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


def build_monthly_breakdown(monthly_reports: list[dict]) -> list[dict]:
    """
    Build a compact monthly breakdown array for inclusion in quarterly reports.
    One entry per constituent monthly report.
    """
    breakdown = []
    for mr in monthly_reports:
        report = mr["report"]
        pm = report.get("period_metadata") or {}
        price = report.get("price") or {}
        ss = report.get("signal_summary") or {}
        breakdown.append({
            "period_id": pm.get("period_id"),
            "start_date": pm.get("start_date"),
            "end_date": pm.get("end_date"),
            "overall_lean": ss.get("overall_lean"),
            "close_usd": price.get("close_usd"),
            "price_change_month_pct": price.get("price_change_month_pct"),
            "signals_available": ss.get("signals_available"),
            "data_quality": pm.get("data_quality"),
        })
    return breakdown


def aggregate_quarterly_accumulation_metadata(monthly_reports: list[dict]) -> dict:
    """
    Compute accumulation_metadata block for a quarterly report.
    Constituent periods are months.
    """
    months_possible = 3
    months_available = len(monthly_reports)
    months_missing = max(0, months_possible - months_available)

    if months_missing == 0:
        data_quality = "complete"
    elif months_available >= MIN_MONTHS_FOR_QUARTERLY:
        data_quality = "partial"
    else:
        data_quality = "insufficient"

    return {
        "source": "accumulated",
        "constituent_periods": months_possible,
        "constituent_periods_available": months_available,
        "constituent_periods_missing": months_missing,
        "data_quality": data_quality,
    }


def aggregate_cex_dex_spread_quarterly(monthly_reports: list[dict]) -> dict:
    """
    Aggregate CEX/DEX spread from monthly reports into a quarterly summary.
    """
    from statistics import mean

    avgs = [mr["report"].get("cex_dex_spread", {}).get("avg_spread_pct")
            for mr in monthly_reports
            if (mr["report"].get("cex_dex_spread") or {}).get("avg_spread_pct") is not None]
    maxes = [mr["report"].get("cex_dex_spread", {}).get("max_spread_pct")
             for mr in monthly_reports
             if (mr["report"].get("cex_dex_spread") or {}).get("max_spread_pct") is not None]
    mins = [mr["report"].get("cex_dex_spread", {}).get("min_spread_pct")
            for mr in monthly_reports
            if (mr["report"].get("cex_dex_spread") or {}).get("min_spread_pct") is not None]

    return {
        "avg_spread_pct": round(mean(avgs), 6) if avgs else None,
        "max_spread_pct": max(maxes) if maxes else None,
        "min_spread_pct": min(mins) if mins else None,
    }


def write_quarterly_report(cal_year, quarter, config, paths, monthly_reports,
                            price_agg, technicals_agg, derivatives_agg, on_chain_agg,
                            macro_agg, sentiment_agg, news_agg, signal_summary_agg,
                            accumulation_metadata, cex_dex_spread, monthly_breakdown):
    from datetime import datetime, timezone
    filename = f"quarterly_{cal_year}_Q{quarter}.json"
    output_path = paths["quarterly"] / filename

    start_date = monthly_reports[0].get("start_date")
    end_date = monthly_reports[-1].get("end_date")

    output = {
        "period_metadata": {
            "type": "quarterly",
            "period_id": f"{cal_year}-Q{quarter}",
            "cal_year": cal_year,
            "quarter": quarter,
            "start_date": start_date,
            "end_date": end_date,
            "token": config["token_name"],
            "months_included": len(monthly_reports),
            "months_possible": 3,
            "source": "accumulated",
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "data_quality": accumulation_metadata["data_quality"],
        },
        "price": price_agg,
        "technicals": technicals_agg,
        "derivatives": derivatives_agg,
        "on_chain": on_chain_agg,
        "macro": macro_agg,
        "sentiment": sentiment_agg,
        "news": news_agg,
        "signal_summary": signal_summary_agg,
        "monthly_breakdown": monthly_breakdown,
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
        "schema_v2_fields": {
            "tech_sector_lean_modal":     None,  # modal aggregation not yet implemented
            "coinbase_premium_pct_avg":   None,  # avg aggregation not yet implemented
            "etf_net_inflow_sum_usd":     None,  # sum aggregation not yet implemented
            "schema_note":                "new fields from schema 2.0.0 — aggregation planned for v1.2.0",
        },
    }

    try:
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        return output_path
    except Exception as e:
        print(f"  Failed to write {filename}: {e}")
        return None


def write_accumulation_index(config: dict, paths: dict) -> Path:
    """
    Write a single accumulation_index.json to the token context root.
    Lists all available weekly, monthly, and quarterly periods with key summary fields.
    Axo reads this index to discover what accumulated periods are available.
    """
    from datetime import datetime, timezone

    def summarise_report(f: Path, period_type: str) -> dict | None:
        try:
            report = json.loads(f.read_text())
            pm = report.get("period_metadata") or {}
            ss = report.get("signal_summary") or {}
            price = report.get("price") or {}
            am = report.get("accumulation_metadata") or {}
            return {
                "period_id": pm.get("period_id"),
                "period_type": period_type,
                "start_date": pm.get("start_date"),
                "end_date": pm.get("end_date"),
                "data_quality": am.get("data_quality"),
                "overall_lean": ss.get("overall_lean"),
                "signals_available": ss.get("signals_available"),
                "close_usd": price.get("close_usd"),
                "price_change_pct": (
                    price.get("price_change_week_pct")
                    or price.get("price_change_month_pct")
                    or price.get("price_change_quarter_pct")
                ),
                "filename": f.name,
            }
        except Exception as e:
            print(f"  ⚠ Failed to index {f.name}: {e}")
            return None

    weekly_entries = []
    for f in sorted(paths["weekly"].glob("weekly_*.json")):
        entry = summarise_report(f, "weekly")
        if entry:
            weekly_entries.append(entry)

    monthly_entries = []
    for f in sorted(paths["monthly"].glob("monthly_*.json")):
        entry = summarise_report(f, "monthly")
        if entry:
            monthly_entries.append(entry)

    quarterly_entries = []
    for f in sorted(paths["quarterly"].glob("quarterly_*.json")):
        entry = summarise_report(f, "quarterly")
        if entry:
            quarterly_entries.append(entry)

    all_entries = weekly_entries + monthly_entries + quarterly_entries

    index = {
        "token": config["token_name"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
        "schema_version": SCHEMA_VERSION,
        "total_periods": len(all_entries),
        "weekly_count": len(weekly_entries),
        "monthly_count": len(monthly_entries),
        "quarterly_count": len(quarterly_entries),
        "most_recent_weekly": weekly_entries[-1] if weekly_entries else None,
        "most_recent_monthly": monthly_entries[-1] if monthly_entries else None,
        "most_recent_quarterly": quarterly_entries[-1] if quarterly_entries else None,
        "periods": {
            "weekly": weekly_entries,
            "monthly": monthly_entries,
            "quarterly": quarterly_entries,
        },
    }

    index_path = paths["context_root"] / "accumulation_index.json"
    try:
        with open(index_path, "w") as f:
            json.dump(index, f, indent=2)
        return index_path
    except Exception as e:
        print(f"  Failed to write accumulation_index.json: {e}")
        return None


def select_files_to_prune(files: list, retain: int) -> list:
    """
    Given a list of Path objects already sorted ascending (oldest first,
    matching sorted(glob(...)) filename ordering), return the files that
    exceed the retention count — i.e. everything except the most recent
    `retain` files. Returns [] if len(files) <= retain. Pure — no I/O.
    """
    if len(files) <= retain:
        return []
    return files[:-retain]


def prune_accumulated_output(paths: dict, config: dict) -> dict:
    """
    Prune old accumulated files according to retention thresholds, both
    locally and on GitHub. Keeps the most recent RETAIN_WEEKLY weekly files
    and RETAIN_MONTHLY monthly files. Quarterly files are never pruned.
    Non-blocking — a GitHub deletion failure for one file logs and continues,
    it never raises or stops local pruning.
    Returns a dict summarising what was deleted locally and remotely.
    """
    deleted = {
        "weekly": [], "monthly": [],
        "weekly_remote": [], "monthly_remote": [],
        "remote_skipped_no_sha": [], "remote_failed": [],
    }

    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")
    github_enabled = bool(token and repo)
    if not github_enabled:
        print("  ⚠ GitHub prune skipped — GITHUB_TOKEN or GITHUB_REPO not set in .env")

    token_lower = config["token_name"].lower()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    } if github_enabled else {}

    def delete_remote(github_path: str) -> str:
        """Returns 'deleted', 'skipped_no_sha', or 'failed'."""
        try:
            api_url = f"https://api.github.com/repos/{repo}/contents/{github_path}"
            check = requests.get(api_url, headers=headers, timeout=10)
            if check.status_code != 200:
                return "skipped_no_sha"
            sha = check.json().get("sha")
            if not sha:
                return "skipped_no_sha"
            payload = {
                "message": f"accumulator: prune {github_path.split('/')[-1]}",
                "sha": sha,
                "branch": branch,
            }
            response = requests.delete(api_url, json=payload, headers=headers, timeout=15)
            if response.status_code == 200:
                return "deleted"
            print(f"  ✗ GitHub prune failed — {github_path} HTTP {response.status_code}: {response.text[:100]}")
            return "failed"
        except Exception as e:
            print(f"  ✗ GitHub prune failed — {github_path}: {e}")
            return "failed"

    def prune_group(files: list, retain: int, local_key: str, remote_subdir: str):
        to_delete = select_files_to_prune(sorted(files), retain)
        for f in to_delete:
            try:
                f.unlink()
                deleted[local_key].append(f.name)
                print(f"  Pruned {local_key}: {f.name}")
            except Exception as e:
                print(f"  ⚠ Failed to prune {f.name}: {e}")
                continue
            if github_enabled:
                github_path = f"context/{token_lower}/accumulated/{remote_subdir}/{f.name}"
                result = delete_remote(github_path)
                if result == "deleted":
                    deleted[f"{local_key}_remote"].append(f.name)
                    print(f"  ✓ GitHub prune — {github_path}")
                elif result == "skipped_no_sha":
                    deleted["remote_skipped_no_sha"].append(f.name)
                else:
                    deleted["remote_failed"].append(f.name)

    prune_group(paths["weekly"].glob("weekly_*.json"), RETAIN_WEEKLY, "weekly", "weekly")
    prune_group(paths["monthly"].glob("monthly_*.json"), RETAIN_MONTHLY, "monthly", "monthly")

    return deleted


def push_weekly_files_to_github(paths: dict, config: dict, weeks: set) -> set:
    """
    Push the weekly rollup files for the given (iso_year, iso_week) tuples to GitHub.
    Returns the set of weeks whose PUT succeeded (HTTP 200/201). Never raises.
    """
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")
    if not token or not repo:
        print("  ⚠ Weekly push skipped — GITHUB_TOKEN or GITHUB_REPO not set; no daily reports will be deleted")
        return set()

    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    token_lower = config["token_name"].lower()
    confirmed = set()
    for yr, wk in sorted(weeks):
        filename = f"weekly_{yr}_W{wk:02d}.json"
        github_path = f"context/{token_lower}/accumulated/weekly/{filename}"
        try:
            local_path = paths["weekly"] / filename
            if not local_path.exists():
                print(f"  ⚠ Weekly file missing locally — {filename} not pushed")
                continue
            api_url = f"https://api.github.com/repos/{repo}/contents/{github_path}"
            sha = None
            get_resp = requests.get(api_url, headers=headers, timeout=10)
            if get_resp.status_code == 200:
                sha = get_resp.json().get("sha")
            payload = {
                "message": f"accumulator: update {filename}",
                "content": base64.b64encode(local_path.read_text(encoding="utf-8").encode()).decode(),
                "branch": branch,
            }
            if sha:
                payload["sha"] = sha
            put_resp = requests.put(api_url, headers=headers, json=payload, timeout=15)
            if put_resp.status_code in (200, 201):
                confirmed.add((yr, wk))
                print(f"  ✓ Weekly confirmed on GitHub — {github_path}")
            else:
                print(f"  ✗ Weekly push failed — {github_path} HTTP {put_resp.status_code}: {put_resp.text[:100]}")
        except Exception as e:
            print(f"  ✗ Weekly push failed — {github_path}: {e}")
    return confirmed

def push_accumulated_to_github(paths: dict, config: dict) -> None:
    """
    Push all accumulated output files to GitHub.
    Non-blocking — missing keys log a warning, never crash the script.
    Follows the same pattern as push_report_to_github in market_report.py.
    """
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")

    if not token or not repo:
        print("  ⚠ GitHub push skipped — GITHUB_TOKEN or GITHUB_REPO not set in .env")
        return

    token_lower = config["token_name"].lower()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    def push_file(local_path: Path, github_path: str) -> bool:
        try:
            content = local_path.read_text(encoding="utf-8")
            encoded = base64.b64encode(content.encode()).decode()
            api_url = f"https://api.github.com/repos/{repo}/contents/{github_path}"

            sha = None
            check = requests.get(api_url, headers=headers, timeout=10)
            if check.status_code == 200:
                sha = check.json().get("sha")

            payload = {
                "message": f"accumulator: update {local_path.name}",
                "content": encoded,
                "branch": branch,
            }
            if sha:
                payload["sha"] = sha

            response = requests.put(api_url, json=payload, headers=headers, timeout=15)
            if response.status_code in (200, 201):
                print(f"  ✓ GitHub push — {github_path}")
                return True
            else:
                print(f"  ✗ GitHub push failed — {github_path} HTTP {response.status_code}: {response.text[:100]}")
                return False
        except Exception as e:
            print(f"  ✗ GitHub push failed — {github_path}: {e}")
            return False

    pushed = 0
    failed = 0

    # Push weekly files
    for f in sorted(paths["weekly"].glob("weekly_*.json")):
        github_path = f"context/{token_lower}/accumulated/weekly/{f.name}"
        if push_file(f, github_path):
            pushed += 1
        else:
            failed += 1

    # Push monthly files
    for f in sorted(paths["monthly"].glob("monthly_*.json")):
        github_path = f"context/{token_lower}/accumulated/monthly/{f.name}"
        if push_file(f, github_path):
            pushed += 1
        else:
            failed += 1

    # Push quarterly files
    for f in sorted(paths["quarterly"].glob("quarterly_*.json")):
        github_path = f"context/{token_lower}/accumulated/quarterly/{f.name}"
        if push_file(f, github_path):
            pushed += 1
        else:
            failed += 1

    # Push accumulation index
    index_path = paths["context_root"] / "accumulation_index.json"
    if index_path.exists():
        github_path = f"context/{token_lower}/accumulation_index.json"
        if push_file(index_path, github_path):
            pushed += 1
        else:
            failed += 1

    print(f"  GitHub: {pushed} pushed, {failed} failed.")


def pull_reports_from_github(token_symbol: str, output_prefix: str) -> int:
    """
    Download daily report files from GitHub into the local reports/ directory.
    Enables stateless Railway container execution by repopulating reports/
    from the GitHub-hosted source of truth before accumulation runs.
    Non-blocking — any failure logs a warning and returns 0, never raises.
    """
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")

    if not token or not repo:
        print("  ⚠ GitHub pull skipped — GITHUB_TOKEN or GITHUB_REPO not set in .env")
        return 0

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    try:
        api_url = f"https://api.github.com/repos/{repo}/contents/reports"
        response = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=15)
        if response.status_code != 200:
            print(f"  ⚠ GitHub pull skipped — failed to list reports/ HTTP {response.status_code}")
            return 0

        files = response.json()
        if not isinstance(files, list):
            print("  ⚠ GitHub pull skipped — unexpected response listing reports/")
            return 0

        for entry in files:
            name = entry.get("name")
            download_url = entry.get("download_url")
            if not name or not download_url:
                continue
            if not name.startswith(output_prefix) or not name.endswith(".json"):
                continue

            local_path = reports_dir / name
            if local_path.exists():
                continue

            try:
                file_response = requests.get(download_url, headers=headers, timeout=15)
                if file_response.status_code == 200:
                    local_path.write_bytes(file_response.content)
                    downloaded += 1
                else:
                    print(f"  ⚠ Failed to download {name}: HTTP {file_response.status_code}")
            except Exception as e:
                print(f"  ⚠ Failed to download {name}: {e}")

        return downloaded
    except Exception as e:
        print(f"  ⚠ GitHub pull failed for {token_symbol}: {e}")
        return 0


def pull_accumulated_from_github(paths: dict, config: dict) -> dict:
    """
    Download existing weekly/monthly/quarterly rollup files for one token (or
    "global") from GitHub into the local accumulated folders. Never overwrites
    an existing local file. A 404 on a subfolder is treated as empty.
    Reads from a public repository; no token is used or required.
    Non-blocking — never raises.
    """
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")

    result = {
        "weekly": 0, "monthly": 0, "quarterly": 0,
        "skipped_existing": 0, "failed": 0,
        "listing_failed": [], "skipped": False,
    }

    if not repo:
        print("  ⚠ Accumulated pull skipped — GITHUB_REPO not set")
        result["skipped"] = True
        return result

    headers = {"Accept": "application/vnd.github.v3+json"}

    try:
        token_lower = config["token_name"].lower()
        for subdir in ("weekly", "monthly", "quarterly"):
            try:
                api_url = (
                    f"https://api.github.com/repos/{repo}/contents/"
                    f"context/{token_lower}/accumulated/{subdir}"
                )
                response = requests.get(api_url, headers=headers, params={"ref": branch}, timeout=15)
                if response.status_code == 404:
                    continue
                if response.status_code != 200:
                    print(f"  ⚠ Accumulated pull: failed to list {subdir}/ HTTP {response.status_code}")
                    result["listing_failed"].append(subdir)
                    continue
                entries = response.json()
                if not isinstance(entries, list):
                    print(f"  ⚠ Accumulated pull: unexpected response listing {subdir}/")
                    result["listing_failed"].append(subdir)
                    continue
            except Exception as e:
                print(f"  ⚠ Accumulated pull: failed to list {subdir}/: {e}")
                result["listing_failed"].append(subdir)
                continue

            for entry in entries:
                try:
                    if not isinstance(entry, dict) or entry.get("type") != "file":
                        continue
                    name = entry["name"]
                    download_url = entry.get("download_url")
                    if not (name.startswith(f"{subdir}_") and name.endswith(".json") and download_url):
                        continue

                    local_path = paths[subdir] / name
                    if local_path.exists():
                        result["skipped_existing"] += 1
                        continue

                    file_response = requests.get(download_url, headers=headers, timeout=15)
                    if file_response.status_code != 200:
                        print(f"  ⚠ Failed to download {subdir}/{name}: HTTP {file_response.status_code}")
                        result["failed"] += 1
                        continue
                    json.loads(file_response.content)
                    local_path.write_bytes(file_response.content)
                    result[subdir] += 1
                except Exception as e:
                    print(f"  ⚠ Failed to download {subdir}/{entry.get('name') if isinstance(entry, dict) else entry}: {e}")
                    result["failed"] += 1
    except Exception as e:
        print(f"  ⚠ Accumulated pull failed: {e}")

    print(
        f"  Accumulated pull ({config.get('token_name')}): "
        f"{result['weekly']} weekly, {result['monthly']} monthly, {result['quarterly']} quarterly, "
        f"{result['skipped_existing']} existing, {result['failed']} failed, "
        f"listing_failed={result['listing_failed']}"
    )
    return result


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

# ---------------------------------------------------------------------------
# Global weekly rollup (local only — no network, no deletion)
# ---------------------------------------------------------------------------

def _global_values(daily_data: list[dict], *keys) -> list:
    """Non-None values of a nested key across daily reports, in order."""
    out = []
    for d in daily_data:
        v = _get_nested(d, *keys)
        if v is not None:
            out.append(v)
    return out

def _global_modal(values: list):
    """Most frequent non-None value; ties go to the value whose last occurrence is latest."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    counts, last_idx = {}, {}
    for i, v in enumerate(vals):
        counts[v] = counts.get(v, 0) + 1
        last_idx[v] = i
    return max(counts, key=lambda v: (counts[v], last_idx[v]))

def _global_pct_change(start, end):
    if start is None or end is None or start == 0:
        return None
    return round((end / start - 1) * 100, 4)

def _global_mean(values: list, digits: int = 4):
    if not values:
        return None
    return round(sum(values) / len(values), digits)

def _global_start_end(values: list):
    if not values:
        return None, None
    return values[0], values[-1]

def _global_lean_streaks(daily_leans: list) -> dict:
    """Streak logic identical to aggregate_weekly_signal_summary, over a list of daily leans."""
    current_lean = None
    current_len = 0
    longest_bull = 0
    longest_bear = 0
    if daily_leans:
        current_lean = daily_leans[-1]
        for lean in reversed(daily_leans):
            if lean == current_lean:
                current_len += 1
            else:
                break
        streak = 1
        for i in range(1, len(daily_leans)):
            if daily_leans[i] == daily_leans[i - 1] and daily_leans[i] is not None:
                streak += 1
            else:
                streak = 1
            if daily_leans[i] == "bullish":
                longest_bull = max(longest_bull, streak)
            elif daily_leans[i] == "bearish":
                longest_bear = max(longest_bear, streak)
        if daily_leans[0] == "bullish":
            longest_bull = max(longest_bull, 1)
        elif daily_leans[0] == "bearish":
            longest_bear = max(longest_bear, 1)
    return {
        "current_streak_lean": current_lean,
        "current_streak_length": current_len,
        "longest_bullish_streak_in_period": longest_bull,
        "longest_bearish_streak_in_period": longest_bear,
    }

def _global_last_nonempty_list(daily_data: list[dict], *keys, limit: int = 10) -> list:
    for d in reversed(daily_data):
        v = _get_nested(d, *keys)
        if isinstance(v, list) and v:
            return v[:limit]
    return []

def aggregate_global_weekly_market_structure(daily_data: list[dict]) -> dict:
    ms = lambda k: _global_values(daily_data, "market_structure", k)
    cap_s, cap_e = _global_start_end(ms("total_market_cap_usd"))
    btcd_s, btcd_e = _global_start_end(ms("btc_dominance_pct"))
    ethd_s, ethd_e = _global_start_end(ms("eth_dominance_pct"))
    t2_s, t2_e = _global_start_end(ms("total2_usd"))
    t3_s, t3_e = _global_start_end(ms("total3_usd"))
    alt = ms("altcoin_season_index")
    return {
        "total_market_cap_usd_start": cap_s,
        "total_market_cap_usd_end": cap_e,
        "total_market_cap_change_pct": _global_pct_change(cap_s, cap_e),
        "btc_dominance_pct_start": btcd_s,
        "btc_dominance_pct_end": btcd_e,
        "eth_dominance_pct_start": ethd_s,
        "eth_dominance_pct_end": ethd_e,
        "total2_usd_start": t2_s,
        "total2_usd_end": t2_e,
        "total3_usd_start": t3_s,
        "total3_usd_end": t3_e,
        "total_volume_24h_usd_avg": _global_mean(ms("total_volume_24h_usd")),
        "altcoin_season_index_avg": _global_mean(alt),
        "altcoin_season_index_close": alt[-1] if alt else None,
        "altcoin_season_lean_modal": _global_modal(ms("altcoin_season_lean")),
        "days_included": len(daily_data),
    }

def aggregate_global_weekly_btc(daily_data: list[dict]) -> dict:
    bt = lambda k: _global_values(daily_data, "btc", k)
    prices = bt("price_usd")
    p_open, p_close = _global_start_end(prices)
    oi_s, oi_e = _global_start_end(bt("oi_usd"))
    return {
        "price_open": p_open,
        "price_close": p_close,
        "price_high": max(prices) if prices else None,
        "price_low": min(prices) if prices else None,
        "period_return_pct": _global_pct_change(p_open, p_close),
        "oi_usd_start": oi_s,
        "oi_usd_end": oi_e,
        "oi_change_pct": _global_pct_change(oi_s, oi_e),
        "funding_rate_avg": _global_mean(bt("funding_rate_latest"), digits=8),
        "long_short_ratio_avg": _global_mean(bt("long_short_ratio")),
        "direction_modal": _global_modal(bt("direction")),
        "funding_lean_modal": _global_modal(bt("funding_lean")),
        "btc_ls_lean_modal": _global_modal(bt("btc_ls_lean")),
        "days_included": len(daily_data),
    }

def aggregate_global_weekly_eth(daily_data: list[dict]) -> dict:
    et = lambda k: _global_values(daily_data, "eth", k)
    p_open, p_close = _global_start_end(et("price_usd"))
    r_s, r_e = _global_start_end(et("eth_btc_ratio"))
    return {
        "price_open": p_open,
        "price_close": p_close,
        "period_return_pct": _global_pct_change(p_open, p_close),
        "eth_btc_ratio_start": r_s,
        "eth_btc_ratio_end": r_e,
        "eth_btc_lean_modal": _global_modal(et("eth_btc_lean")),
        "days_included": len(daily_data),
    }

def aggregate_global_weekly_defi(daily_data: list[dict]) -> dict:
    df = lambda k: _global_values(daily_data, "defi", k)
    tvl_s, tvl_e = _global_start_end(df("total_defi_tvl_usd"))
    sc_s, sc_e = _global_start_end(df("stablecoin_market_cap_usd"))
    sd_s, sd_e = _global_start_end(df("stablecoin_dominance_pct"))
    return {
        "total_defi_tvl_usd_start": tvl_s,
        "total_defi_tvl_usd_end": tvl_e,
        "tvl_change_pct": _global_pct_change(tvl_s, tvl_e),
        "stablecoin_market_cap_usd_start": sc_s,
        "stablecoin_market_cap_usd_end": sc_e,
        "stablecoin_dominance_pct_start": sd_s,
        "stablecoin_dominance_pct_end": sd_e,
        "days_included": len(daily_data),
    }

def aggregate_global_weekly_macro(daily_data: list[dict]) -> dict:
    mc = lambda k: _global_values(daily_data, "macro", k)
    dxy_s, dxy_e = _global_start_end(mc("dxy"))
    bd_s, bd_e = _global_start_end(mc("broad_dollar_index"))
    spy_s, spy_e = _global_start_end(mc("spy_close"))
    vix = mc("vix_close")
    return {
        "dxy_start": dxy_s,
        "dxy_end": dxy_e,
        "dxy_lean_modal": _global_modal(mc("dxy_lean")),
        "broad_dollar_index_start": bd_s,
        "broad_dollar_index_end": bd_e,
        "broad_dollar_index_lean_modal": _global_modal(mc("broad_dollar_index_lean")),
        "spy_close_start": spy_s,
        "spy_close_end": spy_e,
        "spy_return_pct": _global_pct_change(spy_s, spy_e),
        "spy_lean_modal": _global_modal(mc("spy_lean")),
        "vix_close_avg": _global_mean(vix),
        "vix_close_end": vix[-1] if vix else None,
        "vix_lean_modal": _global_modal(mc("vix_lean")),
        "coinbase_premium_pct_avg": _global_mean(mc("coinbase_premium_pct")),
        "coinbase_premium_lean_modal": _global_modal(mc("coinbase_premium_lean")),
    }

def aggregate_global_weekly_tech_equities(daily_data: list[dict]) -> dict:
    leans = _global_values(daily_data, "tech_equities", "tech_sector_lean")
    return {
        "tech_sector_lean_modal": _global_modal(leans),
        "days_with_data": len(leans),
    }

def aggregate_global_weekly_etf_flows(daily_data: list[dict]) -> dict:
    by_date = {}
    for d in daily_data:  # ascending, so later reports overwrite earlier ones for the same data_date
        data_date = _get_nested(d, "etf_flows", "data_date")
        net = _get_nested(d, "etf_flows", "total_net_inflow_usd")
        if data_date is None or net is None:
            continue
        by_date[data_date] = d["etf_flows"]
    days_with_data = len(_global_values(daily_data, "etf_flows", "total_net_inflow_usd"))
    if not by_date:
        return {
            "net_inflow_sum_usd": None,
            "unique_data_dates": 0,
            "cum_net_inflow_usd_close": None,
            "total_net_assets_usd_close": None,
            "flow_lean_modal": None,
            "days_with_data": 0,
            "daily_flows": [],
        }
    cum = _global_values(daily_data, "etf_flows", "cum_net_inflow_usd")
    assets = _global_values(daily_data, "etf_flows", "total_net_assets_usd")
    return {
        "net_inflow_sum_usd": sum(v["total_net_inflow_usd"] for v in by_date.values()),
        "unique_data_dates": len(by_date),
        "cum_net_inflow_usd_close": cum[-1] if cum else None,
        "total_net_assets_usd_close": assets[-1] if assets else None,
        "flow_lean_modal": _global_modal([v.get("flow_lean") for v in by_date.values()]),
        "days_with_data": days_with_data,
        "daily_flows": [
            {"data_date": dd, "net_inflow_usd": by_date[dd]["total_net_inflow_usd"]}
            for dd in sorted(by_date)
        ],
    }

def aggregate_global_weekly_sentiment(daily_data: list[dict]) -> dict:
    fg = _global_values(daily_data, "sentiment", "fear_greed_value")
    label = None
    for d in reversed(daily_data):
        if _get_nested(d, "sentiment", "fear_greed_value") is not None:
            label = _get_nested(d, "sentiment", "fear_greed_label")
            break
    return {
        "fear_greed_avg": _global_mean(fg),
        "fear_greed_min": min(fg) if fg else None,
        "fear_greed_max": max(fg) if fg else None,
        "fear_greed_close": fg[-1] if fg else None,
        "fear_greed_label_close": label,
        "fear_greed_lean_modal": _global_modal(_global_values(daily_data, "sentiment", "fear_greed_lean")),
    }

def aggregate_global_weekly_news(daily_data: list[dict]) -> dict:
    counts = []
    for d in daily_data:
        c = _get_nested(d, "news", "article_count_24h")
        if c is None:
            c = _get_nested(d, "news", "news_article_count_24h")
        if c is not None:
            counts.append(c)
    nw = lambda k: _global_values(daily_data, "news", k)
    return {
        "article_count_sum": sum(counts),
        "positive_count_sum": sum(nw("positive_count")),
        "negative_count_sum": sum(nw("negative_count")),
        "neutral_count_sum": sum(nw("neutral_count")),
        "sentiment_ratio_avg": _global_mean(nw("sentiment_ratio")),
        "news_spike_days": sum(1 for v in nw("news_spike") if v is True),
        "news_volume_lean_modal": _global_modal(nw("news_volume_lean")),
        "top_headlines": _global_last_nonempty_list(daily_data, "news", "top_headlines"),
        "macro_article_count_sum": sum(_global_values(daily_data, "macro_news", "article_count_24h")),
        "macro_headlines": _global_last_nonempty_list(daily_data, "macro_news", "headlines"),
    }

def aggregate_global_weekly_signal_summary(daily_data: list[dict]) -> dict:
    period_end = {}
    modal = {}
    for k in GLOBAL_SIGNAL_KEYS:
        vals = _global_values(daily_data, "signal_summary", "signals", k)
        period_end[k] = vals[-1] if vals else None
        modal[k] = _global_modal(vals)
    available = [k for k, v in period_end.items() if v is not None]
    bullish = sum(1 for v in period_end.values() if v == "bullish")
    bearish = sum(1 for v in period_end.values() if v == "bearish")
    neutral = sum(1 for v in period_end.values() if v == "neutral")
    if not available:
        overall = None
    elif bullish > bearish and bullish > neutral:
        overall = "bullish"
    elif bearish > bullish and bearish > neutral:
        overall = "bearish"
    else:
        overall = "neutral"
    daily_leans = [_get_nested(d, "signal_summary", "overall_lean") for d in daily_data]
    return {
        "signals_evaluated": len(GLOBAL_SIGNAL_KEYS),
        "signals_available": len(available),
        "signals_null": len(GLOBAL_SIGNAL_KEYS) - len(available),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "overall_lean": overall,
        "signals": period_end,
        "signal_modal": modal,
        "signal_streaks": _global_lean_streaks(daily_leans),
    }

GLOBAL_GAP_PATHS = [
    "market_structure.total_market_cap_usd", "btc.price_usd", "eth.price_usd",
    "defi.total_defi_tvl_usd", "macro.dxy", "macro.spy_close", "macro.vix_close",
    "macro.coinbase_premium_pct", "tech_equities.tech_sector_lean",
    "etf_flows.total_net_inflow_usd", "sentiment.fear_greed_value", "news.article_count_24h",
]

def aggregate_global_weekly_data_gaps(daily_data: list[dict]) -> list:
    return [p for p in GLOBAL_GAP_PATHS if not _global_values(daily_data, *p.split("."))]

def aggregate_global_weekly_accumulation_metadata(daily_data: list[dict]) -> dict:
    available = len(daily_data)
    missing = 7 - available
    if missing == 0:
        quality = "complete"
    elif available >= MIN_DAYS_FOR_WEEKLY:
        quality = "partial"
    else:
        quality = "insufficient"
    versions = sorted({v for v in _global_values(daily_data, "report_metadata", "schema_version") if isinstance(v, str)})
    return {
        "source": "accumulated",
        "constituent_periods": 7,
        "constituent_periods_available": available,
        "constituent_periods_missing": missing,
        "data_quality": quality,
        "schema_versions_input": versions,
        "days_with_fetch_errors": sum(1 for d in daily_data if d.get("fetch_errors")),
    }

def write_global_weekly_report(iso_year, iso_week, paths, deduped, daily_data) -> Path | None:
    try:
        acc = aggregate_global_weekly_accumulation_metadata(daily_data)
        report = {
            "period_metadata": {
                "type": "weekly",
                "scope": "global",
                "period_id": f"{iso_year}-W{iso_week:02d}",
                "iso_year": iso_year,
                "iso_week": iso_week,
                "start_date": deduped[0]["date_str"],
                "end_date": deduped[-1]["date_str"],
                "token": "global",
                "days_included": len(daily_data),
                "days_possible": 7,
                "source": "accumulated",
                "script_version": SCRIPT_VERSION,
                "schema_version": GLOBAL_ROLLUP_SCHEMA_VERSION,
                "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
                "data_quality": acc["data_quality"],
            },
            "market_structure": aggregate_global_weekly_market_structure(daily_data),
            "btc": aggregate_global_weekly_btc(daily_data),
            "eth": aggregate_global_weekly_eth(daily_data),
            "defi": aggregate_global_weekly_defi(daily_data),
            "macro": aggregate_global_weekly_macro(daily_data),
            "tech_equities": aggregate_global_weekly_tech_equities(daily_data),
            "etf_flows": aggregate_global_weekly_etf_flows(daily_data),
            "sentiment": aggregate_global_weekly_sentiment(daily_data),
            "news": aggregate_global_weekly_news(daily_data),
            "signal_summary": aggregate_global_weekly_signal_summary(daily_data),
            "data_gaps": aggregate_global_weekly_data_gaps(daily_data),
            "not_aggregated": {"liquidation_map": "all cluster fields null in every source report; intraday data, not aggregated"},
            "accumulation_metadata": acc,
        }
        out_path = paths["weekly"] / f"weekly_{iso_year}_W{iso_week:02d}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        return out_path
    except Exception as e:
        print(f"  ✗ Failed to write global weekly {iso_year}-W{iso_week:02d}: {e}")
        return None

def existing_rollup_days(path: Path) -> int | None:
    """days_included of an existing rollup file, or None if absent/unparseable/not a real int."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        days = (data.get("period_metadata") or {}).get("days_included")
        if isinstance(days, int) and not isinstance(days, bool):
            return days
        return None
    except Exception:
        return None

def run_global_weekly() -> list[Path]:
    """Build global weekly rollups from reports/market_report_global_*.json. Local only."""
    paths = setup_directories(GLOBAL_CONFIG)
    daily_reports = discover_daily_reports(paths["reports"], GLOBAL_CONFIG["output_prefix"])
    print(f"Found {len(daily_reports)} global daily reports")
    if not daily_reports:
        return []
    written = []
    for (yr, wk), reports in sorted(group_by_iso_week(daily_reports).items()):
        deduped = deduplicate_by_date(reports)
        if len(deduped) < MIN_DAYS_FOR_WEEKLY:
            print(f"  Skipping {yr}-W{wk:02d}: only {len(deduped)} days after dedup")
            continue
        daily_data = [d for d in (load_daily_report(r["path"]) for r in deduped) if d is not None]
        if len(daily_data) < MIN_DAYS_FOR_WEEKLY:
            print(f"  Skipping {yr}-W{wk:02d}: only {len(daily_data)} reports loaded")
            continue
        target = paths["weekly"] / f"weekly_{yr}_W{wk:02d}.json"
        existing_days = existing_rollup_days(target)
        if not should_overwrite_weekly(len(daily_data), existing_days):
            print(f"  Preserving {yr}-W{wk:02d}: existing weekly has {existing_days} days, regenerated would have {len(daily_data)}")
            continue
        out = write_global_weekly_report(yr, wk, paths, deduped, daily_data)
        if out:
            written.append(out)
            print(f"  Written: {out.name}")
    print(f"\n{len(written)} global weekly reports written.")
    return written

# ---------------------------------------------------------------------------
# Global monthly / quarterly rollups (local only — no network, no deletion)
# ---------------------------------------------------------------------------

def _global_weights(reports: list[dict]) -> list[int]:
    return [(_get_nested(r, "period_metadata", "days_included") or 1) for r in reports]

def _global_weighted_mean(reports: list[dict], *keys, digits: int = 4):
    total = 0.0
    weight = 0
    for r, w in zip(reports, _global_weights(reports)):
        v = _get_nested(r, *keys)
        if v is not None:
            total += v * w
            weight += w
    if weight == 0:
        return None
    return round(total / weight, digits)

def _global_first(reports: list[dict], *keys):
    vals = _global_values(reports, *keys)
    return vals[0] if vals else None

def _global_last(reports: list[dict], *keys):
    vals = _global_values(reports, *keys)
    return vals[-1] if vals else None

def _global_sum(reports: list[dict], *keys):
    vals = _global_values(reports, *keys)
    return sum(vals) if vals else None

def _global_max(reports: list[dict], *keys):
    vals = _global_values(reports, *keys)
    return max(vals) if vals else None

def _global_min(reports: list[dict], *keys):
    vals = _global_values(reports, *keys)
    return min(vals) if vals else None

def _global_reps(period_reports: list[dict]) -> list[dict]:
    return [p["report"] for p in period_reports]

def aggregate_global_rollup_market_structure(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    ms = lambda *k: ("market_structure",) + k
    out = {
        "total_market_cap_usd_start": _global_first(r, *ms("total_market_cap_usd_start")),
        "total_market_cap_usd_end": _global_last(r, *ms("total_market_cap_usd_end")),
    }
    out["total_market_cap_change_pct"] = _global_pct_change(out["total_market_cap_usd_start"], out["total_market_cap_usd_end"])
    for name in ("btc_dominance_pct", "eth_dominance_pct", "total2_usd", "total3_usd"):
        out[f"{name}_start"] = _global_first(r, *ms(f"{name}_start"))
        out[f"{name}_end"] = _global_last(r, *ms(f"{name}_end"))
    out["total_volume_24h_usd_avg"] = _global_weighted_mean(r, *ms("total_volume_24h_usd_avg"))
    out["altcoin_season_index_avg"] = _global_weighted_mean(r, *ms("altcoin_season_index_avg"))
    out["altcoin_season_index_close"] = _global_last(r, *ms("altcoin_season_index_close"))
    out["altcoin_season_lean_modal"] = _global_modal(_global_values(r, *ms("altcoin_season_lean_modal")))
    out["periods_included"] = len(period_reports)
    return out

def aggregate_global_rollup_btc(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    p_open = _global_first(r, "btc", "price_open")
    p_close = _global_last(r, "btc", "price_close")
    oi_s = _global_first(r, "btc", "oi_usd_start")
    oi_e = _global_last(r, "btc", "oi_usd_end")
    return {
        "price_open": p_open,
        "price_close": p_close,
        "price_high": _global_max(r, "btc", "price_high"),
        "price_low": _global_min(r, "btc", "price_low"),
        "period_return_pct": _global_pct_change(p_open, p_close),
        "oi_usd_start": oi_s,
        "oi_usd_end": oi_e,
        "oi_change_pct": _global_pct_change(oi_s, oi_e),
        "funding_rate_avg": _global_weighted_mean(r, "btc", "funding_rate_avg", digits=8),
        "long_short_ratio_avg": _global_weighted_mean(r, "btc", "long_short_ratio_avg"),
        "direction_modal": _global_modal(_global_values(r, "btc", "direction_modal")),
        "funding_lean_modal": _global_modal(_global_values(r, "btc", "funding_lean_modal")),
        "btc_ls_lean_modal": _global_modal(_global_values(r, "btc", "btc_ls_lean_modal")),
        "periods_included": len(period_reports),
    }

def aggregate_global_rollup_eth(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    p_open = _global_first(r, "eth", "price_open")
    p_close = _global_last(r, "eth", "price_close")
    return {
        "price_open": p_open,
        "price_close": p_close,
        "period_return_pct": _global_pct_change(p_open, p_close),
        "eth_btc_ratio_start": _global_first(r, "eth", "eth_btc_ratio_start"),
        "eth_btc_ratio_end": _global_last(r, "eth", "eth_btc_ratio_end"),
        "eth_btc_lean_modal": _global_modal(_global_values(r, "eth", "eth_btc_lean_modal")),
        "periods_included": len(period_reports),
    }

def aggregate_global_rollup_defi(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    tvl_s = _global_first(r, "defi", "total_defi_tvl_usd_start")
    tvl_e = _global_last(r, "defi", "total_defi_tvl_usd_end")
    return {
        "total_defi_tvl_usd_start": tvl_s,
        "total_defi_tvl_usd_end": tvl_e,
        "tvl_change_pct": _global_pct_change(tvl_s, tvl_e),
        "stablecoin_market_cap_usd_start": _global_first(r, "defi", "stablecoin_market_cap_usd_start"),
        "stablecoin_market_cap_usd_end": _global_last(r, "defi", "stablecoin_market_cap_usd_end"),
        "stablecoin_dominance_pct_start": _global_first(r, "defi", "stablecoin_dominance_pct_start"),
        "stablecoin_dominance_pct_end": _global_last(r, "defi", "stablecoin_dominance_pct_end"),
        "periods_included": len(period_reports),
    }

def aggregate_global_rollup_macro(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    mm = lambda k: _global_modal(_global_values(r, "macro", k))
    spy_s = _global_first(r, "macro", "spy_close_start")
    spy_e = _global_last(r, "macro", "spy_close_end")
    return {
        "dxy_start": _global_first(r, "macro", "dxy_start"),
        "dxy_end": _global_last(r, "macro", "dxy_end"),
        "dxy_lean_modal": mm("dxy_lean_modal"),
        "broad_dollar_index_start": _global_first(r, "macro", "broad_dollar_index_start"),
        "broad_dollar_index_end": _global_last(r, "macro", "broad_dollar_index_end"),
        "broad_dollar_index_lean_modal": mm("broad_dollar_index_lean_modal"),
        "spy_close_start": spy_s,
        "spy_close_end": spy_e,
        "spy_return_pct": _global_pct_change(spy_s, spy_e),
        "spy_lean_modal": mm("spy_lean_modal"),
        "vix_close_avg": _global_weighted_mean(r, "macro", "vix_close_avg"),
        "vix_close_end": _global_last(r, "macro", "vix_close_end"),
        "vix_lean_modal": mm("vix_lean_modal"),
        "coinbase_premium_pct_avg": _global_weighted_mean(r, "macro", "coinbase_premium_pct_avg"),
        "coinbase_premium_lean_modal": mm("coinbase_premium_lean_modal"),
    }

def aggregate_global_rollup_tech_equities(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    return {
        "tech_sector_lean_modal": _global_modal(_global_values(r, "tech_equities", "tech_sector_lean_modal")),
        "days_with_data": _global_sum(r, "tech_equities", "days_with_data") or 0,
    }

def aggregate_global_rollup_etf_flows(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    merged = {}
    for rep in r:  # later constituent overrides earlier for the same data_date
        flows = _get_nested(rep, "etf_flows", "daily_flows")
        if not isinstance(flows, list):
            continue
        for entry in flows:
            if isinstance(entry, dict) and entry.get("data_date") is not None and entry.get("net_inflow_usd") is not None:
                merged[entry["data_date"]] = entry["net_inflow_usd"]
    return {
        "net_inflow_sum_usd": sum(merged.values()) if merged else None,
        "unique_data_dates": len(merged),
        "cum_net_inflow_usd_close": _global_last(r, "etf_flows", "cum_net_inflow_usd_close"),
        "total_net_assets_usd_close": _global_last(r, "etf_flows", "total_net_assets_usd_close"),
        "flow_lean_modal": _global_modal(_global_values(r, "etf_flows", "flow_lean_modal")),
        "days_with_data": _global_sum(r, "etf_flows", "days_with_data") or 0,
        "daily_flows": [{"data_date": d, "net_inflow_usd": merged[d]} for d in sorted(merged)],
    }

def aggregate_global_rollup_sentiment(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    return {
        "fear_greed_avg": _global_weighted_mean(r, "sentiment", "fear_greed_avg"),
        "fear_greed_min": _global_min(r, "sentiment", "fear_greed_min"),
        "fear_greed_max": _global_max(r, "sentiment", "fear_greed_max"),
        "fear_greed_close": _global_last(r, "sentiment", "fear_greed_close"),
        "fear_greed_label_close": _global_last(r, "sentiment", "fear_greed_label_close"),
        "fear_greed_lean_modal": _global_modal(_global_values(r, "sentiment", "fear_greed_lean_modal")),
    }

def aggregate_global_rollup_news(period_reports: list[dict]) -> dict:
    r = _global_reps(period_reports)
    return {
        "article_count_sum": _global_sum(r, "news", "article_count_sum"),
        "positive_count_sum": _global_sum(r, "news", "positive_count_sum"),
        "negative_count_sum": _global_sum(r, "news", "negative_count_sum"),
        "neutral_count_sum": _global_sum(r, "news", "neutral_count_sum"),
        "sentiment_ratio_avg": _global_weighted_mean(r, "news", "sentiment_ratio_avg"),
        "news_spike_days": _global_sum(r, "news", "news_spike_days"),
        "news_volume_lean_modal": _global_modal(_global_values(r, "news", "news_volume_lean_modal")),
        "top_headlines": _global_last_nonempty_list(r, "news", "top_headlines"),
        "macro_article_count_sum": _global_sum(r, "news", "macro_article_count_sum"),
        "macro_headlines": _global_last_nonempty_list(r, "news", "macro_headlines"),
    }

def aggregate_global_rollup_signal_summary(period_reports: list[dict], unit: str) -> dict:
    r = _global_reps(period_reports)
    period_end = {}
    modal = {}
    for k in GLOBAL_SIGNAL_KEYS:
        period_end[k] = _global_last(r, "signal_summary", "signals", k)
        modal[k] = _global_modal(_global_values(r, "signal_summary", "signal_modal", k))
    available = [k for k, v in period_end.items() if v is not None]
    bullish = sum(1 for v in period_end.values() if v == "bullish")
    bearish = sum(1 for v in period_end.values() if v == "bearish")
    neutral = sum(1 for v in period_end.values() if v == "neutral")
    if not available:
        overall = None
    elif bullish > bearish and bullish > neutral:
        overall = "bullish"
    elif bearish > bullish and bearish > neutral:
        overall = "bearish"
    else:
        overall = "neutral"
    streaks = _global_lean_streaks([_get_nested(x, "signal_summary", "overall_lean") for x in r])
    streaks["streak_unit"] = unit
    return {
        "signals_evaluated": len(GLOBAL_SIGNAL_KEYS),
        "signals_available": len(available),
        "signals_null": len(GLOBAL_SIGNAL_KEYS) - len(available),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "overall_lean": overall,
        "signals": period_end,
        "signal_modal": modal,
        "signal_streaks": streaks,
    }

def aggregate_global_rollup_data_gaps(period_reports: list[dict]) -> list:
    gap_sets = []
    for rep in _global_reps(period_reports):
        gaps = rep.get("data_gaps") if isinstance(rep, dict) else None
        gap_sets.append(set(gaps) if isinstance(gaps, list) else set())
    if not gap_sets:
        return []
    return sorted(set.intersection(*gap_sets))

def build_global_rollup_breakdown(period_reports: list[dict], unit: str) -> list[dict]:
    out = []
    for rep in _global_reps(period_reports):
        out.append({
            "period_id": _get_nested(rep, "period_metadata", "period_id"),
            "start_date": _get_nested(rep, "period_metadata", "start_date"),
            "end_date": _get_nested(rep, "period_metadata", "end_date"),
            "overall_lean": _get_nested(rep, "signal_summary", "overall_lean"),
            "btc_price_close": _get_nested(rep, "btc", "price_close"),
            "btc_period_return_pct": _get_nested(rep, "btc", "period_return_pct"),
            "fear_greed_close": _get_nested(rep, "sentiment", "fear_greed_close"),
            "total_market_cap_change_pct": _get_nested(rep, "market_structure", "total_market_cap_change_pct"),
            "days_included": _get_nested(rep, "period_metadata", "days_included"),
            "data_quality": _get_nested(rep, "period_metadata", "data_quality"),
        })
    return out

def aggregate_global_rollup_accumulation_metadata(period_reports: list[dict], unit: str) -> dict:
    r = _global_reps(period_reports)
    n = len(r)
    if unit == "week":
        possible = 4
        missing = max(0, possible - n)
        quality = "complete" if missing == 0 else ("partial" if n >= MIN_WEEKS_FOR_MONTHLY else "insufficient")
        no_flows = sum(1 for x in r if not (isinstance(_get_nested(x, "etf_flows"), dict) and "daily_flows" in x["etf_flows"]))
    else:
        possible = 3
        missing = max(0, possible - n)
        quality = "complete" if n == 3 else ("partial" if n >= MIN_MONTHS_FOR_QUARTERLY else "insufficient")
        no_flows = sum((_get_nested(x, "accumulation_metadata", "weeks_missing_daily_flows") or 0) for x in r)
    versions = sorted({v for v in _global_values(r, "period_metadata", "schema_version") if isinstance(v, str)})
    return {
        "source": "accumulated",
        "constituent_periods": possible,
        "constituent_periods_available": n,
        "constituent_periods_missing": missing,
        "data_quality": quality,
        "days_included_total": sum(_get_nested(x, "period_metadata", "days_included") or 0 for x in r),
        "schema_versions_input": versions,
        "weeks_missing_daily_flows": no_flows,
    }

def _build_global_rollup(kind, pm_head, pm_tail, period_reports, unit, breakdown_key, out_path):
    """Assemble and write one global monthly/quarterly report. Returns the path, or None on failure."""
    try:
        acc = aggregate_global_rollup_accumulation_metadata(period_reports, unit)
        pm = {"type": kind, "scope": "global"}
        pm.update(pm_head)
        pm["start_date"] = period_reports[0]["start_date"]
        pm["end_date"] = period_reports[-1]["end_date"]
        pm["token"] = "global"
        pm.update(pm_tail)
        pm["days_included"] = acc["days_included_total"]
        pm.update({
            "source": "accumulated",
            "script_version": SCRIPT_VERSION,
            "schema_version": GLOBAL_ROLLUP_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "data_quality": acc["data_quality"],
        })
        report = {
            "period_metadata": pm,
            "market_structure": aggregate_global_rollup_market_structure(period_reports),
            "btc": aggregate_global_rollup_btc(period_reports),
            "eth": aggregate_global_rollup_eth(period_reports),
            "defi": aggregate_global_rollup_defi(period_reports),
            "macro": aggregate_global_rollup_macro(period_reports),
            "tech_equities": aggregate_global_rollup_tech_equities(period_reports),
            "etf_flows": aggregate_global_rollup_etf_flows(period_reports),
            "sentiment": aggregate_global_rollup_sentiment(period_reports),
            "news": aggregate_global_rollup_news(period_reports),
            "signal_summary": aggregate_global_rollup_signal_summary(period_reports, "weeks" if unit == "week" else "months"),
            breakdown_key: build_global_rollup_breakdown(period_reports, unit),
            "data_gaps": aggregate_global_rollup_data_gaps(period_reports),
            "not_aggregated": {"liquidation_map": "all cluster fields null in every source report; intraday data, not aggregated"},
            "accumulation_metadata": acc,
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        return out_path
    except Exception as e:
        print(f"  ✗ Failed to write global {kind} report {out_path.name}: {e}")
        return None

def write_global_monthly_report(cal_year, cal_month, paths, weekly_reports) -> Path | None:
    return _build_global_rollup(
        "monthly",
        {"period_id": f"{cal_year}-M{cal_month:02d}", "cal_year": cal_year, "cal_month": cal_month},
        {"weeks_included": len(weekly_reports), "weeks_possible": 4},
        weekly_reports, "week", "weekly_breakdown",
        paths["monthly"] / f"monthly_{cal_year}_M{cal_month:02d}.json",
    )

def write_global_quarterly_report(cal_year, quarter, paths, monthly_reports) -> Path | None:
    return _build_global_rollup(
        "quarterly",
        {"period_id": f"{cal_year}-Q{quarter}", "cal_year": cal_year, "quarter": quarter},
        {"months_included": len(monthly_reports), "months_possible": 3},
        monthly_reports, "month", "monthly_breakdown",
        paths["quarterly"] / f"quarterly_{cal_year}_Q{quarter}.json",
    )

def run_global_rollups() -> dict:
    """Build global monthly then quarterly rollups from existing global weekly files. Local only."""
    paths = setup_directories(GLOBAL_CONFIG)
    result = {"monthly": [], "quarterly": [], "preserved": []}

    weekly = discover_weekly_reports(paths["weekly"])
    print(f"Found {len(weekly)} global weekly reports")
    for (yr, mo), weeks in sorted(group_by_calendar_month(weekly).items()):
        if len(weeks) < MIN_WEEKS_FOR_MONTHLY:
            print(f"  Skipping {yr}-M{mo:02d}: only {len(weeks)} weeks (need {MIN_WEEKS_FOR_MONTHLY})")
            continue
        new_days = sum((w["report"].get("period_metadata") or {}).get("days_included") or 0 for w in weeks)
        target = paths["monthly"] / f"monthly_{yr}_M{mo:02d}.json"
        existing_days = existing_rollup_days(target)
        if not should_overwrite_weekly(new_days, existing_days):
            print(f"  Preserving {yr}-M{mo:02d}: existing monthly has {existing_days} days, regenerated would have {new_days}")
            result["preserved"].append(target.name)
            continue
        out = write_global_monthly_report(yr, mo, paths, weeks)
        if out:
            result["monthly"].append(out)
            print(f"  Written: {out.name}")
    print(f"\n{len(result['monthly'])} global monthly reports written.")

    monthly = discover_monthly_reports(paths["monthly"])
    for (yr, q), months in sorted(group_by_quarter(monthly).items()):
        if len(months) < MIN_MONTHS_FOR_QUARTERLY:
            print(f"  Skipping {yr}-Q{q}: only {len(months)} months (need {MIN_MONTHS_FOR_QUARTERLY})")
            continue
        new_days = sum((m["report"].get("period_metadata") or {}).get("days_included") or 0 for m in months)
        target = paths["quarterly"] / f"quarterly_{yr}_Q{q}.json"
        existing_days = existing_rollup_days(target)
        if not should_overwrite_weekly(new_days, existing_days):
            print(f"  Preserving {yr}-Q{q}: existing quarterly has {existing_days} days, regenerated would have {new_days}")
            result["preserved"].append(target.name)
            continue
        out = write_global_quarterly_report(yr, q, paths, months)
        if out:
            result["quarterly"].append(out)
            print(f"  Written: {out.name}")
    print(f"\n{len(result['quarterly'])} global quarterly reports written.")
    return result

def write_global_accumulation_index(paths: dict) -> Path | None:
    """Write context/global/accumulation_index.json summarising every global rollup. Never raises."""
    try:
        periods = {"weekly": [], "monthly": [], "quarterly": []}
        for subdir, pattern in (("weekly", "weekly_*.json"), ("monthly", "monthly_*.json"), ("quarterly", "quarterly_*.json")):
            for f in sorted(paths[subdir].glob(pattern)):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        raise ValueError("not a JSON object")
                except Exception as e:
                    print(f"  ⚠ Skipping {f.name} in global index: {e}")
                    continue
                pm = data.get("period_metadata") or {}
                dq = (data.get("accumulation_metadata") or {}).get("data_quality") or pm.get("data_quality")
                periods[subdir].append({
                    "period_id": pm.get("period_id"),
                    "period_type": subdir,
                    "start_date": pm.get("start_date"),
                    "end_date": pm.get("end_date"),
                    "data_quality": dq,
                    "overall_lean": (data.get("signal_summary") or {}).get("overall_lean"),
                    "signals_available": (data.get("signal_summary") or {}).get("signals_available"),
                    "days_included": pm.get("days_included"),
                    "btc_price_close": (data.get("btc") or {}).get("price_close"),
                    "btc_period_return_pct": (data.get("btc") or {}).get("period_return_pct"),
                    "total_market_cap_change_pct": (data.get("market_structure") or {}).get("total_market_cap_change_pct"),
                    "fear_greed_close": (data.get("sentiment") or {}).get("fear_greed_close"),
                    "filename": f.name,
                })

        all_entries = [e for lst in periods.values() for e in lst]
        starts = [e["start_date"] for e in all_entries if e["start_date"]]
        ends = [e["end_date"] for e in all_entries if e["end_date"]]
        index = {
            "token": "global",
            "scope": "global",
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "script_version": SCRIPT_VERSION,
            "schema_version": GLOBAL_ROLLUP_SCHEMA_VERSION,
            "total_periods": len(all_entries),
            "weekly_count": len(periods["weekly"]),
            "monthly_count": len(periods["monthly"]),
            "quarterly_count": len(periods["quarterly"]),
            "coverage_start": min(starts) if starts else None,
            "coverage_end": max(ends) if ends else None,
            "most_recent_weekly": periods["weekly"][-1] if periods["weekly"] else None,
            "most_recent_monthly": periods["monthly"][-1] if periods["monthly"] else None,
            "most_recent_quarterly": periods["quarterly"][-1] if periods["quarterly"] else None,
            "periods": periods,
        }
        out_path = paths["context_root"] / "accumulation_index.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)
        return out_path
    except Exception as e:
        print(f"  ✗ Failed to write global accumulation index: {e}")
        return None

def _global_file_range(path: Path) -> tuple[str, str] | None:
    """(start_date, end_date) from a rollup file's period_metadata, or None. Never raises."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        pm = data.get("period_metadata")
        if not isinstance(pm, dict):
            return None
        s, e = pm.get("start_date"), pm.get("end_date")
        if not isinstance(s, str) or not isinstance(e, str):
            return None
        return (s, e)
    except Exception:
        return None

def _global_is_covered(rng, covering_ranges) -> bool:
    return any(s <= rng[0] and rng[1] <= e for s, e in covering_ranges)

def select_global_files_to_prune(paths: dict, retain_weekly: int = RETAIN_WEEKLY, retain_monthly: int = RETAIN_MONTHLY) -> dict:
    """Pure selection: weeklies beyond the newest N covered by a monthly, monthlies beyond the newest N covered by a quarterly."""
    weekly_files = sorted(paths["weekly"].glob("weekly_*.json"))
    monthly_files = sorted(paths["monthly"].glob("monthly_*.json"))
    quarterly_files = sorted(paths["quarterly"].glob("quarterly_*.json"))
    monthly_ranges = [r for r in map(_global_file_range, monthly_files) if r]
    quarterly_ranges = [r for r in map(_global_file_range, quarterly_files) if r]

    def pick(files, retain, covering):
        candidates = files[:-retain] if len(files) > retain else []
        out = []
        for c in candidates:
            rng = _global_file_range(c)
            if rng is not None and _global_is_covered(rng, covering):
                out.append(c)
        return out

    return {
        "weekly": pick(weekly_files, retain_weekly, monthly_ranges),
        "monthly": pick(monthly_files, retain_monthly, quarterly_ranges),
    }

def prune_global_accumulated_output(paths: dict, dry_run: bool = False, retain_weekly: int = RETAIN_WEEKLY, retain_monthly: int = RETAIN_MONTHLY) -> dict:
    """Delete (or preview deleting) coverage-gated global rollups locally. Quarterly is never pruned."""
    selection = select_global_files_to_prune(paths, retain_weekly, retain_monthly)
    result = {"weekly_deleted": [], "monthly_deleted": [], "dry_run": dry_run}
    for key in ("weekly", "monthly"):
        for path in selection[key]:
            print(f"  {'Would prune' if dry_run else 'Pruning'}: {path.name}")
            if not dry_run:
                try:
                    path.unlink()
                except OSError as e:
                    print(f"  ⚠ Could not delete {path.name}: {e}")
                    continue
            result[f"{key}_deleted"].append(path.name)
    return result

def main():
    args = parse_args()

    if args.global_prune_dry_run:
        result = prune_global_accumulated_output(setup_directories(GLOBAL_CONFIG), dry_run=True)
        print(f"Global prune preview: {len(result['weekly_deleted'])} weekly, {len(result['monthly_deleted'])} monthly would be pruned (nothing deleted)")
        return

    if args.global_weekly or args.global_rollups:
        if args.global_weekly:
            run_global_weekly()
        if args.global_rollups:
            run_global_rollups()
        index_path = write_global_accumulation_index(setup_directories(GLOBAL_CONFIG))
        print(f"Global accumulation index: {index_path}")
        return

    config_files = sorted(Path("configs").glob("*.json"))
    if not config_files:
        print("No config files found in configs/")
        return

    for config_path in config_files:
        config = load_config(str(config_path))
        print(f"\n{'='*60}")
        print(f"Muneo Market Accumulator v{SCRIPT_VERSION}")
        print(f"Config: {config_path}")
        print(f"Mode: {'REBUILD' if args.rebuild else 'INCREMENTAL'}")
        print(f"\nToken: {config['token_name']}")
        print(f"Output prefix: {config['output_prefix']}")

        # Setup directories
        paths = setup_directories(config)

        # Rebuild mode: clear existing accumulated output
        if args.rebuild:
            clear_accumulated_output(paths)

        # Pull daily reports from GitHub (stateless container support)
        downloaded = pull_reports_from_github(
            token_symbol=config["token_name"],
            output_prefix=config["output_prefix"]
        )
        if downloaded > 0:
            print(f"  Pulled {downloaded} report(s) from GitHub")

        # Discover daily reports
        print(f"\nScanning reports for {config['output_prefix']}*.json ...")
        daily_reports = discover_daily_reports(paths["reports"], config["output_prefix"])
        print(f"  Found {len(daily_reports)} daily reports")

        if not daily_reports:
            print("  No daily reports found. Nothing to accumulate.")
            continue

        # Group by ISO week
        weekly_groups = group_by_iso_week(daily_reports)
        print(f"  Grouped into {len(weekly_groups)} ISO weeks:")
        for (yr, wk), reports in sorted(weekly_groups.items()):
            days = len(reports)
            status = "✓ eligible" if days >= MIN_DAYS_FOR_WEEKLY else f"✗ only {days} days (need {MIN_DAYS_FOR_WEEKLY})"
            print(f"    {yr}-W{wk:02d}: {days} days — {status}")

        print("\nGenerating weekly reports...")
        weekly_written = []
        rolled_up_weeks = set()
        for (yr, wk), reports in sorted(weekly_groups.items()):
            deduped = deduplicate_by_date(reports)
            if len(deduped) < MIN_DAYS_FOR_WEEKLY:
                print(f"  Skipping {yr}-W{wk:02d}: only {len(deduped)} days after dedup")
                continue
            daily_data = [d for d in [load_daily_report(r["path"]) for r in deduped] if d is not None]
            if len(daily_data) < MIN_DAYS_FOR_WEEKLY:
                print(f"  Skipping {yr}-W{wk:02d}: only {len(daily_data)} reports loaded")
                continue
            # Schema 2.0.0 fields — null-safe for pre-upgrade reports
            for report in daily_data:
                tech_sector_lean   = _get_nested(report, "tech_equities", "tech_sector_lean")
                coinbase_premium   = _get_nested(report, "macro", "coinbase_premium_pct")
                etf_net_inflow     = _get_nested(report, "etf_flows", "total_net_inflow_usd")
                # liquidation_map is intraday data — not aggregated, passed through as null
            price_agg = aggregate_weekly_price(daily_data)
            existing, source = fetch_existing_weekly(paths, config, yr, wk)
            existing_days = (existing or {}).get("period_metadata", {}).get("days_included")
            if not should_overwrite_weekly(price_agg["days_included"], existing_days):
                print(f"  Preserving {yr}-W{wk:02d}: existing weekly has {existing_days} days, regenerated would have {price_agg['days_included']}")
                if source == "github":
                    try:
                        preserved_path = paths["weekly"] / f"weekly_{yr}_W{wk:02d}.json"
                        with open(preserved_path, "w", encoding="utf-8") as f:
                            json.dump(existing, f, indent=2)
                    except Exception as e:
                        print(f"  ⚠ Could not copy down existing weekly {yr}-W{wk:02d}: {e}")
                rolled_up_weeks.add((yr, wk))
                continue
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
                rolled_up_weeks.add((yr, wk))
                print(f"  Written: {output_path.name}")
        print(f"\n{len(weekly_written)} weekly reports written.")

        print("\nPushing weekly reports to GitHub before cleanup...")
        confirmed_weeks = push_weekly_files_to_github(paths, config, rolled_up_weeks)
        unconfirmed = rolled_up_weeks - confirmed_weeks
        print(f"  {len(confirmed_weeks)} weekly report(s) confirmed on GitHub.")
        if unconfirmed:
            print(f"  ⚠ {len(unconfirmed)} week(s) not confirmed — their daily reports will NOT be deleted this run")

        # Clean up old daily reports (local + GitHub) now rolled up into weekly reports
        print("\nCleaning up old daily reports...")
        cleanup_result = cleanup_old_daily_reports(daily_reports, confirmed_weeks)
        local_deleted = len(cleanup_result["local"])
        remote_deleted = len(cleanup_result["remote"])
        if local_deleted == 0 and remote_deleted == 0:
            print("  Nothing to clean up.")
        else:
            print(f"  Deleted {local_deleted} local, {remote_deleted} remote daily report(s).")
        if cleanup_result["skipped_no_sha"]:
            print(f"  ⚠ {len(cleanup_result['skipped_no_sha'])} file(s) skipped — could not fetch GitHub sha")

        # Monthly aggregation
        print("\nGenerating monthly reports...")
        weekly_dir = paths["weekly"]
        all_weekly = discover_weekly_reports(weekly_dir)
        monthly_groups = group_by_calendar_month(all_weekly)
        print(f"  Found {len(monthly_groups)} calendar months from {len(all_weekly)} weekly reports")

        monthly_written = []
        for (yr, mo), weeks in sorted(monthly_groups.items()):
            if len(weeks) < MIN_WEEKS_FOR_MONTHLY:
                print(f"  Skipping {yr}-M{mo:02d}: only {len(weeks)} weeks (need {MIN_WEEKS_FOR_MONTHLY})")
                continue
            price_agg = aggregate_monthly_price(weeks)
            technicals_agg = aggregate_monthly_technicals(weeks)
            derivatives_agg = aggregate_monthly_derivatives(weeks)
            on_chain_agg = aggregate_monthly_on_chain(weeks)
            macro_agg = aggregate_monthly_macro(weeks)
            sentiment_agg = aggregate_monthly_sentiment(weeks)
            news_agg = aggregate_monthly_news(weeks)
            signal_summary_agg = aggregate_monthly_signal_summary(weeks)
            accumulation_metadata = aggregate_monthly_accumulation_metadata(weeks)
            cex_dex_spread_agg = aggregate_cex_dex_spread_monthly(weeks)
            weekly_breakdown = build_weekly_breakdown(weeks)
            output_path = write_monthly_report(
                yr, mo, config, paths, weeks,
                price_agg, technicals_agg, derivatives_agg, on_chain_agg,
                macro_agg, sentiment_agg, news_agg, signal_summary_agg,
                accumulation_metadata, cex_dex_spread_agg, weekly_breakdown
            )
            if output_path:
                monthly_written.append(output_path)
                print(f"  Written: {output_path.name}")
        print(f"\n{len(monthly_written)} monthly reports written.")

        # Quarterly aggregation
        print("\nGenerating quarterly reports...")
        monthly_dir = paths["monthly"]
        all_monthly = discover_monthly_reports(monthly_dir)
        quarterly_groups = group_by_quarter(all_monthly)
        print(f"  Found {len(quarterly_groups)} quarters from {len(all_monthly)} monthly reports")

        quarterly_written = []
        for (yr, q), months in sorted(quarterly_groups.items()):
            if len(months) < MIN_MONTHS_FOR_QUARTERLY:
                print(f"  Skipping {yr}-Q{q}: only {len(months)} months (need {MIN_MONTHS_FOR_QUARTERLY})")
                continue
            price_agg = aggregate_quarterly_price(months)
            technicals_agg = aggregate_quarterly_technicals(months)
            derivatives_agg = aggregate_quarterly_derivatives(months)
            on_chain_agg = aggregate_quarterly_on_chain(months)
            macro_agg = aggregate_quarterly_macro(months)
            sentiment_agg = aggregate_quarterly_sentiment(months)
            news_agg = aggregate_quarterly_news(months)
            signal_summary_agg = aggregate_quarterly_signal_summary(months)
            accumulation_metadata = aggregate_quarterly_accumulation_metadata(months)
            cex_dex_spread_agg = aggregate_cex_dex_spread_quarterly(months)
            monthly_breakdown = build_monthly_breakdown(months)
            output_path = write_quarterly_report(
                yr, q, config, paths, months,
                price_agg, technicals_agg, derivatives_agg, on_chain_agg,
                macro_agg, sentiment_agg, news_agg, signal_summary_agg,
                accumulation_metadata, cex_dex_spread_agg, monthly_breakdown
            )
            if output_path:
                quarterly_written.append(output_path)
                print(f"  Written: {output_path.name}")
        print(f"\n{len(quarterly_written)} quarterly reports written.")

        # Prune old accumulated files
        print("\nPruning old accumulated files...")
        pruned = prune_accumulated_output(paths, config)
        weekly_pruned = len(pruned["weekly"])
        monthly_pruned = len(pruned["monthly"])
        if weekly_pruned == 0 and monthly_pruned == 0:
            print("  Nothing to prune.")
        else:
            print(f"  Pruned {weekly_pruned} weekly, {monthly_pruned} monthly files.")

        # Write accumulation index
        print("\nWriting accumulation index...")
        index_path = write_accumulation_index(config, paths)
        if index_path:
            print(f"  Written: {index_path.name}")
        else:
            print("  WARNING: accumulation index failed to write")

        # Push accumulated output to GitHub
        print("\nPushing accumulated output to GitHub...")
        push_accumulated_to_github(paths, config)

        print("Accumulator run complete.")

if __name__ == "__main__":
    main()