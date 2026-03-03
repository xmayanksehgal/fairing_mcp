# Fairing MCP

## Project Overview

This is a **Model Context Protocol (MCP) server** that connects Claude to the [Fairing](https://www.fairing.co) post-purchase survey platform. It enables natural-language marketing attribution analysis — ask Claude which YouTube channels, podcasts, or Instagram accounts are driving the most orders and revenue for your brand.

## What it does

Fairing asks customers "How did you hear about us?" after purchase. This MCP server:

1. Fetches survey responses from the Fairing API
2. Extrapolates raw mention counts to estimated total orders (accounting for non-respondents)
3. Exposes 9 tools Claude can call to answer attribution questions

## Architecture

Single-file implementation: `fairing_mcp.py`

```
fairing_mcp.py
├── Constants & config          # Question IDs, response rates, API base URL
├── Internal helpers            # _resolve_rates, _compound_rate, _extrapolate, fetch_all_responses
├── Channel matching            # _normalize, _matches (fuzzy + exact match)
└── MCP Tools (9 exported)
    ├── get_other_responses
    ├── get_attribution_overview
    ├── rank_youtube_channels
    ├── get_channel_performance
    ├── rank_podcast_channels
    ├── get_podcast_performance
    ├── rank_instagram_channels
    ├── get_instagram_channel_performance
    └── get_creator_performance
```

## Configuration

### Required environment variable

| Variable | Description |
|---|---|
| `FAIRING_API_KEY` | Bearer token from your Fairing account |

### Optional environment variables

| Variable | Default | Description |
|---|---|---|
| `MAIN_QUESTION_RESPONSE_RATE` | `0.33` | % of orders that answer the main "how did you hear" question |
| `YOUTUBE_CLARIFICATION_RESPONSE_RATE` | `0.55` | % of YouTube respondents who answer the clarification follow-up |
| `PODCAST_CLARIFICATION_RESPONSE_RATE` | `0.80` | % of Podcast respondents who answer the clarification follow-up |
| `INSTAGRAM_CLARIFICATION_RESPONSE_RATE` | `0.80` | % of Instagram respondents who answer the clarification follow-up |
| `INSTAGRAM_CLARIFICATION_QUESTION_ID` | `146913` | Fairing question ID for the Instagram clarification question |

### Hardcoded question IDs (edit in source if yours differ)

| Constant | Value | Purpose |
|---|---|---|
| `MAIN_QUESTION_ID` | `32778` | "How did you hear about us?" |
| `YOUTUBE_CLARIFICATION_QUESTION_ID` | `145964` | "Which YouTube channel?" |
| `PODCAST_CLARIFICATION_QUESTION_ID` | `145963` | "Which podcast?" |

## Setup

### Install dependencies

```bash
pip install mcp requests
```

### Run the MCP server

```bash
FAIRING_API_KEY=your_key python fairing_mcp.py
```

### Add to Claude Desktop

In `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fairing": {
      "command": "python",
      "args": ["/path/to/fairing_mcp.py"],
      "env": {
        "FAIRING_API_KEY": "your_key_here"
      }
    }
  }
}
```

## MCP Tools Reference

### `get_attribution_overview`
High-level breakdown of all discovery channels with mention counts, attributed revenue, and extrapolated estimates. Best first call.

### `rank_youtube_channels`
Rank all YouTube channels by `mentions`, `revenue`, `extrapolated_mentions`, or `extrapolated_revenue`.

### `get_channel_performance`
Detailed stats for a specific YouTube channel (partial name match, case-insensitive).

### `rank_podcast_channels`
Rank all podcasts by mentions or revenue.

### `get_podcast_performance`
Detailed stats for a specific podcast.

### `rank_instagram_channels`
Rank all Instagram accounts by mentions or revenue.

### `get_instagram_channel_performance`
Detailed stats for a specific Instagram account.

### `get_creator_performance`
Cross-platform search — finds a creator across YouTube, podcast, and Instagram simultaneously.

### `get_other_responses`
Return raw free-text "Other" responses to inspect what customers wrote.

## Key Concepts

### Response rate extrapolation

Not every customer answers the survey. The server extrapolates raw mention counts to estimate how many total orders a channel likely drove:

```
extrapolated_orders = raw_mentions / compound_response_rate
```

For clarification questions (e.g. "which YouTube channel?"), the compound rate multiplies:
```
compound = main_question_rate × clarification_rate
         = 0.33 × 0.55 ≈ 0.18  (18% of orders are captured)
```

Tune these rates via environment variables as you gather your own data.

### Channel matching

Matching is case-insensitive and uses two strategies:
1. **Exact substring** — "Marine X" matches "MarineX Podcast"
2. **Normalized fuzzy** — strips punctuation/spaces, so "Marine X" also matches "MarineX"

### Date filtering

All tools accept `after_date` and `before_date` as ISO 8601 strings (e.g. `"2025-01-01"`). Defaults to Aug 22 2025 – today when omitted (for spend tools).

## Development Notes

- The server uses [FastMCP](https://github.com/jlowin/fastmcp) — a lightweight Python framework for building MCP servers
- All Fairing API responses are paginated; the `fetch_all_responses` helper handles this automatically
- The `debug=True` parameter on any tool returns pagination metadata (pages fetched, API calls, date ranges)
- Monthly trend breakdowns are available on ranking tools via `include_monthly_trend=True`
