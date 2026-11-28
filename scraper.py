"""Free local company-domain discovery. Search results are candidates, not ownership proof."""
import re
import time
import sqlite3
import random
import ipaddress
import math
import json
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
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


# Process-local provider cooldowns. Bing RSS shares Bing's cooldown.
PROVIDERS = ("duckduckgo", "bing", "google", "wikidata")
_PROVIDER_UNTIL = {}


def provider_wait(provider):
    return max(0, math.ceil(_PROVIDER_UNTIL.get(provider, 0) - time.monotonic()))


def all_providers_wait():
    return min(provider_wait(provider) for provider in PROVIDERS)


def cooldown_after_error(provider, exc):
    """Honor explicit throttling and temporary service failures without bypassing them."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    message = (type(exc).__name__ + " " + str(exc)).lower()
    throttled = status in (403, 429) or any(
        phrase in message for phrase in ("ratelimit", "rate limit", "too many requests", "captcha", "429")
    )
    temporary = status == 503 or isinstance(exc, requests.Timeout)
    if not (throttled or temporary):
        return 0
    seconds = 300 if throttled else 60
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            seconds = max(seconds, int(retry_after))
        except (ValueError, TypeError):
            try:
                seconds = max(seconds, math.ceil((parsedate_to_datetime(retry_after)
                                                  - datetime.now(timezone.utc)).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                pass
    _PROVIDER_UNTIL[provider] = max(_PROVIDER_UNTIL.get(provider, 0),
                                    time.monotonic() + seconds)
    return provider_wait(provider)


def provider_error(provider, exc):
    wait = cooldown_after_error(provider, exc)
    return f"{provider}: {type(exc).__name__} {str(exc)[:80]}" + (
        f" (cooldown {wait}s)" if wait else ""
    )


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
            cx.execute('''CREATE TABLE IF NOT EXISTS research_cache(
                cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL
            )''')
            cx.execute("DELETE FROM research_cache WHERE expires_at <= ?", (time.time(),))
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
    """Accept only HTTP(S) public-looking DNS hostnames, not IP literals."""
    try:
        if not isinstance(url, str) or not url.strip():
            return ""
        url = url.strip()
        p = urlparse(url if "://" in url else "https://" + url)
        if p.scheme not in ("http", "https") or p.username is not None or p.password is not None:
            return ""
        if p.port not in (None, 80, 443):
            return ""
        hostname = (p.hostname or "").rstrip(".").lower().encode("idna").decode("ascii")
        hostname = hostname.removeprefix("www.")
        try:
            ipaddress.ip_address(hostname)
            return ""
        except ValueError:
            pass
        labels = hostname.split(".")
        if (len(labels) < 2 or len(hostname) > 253 or not labels[-1][0:1].isalpha()
                or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
                or labels[-1] in {"local", "localhost", "internal", "lan", "home"}
                or any(hostname == x or hostname.endswith("." + x) for x in EXCLUDE)):
            return ""
        return hostname
    except (ValueError, TypeError, UnicodeError):
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
            if provider_wait(backend):
                continue
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
                errors.append(provider_error(backend, exc))
            time.sleep(pause + random.uniform(0, 0.5))
        # Unofficial libraries change; Bing RSS offers a second public discovery path,
        # subject to availability and service access rules.
        if len(candidates) < max_results and not provider_wait("bing"):
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
                errors.append("RSS " + provider_error("bing", exc))
            time.sleep(pause)
    for provider in ("duckduckgo", "bing", "google"):
        if provider_wait(provider) and not any(provider in err for err in errors):
            errors.append(f"{provider}: cooldown {provider_wait(provider)}s")
    return candidates, errors


def discover_wikidata(company, max_results=4):
    """Retrieve official-website (P856) candidates for closely named entities."""
    if provider_wait("wikidata"):
        return {}, [f"wikidata: cooldown {provider_wait('wikidata')}s"]
    endpoint = "https://www.wikidata.org/w/api.php"
    candidates = {}
    errors = []
    try:
        response = requests.get(endpoint, params={
            "action": "wbsearchentities", "search": company, "language": "en",
            "type": "item", "limit": 8, "format": "json",
        }, headers=HEADERS, timeout=(5, 12))
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ValueError("Wikidata search API error")
        matches = [item for item in payload.get("search", [])
                   if re.fullmatch(r"Q[1-9][0-9]*", item.get("id", ""))
                   and max([name_fit(company, item.get("label", ""))]
                           + [name_fit(company, alias) for alias in item.get("aliases", [])]) >= 0.85]
        if not matches:
            return candidates, errors
        response = requests.get(endpoint, params={
            "action": "wbgetentities", "ids": "|".join(item["id"] for item in matches),
            "props": "claims", "format": "json",
        }, headers=HEADERS, timeout=(5, 12))
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ValueError("Wikidata entity API error")
        for item in matches:
            entity = payload.get("entities", {}).get(item["id"], {})
            for claim in entity.get("claims", {}).get("P856", []):
                if claim.get("rank") == "deprecated":
                    continue
                url = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                    continue
                dom = domain_from(url)
                if not dom:
                    continue
                candidates.setdefault(dom, {
                    "href": url, "title": item.get("label", ""), "body": item.get("description", ""),
                    "search_source": "wikidata", "source_entity": "https://www.wikidata.org/wiki/" + item["id"],
                })
                if len(candidates) >= max_results:
                    return candidates, errors
    except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
        errors.append(provider_error("wikidata", exc))
    return candidates, errors


def site_evidence(url):
    """Read bounded HTML and follow only redirects within the candidate hostname."""
    original = domain_from(url)
    if not original or not url.startswith(("http://", "https://")):
        return "", "", "Invalid candidate URL", ""
    current = url
    try:
        for _ in range(5):
            with requests.get(current, headers=HEADERS, timeout=(5, 12),
                              allow_redirects=False, stream=True) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        return "", "", "Redirect missing location", current
                    target = urljoin(current, location)
                    if not target.startswith(("http://", "https://")) or domain_from(target) != original:
                        return "", "", "Cross-host or excluded redirect; manual review required", current
                    current = target
                    continue
                if response.status_code != 200 or "text/html" not in response.headers.get("content-type", "").lower():
                    return "", "", "Site unavailable or non-HTML", current
                chunks = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    chunks.extend(chunk[:300000 - len(chunks)])
                    if len(chunks) >= 300000:
                        break
                soup = BeautifulSoup(bytes(chunks), "html.parser")
                title = soup.title.get_text(" ", strip=True) if soup.title else ""
                meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
                desc = meta.get("content", "") if meta else ""
                h1 = " ".join(x.get_text(" ", strip=True) for x in soup.find_all("h1", limit=2))
                return title[:250], (title + " " + desc + " " + h1)[:1200], "", current
        return "", "", "Redirect limit exceeded", current
    except (requests.RequestException, ValueError) as e:
        return "", "", type(e).__name__, current


CACHE_TTL = 24 * 60 * 60
CACHE_VERSION = 1


def cache_key(kind, *parts):
    return json.dumps([CACHE_VERSION, kind, *parts], ensure_ascii=False)


def cache_get(cx, key):
    if cx is None:
        return None
    row = cx.execute("SELECT payload FROM research_cache WHERE cache_key=? AND expires_at>?",
                     (key, time.time())).fetchone()
    if row:
        try:
            return json.loads(row[0])
        except (ValueError, TypeError):
            return None
    return None


def cache_put(cx, key, payload):
    if cx is None:
        return
    with cx:
        cx.execute("""INSERT INTO research_cache(cache_key, payload, expires_at) VALUES (?,?,?)
                   ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,
                       expires_at=excluded.expires_at""",
                   (key, json.dumps(payload, ensure_ascii=False), time.time() + CACHE_TTL))


def clear_research_cache(cx):
    with cx:
        cx.execute("DELETE FROM research_cache")


def discover_candidates(company, country, max_results, pause, cx=None, refresh=False):
    """Reuse only nonempty, error-free discovery results; never cache outages."""
    key = cache_key("candidates", *company_key(company, country), max_results)
    if not refresh:
        cached = cache_get(cx, key)
        if isinstance(cached, dict) and cached:
            return cached, [], True
    candidates, errors = discover_search(company, country, max_results, pause)
    structured, structured_errors = discover_wikidata(company)
    errors.extend(structured_errors)
    for dom, candidate in candidates.items():
        structured.setdefault(dom, candidate)
    if structured and not errors:
        # Persist only fields used by the scorer, not arbitrary upstream metadata.
        clean = {dom: {field: str(item.get(field) or "") for field in
                       ("href", "title", "body", "search_source", "source_entity")}
                 for dom, item in structured.items()}
        cache_put(cx, key, clean)
    return structured, errors, False


def cached_site_evidence(url, cx=None, refresh=False):
    key = cache_key("homepage", url)
    if not refresh:
        cached = cache_get(cx, key)
        if isinstance(cached, list) and len(cached) == 4:
            return tuple(cached), True
    result = site_evidence(url)
    if not result[2] and result[1]:
        cache_put(cx, key, result)
    return result, False


def lookup(company, country="", max_results=6, pause=2.0, cx=None, refresh=False):
    """Discover candidate domains and check whether site content supports ownership."""
    candidates, errors, cached_discovery = discover_candidates(company, country, max_results, pause, cx, refresh)
    scored = []
    site_errors = []
    for dom, result in list(candidates.items())[:max_results + 4]:
        url = result.get("href", "")
        (title, evidence, err, final_url), cached_homepage = cached_site_evidence(url, cx, refresh)
        if err:
            site_errors.append(f"{dom}: {err}")
        search_title = result.get("title", "")
        search_snippet = result.get("body", "")
        # Titles often contain an exact brand followed by a marketing tagline.
        # A title alone is weaker than full-page evidence, so cap its contribution.
        title_parts = re.split(r"\s+[|–—-]\s+|\s*\|\s*", title)
        title_score = max((name_fit(company, part) for part in title_parts), default=0)
        company_score = max(name_fit(company, evidence), min(0.78, title_score))
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
            "evidence_url": final_url or url, "evidence_title": title or search_title,
            "matched_name": search_title,
            "notes": f"Search: {result.get('search_source', 'unknown')}. "
                     + (err or "Compared website title, meta description and headings")
                     + (f"; entity: {result['source_entity']}" if result.get("source_entity") else ""),
        })
        if cached_homepage:
            scored[-1]["notes"] += "; cached homepage evidence (up to 24h old)"
        else:
            time.sleep(max(0.5, pause / 2))

    scored.sort(key=lambda row: row["confidence"], reverse=True)
    if not scored or scored[0]["confidence"] < 40:
        return {
            "domain": "", "confidence": 0, "evidence_url": "",
            "evidence_title": "", "matched_name": "",
            "status": "review",
            "notes": "No defensible domain match. " + "; ".join((site_errors + errors)[:3]),
        }
    best = scored[0]
    if len(scored) > 1 and best["confidence"] - scored[1]["confidence"] < 10:
        best["confidence"] = min(best["confidence"], 60)
        best["notes"] += "; competing plausible domains"
    best["status"] = "review"
    if cached_discovery:
        best["notes"] += "; cached discovery (up to 24h old)"
    if errors:
        best["notes"] += "; some search sources unavailable: " + "; ".join(errors[:2])
    if site_errors:
        best["notes"] += "; some candidate sites unavailable: " + "; ".join(site_errors[:2])
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
