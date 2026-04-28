"""
AI Website Tester Agent
=======================
Uses Playwright (real browser) to crawl a website, collects real data,
then sends it to Claude AI for a comprehensive frontend audit report.

Install dependencies:
    pip install playwright anthropic rich
    playwright install chromium

Usage:
    python ai_agent_web.py --url https://example.com
    python ai_agent_web.py --url https://example.com --categories seo performance security
    python ai_agent_web.py --url https://example.com --output report.json
"""

import asyncio
import argparse
import json
import time
import re
import sys
from datetime import datetime
from typing import Optional

# ── Third-party ──────────────────────────────────────────────────────────────────────
try:
    from playwright.async_api import async_playwright, Page, Response
    import anthropic
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich import print as rprint
    from rich.syntax import Syntax
except ImportError:
    print("Missing dependencies. Run:\n  pip install playwright anthropic rich\n  playwright install chromium")
    sys.exit(1)

console = Console()

# ───────────────────────────────────────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────────────────────────────────────

ALL_CATEGORIES = ["seo", "performance", "accessibility", "security", "bestpractices", "mobile", "content", "links"]

CLAUDE_MODEL = "claude-opus-4-5"   # change to claude-sonnet-4-5 for faster/cheaper

# ───────────────────────────────────────────────────────────────────────────────
# BROWSER DATA COLLECTOR
# ───────────────────────────────────────────────────────────────────────────────

class BrowserCollector:
    """Uses Playwright to collect real browser data from the target URL."""

    def __init__(self, url: str):
        self.url = url
        self.data: dict = {}

    async def collect(self) -> dict:
        console.log(f"[cyan]Launching Chromium browser...[/cyan]")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            ctx_desktop = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await ctx_desktop.new_page()

            requests_log: list[dict] = []
            console_errors: list[str] = []

            page.on("console", lambda msg: console_errors.append(f"[{msg.type}] {msg.text}") if msg.type in ("error", "warning") else None)
            page.on("request", lambda req: requests_log.append({
                "url": req.url,
                "method": req.method,
                "resource_type": req.resource_type
            }))

            responses_log: list[dict] = []
            page.on("response", lambda resp: responses_log.append({
                "url": resp.url,
                "status": resp.status,
                "headers": dict(resp.headers)
            }))

            console.log("[cyan]Navigating to target URL...[/cyan]")
            t0 = time.time()
            try:
                await page.goto(self.url, wait_until="networkidle", timeout=30000)
            except Exception as e:
                console.log(f"[yellow]Warning during load: {e}[/yellow]")
            load_time_ms = int((time.time() - t0) * 1000)

            await page.wait_for_timeout(2000)

            console.log("[cyan]Extracting page data...[/cyan]")
            page_data = await page.evaluate("""() => {
                const getMeta = (name) => {
                    const el = document.querySelector(`meta[name="${name}"], meta[property="${name}"]`);
                    return el ? el.getAttribute('content') : null;
                };
                const getAllMeta = () => {
                    return Array.from(document.querySelectorAll('meta')).map(m => ({
                        name: m.getAttribute('name') || m.getAttribute('property'),
                        content: m.getAttribute('content')
                    })).filter(m => m.name);
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
                        src: img.src,
                        alt: img.alt,
                        width: img.naturalWidth,
                        height: img.naturalHeight
                    })).slice(0, 20);
                };
                const getLinks = () => {
                    return Array.from(document.querySelectorAll('a[href]')).map(a => ({
                        href: a.href,
                        text: a.innerText.trim(),
                        rel: a.rel
                    })).slice(0, 30);
                };
                const getForms = () => {
                    return Array.from(document.querySelectorAll('form')).map(f => ({
                        action: f.action,
                        method: f.method,
                        inputs: Array.from(f.querySelectorAll('input, textarea, select')).map(i => ({
                            type: i.type || i.tagName.toLowerCase(),
                            name: i.name,
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
                    allMeta: getAllMeta(),
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

            console.log("[cyan]Taking desktop screenshot...[/cyan]")
            screenshot_path = "screenshot_desktop.png"
            await page.screenshot(path=screenshot_path, full_page=False)

            main_response = next((r for r in responses_log if self.url.split('?')[0] in r['url'] or r['url'] == self.url), None)
            main_headers = main_response['headers'] if main_response else {}

            await ctx_desktop.close()

            console.log("[cyan]Testing mobile viewport...[/cyan]")
            ctx_mobile = await browser.new_context(
                viewport={"width": 390, "height": 844},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15"
            )
            mob_page = await ctx_mobile.new_page()
            mob_t0 = time.time()
            try:
                await mob_page.goto(self.url, wait_until="networkidle", timeout=30000)
            except Exception:
                pass
            mob_load_ms = int((time.time() - mob_t0) * 1000)
            mob_viewport_ok = await mob_page.evaluate("() => document.querySelector('meta[name=\\"viewport\\"]') !== null")
            mob_screenshot = "screenshot_mobile.png"
            await mob_page.screenshot(path=mob_screenshot)
            await ctx_mobile.close()

            await browser.close()

            resource_summary = {}
            for r in requests_log:
                rt = r['resource_type']
                resource_summary[rt] = resource_summary.get(rt, 0) + 1

            self.data = {
                "url": self.url,
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
                "screenshot_desktop": screenshot_path,
                "screenshot_mobile": mob_screenshot,
            }

            console.log(f"[green]\u2713 Data collection complete ({load_time_ms}ms load time)[/green]")
            return self.data


# ───────────────────────────────────────────────────────────────────────────────
# CLAUDE AI ANALYST
# ───────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert senior frontend QA engineer, web performance specialist, security auditor, and accessibility consultant. You receive real browser-collected data about a website and produce a structured, actionable audit report.

Your analysis is:
- Specific (reference actual values from the data, not generic advice)
- Prioritized (highlight critical issues first)
- Actionable (each finding has a clear fix)
- Balanced (acknowledge what's working well)

Always return valid JSON only \u2014 no markdown, no explanation outside the JSON."""

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
# REPORT RENDERER
# ───────────────────────────────────────────────────────────────────────────────

STATUS_COLORS = {"pass": "green", "fail": "red", "warn": "yellow", "info": "blue"}
PRIORITY_COLORS = {"critical": "red", "high": "orange3", "medium": "yellow", "low": "dim"}

def render_report(report: dict):
    console.print()
    console.rule("[bold cyan]AI WEBSITE AUDIT REPORT[/bold cyan]")
    console.print()

    score = report.get("overall_score", 0)
    grade = report.get("grade", "?")
    score_color = "green" if score >= 80 else "yellow" if score >= 60 else "red"

    console.print(Panel(
        f"[bold]URL:[/bold] {report.get('url')}\n"
        f"[bold]Overall Score:[/bold] [{score_color}]{score}/100 (Grade {grade})[/{score_color}]\n"
        f"[bold]Summary:[/bold] {report.get('summary', '')}",
        title="\U0001f4ca Overview", border_style="cyan"
    ))

    cat_table = Table(title="Category Scores", show_header=True, header_style="bold magenta")
    cat_table.add_column("Category", style="cyan")
    cat_table.add_column("Score", justify="center")
    cat_table.add_column("Bar", min_width=20)

    for cat, score_val in report.get("category_scores", {}).items():
        color = "green" if score_val >= 80 else "yellow" if score_val >= 60 else "red"
        bar_filled = int(score_val / 5)
        bar = f"[{color}]{'\u2588' * bar_filled}[/{color}]{'\u2591' * (20 - bar_filled)}"
        cat_table.add_row(cat, f"[{color}]{score_val}[/{color}]", bar)

    console.print(cat_table)
    console.print()

    critical = report.get("critical_issues", [])
    if critical:
        console.print(Panel(
            "\n".join(f"[red]\u2022 {c}[/red]" for c in critical),
            title="\U0001f6a8 Critical Issues", border_style="red"
        ))
        console.print()

    wins = report.get("quick_wins", [])
    if wins:
        console.print(Panel(
            "\n".join(f"[green]\u2713 {w}[/green]" for w in wins),
            title="\u26a1 Quick Wins", border_style="green"
        ))
        console.print()

    console.print("[bold cyan]Detailed Findings[/bold cyan]")
    console.print()
    for result in report.get("results", []):
        status = result.get("status", "info")
        priority = result.get("priority", "low")
        color = STATUS_COLORS.get(status, "white")
        p_color = PRIORITY_COLORS.get(priority, "white")

        header = (
            f"{result.get('icon', '')} [{color}]{result.get('title')}[/{color}]  "
            f"[{color}][{status.upper()}][/{color}]  "
            f"[{p_color}](priority: {priority})[/{p_color}]"
        )
        body = result.get("summary", "") + "\n"
        for item in result.get("items", []):
            val = f" \u2192 [italic]{item.get('value', '')}[/italic]" if item.get("value") else ""
            rec = f"\n    [dim]Fix: {item.get('recommendation', '')}[/dim]" if item.get("recommendation") else ""
            body += f"\n  \u2022 [bold]{item.get('label')}[/bold]{val}{rec}"

        console.print(Panel(body, title=header, border_style=color))

    console.print()
    console.rule("[bold cyan]End of Report[/bold cyan]")


# ───────────────────────────────────────────────────────────────────────────────
# MAIN
# ───────────────────────────────────────────────────────────────────────────────

async def main(url: str, categories: list, api_key: str, output: Optional[str]):
    console.print(Panel(
        f"[bold cyan]AI Website Tester Agent[/bold cyan]\n"
        f"Target: [green]{url}[/green]\n"
        f"Categories: {', '.join(categories)}",
        border_style="cyan"
    ))

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("Collecting browser data...", total=None)
        collector = BrowserCollector(url)
        browser_data = await collector.collect()
        progress.remove_task(task)

    console.log(f"[green]\u2713 Collected {browser_data['total_requests']} network requests[/green]")
    console.log(f"[green]\u2713 Page load: {browser_data['load_time_ms']}ms desktop / {browser_data['mobile_load_time_ms']}ms mobile[/green]")
    console.log(f"[green]\u2713 Console errors: {len(browser_data['console_errors'])}[/green]")

    console.print()
    console.log("[cyan]Sending data to Claude AI for analysis...[/cyan]")

    client = anthropic.Anthropic(api_key=api_key)

    with Progress(SpinnerColumn(), TextColumn("{task.description}"), console=console) as progress:
        task = progress.add_task("Claude is analyzing...", total=None)
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_user_prompt(browser_data, categories)}]
        )
        progress.remove_task(task)

    raw = message.content[0].text
    console.log("[green]\u2713 AI analysis complete[/green]")

    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        start = clean.index("{")
        end = clean.rindex("}") + 1
        report = json.loads(clean[start:end])
    except Exception as e:
        console.print(f"[red]Failed to parse Claude response: {e}[/red]")
        console.print(Syntax(raw[:2000], "json", theme="monokai"))
        sys.exit(1)

    render_report(report)

    if output:
        full_output = {
            "report": report,
            "browser_data": browser_data,
            "generated_at": datetime.now().isoformat()
        }
        with open(output, "w") as f:
            json.dump(full_output, f, indent=2, default=str)
        console.print(f"\n[green]\u2713 Full report saved to: {output}[/green]")


if __name__ == "__main__":
    import os
    parser = argparse.ArgumentParser(description="AI Website Tester Agent \u2014 Playwright + Claude")
    parser.add_argument("--url", required=True, help="Website URL to test (must start with http/https)")
    parser.add_argument("--categories", nargs="+", default=ALL_CATEGORIES,
                        choices=ALL_CATEGORIES, metavar="CAT",
                        help=f"Test categories to run (default: all). Choices: {', '.join(ALL_CATEGORIES)}")
    parser.add_argument("--api-key", default=None,
                        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument("--output", default=None,
                        help="Save full JSON report to this file (e.g. report.json)")
    parser.add_argument("--model", default=CLAUDE_MODEL,
                        help=f"Claude model to use (default: {CLAUDE_MODEL})")

    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        console.print("[red]Error: Anthropic API key required.[/red]")
        console.print("Set it via --api-key or export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    if not args.url.startswith("http"):
        console.print("[red]Error: URL must start with http:// or https://[/red]")
        sys.exit(1)

    CLAUDE_MODEL = args.model

    asyncio.run(main(args.url, args.categories, api_key, args.output))
