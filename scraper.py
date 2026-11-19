"""Free local company-domain discovery. Search results are candidates, not ownership proof."""
import re
import time
import sqlite3
import random
from datetime import datetime, timezone
from urllib.parse import urlparse, quote
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS
from rapidfuzz.fuzz import ratio

DATA_DIR = Path("local_data")
DATA_DIR.mkdir(exist_ok=True)
DB = DATA_DIR / "domain_finder.sqlite3"
HEADERS = {"User-Agent": "BWC-DomainFinder/1.0 (respectful company website research)"}
EXCLUDE = {
    "linkedin.com", "facebook.com", "instagram.com", "youtube.com", "twitter.com",
    "x.com", "reddit.com", "glassdoor.com", "indeed.com", "crunchbase.com",
    "zoominfo.com", "kompass.com", "europages.com", "wikipedia.org",
    "wikidata.org", "northdata.de", "yellowpages.com", "dnb.com",
    "amazon.com", "google.com", "duckduckgo.com", "bing.com",
}
LEGAL = {
    "gmbh", "ag", "se", "ug", "mbh", "co", "ltd", "limited", "inc",
    "corp", "corporation", "llc", "bv", "srl", "sa", "pvt",
    "private", "plc", "kg", "and", "the", "of",
}


def connect():
    cx = sqlite3.connect(DB)
    cx.execute("""CREATE TABLE IF NOT EXISTS results(
        company TEXT PRIMARY KEY, domain TEXT, confidence INTEGER DEFAULT 0,
        evidence_url TEXT, evidence_title TEXT, matched_name TEXT,
        status TEXT DEFAULT 'pending', notes TEXT DEFAULT '',
        checked_at TEXT
    )""")
    cx.commit()
    return cx


def normalize(name):
    text = str(name or "").casefold().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text)).strip()


def tokens(name):
    return [s for s in normalize(name).split() if s not in LEGAL and len(s) > 1]


def domain_from(url):
    try:
        p = urlparse(url if "://" in url else "https://" + url)
        hostname = (p.hostname or "").lower().removeprefix("www.")
        if not hostname or "." not in hostname or any(hostname == x or hostname.endswith("." + x) for x in EXCLUDE):
            return ""
        return hostname
    except (ValueError, TypeError):
        return ""


def name_fit(company, text):
    wanted = tokens(company)
    if not wanted:
        return 0
    sample = normalize(text)
    present = sum(1 for t in wanted if re.search(r"\b" + re.escape(t) + r"\b", sample))
    coverage = present / len(wanted)
    clean = " ".join(wanted)
    comparable = " ".join(tokens(text))
    fuzzy = ratio(clean, comparable) / 100 if comparable else 0
    return max(0, min(1, coverage * 0.8 + fuzzy * 0.2))


def site_evidence(url):
    """Inspect only one landing page. Do not bypass access restrictions."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=(5, 12), allow_redirects=True)
        if response.status_code != 200 or "text/html" not in response.headers.get("content-type", "").lower():
            return "", "", "Site unavailable or non-HTML"
        soup = BeautifulSoup(response.text[:300000], "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
        desc = meta.get("content", "") if meta else ""
        h1 = " ".join(x.get_text(" ", strip=True) for x in soup.find_all("h1", limit=2))
        return title[:250], (title + " " + desc + " " + h1)[:1200], ""
    except requests.RequestException as e:
        return "", "", type(e).__name__


def lookup(company, country="", max_results=5, pause=2.0):
    """Find website candidates through unofficial DDG search and verify homepage text."""
    queries = [f'"{company}" official website']
    short = " ".join(tokens(company))
    if short and normalize(short) != normalize(company):
        queries.append(f'"{short}" company {country} official site'.strip())
    candidates = {}
    errors = []
    for q in queries:
        try:
            with DDGS() as client:
                results = list(client.text(q, max_results=max_results))
            for result in results:
                url = result.get("href") or result.get("url") or ""
                dom = domain_from(url)
                if not dom:
                    continue
                # Rank evidence from the search result, but verify against company site.
                old = candidates.get(dom)
                if old is None or name_fit(company, result.get("title", "")) > name_fit(company, old.get("title", "")):
                    candidates[dom] = dict(result, href=url)
        except Exception as e:
            errors.append(f"Search failed: {type(e).__name__}: {str(e)[:100]}")
        time.sleep(pause + random.uniform(0, 0.8))
        if len(candidates) >= max_results:
            break

    scored = []
    for dom, result in list(candidates.items())[:max_results + 3]:
        url = result.get("href", "")
        title, evidence, err = site_evidence(url)
        search_title = result.get("title", "")
        search_snippet = result.get("body", "")
        company_score = name_fit(company, evidence)
        result_score = max(name_fit(company, search_title), name_fit(company, search_snippet))
        # A high score needs the company's own homepage text, not solely search snippets.
        if company_score >= 0.80:
            confidence = min(92, int(60 + company_score * 30 + result_score * 2))
        elif company_score >= 0.50:
            confidence = min(72, int(30 + company_score * 45))
        else:
            confidence = min(49, int(10 + result_score * 40))
        if err:
            confidence = min(confidence, 39)
        scored.append({
            "domain": dom, "confidence": confidence, "evidence_url": url,
            "evidence_title": title or search_title,
            "matched_name": search_title, "notes": err or "Match based on homepage title, meta description and headings",
        })
        time.sleep(max(0.5, pause / 2))
    scored.sort(key=lambda row: row["confidence"], reverse=True)
    if not scored:
        return {
            "domain": "", "confidence": 0, "evidence_url": "",
            "evidence_title": "", "matched_name": "",
            "status": "review", "notes": "; ".join(errors) or "No usable search results; manual research required",
        }
    best = scored[0]
    if len(scored) > 1 and scored[1]["domain"] != best["domain"] and best["confidence"] - scored[1]["confidence"] < 8:
        best["confidence"] = min(best["confidence"], 64)
        best["notes"] += "; competing domains found"
    best["status"] = "review"
    if errors:
        best["notes"] += "; " + "; ".join(errors)
    return best


def write_result(cx, company, result):
    cx.execute("""INSERT INTO results(company, domain, confidence, evidence_url, evidence_title,
            matched_name, status, notes, checked_at) VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(company) DO UPDATE SET
            domain=excluded.domain, confidence=excluded.confidence,
            evidence_url=excluded.evidence_url, evidence_title=excluded.evidence_title,
            matched_name=excluded.matched_name, status=excluded.status,
            notes=excluded.notes, checked_at=excluded.checked_at""",
        (company, result.get("domain", ""), result.get("confidence", 0),
         result.get("evidence_url", ""), result.get("evidence_title", ""),
         result.get("matched_name", ""), result.get("status", "review"),
         result.get("notes", ""), datetime.now(timezone.utc).isoformat()))
    cx.commit()
