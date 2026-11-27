import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import scraper


class DomainFinderTests(unittest.TestCase):
    def setUp(self):
        cooldowns = patch.object(scraper, '_PROVIDER_UNTIL', {})
        cooldowns.start()
        self.addCleanup(cooldowns.stop)

    def test_retry_after_and_expiry(self):
        response = scraper.requests.Response()
        response.status_code = 429
        response.headers["Retry-After"] = "900"
        error = scraper.requests.HTTPError("Too Many Requests", response=response)
        with patch.object(scraper.time, "monotonic", return_value=100):
            self.assertEqual(scraper.cooldown_after_error("bing", error), 900)
        with patch.object(scraper.time, "monotonic", return_value=999):
            self.assertEqual(scraper.provider_wait("bing"), 1)
        with patch.object(scraper.time, "monotonic", return_value=1000):
            self.assertEqual(scraper.provider_wait("bing"), 0)

    def test_bing_throttling_skips_rss_and_other_queries(self):
        response = scraper.requests.Response()
        response.status_code = 429
        error = scraper.requests.HTTPError("Too Many Requests", response=response)
        with patch.object(scraper.time, "monotonic", return_value=100), \
             patch.object(scraper.time, "sleep"), \
             patch.object(scraper, "DDGS") as ddgs, \
             patch.object(scraper.requests, "get") as rss:
            client = ddgs.return_value.__enter__.return_value
            def search(*args, **kwargs):
                if kwargs["backend"] == "bing":
                    raise error
                return []
            client.text.side_effect = search
            _, errors = scraper.discover_search("Acme GmbH")
            backends = [call.kwargs["backend"] for call in client.text.call_args_list]
            self.assertEqual(backends.count("bing"), 1)
            self.assertIn("google", backends)
            self.assertIn("duckduckgo", backends)
            rss.assert_not_called()
            self.assertTrue(any("cooldown" in message for message in errors))

    def test_all_provider_wait_tracks_earliest_recovery(self):
        with patch.object(scraper.time, "monotonic", return_value=100):
            scraper._PROVIDER_UNTIL.update({p: 400 for p in scraper.PROVIDERS})
            scraper._PROVIDER_UNTIL["wikidata"] = 160
            self.assertEqual(scraper.all_providers_wait(), 60)
            scraper._PROVIDER_UNTIL["google"] = 99
            self.assertEqual(scraper.all_providers_wait(), 0)

    def test_wikidata_skips_requests_during_cooldown(self):
        with patch.object(scraper.time, "monotonic", return_value=100), \
             patch.object(scraper.requests, "get") as get:
            scraper._PROVIDER_UNTIL["wikidata"] = 160
            candidates, errors = scraper.discover_wikidata("Acme")
            self.assertEqual(candidates, {})
            self.assertIn("cooldown 60s", errors[0])
            get.assert_not_called()

    def test_domain_parsing(self):
        self.assertEqual(scraper.domain_from("https://www.example.com/about"), "example.com")
        self.assertEqual(scraper.domain_from("https://uk.linkedin.com/company/a"), "")
        self.assertEqual(scraper.domain_from("invalid"), "")

    def test_rejects_invalid_and_nonweb_hosts(self):
        for url in ("ftp://example.com", "https://user:pass@example.com",
                    "http://127.0.0.1", "http://[::1]", "http://router.local",
                    "https://bad_host.example", "https://192.168.0.1", None):
            with self.subTest(url=url):
                self.assertEqual(scraper.domain_from(url), "")

    def test_cross_host_redirect_is_not_followed(self):
        with patch.object(scraper.requests, "get") as get:
            response = get.return_value.__enter__.return_value
            response.status_code = 302
            response.headers = {"Location": "https://unrelated.example/"}
            title, evidence, error, final_url = scraper.site_evidence("https://acme.example")
            self.assertEqual((title, evidence), ("", ""))
            self.assertIn("Cross-host", error)
            self.assertEqual(get.call_count, 1)

    def test_same_host_redirect_and_final_evidence_url(self):
        with patch.object(scraper.requests, "get") as get:
            from unittest.mock import MagicMock
            redirect = MagicMock()
            redirect.__enter__.return_value.status_code = 301
            redirect.__enter__.return_value.headers = {"Location": "https://www.acme.example/en"}
            page = MagicMock()
            response = page.__enter__.return_value
            response.status_code = 200
            response.headers = {"content-type": "text/html"}
            response.iter_content.return_value = [b"<title>Acme</title><h1>Robotics</h1>"]
            get.side_effect = [redirect, page]
            title, evidence, error, final_url = scraper.site_evidence("http://acme.example")
            self.assertEqual(title, "Acme")
            self.assertIn("Robotics", evidence)
            self.assertEqual(error, "")
            self.assertEqual(final_url, "https://www.acme.example/en")
            self.assertEqual(get.call_count, 2)
            self.assertTrue(all(not call.kwargs["allow_redirects"] for call in get.call_args_list))

    def test_redirect_loop_is_bounded(self):
        with patch.object(scraper.requests, "get") as get:
            response = get.return_value.__enter__.return_value
            response.status_code = 302
            response.headers = {"Location": "/loop"}
            _, _, error, _ = scraper.site_evidence("https://acme.example")
            self.assertIn("limit", error)
            self.assertEqual(get.call_count, 5)

    def test_ambiguous_names(self):
        self.assertLess(scraper.name_fit("AGRO AG", "Danish Agro"), 0.9)
        self.assertGreater(scraper.name_fit("Acme Robotics GmbH", "Acme Robotics"), 0.9)

    def test_eligibility(self):
        companies = [("New", "UK"), ("New", "US"), ("New", "UK"), ("Approved", ""), ("Rejected", ""),
                     ("Missing", ""), ("Matched", "")]
        saved = [
            {"company": "Approved", "domain": "", "status": "approved"},
            {"company": "Rejected", "domain": "", "status": "rejected"},
            {"company": "Missing", "domain": "", "status": "review"},
            {"company": "Matched", "domain": "example.com", "status": "review"},
        ]
        self.assertEqual(scraper.eligible_companies(companies, saved), [("New", "UK"), ("New", "US")])
        self.assertEqual(scraper.eligible_companies(companies, saved, True),
                         [("New", "UK"), ("New", "US"), ("Missing", "")])

    def test_reviewed_records_cannot_be_overwritten(self):
        with TemporaryDirectory() as tmp, patch.object(scraper, "DB", Path(tmp) / "test.sqlite"):
            cx = scraper.connect()
            try:
                for status in ("approved", "rejected", "review"):
                    scraper.write_result(cx, status, {"domain": "original.example", "status": status})
                    scraper.write_result(cx, status, {"domain": "replacement.example"})
                rows = dict(cx.execute("SELECT company, domain FROM results"))
                self.assertEqual(rows["approved"], "original.example")
                self.assertEqual(rows["rejected"], "original.example")
                self.assertEqual(rows["review"], "replacement.example")
            finally:
                cx.close()

    def test_country_specific_records_and_review_isolation(self):
        with TemporaryDirectory() as tmp, patch.object(scraper, "DB", Path(tmp) / "test.sqlite"):
            cx = scraper.connect()
            try:
                scraper.write_result(cx, "Acme", {"domain": "uk.example", "status": "approved"}, country="UK")
                scraper.write_result(cx, "Acme", {"domain": "us.example"}, country="US")
                scraper.write_result(cx, "Acme", {"domain": "other.example"}, country="UK")
                scraper.write_result(cx, "Acme", {"domain": "updated.example"}, country="US")
                rows = cx.execute("SELECT country, domain, status FROM results ORDER BY country").fetchall()
                self.assertEqual(rows, [("UK", "uk.example", "approved"), ("US", "updated.example", "review")])
            finally:
                cx.close()

    def test_legacy_migration_preserves_all_fields_and_is_idempotent(self):
        with TemporaryDirectory() as tmp, patch.object(scraper, "DB", Path(tmp) / "test.sqlite"):
            legacy = sqlite3.connect(scraper.DB)
            legacy.execute("""CREATE TABLE results(
                company TEXT PRIMARY KEY, domain TEXT, confidence INTEGER DEFAULT 0,
                evidence_url TEXT, evidence_title TEXT, matched_name TEXT,
                status TEXT DEFAULT 'pending', notes TEXT DEFAULT '', checked_at TEXT)""")
            original = ("Acme", "acme.example", 92, "https://acme.example", "Acme", "Acme Inc",
                        "approved", "checked manually", "2026-11-19")
            legacy.execute("INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?)", original)
            legacy.commit()
            legacy.close()
            for _ in range(2):
                cx = scraper.connect()
                try:
                    row = cx.execute("""SELECT company, domain, confidence, evidence_url,
                        evidence_title, matched_name, status, notes, checked_at FROM results
                        WHERE country=''""").fetchone()
                    self.assertEqual(row, original)
                    self.assertEqual(cx.execute("SELECT COUNT(*) FROM results").fetchone()[0], 1)
                finally:
                    cx.close()

    def test_wikidata_filters_ambiguous_and_deprecated_claims(self):
        with patch.object(scraper.requests, "get") as get:
            get.return_value.json.side_effect = [
                {"search": [
                    {"id": "Q1", "label": "Acme Robotics"},
                    {"id": "Q2", "label": "Other Robotics"},
                ]},
                {"entities": {"Q1": {"claims": {"P856": [
                    {"rank": "deprecated", "mainsnak": {"datavalue": {"value": "https://old.example"}}},
                    {"rank": "normal", "mainsnak": {"datavalue": {"value": "https://acme.example"}}},
                    {"rank": "normal", "mainsnak": {"datavalue": {"value": "https://linkedin.com/acme"}}},
                ]}}}},
            ]
            candidates, errors = scraper.discover_wikidata("Acme Robotics GmbH")
            self.assertFalse(errors)
            self.assertEqual(list(candidates), ["acme.example"])
            self.assertEqual(candidates["acme.example"]["source_entity"], "https://www.wikidata.org/wiki/Q1")
            self.assertEqual(get.call_args.kwargs["params"]["ids"], "Q1")

    def test_wikidata_failure_is_nonfatal(self):
        with patch.object(scraper.requests, "get", side_effect=scraper.requests.Timeout):
            candidates, errors = scraper.discover_wikidata("Acme")
            self.assertEqual(candidates, {})
            self.assertIn("wikidata:", errors[0])

    def test_structured_claim_requires_homepage_evidence(self):
        candidate = {"acme.example": {"href": "https://acme.example", "title": "Acme",
                    "search_source": "wikidata", "source_entity": "https://www.wikidata.org/wiki/Q1"}}
        with patch.object(scraper, "discover_search", return_value=({}, [])), \
             patch.object(scraper, "discover_wikidata", return_value=(candidate, [])), \
             patch.object(scraper, "site_evidence", return_value=("", "", "Unavailable", "")), \
             patch.object(scraper.time, "sleep"):
            self.assertEqual(scraper.lookup("Acme")["domain"], "")

    def test_title_match_is_capped_and_source_is_saved(self):
        candidate = {"acme.example": {"href": "https://acme.example", "title": "Acme",
                    "search_source": "wikidata", "source_entity": "https://www.wikidata.org/wiki/Q1"}}
        with patch.object(scraper, "discover_search", return_value=({}, [])), \
             patch.object(scraper, "discover_wikidata", return_value=(candidate, [])), \
             patch.object(scraper, "site_evidence", return_value=("Acme | Solutions", "Acme Solutions Worldwide", "", "https://acme.example")), \
             patch.object(scraper.time, "sleep"):
            result = scraper.lookup("Acme")
            self.assertEqual(result["domain"], "acme.example")
            self.assertLessEqual(result["confidence"], 74)
            self.assertEqual(result["status"], "review")
            self.assertIn("https://www.wikidata.org/wiki/Q1", result["notes"])

    def test_search_includes_country_without_legal_suffix(self):
        with patch.object(scraper, "DDGS") as ddgs:
            client = ddgs.return_value.__enter__.return_value
            client.text.return_value = [{"href": "https://example.com"}]
            scraper.discover_search("Acme Robotics", "Germany", max_results=1)
            query = client.text.call_args.args[0]
            self.assertIn("Germany", query)


if __name__ == "__main__":
    unittest.main()
