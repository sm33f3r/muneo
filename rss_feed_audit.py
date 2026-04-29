#!/usr/bin/env python3
"""
One-time RSS feed audit for the Muneo market analysis stack.
Fetches candidate RSS feeds, classifies them, and writes structured results.
"""

import urllib.request
import urllib.error
import json
import datetime
import time
import feedparser
from typing import Dict, List, Any, Optional
import sys

FEEDS = {
    "macro_world_news": [
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "https://feeds.skynews.com/feeds/rss/world.xml",
        "https://www.france24.com/en/rss",
        "https://en.yna.co.kr/RSS/economy-finance.xml",
        "https://feeds.npr.org/1017/rss.xml",
        "https://www.theguardian.com/economy/rss",
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.ft.com/?format=rss",
    ],
    "tech_general_news": [
        "https://www.independent.co.uk/news/technology/rss",
        "https://www.engadget.com/rss.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    ],
    "crypto_news": [
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.theblock.co/rss.xml",
        "https://cryptoslate.com/feed/",
        "https://bitcoinist.com/feed/",
    ],
    "ai_tech_deep": [
        "https://venturebeat.com/category/ai/feed/",
        "https://www.technologyreview.com/feed/",
        "https://thenextweb.com/feed/",
    ],
}


def fetch_and_parse_feed(url: str) -> Dict[str, Any]:
    """Fetch RSS feed and parse with feedparser."""
    result = {
        "group": None,
        "url": url,
        "status": "error",
        "error_reason": None,
        "entry_count": 0,
        "most_recent_entry_age_hours": None,
        "feed_title": None,
    }

    try:
        # Fetch with custom User-Agent
        req = urllib.request.Request(
            url,
            headers={'User-Agent': 'Muneo/1.0 RSS audit bot'}
        )
        response = urllib.request.urlopen(req, timeout=10)

        # Check HTTP status
        status = response.getcode()
        if status in (401, 403, 404):
            result["error_reason"] = f"HTTP {status}"
            return result

        # Parse feed
        feed_data = feedparser.parse(response.read())

        # Store basic info
        result["feed_title"] = feed_data.feed.get('title') if hasattr(feed_data, 'feed') else None

        # Check for parse errors
        if feed_data.bozo:
            result["error_reason"] = "Parse error"
            return result

        # Count entries
        entries = feed_data.entries if hasattr(feed_data, 'entries') else []
        result["entry_count"] = len(entries)

        # Check if empty
        if result["entry_count"] == 0:
            result["status"] = "empty"
            return result

        # Find most recent entry
        most_recent = None
        has_recent_entry = False

        for entry in entries:
            # Try published_parsed first, then updated_parsed
            entry_time = None
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                entry_time = datetime.datetime(*entry.published_parsed[:6])
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                entry_time = datetime.datetime(*entry.updated_parsed[:6])

            if entry_time:
                if not most_recent or entry_time > most_recent:
                    most_recent = entry_time

        # Calculate age if we found timestamps
        if most_recent:
            now = datetime.datetime.utcnow()
            age_hours = (now - most_recent).total_seconds() / 3600
            result["most_recent_entry_age_hours"] = round(age_hours, 2)

            # Check if any entry is within 48 hours
            if age_hours <= 48:
                has_recent_entry = True

        # Classify feed
        if has_recent_entry:
            result["status"] = "pass"
        elif most_recent:
            result["status"] = "stale"
        else:
            # No parseable dates at all
            result["status"] = "stale"

    except urllib.error.HTTPError as e:
        result["error_reason"] = f"HTTP {e.code}"
    except urllib.error.URLError as e:
        result["error_reason"] = str(e.reason) if e.reason else "URL error"
    except Exception as e:
        result["error_reason"] = str(e)

    return result


def main():
    """Main audit function."""
    print(f"Starting RSS feed audit for {sum(len(urls) for urls in FEEDS.values())} feeds...")

    results = []
    total_count = 0

    # Process all feeds
    for group_name, urls in FEEDS.items():
        for url in urls:
            print(f"  Processing {url[:60]}...", end="", flush=True)
            result = fetch_and_parse_feed(url)
            result["group"] = group_name
            results.append(result)
            total_count += 1
            print(f" {result['status']}")

    # Calculate counts
    pass_count = sum(1 for r in results if r["status"] == "pass")
    stale_count = sum(1 for r in results if r["status"] == "stale")
    empty_count = sum(1 for r in results if r["status"] == "empty")
    error_count = sum(1 for r in results if r["status"] == "error")

    # Create output data
    output = {
        "audit_timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "total_feeds": total_count,
        "pass_count": pass_count,
        "stale_count": stale_count,
        "empty_count": empty_count,
        "error_count": error_count,
        "results": results,
    }

    # Write JSON file
    output_file = "rss_feed_audit_results.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    # Print summary table
    print("\n" + "="*100)
    print(f"{'GROUP':20} {'URL':50} {'STATUS':8} {'ENTRIES':8} {'AGE_H':6} {'ERROR':20}")
    print("-"*100)

    for result in results:
        group = result["group"][:20]
        url = result["url"][:50]
        status = result["status"][:8]
        entries = str(result["entry_count"])[:8]
        age_h = str(result["most_recent_entry_age_hours"] or "-")[:6]
        error = (result["error_reason"] or "-")[:20]

        print(f"{group:20} {url:50} {status:8} {entries:8} {age_h:6} {error:20}")

    print("="*100)
    print(f"Audit complete. Results written to {output_file}")
    print(f"Summary: {pass_count} pass, {stale_count} stale, {empty_count} empty, {error_count} error")


if __name__ == "__main__":
    main()