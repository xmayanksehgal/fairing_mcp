import os
import re
from datetime import datetime, timezone
from collections import defaultdict
import statistics
import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Fairing")

FAIRING_API_KEY = os.environ.get("FAIRING_API_KEY")
BASE_URL = "https://app.fairing.co/api"
HEADERS = {"Authorization": FAIRING_API_KEY, "Accept": "application/json"}

MAIN_QUESTION_ID = 32778
YOUTUBE_CLARIFICATION_QUESTION_ID = 145964
PODCAST_CLARIFICATION_QUESTION_ID = 145963
INSTAGRAM_CLARIFICATION_QUESTION_ID = int(os.environ.get("INSTAGRAM_CLARIFICATION_QUESTION_ID", "146913"))

# Response rates — update these based on your actual data.
# main_question_rate: % of all orders that answer the main "how did you hear" question
# youtube_clarification_rate: % of main-question YouTube respondents who answer the clarification
# podcast_clarification_rate: same but for podcast
# instagram_clarification_rate: same but for Instagram
RESPONSE_RATES = {
    "main_question": float(os.environ.get("MAIN_QUESTION_RESPONSE_RATE", "0.33")),
    "youtube_clarification": float(os.environ.get("YOUTUBE_CLARIFICATION_RESPONSE_RATE", "0.55")),
    "podcast_clarification": float(os.environ.get("PODCAST_CLARIFICATION_RESPONSE_RATE", "0.80")),
    "instagram_clarification": float(os.environ.get("INSTAGRAM_CLARIFICATION_RESPONSE_RATE", "0.80")),
}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_rates(overrides: dict = None) -> dict:
    """
    Merge env-var defaults with any per-call overrides.
    Overrides dict keys: main_question, youtube_clarification,
    podcast_clarification, instagram_clarification.
    """
    rates = dict(RESPONSE_RATES)
    if overrides:
        for k, v in overrides.items():
            if k in rates and v is not None:
                rates[k] = float(v)
    return rates


def _compound_rate(rates: dict, *keys: str) -> float:
    result = 1.0
    for k in keys:
        result *= rates[k]
    return result


def _extrapolate(value: float, compound: float) -> float:
    return value / compound if compound > 0 else value


def fetch_all_responses(
    question_id: int,
    after_date: str = None,
    before_date: str = None,
    debug: bool = False,
) -> tuple[list, dict]:
    """
    Paginate through all responses with optional date filtering.

    Returns:
        (responses, fetch_meta) where fetch_meta contains pagination debug info
        when debug=True, otherwise an empty dict.
    """
    all_responses = []
    url = f"{BASE_URL}/responses"
    params = {"limit": 100, "question_id": question_id}

    after_dt = datetime.fromisoformat(after_date).replace(tzinfo=timezone.utc) if after_date else None
    before_dt = datetime.fromisoformat(before_date).replace(tzinfo=timezone.utc) if before_date else None

    pages_fetched = 0
    api_calls = 0
    earliest_ts = None
    latest_ts = None

    while url:
        resp = requests.get(url, headers=HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()
        api_calls += 1
        rows = data.get("data", [])
        if not rows:
            break

        pages_fetched += 1
        for r in rows:
            ts_str = r.get("inserted_at") or r.get("response_provided_at")
            if ts_str:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if after_dt and dt < after_dt:
                    # Results are in reverse-chron order; once we're before the window, stop.
                    url = None
                    break
                if before_dt and dt > before_dt:
                    continue
                if debug:
                    if earliest_ts is None or dt < earliest_ts:
                        earliest_ts = dt
                    if latest_ts is None or dt > latest_ts:
                        latest_ts = dt
            all_responses.append(r)

        if url is None:
            break

        next_url = data.get("next")
        url = next_url if next_url else None
        params = {}

    fetch_meta = {}
    if debug:
        fetch_meta = {
            "pages_fetched": pages_fetched,
            "api_calls_made": api_calls,
            "total_records_returned": len(all_responses),
            "earliest_response_at": earliest_ts.isoformat() if earliest_ts else None,
            "latest_response_at": latest_ts.isoformat() if latest_ts else None,
        }

    return all_responses, fetch_meta


def _aov_stats(all_revenues: list[float], channel_aov: float) -> dict:
    """Return z-score and percentile rank of channel_aov vs population."""
    if len(all_revenues) < 2:
        return {}
    mean = statistics.mean(all_revenues)
    stdev = statistics.stdev(all_revenues)
    z = (channel_aov - mean) / stdev if stdev else 0.0
    percentile = sum(1 for v in all_revenues if v <= channel_aov) / len(all_revenues) * 100
    return {
        "population_mean_aov": round(mean, 2),
        "population_stdev_aov": round(stdev, 2),
        "z_score": round(z, 2),
        "percentile": round(percentile, 1),
    }


# ---------------------------------------------------------------------------
# Channel matching helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """
    Lowercase, strip non-alphanumeric characters, and collapse whitespace.
    e.g. "Marine X" -> "marinex", "Gideon's Tactical" -> "gideonstactical"
    """
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _matches_channel(query: str, response_text: str) -> bool:
    """
    Returns True if response_text matches the query via either:
      1. Exact substring match (case-insensitive) — preserves original behavior
      2. Normalized fuzzy match — strips spaces/punctuation before comparing

    This catches misspellings like "MarineX" / "MARINEX" / "Marine x"
    when the query is "Marine X".
    """
    query_lower = query.lower()
    response_lower = response_text.lower()

    # 1. Exact substring (original behavior)
    if query_lower in response_lower:
        return True

    # 2. Normalized match: strip all non-alphanumeric chars from both sides
    query_norm = _normalize(query)
    response_norm = _normalize(response_text)
    if query_norm and query_norm in response_norm:
        return True

    return False


# ---------------------------------------------------------------------------
# Tool: get_other_responses
# ---------------------------------------------------------------------------

@mcp.tool()
def get_other_responses(
    after_date: str = None,
    before_date: str = None,
    question: str = "main",
    debug: bool = False,
) -> dict:
    """
    Return full free-text "Other" responses so you can identify channels
    that respondents described but didn't match a preset answer option.

    Args:
        after_date: ISO date string, e.g. "2025-08-22"
        before_date: ISO date string, e.g. "2026-02-23"
        question: Which question to inspect — "main", "youtube", "podcast", or "instagram"
        debug: If True, include pagination metadata
    """
    question_map = {
        "main": MAIN_QUESTION_ID,
        "youtube": YOUTUBE_CLARIFICATION_QUESTION_ID,
        "podcast": PODCAST_CLARIFICATION_QUESTION_ID,
        "instagram": INSTAGRAM_CLARIFICATION_QUESTION_ID,
    }
    qid = question_map.get(question)
    if not qid:
        return {"error": f"Unknown question '{question}'. Use: main, youtube, podcast, instagram"}

    responses, fetch_meta = fetch_all_responses(qid, after_date, before_date, debug=debug)

    # Collect records where the response is "Other" or where other_response is populated
    other_records = [
        r for r in responses
        if (r.get("response") or "").strip().lower() == "other"
        or r.get("other_response")
    ]

    # Aggregate free-text values
    text_counts: dict[str, dict] = defaultdict(lambda: {"count": 0, "revenue": 0.0})
    for r in other_records:
        text = (r.get("other_response") or r.get("response") or "").strip()
        if not text or text.lower() == "other":
            text = "(blank / unparseable)"
        text_counts[text]["count"] += 1
        text_counts[text]["revenue"] += float(r.get("order_total") or 0)

    sorted_entries = sorted(text_counts.items(), key=lambda x: x[1]["count"], reverse=True)

    result = {
        "question": question,
        "period": {"after": after_date, "before": before_date},
        "total_other_responses": len(other_records),
        "unique_values": len(sorted_entries),
        "entries": [
            {
                "text": text,
                "count": stats["count"],
                "raw_attributed_revenue": round(stats["revenue"], 2),
                "avg_order_value": round(stats["revenue"] / stats["count"], 2) if stats["count"] else 0,
            }
            for text, stats in sorted_entries
        ],
    }
    if debug:
        result["fetch_meta"] = fetch_meta
    return result


# ---------------------------------------------------------------------------
# Tool: get_attribution_overview
# ---------------------------------------------------------------------------

@mcp.tool()
def get_attribution_overview(
    after_date: str = None,
    before_date: str = None,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Get a high-level breakdown of all discovery channels with mention counts,
    attributed revenue, and extrapolated estimates.

    Args:
        after_date: ISO date string
        before_date: ISO date string
        rate_overrides: Optional dict to override response rates for this call only.
            Keys: main_question, youtube_clarification, podcast_clarification, instagram_clarification.
            Example: {"main_question": 0.25}
        debug: If True, include pagination metadata
    """
    rates = _resolve_rates(rate_overrides)
    main_rate = rates["main_question"]

    responses, fetch_meta = fetch_all_responses(MAIN_QUESTION_ID, after_date, before_date, debug=debug)

    channel_stats = defaultdict(lambda: {"mentions": 0, "revenue": 0.0})
    for r in responses:
        channel = r.get("response") or "Unknown"
        channel_stats[channel]["mentions"] += 1
        channel_stats[channel]["revenue"] += float(r.get("order_total") or 0)

    total_raw = len(responses)
    total_extrap = round(total_raw / main_rate, 1)
    sorted_stats = sorted(channel_stats.items(), key=lambda x: x[1]["mentions"], reverse=True)

    result = {
        "period": {"after": after_date, "before": before_date},
        "response_rates_used": {"main_question": main_rate},
        "total_responses": total_raw,
        "estimated_total_orders": total_extrap,
        "channels": [
            {
                "channel": name,
                "raw_mentions": stats["mentions"],
                "share_pct": round(stats["mentions"] / total_raw * 100, 1) if total_raw else 0,
                "raw_attributed_revenue": round(stats["revenue"], 2),
                "extrapolated_mentions": round(stats["mentions"] / main_rate, 1),
                "extrapolated_revenue": round(stats["revenue"] / main_rate, 2),
                "avg_order_value": round(stats["revenue"] / stats["mentions"], 2) if stats["mentions"] else 0,
            }
            for name, stats in sorted_stats
        ],
    }
    if debug:
        result["fetch_meta"] = fetch_meta
    return result


# ---------------------------------------------------------------------------
# Tool: rank_youtube_channels
# ---------------------------------------------------------------------------

@mcp.tool()
def rank_youtube_channels(
    after_date: str = None,
    before_date: str = None,
    rank_by: str = "mentions",
    top_n: int = 15,
    include_monthly_trend: bool = False,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Rank all YouTube channels by number of mentions or attributed revenue.

    Args:
        rank_by: "mentions", "revenue", "extrapolated_mentions", or "extrapolated_revenue"
        include_monthly_trend: If True, each channel entry includes a by_month breakdown
        rate_overrides: Optional dict to override response rates for this call only
        debug: If True, include pagination metadata
    """
    rates = _resolve_rates(rate_overrides)
    compound = _compound_rate(rates, "main_question", "youtube_clarification")

    responses, fetch_meta = fetch_all_responses(
        YOUTUBE_CLARIFICATION_QUESTION_ID, after_date, before_date, debug=debug
    )

    channel_stats: dict[str, dict] = defaultdict(lambda: {"mentions": 0, "revenue": 0.0, "by_month": defaultdict(lambda: {"mentions": 0, "revenue": 0.0})})
    all_aovs = []

    for r in responses:
        channel = r.get("response") or r.get("other_response") or "Unknown"
        order_total = float(r.get("order_total") or 0)
        channel_stats[channel]["mentions"] += 1
        channel_stats[channel]["revenue"] += order_total
        if order_total:
            all_aovs.append(order_total)
        if include_monthly_trend:
            month = (r.get("inserted_at") or "")[:7]
            channel_stats[channel]["by_month"][month]["mentions"] += 1
            channel_stats[channel]["by_month"][month]["revenue"] += order_total

    def sort_key(item):
        s = item[1]
        if rank_by in ("extrapolated_mentions", "mentions"):
            return s["mentions"]
        return s["revenue"]

    sorted_channels = sorted(channel_stats.items(), key=sort_key, reverse=True)[:top_n]

    rankings = []
    for i, (name, stats) in enumerate(sorted_channels):
        raw_mentions = stats["mentions"]
        raw_revenue = stats["revenue"]
        channel_aov = round(raw_revenue / raw_mentions, 2) if raw_mentions else 0

        entry = {
            "rank": i + 1,
            "channel": name,
            "raw_mentions": raw_mentions,
            "raw_attributed_revenue": round(raw_revenue, 2),
            "extrapolated_mentions": round(raw_mentions / compound, 1),
            "extrapolated_revenue": round(raw_revenue / compound, 2),
            "avg_order_value": channel_aov,
            "aov_stats": _aov_stats(all_aovs, channel_aov),
        }

        if include_monthly_trend:
            entry["by_month"] = {
                month: {
                    "raw_mentions": v["mentions"],
                    "raw_revenue": round(v["revenue"], 2),
                    "extrapolated_mentions": round(v["mentions"] / compound, 1),
                    "extrapolated_revenue": round(v["revenue"] / compound, 2),
                }
                for month, v in sorted(stats["by_month"].items())
            }

        rankings.append(entry)

    result = {
        "period": {"after": after_date, "before": before_date},
        "ranked_by": rank_by,
        "response_rates_used": {
            "main_question": rates["main_question"],
            "youtube_clarification": rates["youtube_clarification"],
            "compound": round(compound, 4),
        },
        "total_youtube_responses": len(responses),
        "estimated_total_youtube_orders": round(len(responses) / compound, 1),
        "rankings": rankings,
    }
    if debug:
        result["fetch_meta"] = fetch_meta
    return result


# ---------------------------------------------------------------------------
# Tool: get_channel_performance  (YouTube)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_channel_performance(
    channel_name: str,
    after_date: str = None,
    before_date: str = None,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Get performance stats for a specific YouTube channel.

    Searches both the YouTube clarification question (compound rate) AND the
    main question "Other" free-text responses (main question rate only), then
    combines them for a complete attribution picture.

    Args:
        channel_name: Channel name to search for (case-insensitive, partial match)
        after_date: ISO date string
        before_date: ISO date string
        rate_overrides: Optional dict to override response rates for this call only
        debug: If True, include pagination metadata
    """
    rates = _resolve_rates(rate_overrides)
    compound = _compound_rate(rates, "main_question", "youtube_clarification")
    main_rate = rates["main_question"]

    # --- Source 1: YouTube clarification question ---
    yt_responses, yt_meta = fetch_all_responses(
        YOUTUBE_CLARIFICATION_QUESTION_ID, after_date, before_date, debug=debug
    )
    all_aovs = [float(r["order_total"]) for r in yt_responses if r.get("order_total")]
    yt_matches = [
        r for r in yt_responses
        if _matches_channel(channel_name, r.get("response") or "")
        or _matches_channel(channel_name, r.get("other_response") or "")
    ]

    # --- Source 2: Main question "Other" free-text ---
    main_responses, main_meta = fetch_all_responses(
        MAIN_QUESTION_ID, after_date, before_date, debug=debug
    )
    main_other_matches = [
        r for r in main_responses
        if (
            (r.get("response") or "").strip().lower() == "other"
            or r.get("other_response")
        ) and (
            _matches_channel(channel_name, r.get("other_response") or "")
            or _matches_channel(channel_name, r.get("response") or "")
        )
    ]

    if not yt_matches and not main_other_matches:
        return {
            "channel": channel_name,
            "mentions": 0,
            "message": "No responses found for this channel in the given period.",
        }

    # --- Compute per-source stats ---
    def _source_stats(matches, rate):
        raw_rev = sum(float(r["order_total"]) for r in matches if r.get("order_total"))
        raw_m = len(matches)
        aov = round(raw_rev / raw_m, 2) if raw_m else 0
        return {
            "raw_mentions": raw_m,
            "raw_revenue": round(raw_rev, 2),
            "avg_order_value": aov,
            "extrapolated_mentions": round(raw_m / rate, 1) if rate else 0,
            "extrapolated_revenue": round(raw_rev / rate, 2) if rate else 0,
        }

    yt_stats = _source_stats(yt_matches, compound)
    main_stats = _source_stats(main_other_matches, main_rate)

    # --- Combined totals ---
    total_raw_mentions = yt_stats["raw_mentions"] + main_stats["raw_mentions"]
    total_raw_revenue = yt_stats["raw_revenue"] + main_stats["raw_revenue"]
    total_ext_mentions = yt_stats["extrapolated_mentions"] + main_stats["extrapolated_mentions"]
    total_ext_revenue = yt_stats["extrapolated_revenue"] + main_stats["extrapolated_revenue"]
    total_aov = round(total_raw_revenue / total_raw_mentions, 2) if total_raw_mentions else 0

    # --- by_month: combine both sources (each extrapolated with its own rate) ---
    by_month = defaultdict(lambda: {"raw_m": 0, "raw_rev": 0.0, "ext_m": 0.0, "ext_rev": 0.0})
    for r in yt_matches:
        month = (r.get("inserted_at") or "")[:7]
        by_month[month]["raw_m"] += 1
        rev = float(r.get("order_total") or 0)
        by_month[month]["raw_rev"] += rev
        by_month[month]["ext_m"] += 1 / compound if compound else 0
        by_month[month]["ext_rev"] += rev / compound if compound else 0
    for r in main_other_matches:
        month = (r.get("inserted_at") or "")[:7]
        by_month[month]["raw_m"] += 1
        rev = float(r.get("order_total") or 0)
        by_month[month]["raw_rev"] += rev
        by_month[month]["ext_m"] += 1 / main_rate if main_rate else 0
        by_month[month]["ext_rev"] += rev / main_rate if main_rate else 0

    result = {
        "channel": channel_name,
        "period": {"after": after_date, "before": before_date},
        "response_rates_used": {
            "main_question": main_rate,
            "youtube_clarification": rates["youtube_clarification"],
            "compound_yt": round(compound, 4),
        },
        "raw": {
            "mentions": total_raw_mentions,
            "attributed_revenue": round(total_raw_revenue, 2),
            "avg_order_value": total_aov,
        },
        "extrapolated": {
            "estimated_total_orders": round(total_ext_mentions, 1),
            "estimated_total_revenue": round(total_ext_revenue, 2),
            "avg_order_value": total_aov,
        },
        "sources": {
            "youtube_clarification": yt_stats,
            "main_question_other": main_stats,
        },
        "aov_stats": _aov_stats(all_aovs, total_aov),
        "by_month": {
            month: {
                "raw_mentions": v["raw_m"],
                "raw_revenue": round(v["raw_rev"], 2),
                "extrapolated_mentions": round(v["ext_m"], 1),
                "extrapolated_revenue": round(v["ext_rev"], 2),
            }
            for month, v in sorted(by_month.items())
        },
        "sample_responses": [
            r.get("response") or r.get("other_response") for r in (yt_matches + main_other_matches)[:5]
        ],
    }
    if debug:
        result["fetch_meta"] = {"youtube_clarification": yt_meta, "main_question": main_meta}
    return result


# ---------------------------------------------------------------------------
# Tool: rank_podcast_channels
# ---------------------------------------------------------------------------

@mcp.tool()
def rank_podcast_channels(
    after_date: str = None,
    before_date: str = None,
    rank_by: str = "mentions",
    top_n: int = 15,
    include_monthly_trend: bool = False,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Rank all podcasts by number of mentions or attributed revenue.

    Args:
        rank_by: "mentions", "revenue", "extrapolated_mentions", or "extrapolated_revenue"
        include_monthly_trend: If True, each entry includes a by_month breakdown
        rate_overrides: Optional dict to override response rates for this call only
        debug: If True, include pagination metadata
    """
    rates = _resolve_rates(rate_overrides)
    compound = _compound_rate(rates, "main_question", "podcast_clarification")

    responses, fetch_meta = fetch_all_responses(
        PODCAST_CLARIFICATION_QUESTION_ID, after_date, before_date, debug=debug
    )

    channel_stats: dict[str, dict] = defaultdict(lambda: {"mentions": 0, "revenue": 0.0, "by_month": defaultdict(lambda: {"mentions": 0, "revenue": 0.0})})
    all_aovs = []

    for r in responses:
        channel = r.get("response") or r.get("other_response") or "Unknown"
        order_total = float(r.get("order_total") or 0)
        channel_stats[channel]["mentions"] += 1
        channel_stats[channel]["revenue"] += order_total
        if order_total:
            all_aovs.append(order_total)
        if include_monthly_trend:
            month = (r.get("inserted_at") or "")[:7]
            channel_stats[channel]["by_month"][month]["mentions"] += 1
            channel_stats[channel]["by_month"][month]["revenue"] += order_total

    def sort_key(item):
        s = item[1]
        if rank_by in ("extrapolated_mentions", "mentions"):
            return s["mentions"]
        return s["revenue"]

    sorted_channels = sorted(channel_stats.items(), key=sort_key, reverse=True)[:top_n]

    rankings = []
    for i, (name, stats) in enumerate(sorted_channels):
        raw_mentions = stats["mentions"]
        raw_revenue = stats["revenue"]
        channel_aov = round(raw_revenue / raw_mentions, 2) if raw_mentions else 0

        entry = {
            "rank": i + 1,
            "channel": name,
            "raw_mentions": raw_mentions,
            "raw_attributed_revenue": round(raw_revenue, 2),
            "extrapolated_mentions": round(raw_mentions / compound, 1),
            "extrapolated_revenue": round(raw_revenue / compound, 2),
            "avg_order_value": channel_aov,
            "aov_stats": _aov_stats(all_aovs, channel_aov),
        }

        if include_monthly_trend:
            entry["by_month"] = {
                month: {
                    "raw_mentions": v["mentions"],
                    "raw_revenue": round(v["revenue"], 2),
                    "extrapolated_mentions": round(v["mentions"] / compound, 1),
                    "extrapolated_revenue": round(v["revenue"] / compound, 2),
                }
                for month, v in sorted(stats["by_month"].items())
            }

        rankings.append(entry)

    result = {
        "period": {"after": after_date, "before": before_date},
        "ranked_by": rank_by,
        "response_rates_used": {
            "main_question": rates["main_question"],
            "podcast_clarification": rates["podcast_clarification"],
            "compound": round(compound, 4),
        },
        "total_podcast_responses": len(responses),
        "estimated_total_podcast_orders": round(len(responses) / compound, 1),
        "rankings": rankings,
    }
    if debug:
        result["fetch_meta"] = fetch_meta
    return result


# ---------------------------------------------------------------------------
# Tool: get_podcast_performance
# ---------------------------------------------------------------------------

@mcp.tool()
def get_podcast_performance(
    podcast_name: str,
    after_date: str = None,
    before_date: str = None,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Get performance stats for a specific podcast.

    Searches both the podcast clarification question (compound rate) AND the
    main question "Other" free-text responses (main question rate only), then
    combines them for a complete attribution picture.

    Args:
        podcast_name: Podcast name to search for (case-insensitive, partial match)
        after_date: ISO date string
        before_date: ISO date string
        rate_overrides: Optional dict to override response rates for this call only
        debug: If True, include pagination metadata
    """
    rates = _resolve_rates(rate_overrides)
    compound = _compound_rate(rates, "main_question", "podcast_clarification")
    main_rate = rates["main_question"]

    # --- Source 1: Podcast clarification question ---
    pod_responses, pod_meta = fetch_all_responses(
        PODCAST_CLARIFICATION_QUESTION_ID, after_date, before_date, debug=debug
    )
    all_aovs = [float(r["order_total"]) for r in pod_responses if r.get("order_total")]
    pod_matches = [
        r for r in pod_responses
        if _matches_channel(podcast_name, r.get("response") or "")
        or _matches_channel(podcast_name, r.get("other_response") or "")
    ]

    # --- Source 2: Main question "Other" free-text ---
    main_responses, main_meta = fetch_all_responses(
        MAIN_QUESTION_ID, after_date, before_date, debug=debug
    )
    main_other_matches = [
        r for r in main_responses
        if (
            (r.get("response") or "").strip().lower() == "other"
            or r.get("other_response")
        ) and (
            _matches_channel(podcast_name, r.get("other_response") or "")
            or _matches_channel(podcast_name, r.get("response") or "")
        )
    ]

    if not pod_matches and not main_other_matches:
        return {
            "podcast": podcast_name,
            "mentions": 0,
            "message": "No responses found for this podcast in the given period.",
        }

    def _source_stats(matches, rate):
        raw_rev = sum(float(r["order_total"]) for r in matches if r.get("order_total"))
        raw_m = len(matches)
        aov = round(raw_rev / raw_m, 2) if raw_m else 0
        return {
            "raw_mentions": raw_m,
            "raw_revenue": round(raw_rev, 2),
            "avg_order_value": aov,
            "extrapolated_mentions": round(raw_m / rate, 1) if rate else 0,
            "extrapolated_revenue": round(raw_rev / rate, 2) if rate else 0,
        }

    pod_stats = _source_stats(pod_matches, compound)
    main_stats = _source_stats(main_other_matches, main_rate)

    total_raw_mentions = pod_stats["raw_mentions"] + main_stats["raw_mentions"]
    total_raw_revenue = pod_stats["raw_revenue"] + main_stats["raw_revenue"]
    total_ext_mentions = pod_stats["extrapolated_mentions"] + main_stats["extrapolated_mentions"]
    total_ext_revenue = pod_stats["extrapolated_revenue"] + main_stats["extrapolated_revenue"]
    total_aov = round(total_raw_revenue / total_raw_mentions, 2) if total_raw_mentions else 0

    by_month = defaultdict(lambda: {"raw_m": 0, "raw_rev": 0.0, "ext_m": 0.0, "ext_rev": 0.0})
    for r in pod_matches:
        month = (r.get("inserted_at") or "")[:7]
        by_month[month]["raw_m"] += 1
        rev = float(r.get("order_total") or 0)
        by_month[month]["raw_rev"] += rev
        by_month[month]["ext_m"] += 1 / compound if compound else 0
        by_month[month]["ext_rev"] += rev / compound if compound else 0
    for r in main_other_matches:
        month = (r.get("inserted_at") or "")[:7]
        by_month[month]["raw_m"] += 1
        rev = float(r.get("order_total") or 0)
        by_month[month]["raw_rev"] += rev
        by_month[month]["ext_m"] += 1 / main_rate if main_rate else 0
        by_month[month]["ext_rev"] += rev / main_rate if main_rate else 0

    result = {
        "podcast": podcast_name,
        "period": {"after": after_date, "before": before_date},
        "response_rates_used": {
            "main_question": main_rate,
            "podcast_clarification": rates["podcast_clarification"],
            "compound_podcast": round(compound, 4),
        },
        "raw": {
            "mentions": total_raw_mentions,
            "attributed_revenue": round(total_raw_revenue, 2),
            "avg_order_value": total_aov,
        },
        "extrapolated": {
            "estimated_total_orders": round(total_ext_mentions, 1),
            "estimated_total_revenue": round(total_ext_revenue, 2),
            "avg_order_value": total_aov,
        },
        "sources": {
            "podcast_clarification": pod_stats,
            "main_question_other": main_stats,
        },
        "aov_stats": _aov_stats(all_aovs, total_aov),
        "by_month": {
            month: {
                "raw_mentions": v["raw_m"],
                "raw_revenue": round(v["raw_rev"], 2),
                "extrapolated_mentions": round(v["ext_m"], 1),
                "extrapolated_revenue": round(v["ext_rev"], 2),
            }
            for month, v in sorted(by_month.items())
        },
        "sample_responses": [
            r.get("response") or r.get("other_response") for r in (pod_matches + main_other_matches)[:5]
        ],
    }
    if debug:
        result["fetch_meta"] = {"podcast_clarification": pod_meta, "main_question": main_meta}
    return result


# ---------------------------------------------------------------------------
# Tool: rank_instagram_channels
# ---------------------------------------------------------------------------

@mcp.tool()
def rank_instagram_channels(
    after_date: str = None,
    before_date: str = None,
    rank_by: str = "mentions",
    top_n: int = 15,
    include_monthly_trend: bool = False,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Rank all Instagram accounts by number of mentions or attributed revenue.
    Requires INSTAGRAM_CLARIFICATION_QUESTION_ID to be set.

    Args:
        rank_by: "mentions", "revenue", "extrapolated_mentions", or "extrapolated_revenue"
        include_monthly_trend: If True, each entry includes a by_month breakdown
        rate_overrides: Optional dict to override response rates for this call only
        debug: If True, include pagination metadata
    """
    if not INSTAGRAM_CLARIFICATION_QUESTION_ID:
        return {
            "error": "INSTAGRAM_CLARIFICATION_QUESTION_ID is not set. Create an Instagram clarification "
                     "question in Fairing and set the ID via the INSTAGRAM_CLARIFICATION_QUESTION_ID env var."
        }

    rates = _resolve_rates(rate_overrides)
    compound = _compound_rate(rates, "main_question", "instagram_clarification")

    responses, fetch_meta = fetch_all_responses(
        INSTAGRAM_CLARIFICATION_QUESTION_ID, after_date, before_date, debug=debug
    )

    channel_stats: dict[str, dict] = defaultdict(lambda: {"mentions": 0, "revenue": 0.0, "by_month": defaultdict(lambda: {"mentions": 0, "revenue": 0.0})})
    all_aovs = []

    for r in responses:
        channel = r.get("response") or r.get("other_response") or "Unknown"
        order_total = float(r.get("order_total") or 0)
        channel_stats[channel]["mentions"] += 1
        channel_stats[channel]["revenue"] += order_total
        if order_total:
            all_aovs.append(order_total)
        if include_monthly_trend:
            month = (r.get("inserted_at") or "")[:7]
            channel_stats[channel]["by_month"][month]["mentions"] += 1
            channel_stats[channel]["by_month"][month]["revenue"] += order_total

    def sort_key(item):
        s = item[1]
        if rank_by in ("extrapolated_mentions", "mentions"):
            return s["mentions"]
        return s["revenue"]

    sorted_channels = sorted(channel_stats.items(), key=sort_key, reverse=True)[:top_n]

    rankings = []
    for i, (name, stats) in enumerate(sorted_channels):
        raw_mentions = stats["mentions"]
        raw_revenue = stats["revenue"]
        channel_aov = round(raw_revenue / raw_mentions, 2) if raw_mentions else 0

        entry = {
            "rank": i + 1,
            "channel": name,
            "raw_mentions": raw_mentions,
            "raw_attributed_revenue": round(raw_revenue, 2),
            "extrapolated_mentions": round(raw_mentions / compound, 1),
            "extrapolated_revenue": round(raw_revenue / compound, 2),
            "avg_order_value": channel_aov,
            "aov_stats": _aov_stats(all_aovs, channel_aov),
        }

        if include_monthly_trend:
            entry["by_month"] = {
                month: {
                    "raw_mentions": v["mentions"],
                    "raw_revenue": round(v["revenue"], 2),
                    "extrapolated_mentions": round(v["mentions"] / compound, 1),
                    "extrapolated_revenue": round(v["revenue"] / compound, 2),
                }
                for month, v in sorted(stats["by_month"].items())
            }

        rankings.append(entry)

    result = {
        "period": {"after": after_date, "before": before_date},
        "ranked_by": rank_by,
        "response_rates_used": {
            "main_question": rates["main_question"],
            "instagram_clarification": rates["instagram_clarification"],
            "compound": round(compound, 4),
        },
        "total_instagram_responses": len(responses),
        "estimated_total_instagram_orders": round(len(responses) / compound, 1),
        "rankings": rankings,
    }
    if debug:
        result["fetch_meta"] = fetch_meta
    return result


# ---------------------------------------------------------------------------
# Tool: get_instagram_channel_performance
# ---------------------------------------------------------------------------

@mcp.tool()
def get_instagram_channel_performance(
    account_name: str,
    after_date: str = None,
    before_date: str = None,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Get performance stats for a specific Instagram account.
    Requires INSTAGRAM_CLARIFICATION_QUESTION_ID to be set.

    Searches both the Instagram clarification question (compound rate) AND the
    main question "Other" free-text responses (main question rate only), then
    combines them for a complete attribution picture.

    Args:
        account_name: Instagram handle or name to search for (case-insensitive, partial match)
        after_date: ISO date string
        before_date: ISO date string
        rate_overrides: Optional dict to override response rates for this call only
        debug: If True, include pagination metadata
    """
    if not INSTAGRAM_CLARIFICATION_QUESTION_ID:
        return {
            "error": "INSTAGRAM_CLARIFICATION_QUESTION_ID is not set. Create an Instagram clarification "
                     "question in Fairing and set the ID via the INSTAGRAM_CLARIFICATION_QUESTION_ID env var."
        }

    rates = _resolve_rates(rate_overrides)
    compound = _compound_rate(rates, "main_question", "instagram_clarification")
    main_rate = rates["main_question"]

    # --- Source 1: Instagram clarification question ---
    ig_responses, ig_meta = fetch_all_responses(
        INSTAGRAM_CLARIFICATION_QUESTION_ID, after_date, before_date, debug=debug
    )
    all_aovs = [float(r["order_total"]) for r in ig_responses if r.get("order_total")]
    ig_matches = [
        r for r in ig_responses
        if _matches_channel(account_name, r.get("response") or "")
        or _matches_channel(account_name, r.get("other_response") or "")
    ]

    # --- Source 2: Main question "Other" free-text ---
    main_responses, main_meta = fetch_all_responses(
        MAIN_QUESTION_ID, after_date, before_date, debug=debug
    )
    main_other_matches = [
        r for r in main_responses
        if (
            (r.get("response") or "").strip().lower() == "other"
            or r.get("other_response")
        ) and (
            _matches_channel(account_name, r.get("other_response") or "")
            or _matches_channel(account_name, r.get("response") or "")
        )
    ]

    if not ig_matches and not main_other_matches:
        return {
            "account": account_name,
            "mentions": 0,
            "message": "No responses found for this Instagram account in the given period.",
        }

    def _source_stats(matches, rate):
        raw_rev = sum(float(r["order_total"]) for r in matches if r.get("order_total"))
        raw_m = len(matches)
        aov = round(raw_rev / raw_m, 2) if raw_m else 0
        return {
            "raw_mentions": raw_m,
            "raw_revenue": round(raw_rev, 2),
            "avg_order_value": aov,
            "extrapolated_mentions": round(raw_m / rate, 1) if rate else 0,
            "extrapolated_revenue": round(raw_rev / rate, 2) if rate else 0,
        }

    ig_stats = _source_stats(ig_matches, compound)
    main_stats = _source_stats(main_other_matches, main_rate)

    total_raw_mentions = ig_stats["raw_mentions"] + main_stats["raw_mentions"]
    total_raw_revenue = ig_stats["raw_revenue"] + main_stats["raw_revenue"]
    total_ext_mentions = ig_stats["extrapolated_mentions"] + main_stats["extrapolated_mentions"]
    total_ext_revenue = ig_stats["extrapolated_revenue"] + main_stats["extrapolated_revenue"]
    total_aov = round(total_raw_revenue / total_raw_mentions, 2) if total_raw_mentions else 0

    by_month = defaultdict(lambda: {"raw_m": 0, "raw_rev": 0.0, "ext_m": 0.0, "ext_rev": 0.0})
    for r in ig_matches:
        month = (r.get("inserted_at") or "")[:7]
        by_month[month]["raw_m"] += 1
        rev = float(r.get("order_total") or 0)
        by_month[month]["raw_rev"] += rev
        by_month[month]["ext_m"] += 1 / compound if compound else 0
        by_month[month]["ext_rev"] += rev / compound if compound else 0
    for r in main_other_matches:
        month = (r.get("inserted_at") or "")[:7]
        by_month[month]["raw_m"] += 1
        rev = float(r.get("order_total") or 0)
        by_month[month]["raw_rev"] += rev
        by_month[month]["ext_m"] += 1 / main_rate if main_rate else 0
        by_month[month]["ext_rev"] += rev / main_rate if main_rate else 0

    result = {
        "account": account_name,
        "period": {"after": after_date, "before": before_date},
        "response_rates_used": {
            "main_question": main_rate,
            "instagram_clarification": rates["instagram_clarification"],
            "compound_instagram": round(compound, 4),
        },
        "raw": {
            "mentions": total_raw_mentions,
            "attributed_revenue": round(total_raw_revenue, 2),
            "avg_order_value": total_aov,
        },
        "extrapolated": {
            "estimated_total_orders": round(total_ext_mentions, 1),
            "estimated_total_revenue": round(total_ext_revenue, 2),
            "avg_order_value": total_aov,
        },
        "sources": {
            "instagram_clarification": ig_stats,
            "main_question_other": main_stats,
        },
        "aov_stats": _aov_stats(all_aovs, total_aov),
        "by_month": {
            month: {
                "raw_mentions": v["raw_m"],
                "raw_revenue": round(v["raw_rev"], 2),
                "extrapolated_mentions": round(v["ext_m"], 1),
                "extrapolated_revenue": round(v["ext_rev"], 2),
            }
            for month, v in sorted(by_month.items())
        },
        "sample_responses": [
            r.get("response") or r.get("other_response") for r in (ig_matches + main_other_matches)[:5]
        ],
    }
    if debug:
        result["fetch_meta"] = {"instagram_clarification": ig_meta, "main_question": main_meta}
    return result


# ---------------------------------------------------------------------------
# Tool: get_creator_performance  (unified cross-platform search)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_creator_performance(
    creator_name: str,
    after_date: str = None,
    before_date: str = None,
    rate_overrides: dict = None,
    debug: bool = False,
) -> dict:
    """
    Search for a creator across YouTube, podcast, and Instagram clarification
    questions simultaneously and return a unified performance view.

    Useful for creators who drive attribution across multiple platforms but
    whose spend may be tracked under a single channel in Creator Jam.

    Args:
        creator_name: Name or handle to search for (case-insensitive, partial match)
        after_date: ISO date string
        before_date: ISO date string
        rate_overrides: Optional dict to override response rates for this call only
        debug: If True, include pagination metadata per platform
    """
    rates = _resolve_rates(rate_overrides)

    platform_configs = [
        {
            "platform": "youtube",
            "question_id": YOUTUBE_CLARIFICATION_QUESTION_ID,
            "rate_keys": ("main_question", "youtube_clarification"),
            "compound_key": "youtube_clarification",
        },
        {
            "platform": "podcast",
            "question_id": PODCAST_CLARIFICATION_QUESTION_ID,
            "rate_keys": ("main_question", "podcast_clarification"),
            "compound_key": "podcast_clarification",
        },
        {
            "platform": "instagram",
            "question_id": INSTAGRAM_CLARIFICATION_QUESTION_ID,
            "rate_keys": ("main_question", "instagram_clarification"),
            "compound_key": "instagram_clarification",
        },
    ]

    platforms = {}
    total_raw_mentions = 0
    total_raw_revenue = 0.0
    combined_by_month: dict[str, dict] = defaultdict(lambda: {"raw_mentions": 0, "raw_revenue": 0.0})
    all_fetch_metas = {}

    for cfg in platform_configs:
        compound = _compound_rate(rates, *cfg["rate_keys"])
        responses, fetch_meta = fetch_all_responses(
            cfg["question_id"], after_date, before_date, debug=debug
        )

        matches = [
            r for r in responses
            if _matches_channel(creator_name, r.get("response") or "")
            or _matches_channel(creator_name, r.get("other_response") or "")
        ]

        if not matches:
            platforms[cfg["platform"]] = {"found": False}
            if debug:
                all_fetch_metas[cfg["platform"]] = fetch_meta
            continue

        raw_revenue = sum(float(r["order_total"]) for r in matches if r.get("order_total"))
        raw_mentions = len(matches)

        by_month = defaultdict(lambda: {"mentions": 0, "revenue": 0.0})
        for r in matches:
            month = (r.get("inserted_at") or "")[:7]
            by_month[month]["mentions"] += 1
            by_month[month]["revenue"] += float(r.get("order_total") or 0)
            combined_by_month[month]["raw_mentions"] += 1
            combined_by_month[month]["raw_revenue"] += float(r.get("order_total") or 0)

        total_raw_mentions += raw_mentions
        total_raw_revenue += raw_revenue

        platforms[cfg["platform"]] = {
            "found": True,
            "raw_mentions": raw_mentions,
            "raw_attributed_revenue": round(raw_revenue, 2),
            "extrapolated_mentions": round(raw_mentions / compound, 1),
            "extrapolated_revenue": round(raw_revenue / compound, 2),
            "avg_order_value": round(raw_revenue / raw_mentions, 2) if raw_mentions else 0,
            "compound_rate_used": round(compound, 4),
            "by_month": {
                month: {
                    "raw_mentions": v["mentions"],
                    "raw_revenue": round(v["revenue"], 2),
                    "extrapolated_mentions": round(v["mentions"] / compound, 1),
                    "extrapolated_revenue": round(v["revenue"] / compound, 2),
                }
                for month, v in sorted(by_month.items())
            },
            "sample_responses": [r.get("response") or r.get("other_response") for r in matches[:3]],
        }
        if debug:
            all_fetch_metas[cfg["platform"]] = fetch_meta

    # Combined totals — note: summing extrapolated across platforms with different
    # compound rates; individual platform figures are more accurate for ROI.
    result = {
        "creator": creator_name,
        "period": {"after": after_date, "before": before_date},
        "note": (
            "Extrapolated figures per platform use that platform's compound response rate. "
            "Do not sum extrapolated_revenue across platforms without accounting for rate differences."
        ),
        "combined_raw": {
            "total_mentions": total_raw_mentions,
            "total_attributed_revenue": round(total_raw_revenue, 2),
        },
        "combined_by_month": {
            month: {
                "raw_mentions": v["raw_mentions"],
                "raw_revenue": round(v["raw_revenue"], 2),
            }
            for month, v in sorted(combined_by_month.items())
        },
        "by_platform": platforms,
    }
    if debug:
        result["fetch_metas"] = all_fetch_metas
    return result


if __name__ == "__main__":
    mcp.run()