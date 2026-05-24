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

def write_weekly_report(iso_year, iso_week, config, paths, daily_reports, price_agg):
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
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC")
        },
        "price": price_agg,
        "technicals": None,
        "derivatives": None,
        "on_chain": None,
        "macro": None,
        "sentiment": None,
        "news": None,
        "signal_summary": None,
        "data_gaps": []
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
        output_path = write_weekly_report(yr, wk, config, paths, deduped, price_agg)
        if output_path:
            weekly_written.append(output_path)
            print(f"  Written: {output_path.name}")
    print(f"\n{len(weekly_written)} weekly reports written.")

    print("Accumulator run complete.")

if __name__ == "__main__":
    main()