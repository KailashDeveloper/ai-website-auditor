# AI Website Auditor — Agent Instructions

## Project Overview

**AI Website Auditor** — crawls any URL with a real Chromium browser (Playwright), collects desktop + mobile data, then sends it to Claude AI for a structured audit report covering SEO, performance, accessibility, security, and more.

Two entry points:

| File | Purpose |
|------|-------|
| `app.py` | Flask web server with live SSE streaming — open http://localhost:5000 |
| `ai_agent_web.py` | CLI tool for terminal usage |

## Setup & Run

```bash
pip install flask playwright anthropic rich
playwright install chromium

# Web UI
export ANTHROPIC_API_KEY=sk-ant-...
python app.py

# CLI
python ai_agent_web.py --url https://example.com
python ai_agent_web.py --url https://example.com --categories seo performance security
python ai_agent_web.py --url https://example.com --output report.json
```

## Architecture

### Web UI Flow (`app.py`)

```
GET /audit/stream?url=...&categories=...
  └─► threading.Thread → run_audit()
        ├─ asyncio loop → collect_browser_data()  [Playwright, async]
        │    ├─ Desktop context (1280×800) — page data, screenshots, network log
        │    └─ Mobile context (390×844 iPhone UA) — load time, viewport check
        └─ anthropic.Anthropic.messages.create()  [Claude AI, sync]
              └─► log_queue → SSE events → browser
```

- **SSE event types**: `log`, `report`, `error`, `done`, `heartbeat`
- All events are JSON: `{"type": "...", "message"|"report": ...}`
- Screenshots saved to `static/screenshot_desktop.png` and `static/screenshot_mobile.png`
- `queue.Queue` bridges the background thread to the SSE generator

### CLI Flow (`ai_agent_web.py`)

- `BrowserCollector` class collects all browser data (same logic as `collect_browser_data` in `app.py`)
- Uses `rich` for terminal output (console, panels, progress spinners, tables)
- Writes JSON report to `--output` file if specified

## Auth

- Login at `/login`, logout at `/logout`
- Credentials configurable via `ADMIN_USERNAME` / `ADMIN_PASSWORD` env vars (default: admin / admin%345)
- Uses `hmac.compare_digest` for constant-time credential comparison
- All routes except `/login` are protected by `@login_required`

## Audit Categories

`ALL_CATEGORIES = ["seo", "performance", "accessibility", "security", "bestpractices", "mobile", "content", "links"]`

The Claude prompt expects `category_scores` keys to exactly match the requested category ids.

## Claude AI Integration

- Model: `claude-opus-4-5` (configurable via `CLAUDE_MODEL` constant — use `claude-sonnet-4-5` for faster/cheaper)
- Prompt in `app.py`: `SYSTEM_PROMPT` + `build_user_prompt(browser_data, categories)`
- Response must be raw JSON — the code strips markdown fences and parses with `json.loads`
- `max_tokens=4096`

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | Anthropic API key |
| `FLASK_SECRET_KEY` | Prod only | insecure default | Flask session signing key |
| `ADMIN_USERNAME` | No | `admin` | Login username |
| `ADMIN_PASSWORD` | No | `admin%345` | Login password |

See `.env.example` for a template.

## Key Conventions

- Browser data collection is **async** (Playwright); Flask routes are **sync** — they bridge via `threading.Thread` + `asyncio.new_event_loop()`
- `app.py` duplicates the browser collection logic from `ai_agent_web.py` (by design — no shared module import to keep the web app self-contained)
- Templates: Jinja2, single file `templates/index.html` (all CSS inline, all JS inline)
- No database — reports are streamed directly to the browser, not persisted
- `static/` holds only runtime-generated screenshots; not committed to git

## Template / Frontend

`templates/index.html` is self-contained: dark-theme CSS variables, inline JS that connects to `/audit/stream` via `EventSource`, renders cards from the JSON report. Edit only this file for UI changes.
