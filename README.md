# PokéPrice: Pokémon Card Market Pricing Pipeline

An end-to-end data-engineering pipeline. It ingests Pokémon card catalogue data and reference prices from public APIs, standardises messy marketplace listings, matches them to the right card, finish and grade, checks data quality, and produces **explainable** price estimates. The results are served through an API and a small dashboard.

`Python` · `PostgreSQL` · `SQL` · `HTTPX (async)` · `RapidFuzz` · `FastAPI` · `Streamlit` · `pytest` · `Docker Compose`

---

## The problem

A collectibles business needs one reliable price for a specific card. The data comes from several sources that don't agree:

- Each source uses different ids for the same card (`sv03.5-199` vs `sv3pt5-199`).
- Each source has its own currency (EUR vs USD) and price type (trend, 7-day average, market).
- Listing titles are free text written by sellers: `"PSA 10 GEM MINT Charizard EX #199 SV 151 SIR 🔥"`.
- Raw, PSA 9 and PSA 10 copies of the same card are different products with very different prices.

## Architecture

```
            ┌──────────── ingestion (async, retries, backoff, rate cap) ────────────┐
TCGdex API ─┤ catalogue + Cardmarket price guide (EUR)                              │
PokémonTCG ─┤ TCGplayer prices (USD), ids resolved by set + collector number        ├─► raw_ingestions (JSONB, hash-deduped)
Frankfurter ┤ ECB FX time series                                                    │   + data/raw/*.json
eBay Browse ┤ active listings (needs keys)  ──or──  synthetic labelled benchmark    │
            └───────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
   cards · card_variants · price_observations · listings · fx_rates     (clean layer, upserts)
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
| **Separate** reference and listing estimates | Price-guide figures and *asking* prices are different things. Neither is a sold price, so there is no `sales` estimate until a sold-data source exists. |
| Raw payloads kept, hash-deduped | Every clean table can be rebuilt from raw, and unchanged payloads aren't stored twice. |
| Weighted median, log-MAD outlier filter | Robust to fat-finger and scam prices. Every estimate stores the exact observations it used. |
| Low-certainty matches go to `review`, not `auto` | A wrong auto-match corrupts prices silently, so a human queue is the safer failure. |
| Data-quality checks as SQL assertions → `dq_results` | Quality is tracked over time, not just checked once. |
| Plain orchestrator + scheduler, no Airflow/Kafka/Spark | One set is about 200 cards and a few thousand rows a day. Those tools would add complexity without adding value at this scale. |

## Measured results (run on 2026-10-19, set: Scarlet & Violet *151*)

All numbers below come from `data/metrics/latest_run.json` and the database. Nothing is estimated.

| Metric | Value |
|---|---|
| Cards ingested | **207 / 207** (100% success, 0 failed requests) |
| Printed variants | 384 (special stamped/foil versions flagged, kept out of base prices) |
| Reference price observations | 3,384 (2,304 Cardmarket + 1,080 TCGplayer) |
| Full pipeline runtime | **≈ 21 s** end to end |
| Idempotency | Two back-to-back runs → identical row counts |
| Data-quality checks | **11 / 11 passing** (4 error-level, 7 warning-level) |
| Unit tests | 28 passing |
| Reference estimates | 360 products. 186 are low confidence, and 168 of those are cards under €1, where the sources disagree a lot in relative terms. |
| TCGplayer vs Cardmarket | Median TCGplayer *market* price is **1.24×** the Cardmarket *trend* price after FX (360 product pairs). This is why the two sources are kept apart and combined only by a weighted median. |

**Matching, measured on the synthetic labelled benchmark.** Read the caveat below.

| Metric | Value |
|---|---|
| Listings | 800, including 32 relisted duplicates and 37 non-single "distractors" (sealed product, lots, proxies, Japanese, "choose your card") |
| Auto-match precision (card + finish + grade all correct) | **100%** (728 / 728) |
| Recall | **95.4%**. The misses are listings with no printed number whose card name is shared by other cards, e.g. three different *Charizard ex* cards. They go to the review queue. |
| Distractors rejected | **37 / 37** |
| Duplicates detected | 29 / 29 relisted real cards. The other 3 relists were junk listings, already excluded before deduplication. |
| Outliers removed before listing estimates | 26 |

> **Caveat:** no real marketplace listings are ingested yet (eBay needs API keys). The benchmark listings are generated from the real catalogue with realistic noise: typos, missing numbers, `#199` style, shuffled words, emojis, grading labels, and the "MEW" set alias that contains a card name. Because the generator and the matcher were written together, these numbers are an **upper bound**. The real test is to run against eBay titles and hand-label a sample. Every synthetic row is flagged `is_synthetic`, and the dashboard labels it as demo data.

## Bugs the pipeline's own checks caught

1. **Wrong entity key.** TCGdex `variantId` looked like a per-card id, but it identifies a variant *type* and repeats across cards. The catalogue collapsed from 207 cards to 8, and matching precision measured 4%. The fix scopes the key to the card, and a regression test covers it.
2. **Missing FX history.** With only today's FX rate, 486 of 800 listings couldn't be converted to EUR. The fix backfills the ECB time series for 45 days.
3. **Re-runs duplicated data.** TCGdex returns the same daily price snapshot with millisecond jitter between requests, so a second run added 1,812 duplicate observations. The fix snaps the snapshot time to the hour, and a regression test covers it.
4. **Short-name collision.** The card "Mew ex" matched inside titles using the set alias "MEW". The fix makes the most specific name win a tie.

## Run it

**Without Docker (Windows):**
```powershell
python -m venv .venv; .venv\Scripts\pip install -r requirements.txt
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

## Next steps

- Add eBay keys, run on real titles, and hand-label about 200 of them to get real precision and recall.
- Find a sold-price source, then add a `sales` estimate type.
- More sets and languages. Japanese needs its own catalogue rather than an exclusion flag.
- CI (pytest + a pipeline smoke test on every push), plus dbt models for the clean and derived layers.
