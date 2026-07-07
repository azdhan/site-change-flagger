#!/usr/bin/env python3
"""
Website change detector.

For every site in sites.yaml:
  1. Fetch the page. Static HTTP first; if the page is JavaScript-rendered
     (Next.js govt portals etc.) fall back to headless Chromium.
  2. Extract visible text + every hyperlink.
  3. Compare against the site's MEMORY of recently-seen content
     (not just the previous fetch — this makes bilingual sites,
     rotating banners and lazy-loaded sections false-alarm-proof):
       - a line/link never seen before  -> reported as NEW (after being
         confirmed by a second fetch in the same run)
       - a line missing for 3 consecutive runs -> reported as REMOVED
  4. Record changes in docs/data/changes.json (dashboard), docs/feed.xml
     (RSS) and alert.txt (email step in the GitHub Actions workflow).

First run for a site just creates a baseline (no alert).
Designed to run inside GitHub Actions; also runs locally.
"""

import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag

import requests
import urllib3
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
SNAP_DIR = ROOT / "snapshots"
DATA_DIR = ROOT / "docs" / "data"
FEED_PATH = ROOT / "docs" / "feed.xml"
ALERT_PATH = ROOT / "alert.txt"

MAX_CHANGELOG_ENTRIES = 1000   # keep the JSON changelog from growing forever
MAX_ADDED_LINES = 20           # preview lines stored per change
MAX_NEW_LINKS = 30             # links stored per change
FEED_ENTRIES = 50
MISS_LIMIT = 8                 # runs a line must be absent before "removed" (~24h)
GRAVE_DAYS = 30                # once reported removed, see-saw silently for this long

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or hashlib.md5(name.encode()).hexdigest()[:10]


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch(url: str) -> str:
    """Fetch a URL with retries. Government sites often have broken TLS
    chains, so fall back to unverified TLS rather than failing."""
    last_err = None
    for attempt in range(3):
        for verify in (True, False):
            try:
                resp = requests.get(
                    url, headers=HEADERS, timeout=45,
                    verify=verify, allow_redirects=True,
                )
                resp.raise_for_status()
                if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
                    resp.encoding = resp.apparent_encoding or "utf-8"
                return resp.text
            except requests.exceptions.SSLError as e:
                last_err = e
                continue  # retry immediately without verification
            except Exception as e:
                last_err = e
                break     # non-TLS error: back off and retry the loop
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"fetch failed after retries: {last_err}")


def fetch_rendered(url: str) -> str:
    """Render a JavaScript-heavy page (Next.js/React govt portals) in
    headless Chromium and return the final HTML. Requires playwright."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        # Full Chromium in new-headless mode with automation hints hidden —
        # the old headless shell gets 403'd by Akamai on many govt sites.
        browser = p.chromium.launch(
            headless=True,
            channel="chromium",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            ctx = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                ignore_https_errors=True,
                viewport={"width": 1366, "height": 768},
                locale="en-IN",
            )
            page = ctx.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )
            resp = page.goto(url, timeout=90000, wait_until="domcontentloaded")
            if resp and resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status} from {url}")
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass  # some pages poll forever; take what we have
            page.wait_for_timeout(4000)
            # scroll through the page so lazy-loaded sections (footers,
            # notice lists) are actually rendered before we snapshot
            page.evaluate(
                """async () => {
                    await new Promise(resolve => {
                        let total = 0;
                        const step = () => {
                            window.scrollBy(0, 700); total += 700;
                            if (total >= document.body.scrollHeight || total > 25000) resolve();
                            else setTimeout(step, 150);
                        };
                        step();
                    });
                }"""
            )
            page.wait_for_timeout(2500)
            # cookie/consent banners appear inconsistently between visits —
            # remove them so they never register as a "change"
            page.evaluate(
                """() => {
                    const rx = /cookie|consent|gdpr/i;
                    document.querySelectorAll('div,section,aside,dialog').forEach(el => {
                        const marker = String(el.id) + ' ' + String(el.className);
                        const text = el.innerText || '';
                        if (rx.test(marker) && text.length > 0 && text.length < 2500)
                            el.remove();
                    });
                }"""
            )
            return page.content()
        finally:
            browser.close()


BLOCK_MARKERS = re.compile(
    r"(?i)access denied|request unsuccessful|attention required|"
    r"errors\.edgesuite\.net|pardon our interruption|just a moment|"
    r"gateway time-?out|bad gateway|service unavailable|"
    r"internal server error|openresty|too many requests"
)


def looks_blocked(snap: dict) -> bool:
    """True when the fetched page is a bot-protection or server-error page."""
    return len(snap["lines"]) < 15 and any(
        BLOCK_MARKERS.search(ln) for ln in snap["lines"]
    )


def is_sparse(snap: dict) -> bool:
    """True when a fetch produced too little content to be the real page
    (typical of client-side-rendered apps or bot walls)."""
    return len(snap["lines"]) < 15 and len(snap["links"]) < 5


# --------------------------------------------------------------------------
# Extraction & diffing
# --------------------------------------------------------------------------

def extract(url: str, html_text: str, ignore_res: list) -> dict:
    """Return normalized visible text lines + all links on the page."""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "iframe", "svg"]):
        tag.decompose()

    raw_lines = (ln.strip() for ln in soup.get_text("\n").splitlines())
    lines, seen = [], set()
    for ln in raw_lines:
        ln = re.sub(r"\s+", " ", ln)
        if len(ln) < 3:
            continue
        if any(rx.search(ln) for rx in ignore_res):
            continue
        if ln in seen:  # dedupe repeated nav items
            continue
        seen.add(ln)
        lines.append(ln)

    links, seen_links = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#")) or not href:
            continue
        absolute = urldefrag(urljoin(url, href)).url
        if not absolute.startswith("http"):
            continue
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:200]
        if absolute in seen_links:
            continue
        seen_links.add(absolute)
        links.append({"url": absolute, "text": text})

    return {"lines": lines, "links": links}


DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def script_of(text: str) -> str:
    return "deva" if DEVANAGARI.search(text) else "latin"


def dominant_script(lines: list) -> str:
    """Which script most of the page is in right now. Bilingual govt sites
    randomly serve Hindi or English; we must not treat that as a change."""
    if not lines:
        return "latin"
    deva = sum(1 for ln in lines if DEVANAGARI.search(ln))
    return "deva" if deva > len(lines) / 2 else "latin"


def seed_memory(snap: dict) -> dict:
    return {
        "lines": {ln: 0 for ln in snap["lines"]},
        "links": {l["url"]: 0 for l in snap["links"]},
    }


def within_days(ts: str, now_ts: str, days: int) -> bool:
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return (datetime.strptime(now_ts, fmt) - datetime.strptime(ts, fmt)).days < days
    except Exception:
        return False


def additions(memory: dict, snap: dict) -> dict:
    """Content in this fetch that the site's memory has never seen."""
    return {
        "added_lines": [ln for ln in snap["lines"] if ln not in memory["lines"]],
        "new_links": [l for l in snap["links"] if l["url"] not in memory["links"]],
    }


def confirm_added(first: dict, second: dict) -> dict:
    """Keep only additions that appear in BOTH fetches of this run — kills
    false alarms from rotating banners and lazy-loaded sections."""
    added2 = set(second["added_lines"])
    urls2 = {l["url"] for l in second["new_links"]}
    return {
        "added_lines": [l for l in first["added_lines"] if l in added2],
        "new_links": [l for l in first["new_links"] if l["url"] in urls2],
    }


def update_memory(memory: dict, snap: dict, run_time: str,
                  extra_lines: set = None, extra_urls: set = None) -> tuple[list, int]:
    """Fold this fetch into the site's memory. Content absent for
    MISS_LIMIT consecutive runs is reported as removed and forgotten.

    Language-aware: an English line's absence only counts while the page
    is rendering in English (and vice versa), so bilingual sites that
    alternate Hindi/English never produce false removals.

    extra_lines/extra_urls: content seen by the other fetch of this run —
    presence in either fetch counts, so a section that failed to load in
    one render doesn't accrue a false miss.
    Returns (removed_lines, removed_link_count)."""
    cur_lines = set(snap["lines"]) | (extra_lines or set())
    cur_urls = {l["url"] for l in snap["links"]} | (extra_urls or set())
    cur_script = dominant_script(snap["lines"])
    # a fetch far smaller than the memory suggests a half-loaded page —
    # freeze all removal counting for this run
    partial = len(cur_lines) < 0.25 * max(1, len(memory["lines"]))

    grave_lines = memory.setdefault("grave_lines", {})
    grave_links = memory.setdefault("grave_links", {})
    for grave in (grave_lines, grave_links):
        for k in list(grave):
            if not within_days(grave[k], run_time, GRAVE_DAYS):
                del grave[k]

    removed_lines = []
    for ln in list(memory["lines"]):
        if ln in cur_lines:
            memory["lines"][ln] = 0
        elif not partial and script_of(ln) == cur_script:
            memory["lines"][ln] += 1
            if memory["lines"][ln] >= MISS_LIMIT:
                del memory["lines"][ln]
                if ln not in grave_lines:      # report each removal once/month
                    removed_lines.append(ln)
                grave_lines[ln] = run_time
    for ln in cur_lines:
        memory["lines"].setdefault(ln, 0)

    removed_links = 0
    for u in list(memory["links"]):
        if u in cur_urls:
            memory["links"][u] = 0
        elif not partial:
            memory["links"][u] += 1
            if memory["links"][u] >= MISS_LIMIT:
                del memory["links"][u]
                if u not in grave_links:
                    removed_links += 1
                grave_links[u] = run_time
    for u in cur_urls:
        memory["links"].setdefault(u, 0)

    return removed_lines, removed_links


def make_entry(slug: str, name: str, url: str, run_time: str,
               added: dict, removed_lines: list, removed_links: int) -> dict:
    added_lines = added["added_lines"]
    new_links = added["new_links"]
    parts = []
    if new_links:
        parts.append(f"{len(new_links)} new link{'s' if len(new_links) != 1 else ''}")
    if added_lines:
        parts.append(f"{len(added_lines)} new text block{'s' if len(added_lines) != 1 else ''}")
    removed_total = len(removed_lines) + removed_links
    if removed_total:
        parts.append(f"{removed_total} item{'s' if removed_total != 1 else ''} no longer on page")
    return {
        "id": hashlib.md5(f"{slug}{run_time}".encode()).hexdigest()[:12],
        "site": name,
        "url": url,
        "time": run_time,
        "summary": ", ".join(parts),
        "added_lines": added_lines[:MAX_ADDED_LINES],
        "added_lines_total": len(added_lines),
        "removed_lines": removed_lines[:10],
        "removed_lines_total": removed_total,
        "new_links": new_links[:MAX_NEW_LINKS],
        "new_links_total": len(new_links),
    }


# --------------------------------------------------------------------------
# Output artifacts
# --------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def build_feed(changes: list):
    """Write an RSS 2.0 feed of the most recent changes."""
    items = []
    for c in changes[:FEED_ENTRIES]:
        link_bits = "".join(
            f"&lt;li&gt;&lt;a href=&quot;{html.escape(l['url'])}&quot;&gt;"
            f"{html.escape(l['text'] or l['url'])}&lt;/a&gt;&lt;/li&gt;"
            for l in c.get("new_links", [])[:10]
        )
        desc = html.escape(c["summary"])
        if c.get("added_lines"):
            desc += "&lt;br&gt;&lt;b&gt;New text:&lt;/b&gt; " + html.escape(
                " | ".join(c["added_lines"][:5])
            )
        if link_bits:
            desc += "&lt;br&gt;&lt;b&gt;New links:&lt;/b&gt;&lt;ul&gt;" + link_bits + "&lt;/ul&gt;"
        pub = datetime.strptime(c["time"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        ).strftime("%a, %d %b %Y %H:%M:%S GMT")
        items.append(
            "<item>"
            f"<title>{html.escape(c['site'])}: {html.escape(c['summary'])}</title>"
            f"<link>{html.escape(c['url'])}</link>"
            f"<guid isPermaLink=\"false\">{c['id']}</guid>"
            f"<pubDate>{pub}</pubDate>"
            f"<description>{desc}</description>"
            "</item>"
        )
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Govt Website Change Alerts</title>"
        "<link>https://github.com/</link>"
        "<description>Automated change detection for monitored government websites</description>"
        f"<lastBuildDate>{datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S GMT')}</lastBuildDate>"
        + "".join(items)
        + "</channel></rss>"
    )
    FEED_PATH.parent.mkdir(parents=True, exist_ok=True)
    FEED_PATH.write_text(feed, encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def check_site(site: dict, ignore_res: list):
    """Fetch one site, picking static vs browser rendering. Returns
    (snapshot, renderer)."""
    url = site["url"]
    renderer = "browser" if site.get("render") else site.get("_last_renderer")

    snap = None
    if renderer != "browser":
        snap = extract(url, fetch(url), ignore_res)
        if is_sparse(snap):
            print("  page looks JS-rendered; retrying with headless browser")
            renderer = "browser"
    if renderer == "browser":
        try:
            rendered = extract(url, fetch_rendered(url), ignore_res)
            if snap is None or len(rendered["lines"]) > len(snap["lines"]):
                snap = rendered
            else:
                renderer = "static"
        except ImportError:
            print("  playwright not installed — using static HTML only")
            renderer = "static"
            if snap is None:
                snap = extract(url, fetch(url), ignore_res)
        except Exception:
            if snap is None:
                raise
            print("  browser render failed; keeping static copy")
            renderer = "static"
    return snap, (renderer or "static")


def main():
    config = yaml.safe_load((ROOT / "sites.yaml").read_text(encoding="utf-8"))
    sites = config.get("sites", [])
    ignore_res = [re.compile(p) for p in config.get("ignore_patterns", [])]

    changes_path = DATA_DIR / "changes.json"
    status_path = DATA_DIR / "status.json"
    changelog = load_json(changes_path, [])
    status = load_json(status_path, {})

    run_time = now_utc()
    alerts = []

    for site in sites:
        name, url = site["name"], site["url"]
        slug = slugify(name)
        snap_path = SNAP_DIR / f"{slug}.json"
        st = status.get(slug, {})
        st.update({"name": name, "url": url, "last_checked": run_time})

        old_snap = load_json(snap_path, None)
        site["_last_renderer"] = old_snap.get("renderer") if old_snap else None

        print(f"[{name}] fetching {url} ...")
        try:
            new_snap, renderer = check_site(site, ignore_res)
        except Exception as e:
            print(f"  ERROR: {e}")
            st["ok"] = False
            st["error"] = str(e)[:300]
            status[slug] = st
            continue

        if looks_blocked(new_snap):
            print("  blocked/error page served — keeping previous snapshot")
            st["ok"] = False
            st["error"] = "Site served a blocked/error page this run"
            status[slug] = st
            continue

        st["ok"] = True
        st.pop("error", None)

        if old_snap is None:
            memory = seed_memory(new_snap)
            st["baseline_at"] = run_time
            print(f"  baseline created ({len(new_snap['lines'])} lines, "
                  f"{len(new_snap['links'])} links)")
        else:
            memory = old_snap.get("memory") or seed_memory(old_snap)
            added = additions(memory, new_snap)
            first_lines = set(new_snap["lines"])
            first_urls = {l["url"] for l in new_snap["links"]}
            fetch_script = dominant_script(new_snap["lines"])
            about_to_remove = [
                ln for ln, m in memory["lines"].items()
                if m >= MISS_LIMIT - 1 and ln not in first_lines
                and script_of(ln) == fetch_script
            ]
            extra_lines, extra_urls = set(), set()
            if added["added_lines"] or added["new_links"] or about_to_remove:
                # Re-fetch once: additions must appear in BOTH fetches to be
                # reported; removals must be absent from both to count.
                print("  change suspected — re-fetching to confirm")
                try:
                    if renderer == "browser":
                        second = extract(url, fetch_rendered(url), ignore_res)
                    else:
                        second = extract(url, fetch(url), ignore_res)
                    if not looks_blocked(second) and not is_sparse(second):
                        added = confirm_added(added, additions(memory, second))
                        extra_lines, extra_urls = first_lines, first_urls
                        new_snap = second
                except Exception as e:
                    print(f"  confirmation fetch failed ({e}); reporting first result")

            # When the site flips display language (Hindi <-> English), the
            # translated lines are "new" to memory but not news — absorb
            # them silently. New links still alert: URLs are language-free.
            prev_script = old_snap.get("script") or dominant_script(old_snap["lines"])
            cur_script = dominant_script(new_snap["lines"])
            if cur_script != prev_script and added["added_lines"]:
                translated = [ln for ln in added["added_lines"]
                              if script_of(ln) == cur_script]
                if translated:
                    print(f"  language flip ({prev_script} -> {cur_script}): "
                          f"absorbed {len(translated)} translated lines silently")
                added["added_lines"] = [ln for ln in added["added_lines"]
                                        if script_of(ln) != cur_script]

            # Content that was recently reported removed and now reappears
            # is a flaky section, not news — restore it silently.
            grave_lines = memory.get("grave_lines", {})
            grave_links = memory.get("grave_links", {})
            resurrected = [ln for ln in added["added_lines"] if ln in grave_lines]
            if resurrected:
                print(f"  {len(resurrected)} recently-removed lines reappeared — restored silently")
                added["added_lines"] = [ln for ln in added["added_lines"]
                                        if ln not in grave_lines]
            added["new_links"] = [l for l in added["new_links"]
                                  if l["url"] not in grave_links]

            removed_lines, removed_links = update_memory(
                memory, new_snap, run_time, extra_lines, extra_urls)

            if added["added_lines"] or added["new_links"] or removed_lines or removed_links:
                entry = make_entry(slug, name, url, run_time,
                                   added, removed_lines, removed_links)
                changelog.insert(0, entry)
                st["last_change"] = run_time
                alerts.append(entry)
                print(f"  CHANGED: {entry['summary']}")
            else:
                print("  no change")

        new_snap["renderer"] = renderer
        new_snap["fetched_at"] = run_time
        new_snap["script"] = dominant_script(new_snap["lines"])
        new_snap["memory"] = memory
        save_json(snap_path, new_snap)
        status[slug] = st

    changelog = changelog[:MAX_CHANGELOG_ENTRIES]
    save_json(changes_path, changelog)
    save_json(status_path, status)
    build_feed(changelog)

    if alerts:
        lines = [f"{len(alerts)} website(s) changed — {run_time}", ""]
        for a in alerts:
            lines.append(f"* {a['site']} — {a['summary']}")
            lines.append(f"  {a['url']}")
            for ln in a["added_lines"][:5]:
                lines.append(f"  + {ln[:150]}")
            for l in a["new_links"][:8]:
                label = l["text"] or "(link)"
                lines.append(f"  -> {label[:100]}: {l['url']}")
            lines.append("")
        ALERT_PATH.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n{len(alerts)} change(s) detected — alert.txt written")
    else:
        ALERT_PATH.unlink(missing_ok=True)
        print("\nNo changes detected on any site.")


if __name__ == "__main__":
    sys.exit(main())
