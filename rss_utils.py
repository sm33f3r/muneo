"""
rss_utils.py — Shared RSS feed fetching and sentiment scoring utilities for Muneo.

Exports:
    CRYPTO_RSS_FEEDS   — verified list of crypto news RSS feed URLs
    fetch_rss_feeds()  — fetch, normalise, deduplicate, and age-filter RSS articles
    score_sentiment()  — keyword-based headline sentiment classifier
"""

# CHANGELOG
# 2026-04-29 — Created. Replaces APITube (APITUBE_API_KEY) in market_report.py
#              and market_global_report.py. APITube key is now deprecated.

import urllib.request
import urllib.error
import urllib.parse
import feedparser
import datetime
import time
import calendar
import re
import sys
from typing import List, Dict, Any, Optional
from datetime import timezone

CRYPTO_RSS_FEEDS: list[str] = [
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.theblock.co/rss.xml",
    "https://cryptoslate.com/feed/",
    "https://bitcoinist.com/feed/",
]


def fetch_rss_feeds(
    feed_urls: list,
    max_age_hours: int = 24,
    user_agent: str = "Muneo/1.0 RSS reader",
    timeout_seconds: int = 10,
) -> list:
    """
    Fetch RSS feeds, normalise articles, deduplicate, and filter by age.

    Args:
        feed_urls: List of RSS feed URLs to fetch
        max_age_hours: Maximum age of articles to include (default: 24)
        user_agent: User-Agent header for HTTP requests
        timeout_seconds: Timeout for HTTP requests

    Returns:
        List of article dicts, sorted by published_at descending (newest first)
    """
    all_articles = []
    now = datetime.datetime.now(timezone.utc)
    max_age = datetime.timedelta(hours=max_age_hours)

    for url in feed_urls:
        try:
            # Build request with custom User-Agent
            req = urllib.request.Request(
                url,
                headers={'User-Agent': user_agent}
            )
            response = urllib.request.urlopen(req, timeout=timeout_seconds)
            feed = feedparser.parse(response.read())

            # Check for parse errors
            if feed.bozo:
                sys.stderr.write(f"WARNING: Failed to parse feed {url} (parse error)\n")
                continue

            # Get source name
            source = ""
            if hasattr(feed, 'feed') and feed.feed.get('title'):
                source = feed.feed.title
            else:
                # Fallback to netloc from URL
                parsed_url = urllib.parse.urlparse(url)
                source = parsed_url.netloc

            # Process entries
            entries = feed.entries if hasattr(feed, 'entries') else []

            for entry in entries:
                # Extract date
                time_struct = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    time_struct = entry.published_parsed
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    time_struct = entry.updated_parsed

                if not time_struct:
                    continue  # Skip entries without dates

                # Convert to UTC-aware datetime
                published_at = datetime.datetime.fromtimestamp(
                    calendar.timegm(time_struct),
                    tz=timezone.utc
                )

                # Skip entries older than max_age_hours
                if now - published_at > max_age:
                    continue

                # Extract and clean title
                title = ""
                if hasattr(entry, 'title') and entry.title:
                    title = re.sub(r'<[^>]+>', '', entry.title).strip()

                # Extract and clean summary
                summary = ""
                if hasattr(entry, 'summary') and entry.summary:
                    summary = re.sub(r'<[^>]+>', '', entry.summary).strip()
                    if len(summary) > 300:
                        summary = summary[:297] + "..."

                # Extract URL
                url_entry = ""
                if hasattr(entry, 'link') and entry.link:
                    url_entry = entry.link

                if not title or not url_entry:
                    continue  # Skip invalid entries

                article = {
                    "title": title,
                    "url": url_entry,
                    "published_at": published_at.isoformat(),
                    "source": source,
                    "summary": summary,
                }

                all_articles.append(article)

        except urllib.error.HTTPError as e:
            sys.stderr.write(f"WARNING: HTTP error {e.code} fetching feed {url}\n")
        except urllib.error.URLError as e:
            sys.stderr.write(f"WARNING: URL error {e.reason} fetching feed {url}\n")
        except Exception as e:
            sys.stderr.write(f"WARNING: Error fetching feed {url}: {str(e)}\n")

    # Deduplicate by URL, keeping most recent
    deduplicated = {}
    for article in all_articles:
        url = article["url"]
        published_at = datetime.datetime.fromisoformat(article["published_at"])

        if url not in deduplicated:
            deduplicated[url] = article
        else:
            # Keep the more recent article
            existing_published_at = datetime.datetime.fromisoformat(
                deduplicated[url]["published_at"]
            )
            if published_at > existing_published_at:
                deduplicated[url] = article

    # Convert back to list and sort by published_at descending
    result = list(deduplicated.values())
    result.sort(
        key=lambda x: datetime.datetime.fromisoformat(x["published_at"]),
        reverse=True
    )

    return result


def score_sentiment(title: str) -> dict:
    """
    Classify headline sentiment as positive, negative, or neutral by keyword matching.

    Args:
        title: Headline text to analyze

    Returns:
        Dict with keys:
            polarity: "positive", "negative", or "neutral"
            score: float between -1.0 and 1.0
    """
    # Bulish and bearish keywords
    bullish_keywords = [
        "rally", "surge", "gain", "bull", "pump", "soar", "breakout",
        "recover", "adoption", "launch", "partnership", "upgrade",
        "milestone", "record", "high", "approval", "etf", "institutional"
    ]

    bearish_keywords = [
        "crash", "dump", "bear", "drop", "fall", "plunge", "hack",
        "exploit", "ban", "lawsuit", "fraud", "liquidat", "rekt",
        "fear", "panic", "sell", "concern", "warning", "investigation"
    ]

    title_lower = title.lower()

    # Count hits
    bullish_hits = 0
    bearish_hits = 0

    for keyword in bullish_keywords:
        if keyword in title_lower:
            bullish_hits += 1

    for keyword in bearish_keywords:
        if keyword in title_lower:
            bearish_hits += 1

    # Determine polarity
    if bullish_hits > bearish_hits:
        polarity = "positive"
    elif bearish_hits > bullish_hits:
        polarity = "negative"
    else:
        polarity = "neutral"

    # Compute score: (bullish_hits - bearish_hits) clamped to -1.0..1.0
    raw_score = bullish_hits - bearish_hits
    # Simple clamping - if difference is > 1 or < -1, set to ±1.0
    if raw_score > 1:
        score = 1.0
    elif raw_score < -1:
        score = -1.0
    elif raw_score == 0:
        score = 0.0
    else:
        # For differences of ±1, score is ±1.0 (or could be ±0.5, but spec says clamped to -1.0..1.0)
        score = float(raw_score)

    return {"polarity": polarity, "score": score}