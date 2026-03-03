# Fairing MCP

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that connects Claude to the [Fairing](https://www.fairing.co) post-purchase survey platform. Ask Claude natural-language questions about which YouTube channels, podcasts, and Instagram accounts are driving orders and revenue for your brand.

## What it does

Fairing asks customers "How did you hear about us?" after purchase. This server:

1. Fetches survey responses from the Fairing API
2. Extrapolates raw mention counts to estimated total orders (accounting for survey non-respondents)
3. Exposes 9 tools Claude can call to answer attribution questions

**Example questions you can ask Claude:**
- "Which YouTube channels drove the most orders last month?"
- "How is the MrBeast sponsorship performing compared to other channels?"
- "What podcasts are bringing in the highest revenue?"
- "Show me a breakdown of all discovery channels this quarter."

## Requirements

- Python 3.8+
- A [Fairing](https://www.fairing.co) account with an API key
- Claude Desktop (or any MCP-compatible client)

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

Or directly:

```bash
pip install mcp requests
```

### 2. Get your Fairing API key

Log in to your Fairing account and retrieve your API key from the account settings.

### 3. Add to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "fairing": {
      "command": "python",
      "args": ["/absolute/path/to/fairing_mcp.py"],
      "env": {
        "FAIRING_API_KEY": "your_key_here"
      }
    }
  }
}
```

Restart Claude Desktop. The server auto-discovers your survey question IDs at startup — no manual configuration needed for standard Fairing setups.

### 4. Run manually (optional)

```bash
FAIRING_API_KEY=your_key python fairing_mcp.py
```

## Configuration

### Required

| Variable | Description |
|---|---|
| `FAIRING_API_KEY` | Bearer token from your Fairing account |

### Optional — response rate tuning

These rates control how raw mention counts are extrapolated to estimated total orders. The defaults are reasonable starting points; tune them as you gather your own data.

| Variable | Default | Description |
|---|---|---|
| `MAIN_QUESTION_RESPONSE_RATE` | `0.33` | Fraction of orders that answer the main "how did you hear" question |
| `YOUTUBE_CLARIFICATION_RESPONSE_RATE` | `0.55` | Fraction of YouTube respondents who answer the follow-up "which channel?" question |
| `PODCAST_CLARIFICATION_RESPONSE_RATE` | `0.80` | Fraction of Podcast respondents who answer the follow-up |
| `INSTAGRAM_CLARIFICATION_RESPONSE_RATE` | `0.80` | Fraction of Instagram respondents who answer the follow-up |

### Optional — question ID overrides

Question IDs are **auto-discovered** at startup. If your survey uses non-standard phrasing, you can override any ID:

| Variable | Purpose |
|---|---|
| `MAIN_QUESTION_ID` | Override the main attribution question ID |
| `YOUTUBE_CLARIFICATION_QUESTION_ID` | Override the YouTube clarification question ID |
| `PODCAST_CLARIFICATION_QUESTION_ID` | Override the Podcast clarification question ID |
| `INSTAGRAM_CLARIFICATION_QUESTION_ID` | Override the Instagram clarification question ID |

Check your MCP server logs (stderr) if a tool returns an error about a missing question ID.

## Available Tools

| Tool | Description |
|---|---|
| `get_attribution_overview` | High-level breakdown of all discovery channels with counts and revenue. Best starting point. |
| `rank_youtube_channels` | Rank YouTube channels by `mentions`, `revenue`, `extrapolated_mentions`, or `extrapolated_revenue` |
| `get_channel_performance` | Detailed stats for a specific YouTube channel (partial name match) |
| `rank_podcast_channels` | Rank podcasts by mentions or revenue |
| `get_podcast_performance` | Detailed stats for a specific podcast |
| `rank_instagram_channels` | Rank Instagram accounts by mentions or revenue |
| `get_instagram_channel_performance` | Detailed stats for a specific Instagram account |
| `get_creator_performance` | Cross-platform search across YouTube, podcast, and Instagram simultaneously |
| `get_other_responses` | Inspect raw free-text "Other" responses |

All tools accept `after_date` and `before_date` as ISO 8601 strings (e.g. `"2025-01-01"`) for date filtering. Ranking tools support `include_monthly_trend=True` for month-by-month breakdowns. Any tool accepts `debug=True` to return pagination metadata.

## How extrapolation works

Not every customer answers the survey. The server estimates total orders driven by a channel by dividing raw mentions by the compound response rate:

```
extrapolated_orders = raw_mentions / compound_response_rate
```

For clarification questions (e.g. "which YouTube channel?"), the compound rate multiplies the main question rate by the clarification rate:

```
compound = main_question_rate × clarification_rate
         = 0.33 × 0.55 ≈ 0.18  (18% of orders are captured)
```

So a channel with 100 mentions implies roughly 556 total orders driven.

## How channel matching works

Search is case-insensitive and uses two strategies:

1. **Substring match** — "Marine X" matches "MarineX Podcast"
2. **Normalized fuzzy match** — strips punctuation/spaces, so "Marine X" also matches "MarineX"

## Architecture

Single-file implementation in `fairing_mcp.py`, built on [FastMCP](https://github.com/jlowin/fastmcp).

```
fairing_mcp.py
├── Constants & config          # Response rates, API base URL
├── Question ID discovery       # Auto-resolves IDs from /api/questions at startup
├── Internal helpers            # Rate resolution, extrapolation, paginated fetching
├── Channel matching            # Fuzzy + exact match
└── MCP Tools (9 exported)
```

## Troubleshooting

**Tools return errors about missing question IDs**
Check stderr logs from the MCP server process. Auto-discovery may have failed due to non-standard survey phrasing or API key permission issues. Set the relevant `*_QUESTION_ID` environment variables to override.

**Extrapolated numbers seem off**
The default response rates are estimates. Measure your actual survey completion rates in Fairing and update `MAIN_QUESTION_RESPONSE_RATE` and the clarification rates accordingly.

**No data returned for a date range**
Fairing defaults to Aug 22 2025 – today when dates are omitted. Pass explicit `after_date`/`before_date` parameters to control the window.
