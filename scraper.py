"""Free local company-domain discovery. Search results are candidates, not ownership proof."""
import re
import time
import sqlite3
import random
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
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


def _create_results(cx):
    cx.execute("""CREATE TABLE IF NOT EXISTS results(
        company TEXT NOT NULL, country TEXT NOT NULL DEFAULT '',
        domain TEXT, confidence INTEGER DEFAULT 0,
        evidence_url TEXT, evidence_title TEXT, matched_name TEXT,
        status TEXT DEFAULT 'pending', notes TEXT DEFAULT '',
        checked_at TEXT, PRIMARY KEY(company, country)
    )""")


def connect():
    cx = sqlite3.connect(DB, timeout=30)
    try:
        # Serialize schema changes and roll back the entire migration on error.
        with cx:
            cx.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in cx.execute("PRAGMA table_info(results)")}
            if columns and "country" not in columns:
                cx.execute("ALTER TABLE results RENAME TO results_legacy")
                _create_results(cx)
                cx.execute("""INSERT INTO results(company, country, domain, confidence,
                    evidence_url, evidence_title, matched_name, status, notes, checked_at)
                    SELECT company, '', domain, confidence, evidence_url, evidence_title,
                           matched_name, status, notes, checked_at FROM results_legacy""")
                cx.execute("DROP TABLE results_legacy")
            else:
                _create_results(cx)
        return cx
    except Exception:
        cx.close()
        raise


def company_key(company, country=""):
    return str(company or "").strip(), str(country or "").strip()


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
    """Name fit is deliberately conservative for short and ambiguous names."""
    wanted = set(tokens(company))
    if not wanted:
        return 0
    sample_tokens = set(tokens(text))
    if not sample_tokens:
        return 0
    present = len(wanted & sample_tokens)
    coverage = present / len(wanted)
    extras = len(sample_tokens - wanted)
    # Do not treat 'AGRO AG' and 'Danish Agro' as an exact company match.
    if len(wanted) <= 2 and extras:
        coverage *= 0.60
    clean = " ".join(tokens(company))
    comparable = " ".join(tokens(text))
    fuzzy = ratio(clean, comparable) / 100 if comparable else 0
    return max(0, min(1, coverage * 0.85 + fuzzy * 0.15))


def discover_search(company, country="", max_results=6, pause=2.0):
    """Try different public search backends with explicit failure reporting."""
    short = " ".join(tokens(company))
    queries = [f'"{company}" {country} official website'.strip()]
    if short and normalize(short) != normalize(company):
        queries.append(f'"{short}" {country} company website'.strip())
    candidates, errors = {}, []
    for query in queries[:2]:
        for backend in ("duckduckgo", "bing", "google"):
            try:
                with DDGS(timeout=15) as client:
                    results = list(client.text(query, backend=backend, max_results=max_results))
                for item in results:
                    url = item.get("href") or item.get("url") or ""
                    dom = domain_from(url)
                    if not dom:
                        continue
                    item["search_source"] = backend
                    if dom not in candidates:
                        candidates[dom] = dict(item, href=url)
                if len(candidates) >= max_results:
                    return candidates, errors
            except Exception as exc:
                errors.append(f"{backend}: {type(exc).__name__} {str(exc)[:80]}")
            time.sleep(pause + random.uniform(0, 0.5))
        # Unofficial libraries change; Bing RSS offers a second public discovery path,
        # subject to availability and service access rules.
        if len(candidates) < max_results:
            try:
                resp = requests.get("https://www.bing.com/search",
                                    params={"q": query, "format": "rss"},
                                    headers=HEADERS, timeout=(5, 15))
                resp.raise_for_status()
                root = ET.fromstring(resp.content[:250000])
                for item in root.findall(".//item"):
                    url = item.findtext("link") or ""
                    dom = domain_from(url)
                    if not dom:
                        continue
                    candidates.setdefault(dom, {
                        "href": url, "title": item.findtext("title") or "",
                        "body": item.findtext("description") or "",
                        "search_source": "bing-rss",
                    })
            except Exception as exc:
                errors.append(f"bing-rss: {type(exc).__name__} {str(exc)[:80]}")
            time.sleep(pause)
    return candidates, errors


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


def lookup(company, country="", max_results=6, pause=2.0):
    """Discover candidate domains and check whether site content supports ownership."""
    candidates, errors = discover_search(company, country, max_results, pause)
    scored = []
    for dom, result in list(candidates.items())[:max_results + 4]:
        url = result.get("href", "")
        title, evidence, err = site_evidence(url)
        search_title = result.get("title", "")
        search_snippet = result.get("body", "")
        company_score = name_fit(company, evidence)
        search_score = max(name_fit(company, search_title), name_fit(company, search_snippet))
        # Pages may be unrelated despite matching name fragments.
        if company_score >= 0.85:
            confidence = min(92, int(65 + company_score * 25))
        elif company_score >= 0.65:
            confidence = min(74, int(40 + company_score * 40))
        elif company_score >= 0.40:
            confidence = min(55, int(18 + company_score * 55))
        else:
            confidence = min(35, int(search_score * 35))
        if err:
            confidence = min(confidence, 32)
        scored.append({
            "domain": dom, "confidence": confidence,
            "evidence_url": url, "evidence_title": title or search_title,
            "matched_name": search_title,
            "notes": f"Search: {result.get('search_source', 'unknown')}. "
                     + (err or "Compared website title, meta description and headings"),
        })
        time.sleep(max(0.5, pause / 2))

    scored.sort(key=lambda row: row["confidence"], reverse=True)
    if not scored or scored[0]["confidence"] < 40:
        return {
            "domain": "", "confidence": 0, "evidence_url": "",
            "evidence_title": "", "matched_name": "",
            "status": "review",
            "notes": "No defensible domain match. " + "; ".join(errors[:3]),
        }
    best = scored[0]
    if len(scored) > 1 and best["confidence"] - scored[1]["confidence"] < 10:
        best["confidence"] = min(best["confidence"], 60)
        best["notes"] += "; competing plausible domains"
    best["status"] = "review"
    if errors:
        best["notes"] += "; some search sources unavailable: " + "; ".join(errors[:2])
    return best


def eligible_companies(companies, saved_rows, retry_unmatched=False):
    """Keep manually approved or rejected results out of automated retries."""
    saved = {company_key(row["company"], row.get("country", "")): row for row in saved_rows}
    pending = []
    seen = set()
    for company, country in companies:
        key = company_key(company, country)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        row = saved.get(key)
        if row is None or (retry_unmatched and not row.get("domain")
                           and row.get("status") not in {"approved", "rejected"}):
            pending.append(key)
    return pending


def write_result(cx, company, result, country=""):
    company, country = company_key(company, country)
    if not company:
        raise ValueError("Company name is required")
    cx.execute("""INSERT INTO results(company, country, domain, confidence, evidence_url, evidence_title,
            matched_name, status, notes, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(company, country) DO UPDATE SET
            domain=excluded.domain, confidence=excluded.confidence,
            evidence_url=excluded.evidence_url, evidence_title=excluded.evidence_title,
            matched_name=excluded.matched_name, status=excluded.status,
            notes=excluded.notes, checked_at=excluded.checked_at
        WHERE COALESCE(results.status, '') NOT IN ('approved', 'rejected')""",
        (company, country, result.get("domain", ""), result.get("confidence", 0),
         result.get("evidence_url", ""), result.get("evidence_title", ""),
         result.get("matched_name", ""), result.get("status", "review"),
         result.get("notes", ""), datetime.now(timezone.utc).isoformat()))
    cx.commit()
