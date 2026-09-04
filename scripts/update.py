#!/usr/bin/env python3
"""Hourly ingest for the Clancy case tracker.

Pulls public RSS/Atom feeds (no API keys), normalizes items, de-duplicates,
merges them into an append-only archive under data/, and writes a status file
that the page uses to show freshness and per-source health.

Standard library only so the GitHub Actions job has nothing to install.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
ARTICLES = os.path.join(DATA, "articles.json")
VIDEOS = os.path.join(DATA, "videos.json")
DISCUSSION = os.path.join(DATA, "discussion.json")
STATUS = os.path.join(DATA, "status.json")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 ClancyCaseTracker/1.0"
TIMEOUT = 25
MAX_ARTICLES = 8000
MAX_VIDEOS = 1500
MAX_DISCUSSION = 1500

# Relevance gate: an item must mention the case to be kept.
RELEVANT = re.compile(r"clancy", re.I)

GN = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
BING = "https://www.bing.com/news/search?q={q}&format=rss&setlang=en-US&mkt=en-US"
YT = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

SOURCES = [
    # Google News aggregates hundreds of outlets; several queries widen recall.
    {"id": "gnews-name", "kind": "gnews", "label": "Google News: \"Lindsay Clancy\"", "url": GN.format(q="%22Lindsay+Clancy%22")},
    {"id": "gnews-duxbury", "kind": "gnews", "label": "Google News: Clancy Duxbury", "url": GN.format(q="Clancy+Duxbury")},
    {"id": "gnews-trial", "kind": "gnews", "label": "Google News: Clancy trial Brockton", "url": GN.format(q="Clancy+trial+Brockton")},
    {"id": "gnews-postpartum", "kind": "gnews", "label": "Google News: Clancy postpartum", "url": GN.format(q="Clancy+postpartum")},
    {"id": "gnews-sjc", "kind": "gnews", "label": "Google News: Clancy SJC appeal", "url": GN.format(q="Clancy+SJC+OR+appeal+OR+retrial+Duxbury")},
    # Bing gives direct article URLs (useful for de-dup against Google's redirect links).
    {"id": "bing-name", "kind": "bing", "label": "Bing News: \"Lindsay Clancy\"", "url": BING.format(q="%22Lindsay+Clancy%22")},
    {"id": "bing-duxbury", "kind": "bing", "label": "Bing News: Clancy Duxbury", "url": BING.format(q="Clancy+Duxbury")},
    # YouTube channel feeds (filtered to items that mention Clancy).
    {"id": "yt-wcvb", "kind": "youtube", "label": "YouTube: WCVB", "url": YT.format(cid="UC72UssJ1DNQcakZXm-B2-zw")},
    {"id": "yt-cbsboston", "kind": "youtube", "label": "YouTube: CBS Boston / WBZ", "url": YT.format(cid="UCi4fcBVyo4CAnmdgXeO-NvA")},
    {"id": "yt-nbc10", "kind": "youtube", "label": "YouTube: NBC10 Boston", "url": YT.format(cid="UCTaVAYaqEV3k6ZxIAP77-jQ")},
    {"id": "yt-boston25", "kind": "youtube", "label": "YouTube: Boston 25", "url": YT.format(cid="UChRLNCEp9Ga3AnjMa6FzgzA")},
    {"id": "yt-courttv", "kind": "youtube", "label": "YouTube: Court TV", "url": YT.format(cid="UCo5E9pEhK_9kWG7-5HHcyRg")},
    {"id": "yt-lawcrime", "kind": "youtube", "label": "YouTube: Law&Crime", "url": YT.format(cid="UCz8K1occVvDTYDfFo7N5EZw")},
    {"id": "yt-wbur", "kind": "youtube", "label": "YouTube: WBUR", "url": YT.format(cid="UCBS9Fn8-aFK6ezfvQitokOQ")},
    {"id": "yt-nbcnews", "kind": "youtube", "label": "YouTube: NBC News", "url": YT.format(cid="UCeY0bbntWzzVIaj2z3QigXg")},
    # Reddit is best-effort: it often blocks datacenter IPs. Failure is tolerated.
    {"id": "reddit-search", "kind": "reddit", "label": "Reddit: search \"Lindsay Clancy\"", "url": "https://www.reddit.com/search.rss?q=%22Lindsay+Clancy%22&sort=new&type=link", "optional": True},
    {"id": "reddit-sub1", "kind": "reddit", "label": "Reddit: r/TheLindsayClancyCase", "url": "https://www.reddit.com/r/TheLindsayClancyCase/new.rss", "optional": True},
    {"id": "reddit-sub2", "kind": "reddit", "label": "Reddit: r/DuxburyDeathsFreeTalk", "url": "https://www.reddit.com/r/DuxburyDeathsFreeTalk/new.rss", "optional": True},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def fetch(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8"})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed: {last}")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def to_iso(dt_text: str | None) -> str | None:
    if not dt_text:
        return None
    dt_text = dt_text.strip()
    try:
        dt = parsedate_to_datetime(dt_text)
    except Exception:  # noqa: BLE001
        try:
            dt = datetime.fromisoformat(dt_text.replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm_title(t: str) -> str:
    t = t.lower()
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t)  # drop trailing " - Source"
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def norm_url(u: str) -> str:
    try:
        p = urllib.parse.urlsplit(u)
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid", "ref", "ncid", "cmpid"))]
        return urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(q), ""))
    except Exception:  # noqa: BLE001
        return u


NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


def parse_rss_items(root: ET.Element):
    for it in root.iter("item"):
        yield {
            "title": strip_html(it.findtext("title")),
            "link": (it.findtext("link") or "").strip(),
            "pub": it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date"),
            "desc": strip_html(it.findtext("description")),
            "source": strip_html(it.findtext("source")) or strip_html(it.findtext("{https://www.bing.com/news/search?q=%22Lindsay+Clancy%22&format=rss&setlang=en-US&mkt=en-US}Source")),
            "_el": it,
        }


def parse_atom_entries(root: ET.Element):
    for e in root.findall("atom:entry", NS):
        link_el = e.find("atom:link", NS)
        link = link_el.get("href") if link_el is not None else ""
        yield {
            "title": strip_html(e.findtext("atom:title", namespaces=NS)),
            "link": (link or "").strip(),
            "pub": e.findtext("atom:published", namespaces=NS) or e.findtext("atom:updated", namespaces=NS),
            "desc": strip_html(e.findtext("atom:summary", namespaces=NS) or e.findtext("atom:content", namespaces=NS)),
            "source": strip_html(e.findtext("atom:author/atom:name", namespaces=NS)),
            "_el": e,
        }


def parse_feed(raw: bytes):
    text = raw.decode("utf-8", errors="replace")
    # Bing's News: namespace is bound to a URL that changes per query; rebind it to a stable one.
    text = re.sub(r'xmlns:News="[^"]*"', 'xmlns:News="urn:bing-news"', text)
    root = ET.fromstring(text)
    tag = root.tag.lower()
    if tag.endswith("feed"):
        return list(parse_atom_entries(root)), "atom"
    return list(parse_rss_items(root)), "rss"


def bing_source(el: ET.Element) -> str:
    return strip_html(el.findtext("{urn:bing-news}Source"))


def bing_real_url(link: str) -> str:
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
        if "url" in q:
            return q["url"][0]
    except Exception:  # noqa: BLE001
        pass
    return link


def gnews_source(title: str, source_el_text: str) -> tuple[str, str]:
    if source_el_text:
        src = source_el_text
    else:
        m = re.search(r"\s-\s([^-]{2,60})$", title)
        src = m.group(1).strip() if m else ""
    clean = re.sub(r"\s-\s" + re.escape(src) + r"$", "", title).strip() if src else title
    return clean, src


def normalize(src: dict, raw_items: list[dict]) -> list[dict]:
    out = []
    for it in raw_items:
        title, link = it["title"], it["link"]
        if not title or not link:
            continue
        source_name = it["source"] or ""
        kind = src["kind"]
        if kind == "gnews":
            title, source_name = gnews_source(title, source_name)
        elif kind == "bing":
            source_name = bing_source(it["_el"]) or source_name
            link = bing_real_url(link)
        elif kind == "youtube":
            source_name = src["label"].replace("YouTube: ", "")
        elif kind == "reddit":
            m = re.search(r"reddit\.com/r/([^/]+)/", link)
            source_name = f"r/{m.group(1)}" if m else "Reddit"
        text_blob = f"{title} {it['desc']}"
        if not RELEVANT.search(text_blob):
            continue
        # Reddit search results sometimes return subreddit listings rather than posts.
        if kind == "reddit" and "/comments/" not in link:
            continue
        pub = to_iso(it["pub"]) or now_iso()
        desc = it["desc"]
        # Google News descriptions are just the headline plus outlet name; drop them.
        if kind == "gnews" or norm_title(desc[: len(title) + 5]).startswith(norm_title(title)[:40]):
            desc = ""
        host = ""
        try:
            host = urllib.parse.urlsplit(link).netloc.lower().replace("www.", "")
        except Exception:  # noqa: BLE001
            pass
        out.append({
            "title": title,
            "url": link,
            "source": source_name or host or src["label"],
            "host": host,
            "published": pub,
            "summary": desc[:400],
            "via": src["id"],
            "kind": kind,
        })
    return out


def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=0, separators=(",", ":"))
        f.write("\n")


def merge(existing: list[dict], incoming: list[dict], cap: int) -> tuple[list[dict], int]:
    """Merge incoming items into existing archive. Returns (merged, added_count)."""
    by_url = {}
    by_title = {}
    for e in existing:
        by_url[norm_url(e["url"])] = e
        by_title.setdefault(norm_title(e["title"]) + "|" + e.get("source", "").lower(), e)
    added = 0
    stamp = now_iso()
    for n in incoming:
        ku = norm_url(n["url"])
        kt = norm_title(n["title"]) + "|" + n.get("source", "").lower()
        hit = by_url.get(ku) or by_title.get(kt)
        if hit:
            # Prefer a direct publisher URL over a Google redirect if we learn one.
            if "news.google.com" in hit["url"] and "news.google.com" not in n["url"]:
                hit["url"] = n["url"]
                hit["host"] = n["host"]
            if not hit.get("summary") and n.get("summary"):
                hit["summary"] = n["summary"]
            continue
        n["first_seen"] = stamp
        existing.append(n)
        by_url[ku] = n
        by_title[kt] = n
        added += 1
    existing.sort(key=lambda x: (x.get("published") or ""), reverse=True)
    return existing[:cap], added


def main() -> int:
    status_prev = load_json(STATUS, {})
    articles = load_json(ARTICLES, {"items": []}).get("items", [])
    videos = load_json(VIDEOS, {"items": []}).get("items", [])
    discussion = load_json(DISCUSSION, {"items": []}).get("items", [])

    new_articles, new_videos, new_discussion = [], [], []
    health = []
    hard_failures = 0
    for src in SOURCES:
        entry = {"id": src["id"], "label": src["label"], "kind": src["kind"], "ok": False, "items": 0, "error": None}
        try:
            raw = fetch(src["url"])
            items, _fmt = parse_feed(raw)
            norm = normalize(src, items)
            entry["ok"] = True
            entry["items"] = len(norm)
            if src["kind"] == "youtube":
                new_videos.extend(norm)
            elif src["kind"] == "reddit":
                new_discussion.extend(norm)
            else:
                new_articles.extend(norm)
        except Exception as e:  # noqa: BLE001
            entry["error"] = str(e)[:200]
            if not src.get("optional"):
                hard_failures += 1
        health.append(entry)
        print(f"[{'ok' if entry['ok'] else 'FAIL'}] {src['id']}: {entry['items']} items {entry['error'] or ''}")

    # If every core (non-optional) source failed, the network is the problem, not the feeds.
    # Write nothing so a broken environment can never publish an empty or all-failed status.
    core = [h for h, s in zip(health, SOURCES) if not s.get("optional")]
    if core and all(not h["ok"] for h in core):
        print("all core sources failed; leaving data/ untouched", file=sys.stderr)
        return 1

    articles, a_added = merge(articles, new_articles, MAX_ARTICLES)
    videos, v_added = merge(videos, new_videos, MAX_VIDEOS)
    discussion, d_added = merge(discussion, new_discussion, MAX_DISCUSSION)

    stamp = now_iso()
    save_json(ARTICLES, {"generated": stamp, "count": len(articles), "items": articles})
    save_json(VIDEOS, {"generated": stamp, "count": len(videos), "items": videos})
    save_json(DISCUSSION, {"generated": stamp, "count": len(discussion), "items": discussion})

    runs = status_prev.get("runs", 0) + 1
    history = status_prev.get("history", [])[-167:]  # keep about a week of hourly runs
    history.append({"at": stamp, "added": a_added + v_added + d_added, "failed_sources": sum(1 for h in health if not h["ok"])})
    save_json(STATUS, {
        "generated": stamp,
        "runs": runs,
        "added": {"articles": a_added, "videos": v_added, "discussion": d_added},
        "totals": {"articles": len(articles), "videos": len(videos), "discussion": len(discussion)},
        "sources": health,
        "history": history,
    })
    print(f"added: {a_added} articles, {v_added} videos, {d_added} discussion; totals {len(articles)}/{len(videos)}/{len(discussion)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
