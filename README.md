# bwc-domain-finder
## Free-source discovery upgrade (November 2026)
- Wikidata entity searches in English and German, including simplified legal-name variants.
- English and German Wikipedia article searches, resolved to Wikidata entity IDs.
- Official-website P856 claims, name/alias similarity and ambiguous-candidate penalties.
- Optional country column during import, preserved in Excel export.
- Retry unmatched rows without overwriting reviewed domains.

**Important:** Wikipedia and Wikidata are connected sources, not independent confirmation. This upgrade improves candidate discovery, but cannot reliably cover all smaller manufacturers. The app does not scrape search engines, invent guessed domains, or claim verified ownership. Wikimedia APIs may throttle heavy batches; use modest batch sizes.

## Automated scraping / research mode (Windows, free)

GitHub Pages hosts the static review tool only. Browser JavaScript cannot reliably fetch search-result pages due to CORS and search-engine restrictions. The new **local Python app** supports automated DuckDuckGo searches with website-title and homepage evidence.

1. Install [Python 3.11 or 3.12](https://www.python.org/downloads/) on Windows and enable **Add python.exe to PATH** during setup.
2. Click **Code → Download ZIP** on this GitHub repository, and extract the ZIP.
3. Double-click **run_windows.bat** inside the extracted folder. This creates a virtual environment, installs dependencies and opens a Streamlit app (usually http://localhost:8501).
4. Upload a CSV/XLSX with a company name column, select columns, and click **Import company list**.
5. Set batch size **5 or 10** for the first test, then click **Start research**. The app saves results to `local_data/domain_finder.sqlite3` after each company.
6. Inspect evidence URLs, edit or approve results, and download an Excel/CSV export.

**Important:** Avoid launching multiple instances from the same folder simultaneously. If you restart the app, upload the same CSV again; previous research results are retained in the SQLite database and eligible rows are skipped by default. Keep a backup of the `local_data` folder between app upgrades.

### Search accuracy / limitations
- DuckDuckGo queries use the unofficial `ddgs` package, not a paid search API. Searches can fail due to throttling or changes to the search provider.
- Domain candidates are not automatically confirmed as official. Even high confidence is a heuristic based on page title, description and name matching; **manual review is required**.
- The application does not bypass CAPTCHAs, authentication, or rate limits.
- Public website retrieval is bounded (timeout + small landing-page read), and searches are paced. Observe website access restrictions.
- The entire list isn't processed in a background job: research runs when the local app is open and the batch is started.
- CSV/XLSX and results stay on your PC; local_data is gitignored.
