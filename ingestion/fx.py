"""Daily FX rates (ECB reference rates via Frankfurter) so every price can be
compared in EUR.

Run:  python -m ingestion.fx
"""
import asyncio
import logging
from datetime import date, timedelta

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection

from common.config import BASE_CURRENCY, FX_BASE_URL
from common.db import engine, pipeline_run
from ingestion.http import get_json, save_raw

QUOTES = ["USD", "GBP", "JPY", "AUD", "CAD", "SGD", "MYR"]
LOOKBACK_DAYS = 45   # covers the listing window (30 days) plus weekends/holidays


async def fetch(start: date) -> dict:
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        return await get_json(client, f"{FX_BASE_URL}/{start.isoformat()}..",
                              params={"base": BASE_CURRENCY, "symbols": ",".join(QUOTES)})


def run() -> dict:
    with pipeline_run("ingest_fx") as stats:
        start = date.today() - timedelta(days=LOOKBACK_DAYS)
        doc = asyncio.run(fetch(start))
        save_raw("frankfurter", f"timeseries/{start.isoformat()}", doc)
        rows = [{"d": d, "b": doc["base"], "q": q, "r": r}
                for d, rates in doc["rates"].items() for q, r in rates.items()]
        with engine().begin() as conn:
            for row in rows:
                conn.execute(text("""insert into fx_rates (rate_date, base, quote, rate)
                                     values (:d, :b, :q, :r)
                                     on conflict (rate_date, base, quote) do update set rate = excluded.rate"""), row)
        stats.rows_in = stats.rows_out = len(rows)
        stats.notes = {"from": doc.get("start_date"), "to": doc.get("end_date"), "days": len(doc["rates"])}
        return stats.notes


def to_eur(conn: Connection, amount: float | None, currency: str | None, on: date | None = None) -> float | None:
    """Convert using the latest EUR rate on or before `on`. None if no rate known."""
    if amount is None or currency is None:
        return None
    currency = currency.upper()
    if currency == BASE_CURRENCY:
        return round(float(amount), 2)
    rate = conn.execute(
        text("""select rate from fx_rates where base = :b and quote = :q and rate_date <= :d
                order by rate_date desc limit 1"""),
        {"b": BASE_CURRENCY, "q": currency, "d": on or date.today()},
    ).scalar()
    return round(float(amount) / float(rate), 2) if rate else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
