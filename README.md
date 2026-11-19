# bwc-domain-finder
## Free-source discovery upgrade (November 2026)
- Wikidata entity searches in English and German, including simplified legal-name variants.
- English and German Wikipedia article searches, resolved to Wikidata entity IDs.
- Official-website P856 claims, name/alias similarity and ambiguous-candidate penalties.
- Optional country column during import, preserved in Excel export.
- Retry unmatched rows without overwriting reviewed domains.

**Important:** Wikipedia and Wikidata are connected sources, not independent confirmation. This upgrade improves candidate discovery, but cannot reliably cover all smaller manufacturers. The app does not scrape search engines, invent guessed domains, or claim verified ownership. Wikimedia APIs may throttle heavy batches; use modest batch sizes.
