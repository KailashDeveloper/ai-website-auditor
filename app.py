"""
AI Website Tester — Web UI
==========================
Flask server with live streaming output via Server-Sent Events (SSE).

Run:
    pip install flask playwright anthropic rich
    playwright install chromium
    python app.py
Then open http://localhost:5000
"""

import asyncio
import json
import os
import sys
import subprocess

# Ensure Playwright browsers are available on Vercel
if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/tmp/ms-playwright")
        try:
        subprocess.run(                                [sys.executable, "-m", "playwright", "install", "chromium"], check=True, capture_output=True)
        except Exception:
            pass  # Will fail at audit time with clear error
import time
import threading
import queue
import re
import hmac
import functools
from datetime import datetime
from typing import Optional

from flask import Flask, render_template, request, Response, jsonify, send_from_directory, session, redirect, url_for

# ── Import core logic from ai_agent_web.py ──────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

try:
    from playwright.async_api import async_playwright
    import anthropic
except ImportError:
    print("Missing dependencies. Run:\n  pip install flask playwright anthropic\n  playwright install chromium")
    sys.exit(1)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me-in-prod-use-env-var-32bytes")

# ── Auth credentials ─────────────────────────────────────────────────────────
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin%345")


def check_credentials(username: str, password: str) -> bool:
    """Constant-time comparison to prevent timing attacks."""
    user_ok = hmac.compare_digest(username.encode(), ADMIN_USERNAME.encode())
    pass_ok = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    return user_ok and pass_ok


def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

ALL_CATEGORIES = ["seo", "performance", "accessibility", "security", "bestpractices", "mobile", "content", "links"]
CLAUDE_MODEL = "claude-opus-4-5"

SYSTEM_PROMPT = """You are an expert senior frontend QA engineer, web performance specialist, security auditor, and accessibility consultant. You receive real browser-collected data about a website and produce a structured, actionable audit report.

Your analysis is:
- Specific (reference actual values from the data, not generic advice)
- Prioritized (highlight critical issues first)
- Actionable (each finding has a clear fix)
- Balanced (acknowledge what's working well)

Always return valid JSON only — no markdown, no explanation outside the JSON."""


def build_user_prompt(browser_data: dict, categories: list) -> str:
    return f"""Analyze this website audit data and produce a comprehensive report.

=== BROWSER-COLLECTED DATA ===
{json.dumps(browser_data, indent=2, default=str)}

=== REQUESTED TEST CATEGORIES ===
{', '.join(categories)}

=== INSTRUCTIONS ===
Return ONLY a raw JSON object with this exact structure:

{{
  "url": "<the tested URL>",
  "overall_score": <integer 0-100>,
  "grade": "<A|B|C|D|F>",
  "summary": "<2-3 sentence executive summary of the site's health>",
  "category_scores": {{
    "<category_id>": <integer 0-100>
  }},
  "results": [
    {{
      "category": "<category_id>",
      "title": "<concise title>",
      "icon": "<single emoji>",
      "status": "<pass|fail|warn|info>",
      "priority": "<critical|high|medium|low>",
      "summary": "<1-2 sentences specific to the actual data>",
      "items": [
        {{
          "label": "<finding label>",
          "value": "<actual value from data if applicable>",
          "recommendation": "<specific fix>"
        }}
      ]
    }}
  ],
  "quick_wins": ["<actionable fix that takes <1 hour>"],
  "critical_issues": ["<must-fix immediately>"]
}}

Rules:
- category_scores keys must exactly match these ids: {', '.join(categories)}
- Reference actual numbers from the data (load time, image count, error count, etc.)
- quick_wins: 3-5 items
- critical_issues: only truly critical items (can be empty list [])
- Each result must have 2-5 items
- Be specific: bad = "add alt text", good = "12 of 20 images are missing alt text"
"""


# ───────────────────────────────────────────────────────────────────────────────
# BROWSER COLLECTOR (async)
# ───────────────────────────────────────────────────────────────────────────────

async def collect_browser_data(url: str, log_queue: queue.Queue) -> dict:
    log_queue.put(("log", "Launching Chromium browser..."))
        # Install Playwright browser on Vercel (runs in /tmp which is writable)
        pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/tmp/ms-playwright")
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = pw_path
    if not os.path.exists(pw_path):
                log_queue.put(("log", "Installing Chromium browser (first run)..."))
                subprocess.run(
                                [sys.executable, "-m", "playwright", "install", "chromium"],
                                check=True, capture_output=True
                )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        ctx_desktop = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = await ctx_desktop.new_page()

        requests_log = []
        console_errors = []

        page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}")
                if msg.type in ("error", "warning") else None)
        page.on("request", lambda req: requests_log.append({
            "url": req.url, "method": req.method, "resource_type": req.resource_type
        }))
        responses_log = []
        page.on("response", lambda resp: responses_log.append({
            "url": resp.url, "status": resp.status, "headers": dict(resp.headers)
        }))

        log_queue.put(("log", f"Navigating to {url}..."))
        t0 = time.time()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception as e:
            log_queue.put(("log", f"Warning during load: {e}"))
        load_time_ms = int((time.time() - t0) * 1000)

        await page.wait_for_timeout(2000)

        log_queue.put(("log", "Extracting page data..."))
        page_data = await page.evaluate("""() => {
            const getMeta = (name) => {
                const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
                return el ? el.getAttribute('content') : null;
            };
            const getHeadings = () => {
                const tags = ['h1','h2','h3','h4','h5','h6'];
                return tags.reduce((acc, t) => {
                    acc[t] = Array.from(document.querySelectorAll(t)).map(el => el.innerText.trim()).filter(Boolean).slice(0, 5);
                    return acc;
                }, {});
            };
            const getImages = () => {
                return Array.from(document.querySelectorAll('img')).map(img => ({
                    src: img.src, alt: img.alt, width: img.naturalWidth, height: img.naturalHeight
                })).slice(0, 20);
            };
            const getLinks = () => {
                return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    href: a.href, text: a.innerText.trim(), rel: a.rel
                })).slice(0, 30);
            };
            const getForms = () => {
                return Array.from(document.querySelectorAll('form')).map(f => ({
                    action: f.action, method: f.method,
                    inputs: Array.from(f.querySelectorAll('input, textarea, select')).map(i => ({
                        type: i.type || i.tagName.toLowerCase(), name: i.name,
                        required: i.required,
                        label: document.querySelector(`label[for="${i.id}"]`)?.innerText || null
                    }))
                }));
            };
            const getAccessibility = () => {
                const noAlt = Array.from(document.querySelectorAll('img')).filter(i => !i.alt).length;
                const noLabel = Array.from(document.querySelectorAll('input, textarea')).filter(i => {
                    return !i.getAttribute('aria-label') && !document.querySelector(`label[for="${i.id}"]`);
                }).length;
                const noLang = !document.documentElement.lang;
                const skipNav = !!document.querySelector('[href="#main"], [href="#content"], .skip-nav, .skip-link');
                const ariaLandmarks = Array.from(document.querySelectorAll(
                    '[role="main"],[role="navigation"],[role="banner"],[role="contentinfo"], main, nav, header, footer'
                )).map(el => el.tagName + (el.getAttribute('role') ? `[${el.getAttribute('role')}]` : ''));
                return { noAlt, noLabel, noLang, skipNav, ariaLandmarks };
            };
            const getPerf = () => {
                const nav = performance.getEntriesByType('navigation')[0];
                return nav ? {
                    domContentLoaded: Math.round(nav.domContentLoadedEventEnd),
                    domComplete: Math.round(nav.domComplete),
                    transferSize: nav.transferSize,
                    encodedBodySize: nav.encodedBodySize
                } : {};
            };
            return {
                title: document.title,
                metaDescription: getMeta('description'),
                metaViewport: getMeta('viewport'),
                metaRobots: getMeta('robots'),
                canonical: document.querySelector('link[rel="canonical"]')?.href || null,
                ogTitle: getMeta('og:title'),
                ogDescription: getMeta('og:description'),
                allMeta: Array.from(document.querySelectorAll('meta')).map(m => ({
                    name: m.getAttribute('name') || m.getAttribute('property'),
                    content: m.getAttribute('content')
                })).filter(m => m.name),
                headings: getHeadings(),
                images: getImages(),
                links: getLinks(),
                forms: getForms(),
                accessibility: getAccessibility(),
                performance: getPerf(),
                hasServiceWorker: 'serviceWorker' in navigator,
                hasHTTPS: location.protocol === 'https:',
                charSet: document.characterSet,
                doctype: document.doctype ? document.doctype.name : null,
                scriptCount: document.querySelectorAll('script').length,
                styleSheetCount: document.querySelectorAll('link[rel="stylesheet"]').length,
                bodyWordCount: (document.body?.innerText || '').split(/\\s+/).filter(Boolean).length,
                htmlLang: document.documentElement.lang || null,
                structuredData: Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s => {
                    try { return JSON.parse(s.textContent); } catch { return null; }
                }).filter(Boolean)
            };
        }""")

        log_queue.put(("log", "Taking desktop screenshot..."))
        screenshot_path = os.path.join(os.path.dirname(__file__), "static", "screenshot_desktop.png")
        os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        await page.screenshot(path=screenshot_path, full_page=False)

        main_response = next((r for r in responses_log if url.split('?')[0] in r['url'] or r['url'] == url), None)
        main_headers = main_response['headers'] if main_response else {}
        await ctx_desktop.close()

        log_queue.put(("log", "Testing mobile viewport..."))
        ctx_mobile = await browser.new_context(
            viewport={"width": 390, "height": 844},
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
        )
        mob_page = await ctx_mobile.new_page()
        mob_t0 = time.time()
        try:
            await mob_page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            pass
        mob_load_ms = int((time.time() - mob_t0) * 1000)
        mob_viewport_ok = await mob_page.evaluate(
            "() => document.querySelector('meta[name=\"viewport\"]') !== null"
        )
        mob_screenshot = os.path.join(os.path.dirname(__file__), "static", "screenshot_mobile.png")
        await mob_page.screenshot(path=mob_screenshot)
        await ctx_mobile.close()
        await browser.close()

        resource_summary = {}
        for r in requests_log:
            rt = r['resource_type']
            resource_summary[rt] = resource_summary.get(rt, 0) + 1

        data = {
            "url": url,
            "collected_at": datetime.now().isoformat(),
            "load_time_ms": load_time_ms,
            "mobile_load_time_ms": mob_load_ms,
            "mobile_viewport_meta": mob_viewport_ok,
            "http_headers": main_headers,
            "console_errors": console_errors[:15],
            "resource_summary": resource_summary,
            "total_requests": len(requests_log),
            "failed_responses": [r for r in responses_log if r['status'] >= 400],
            "page": page_data,
        }

        log_queue.put(("log", f"Data collection complete — {load_time_ms}ms desktop load, {mob_load_ms}ms mobile"))
        return data


# ───────────────────────────────────────────────────────────────────────────────
# WORKER THREAD
# ───────────────────────────────────────────────────────────────────────────────

def run_audit(url: str, categories: list, log_queue: queue.Queue):
    """Runs in a background thread; pushes events to log_queue."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Step 1 — browser collection
        browser_data = loop.run_until_complete(collect_browser_data(url, log_queue))

        log_queue.put(("log", f"Collected {browser_data['total_requests']} requests — sending to Claude AI..."))

        # Step 2 — Claude AI
        if not ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY environment variable is not set.")
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(browser_data, categories)}]
        )
        raw = message.content[0].text

        # Parse
        clean = raw.replace("```json", "").replace("```", "").strip()
        start = clean.index("{")
        end = clean.rindex("}") + 1
        report = json.loads(clean[start:end])

        log_queue.put(("report", report))

    except Exception as e:
        log_queue.put(("error", str(e)))
    finally:
        log_queue.put(("done", None))


# ───────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ───────────────────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if check_credentials(username, password):
            session["logged_in"] = True
            session["username"] = username
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/audit/stream")
@login_required
def audit_stream():
    url = request.args.get("url", "").strip()
    categories = request.args.getlist("categories") or ALL_CATEGORIES

    if not url or not url.startswith("http"):
        return Response('data: {"type":"error","message":"Invalid URL"}\n\n',
                        mimetype="text/event-stream")

    log_queue: queue.Queue = queue.Queue()

    thread = threading.Thread(target=run_audit, args=(url, categories, log_queue), daemon=True)
    thread.start()

    def generate():
        while True:
            try:
                event_type, payload = log_queue.get(timeout=120)
                if event_type == "log":
                    data = json.dumps({"type": "log", "message": payload})
                    yield f"data: {data}\n\n"
                elif event_type == "report":
                    data = json.dumps({"type": "report", "report": payload})
                    yield f"data: {data}\n\n"
                elif event_type == "error":
                    data = json.dumps({"type": "error", "message": payload})
                    yield f"data: {data}\n\n"
                elif event_type == "done":
                    yield 'data: {"type":"done"}\n\n'
                    break
            except queue.Empty:
                yield 'data: {"type":"heartbeat"}\n\n'

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(os.path.dirname(__file__), "static"), filename)


# ───────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ───────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(os.path.join(os.path.dirname(__file__), "static"), exist_ok=True)
    os.makedirs(os.path.join(os.path.dirname(__file__), "templates"), exist_ok=True)
    print("Starting AI Website Tester — open http://localhost:5000")
    app.run(debug=False, port=5000, threaded=True)
