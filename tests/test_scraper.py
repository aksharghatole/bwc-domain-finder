import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import scraper


class DomainFinderTests(unittest.TestCase):
    def test_domain_parsing(self):
        self.assertEqual(scraper.domain_from("https://www.example.com/about"), "example.com")
        self.assertEqual(scraper.domain_from("https://uk.linkedin.com/company/a"), "")
        self.assertEqual(scraper.domain_from("invalid"), "")

    def test_ambiguous_names(self):
        self.assertLess(scraper.name_fit("AGRO AG", "Danish Agro"), 0.9)
        self.assertGreater(scraper.name_fit("Acme Robotics GmbH", "Acme Robotics"), 0.9)

    def test_eligibility(self):
        companies = [("New", "UK"), ("New", "US"), ("Approved", ""), ("Rejected", ""),
                     ("Missing", ""), ("Matched", "")]
        saved = [
            {"company": "Approved", "domain": "", "status": "approved"},
            {"company": "Rejected", "domain": "", "status": "rejected"},
            {"company": "Missing", "domain": "", "status": "review"},
            {"company": "Matched", "domain": "example.com", "status": "review"},
        ]
        self.assertEqual(scraper.eligible_companies(companies, saved), [("New", "UK")])
        self.assertEqual(scraper.eligible_companies(companies, saved, True),
                         [("New", "UK"), ("Missing", "")])

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

    def test_search_includes_country_without_legal_suffix(self):
        with patch.object(scraper, "DDGS") as ddgs:
            client = ddgs.return_value.__enter__.return_value
            client.text.return_value = [{"href": "https://example.com"}]
            scraper.discover_search("Acme Robotics", "Germany", max_results=1)
            query = client.text.call_args.args[0]
            self.assertIn("Germany", query)


if __name__ == "__main__":
    unittest.main()
