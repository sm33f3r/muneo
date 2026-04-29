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
    
    print("\nStep 1 complete — file discovery and grouping done.")
    print("(Aggregation logic comes in later steps.)")

if __name__ == "__main__":
    main()