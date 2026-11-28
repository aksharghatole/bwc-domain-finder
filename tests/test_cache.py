import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import scraper


class ResearchCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db_patch = patch.object(scraper, "DB", Path(self.tmp.name) / "test.sqlite")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.cx = scraper.connect()
        self.addCleanup(lambda: self.cx.close())

    def test_cache_survives_reconnect_and_expires(self):
        key = scraper.cache_key("test", "Acme")
        with patch.object(scraper.time, "time", return_value=1000):
            scraper.cache_put(self.cx, key, {"data": "value"})
            self.cx.close()
            self.cx = scraper.connect()
            self.assertEqual(scraper.cache_get(self.cx, key), {"data": "value"})
        with patch.object(scraper.time, "time", return_value=1000 + scraper.CACHE_TTL):
            self.assertIsNone(scraper.cache_get(self.cx, key))

    def test_candidate_cache_is_country_specific_and_refreshable(self):
        candidate = {"acme.example": {"href": "https://acme.example", "title": "Acme"}}
        with patch.object(scraper, "discover_search", return_value=(candidate, [])) as search, \
             patch.object(scraper, "discover_wikidata", side_effect=lambda *args: ({}, [])):
            self.assertFalse(scraper.discover_candidates("Acme", "UK", 6, 2, self.cx)[2])
            self.assertTrue(scraper.discover_candidates("Acme", "UK", 6, 2, self.cx)[2])
            self.assertFalse(scraper.discover_candidates("Acme", "US", 6, 2, self.cx)[2])
            self.assertFalse(scraper.discover_candidates("Acme", "UK", 6, 2, self.cx, True)[2])
            self.assertEqual(search.call_count, 3)

    def test_outage_and_empty_results_are_not_cached(self):
        for candidates, errors in (({}, []), ({"acme.example": {"href": "https://acme.example"}}, ["rate limit"])):
            with patch.object(scraper, "discover_search", side_effect=lambda *args: (candidates, list(errors))) as search, \
                 patch.object(scraper, "discover_wikidata", side_effect=lambda *args: ({}, [])):
                for _ in range(2):
                    self.assertFalse(scraper.discover_candidates("Acme", "", 6, 2, self.cx)[2])
                self.assertEqual(search.call_count, 2)

    def test_homepage_only_caches_success(self):
        good = ("Acme", "Acme Robotics", "", "https://acme.example")
        bad = ("", "", "Timeout", "https://bad.example")
        with patch.object(scraper, "site_evidence", side_effect=[good, bad, bad]) as site:
            self.assertEqual(scraper.cached_site_evidence("https://acme.example", self.cx), (good, False))
            self.assertEqual(scraper.cached_site_evidence("https://acme.example", self.cx), (good, True))
            for _ in range(2):
                self.assertEqual(scraper.cached_site_evidence("https://bad.example", self.cx), (bad, False))
            self.assertEqual(site.call_count, 3)

    def test_clear_cache_preserves_reviewed_results(self):
        scraper.write_result(self.cx, "Acme", {"domain": "acme.example", "status": "approved"})
        key = scraper.cache_key("test", "Acme")
        scraper.cache_put(self.cx, key, {"value": 1})
        scraper.clear_research_cache(self.cx)
        self.assertIsNone(scraper.cache_get(self.cx, key))
        self.assertEqual(self.cx.execute("SELECT domain, status FROM results").fetchone(),
                         ("acme.example", "approved"))
