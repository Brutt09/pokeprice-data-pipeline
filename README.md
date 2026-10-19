# PokéPrice: Pokémon Card Market Pricing Pipeline

An end-to-end data-engineering pipeline. It ingests Pokémon card catalogue data, reference prices and graded **sold** prices from public APIs, scrapes Japanese set lists with browser automation, standardises messy marketplace listings, matches them to the right card, finish and grade, checks data quality, and produces **explainable** price estimates. The results are served through an API and a small dashboard.

**Objective:** a data-engineering project for collectibles pricing. It builds a reliable, structured source of trading-card prices from messy, inconsistent market data.

`Python` · `PostgreSQL` · `SQL` · `HTTPX (async)` · `Playwright` · `RapidFuzz` · `FastAPI` · `Streamlit` · `pytest` · `Docker Compose`

---

## The problem

A collectibles business needs one reliable price for a specific card. The data comes from several sources that don't agree:

- Each source uses different ids for the same card (`sv03.5-199` vs `sv3pt5-199`).
- Each source has its own currency (EUR vs USD) and price type (trend, 7-day average, market).
- Listing titles are free text written by sellers: `"PSA 10 GEM MINT Charizard EX #199 SV 151 SIR 🔥"`.
- Raw, PSA 9 and PSA 10 copies of the same card are different products with very different prices.
- Japanese prints are different products again, with their own numbering: JP *151* has 210 cards, EN has 207.

## Architecture

```
            ┌──────────── ingestion (async, retries, backoff, rate cap) ────────────┐
TCGdex API ─┤ catalogue + Cardmarket price guide (EUR)                              │
PokémonTCG ─┤ TCGplayer prices (USD), ids resolved by set + collector number        ├─► raw_ingestions (JSONB, hash-deduped)
PriceTrackr ┤ graded eBay SOLD averages (PSA/CGC/BGS), joined by TCGplayer id      │   + data/raw/*.json / *.html
Bulbapedia ─┤ Playwright scraper: Japanese set lists + EN equivalents               │
Frankfurter ┤ ECB FX time series                                                    │
eBay Browse ┤ active listings (needs keys)  ──or──  synthetic labelled benchmark    │
            └───────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
   cards · card_variants · card_equivalents · price_observations · listings · fx_rates   (clean, upserts)
                                   │
          normalize ──► match (entity resolution) ──► estimate ──► data-quality checks
                                   │
                                   ▼
        card_matches · price_estimates · dq_results · pipeline_runs     (derived + ops)
                                   │
                         FastAPI  /  Streamlit
```

Every stage is logged in `pipeline_runs` (status, rows in/out, errors). **Hard** stages stop the run: catalogue, transform, match, estimate, and data-quality *errors*. **Soft** stages are logged, and the run continues without them: FX, the second price source, and listings. When the Pokémon TCG API returned HTTP 500 during development, the pipeline carried on with one reference source.

## Design decisions

| Decision | Why |
|---|---|
| Product key = **(card, finish, grade)** | Raw, PSA 9 and PSA 10 copies, and normal vs reverse-holo, are never priced together. |
| **Three separate** estimates: reference, listing, sales | Price-guide figures, *asking* prices and *sold* prices are different evidence, so they are shown side by side and never blended. |
| Language is part of card identity | A Japanese print is its own catalogue row (`sv2a-201`), *linked* to its English equivalent (`sv03.5-199`) but never priced with it. |
| Scraping is gated in code | A site can only be scraped once it's added to an allow-list with its reviewed terms/licence. robots.txt and Crawl-delay are enforced, and pages are snapshotted before parsing. Sites whose terms forbid it (SNKRDUNK) or that block bots (PriceCharting, 130point) are not scraped. |
| Raw payloads kept, hash-deduped | Every clean table can be rebuilt from raw, and unchanged payloads aren't stored twice. |
| Weighted median, log-MAD outlier filter | Robust to fat-finger and scam prices. Every estimate stores the exact observations it used. |
| Low-certainty matches go to `review`, not `auto` | A wrong auto-match corrupts prices silently, so a human queue is the safer failure. |
| Data-quality checks as SQL assertions → `dq_results` | Quality is tracked over time, not just checked once. |
| Plain orchestrator + scheduler, no Airflow/Kafka/Spark | One set is about 200 cards and a few thousand rows a day. Those tools would add complexity without adding value at this scale. |

## Measured results (run on 2026-10-19, set: Scarlet & Violet *151*, EN + JP)

All numbers below come from `data/metrics/latest_run.json` and the database. Nothing is estimated.

| Metric | Value |
|---|---|
| Cards ingested | **207 / 207** English (100% success) + **210** Japanese scraped |
| JP → EN links | **210 / 210** linked: 205 by exact name + rarity, 5 by name with a number tie-break |
| Printed variants | 384 (special stamped/foil versions flagged, kept out of base prices) |
| Reference price observations | 3,384 (2,304 Cardmarket + 1,080 TCGplayer) |
| Full pipeline runtime | **≈ 50 s** end to end (Bulbapedia's 5 s crawl delay included) |
| Idempotency | Two back-to-back runs → identical row counts |
| Data-quality checks | **12 / 12 passing** (4 error-level, 8 warning-level) |
| Unit tests | 38 passing |
| Reference estimates | 360 products. 186 are low confidence, and 168 of those are cards under €1, where the sources disagree a lot in relative terms. |
| Sold-price (sales) estimates | **717** card × grade products across 45 cards (first daily batch: 90 credits, 0 failures). Grades: PSA 8–10, CGC 8–10 incl. half grades. 226 high / 169 medium / 322 low confidence. Confidence falls when sales are few or the last sale is old. |
| Graded premium | Median **PSA 10 sold ÷ raw reference = 5.3×** (35 cards with high-confidence sales). Example: Charizard ex 199, €1,289 PSA 10 (594 sales) vs €328 raw. |
| TCGplayer vs Cardmarket | Median TCGplayer *market* price is **1.24×** the Cardmarket *trend* price after FX (360 product pairs). This is why the two sources are kept apart and combined only by a weighted median. |

**Matching, measured on the synthetic labelled benchmark.** Read the caveat below.

| Metric | Value |
|---|---|
| Listings | 800, including 43 Japanese-print listings, 24 relisted duplicates and 37 non-single "distractors" (sealed product, lots, proxies, "choose your card") |
| Auto-match precision (card + finish + grade all correct) | **100%** (724 / 724) |
| Recall | **94.9%**. The misses are listings with no printed number whose card name is shared by other cards, e.g. three different *Charizard ex* cards. They go to the review queue. |
| Japanese listings matched to the Japanese print | **43 / 43** |
| Distractors rejected | **37 / 37** |
| Outliers removed before listing estimates | 14 |

> **Caveat:** no real marketplace listings are ingested yet (eBay developer access is pending). The benchmark listings are generated from the real catalogue with realistic noise: typos, missing numbers, `#199` style, shuffled words, emojis, grading labels, and the "MEW" set alias that contains a card name. Because the generator and the matcher were written together, these numbers are an **upper bound**. The real test is to run against eBay titles and hand-label a sample. Every synthetic row is flagged `is_synthetic`, and the dashboard labels it as demo data.

## Bugs the pipeline's own checks caught

1. **Wrong entity key.** TCGdex `variantId` looked like a per-card id, but it identifies a variant *type* and repeats across cards. The catalogue collapsed from 207 cards to 8, and matching precision measured 4%. The fix scopes the key to the card, and a regression test covers it.
2. **Missing FX history.** With only today's FX rate, 486 of 800 listings couldn't be converted to EUR. The fix backfills the ECB time series for 45 days.
3. **Re-runs duplicated data.** TCGdex returns the same daily price snapshot with millisecond jitter between requests, so a second run added 1,812 duplicate observations. The fix snaps the snapshot time to the hour, and a regression test covers it.
4. **Sold data: average vs median.** On the first real response, a PSA 10 had an average of $142 but a median of $114, because one $216 sale skewed the average. The pipeline now uses the median. Half grades (`cgc8_5`) were also being dropped; they're now parsed.
5. **Language silently defaulted.** The matcher's query left out the language column, so all 47 Japanese listings matched English prints (0/47). The new per-noise-type metric exposed it immediately.
6. **Short-name collision.** The card "Mew ex" matched inside titles using the set alias "MEW". The fix makes the most specific name win a tie.

## Run it

**Without Docker (Windows):**
```powershell
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy .env.example .env
powershell -File scripts\dev_db.ps1          # project-local Postgres on :5433
.venv\Scripts\python pipeline.py              # full run (≈20 s)
.venv\Scripts\python -m pytest -q
.venv\Scripts\uvicorn api.main:app            # http://localhost:8000/docs
.venv\Scripts\streamlit run dashboard\app.py  # http://localhost:8501
powershell -File scripts\schedule_windows.ps1 # optional: daily 09:00 run
```

**With Docker:** `docker compose up --build` starts Postgres, the pipeline (runs now and then daily), the API and the dashboard.

Run a single stage with `python pipeline.py --stage match`. The stages are fx, tcgdex, pokemontcg, listings, normalize, match, estimate and validate.

## API

| Endpoint | Returns |
|---|---|
| `GET /cards?q=charizard` | catalogue search |
| `GET /cards/{id}` | card + printed variants |
| `GET /cards/{id}/estimates` | latest estimates per finish × grade × type, with confidence |
| `GET /cards/{id}/estimates/history` | estimate time series |
| `GET /cards/{id}/observations` | the raw price-guide figures behind an estimate |
| `GET /cards/{id}/listings` | matched listings |
| `GET /matches/review` | the review queue of uncertain matches |
| `GET /pipeline/runs`, `GET /quality` | run log, latest data-quality results |

## Sources & licences

- [TCGdex](https://tcgdex.dev): card catalogue + Cardmarket price guide.
- [Pokémon TCG API](https://pokemontcg.io): TCGplayer prices.
- [PokemonPriceTracker](https://www.pokemonpricetracker.com/api): graded eBay sold data. Its terms forbid redistribution, so **none of its data is in this repo**. Bring your own free key.
- [Bulbapedia](https://bulbapedia.bulbagarden.net): Japanese set lists, scraped politely (robots.txt + 5 s crawl delay). Content is [CC BY-NC-SA 2.5](https://creativecommons.org/licenses/by-nc-sa/2.5/).
- [Frankfurter](https://frankfurter.dev): ECB reference FX rates.

Pokémon and card names are trademarks of Nintendo / The Pokémon Company. This is an independent, non-commercial project.

## Next steps

- Add eBay keys (access pending), run on real titles, and hand-label about 200 of them to get real precision and recall.
- Anomaly detection for **manipulated** prices: shill-bid patterns, sudden spikes against history, single-seller dominance.
- A buyback-price tool (estimate × margin rules by confidence) for inventory decisions.
- More sets, and a second category (One Piece) to prove the schema is category-agnostic.
- CI (pytest + a pipeline smoke test on every push), plus dbt models for the clean and derived layers.
