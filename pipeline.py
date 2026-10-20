"""Orchestrator: runs every stage in order, logs each one in pipeline_runs,
and writes a run summary to data/metrics/latest_run.json.

Hard stages (catalogue, transform, match, estimate, data-quality errors) stop
the run. Soft stages (FX, second price source, listings) are logged and the
run continues.

Run:  python pipeline.py            # full run
      python pipeline.py --stage match
"""
import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

from common.config import ROOT
from common.db import apply_schema, pipeline_run, record_skip
from ingestion import bulbapedia, ebay, fx, pokemon_prices, pokepricetracker, synthetic_listings, tcgdex
from ops import alerts
from pricing import anomalies, buyback, comparables, estimator
from transformation import matching, normalize, validation

log = logging.getLogger("pipeline")


def staged(job_name, fn):
    def wrapper():
        with pipeline_run(job_name) as stats:
            notes = fn(stats)
            stats.notes = notes
            return notes
    return wrapper


def sold_stage():
    if pokepricetracker.credentials_present():
        return staged("ingest_pokepricetracker_sold", pokepricetracker.run)()
    record_skip("ingest_pokepricetracker_sold", "POKEPRICETRACKER_API_KEY not set")
    return {"skipped": "no API key"}


def listings_stage():
    if ebay.credentials_present():
        return staged("ingest_ebay", ebay.run)()
    record_skip("ingest_ebay", "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET not set")
    return staged("ingest_synthetic_listings", synthetic_listings.run)()


STAGES = [
    # (name, callable, hard)
    ("fx", fx.run, False),
    ("tcgdex", tcgdex.run, True),
    ("pokemontcg", pokemon_prices.run, False),
    ("bulbapedia", staged("scrape_bulbapedia_jp", bulbapedia.run), False),
    ("sold", sold_stage, False),
    ("listings", listings_stage, False),
    ("normalize", staged("normalize_listings", normalize.run), True),
    ("match", staged("match_listings", matching.run), True),
    ("estimate", staged("estimate_prices", estimator.run), True),
    ("comparables", staged("comparable_estimates", comparables.run), True),
    ("anomalies", staged("detect_anomalies", anomalies.run), True),
    ("buyback", staged("buyback_quotes", buyback.run), True),
    ("validate", staged("data_quality", validation.run), True),
    ("alerts", staged("send_alerts", alerts.run), False),
]


def main(only: str | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    apply_schema()
    started = time.perf_counter()
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "stages": {}}
    exit_code = 0
    for name, fn, hard in STAGES:
        if only and name != only:
            continue
        t0 = time.perf_counter()
        try:
            result = fn()
            summary["stages"][name] = {"status": "success", "seconds": round(time.perf_counter() - t0, 2),
                                       "result": result}
            log.info("stage %s ok: %s", name, result)
        except Exception as exc:
            summary["stages"][name] = {"status": "failed", "seconds": round(time.perf_counter() - t0, 2),
                                       "error": f"{type(exc).__name__}: {exc}"}
            log.error("stage %s failed: %s", name, exc)
            if hard:
                exit_code = 1
                break
    summary["runtime_seconds"] = round(time.perf_counter() - started, 2)
    out = ROOT / "data" / "metrics"
    out.mkdir(parents=True, exist_ok=True)
    # single-stage runs must not overwrite the full-run metrics the README quotes
    name = "latest_run.json" if only is None else f"latest_stage_{only}.json"
    (out / name).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=[s[0] for s in STAGES])
    sys.exit(main(parser.parse_args().stage))
