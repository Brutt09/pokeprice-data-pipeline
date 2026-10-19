"""Pokémon TCG API (pokemontcg.io) ingestion: TCGplayer reference prices (USD).

Second reference source. Its card ids ("sv3pt5-6") differ from TCGdex
("sv03.5-006"), so rows are resolved to the catalogue by set + collector number.
The API was returning HTTP 500 when this was written (2026-10-19); the job
fails cleanly, is logged in pipeline_runs, and the rest of the pipeline runs.

Run:  python -m ingestion.pokemon_prices
"""
import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from common.config import POKEMONTCG_API_KEY, POKEMONTCG_BASE_URL, SET_ID_MAP, SETS
from common.db import engine, pipeline_run
from ingestion.fx import to_eur
from ingestion.http import get_json, save_raw
from ingestion.tcgdex import collector_number

SOURCE = "tcgplayer_via_pokemontcg"
FINISH_MAP = {"normal": "normal", "holofoil": "holo", "reverseHolofoil": "reverse",
              "1stEditionHolofoil": "firstEdition", "1stEditionNormal": "firstEdition"}
PRICE_TYPES = ["market", "mid", "low"]


def parse_prices(card: dict) -> list[dict]:
    tp = card.get("tcgplayer") or {}
    updated = tp.get("updatedAt")
    if not updated:
        return []
    observed = datetime.strptime(updated, "%Y/%m/%d").replace(tzinfo=timezone.utc)
    out = []
    for key, block in (tp.get("prices") or {}).items():
        finish = FINISH_MAP.get(key)
        if not finish:
            continue
        for ptype in PRICE_TYPES:
            value = (block or {}).get(ptype)
            if value is not None and float(value) > 0:
                out.append({"number": collector_number(card["number"]), "finish": finish,
                            "price_type": ptype, "price": float(value), "currency": "USD",
                            "observed_at": observed})
    return out


async def fetch_set(api_set_id: str) -> list[dict]:
    headers = {"X-Api-Key": POKEMONTCG_API_KEY} if POKEMONTCG_API_KEY else {}
    cards, page = [], 1
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        while True:
            doc = await get_json(client, f"{POKEMONTCG_BASE_URL}/cards", retries=1,
                                 params={"q": f"set.id:{api_set_id}", "page": page, "pageSize": 250})
            save_raw("pokemontcg", f"cards/{api_set_id}/page{page}", doc)
            cards.extend(doc.get("data", []))
            if page * doc.get("pageSize", 250) >= doc.get("totalCount", 0):
                return cards
            page += 1


def run() -> dict:
    summary = {}
    for set_id in SETS:
        api_set_id = SET_ID_MAP.get(set_id)
        if not api_set_id:
            continue
        with pipeline_run(f"ingest_pokemontcg:{set_id}") as stats:
            cards = asyncio.run(fetch_set(api_set_id))
            stats.rows_in = len(cards)
            unresolved, inserted = 0, 0
            with engine().begin() as conn:
                lookup = dict(conn.execute(text(
                    "select collector_number, card_id from cards where set_id = :s"), {"s": set_id}).all())
                for card in cards:
                    for obs in parse_prices(card):
                        card_id = lookup.get(obs["number"])
                        if not card_id:
                            unresolved += 1
                            continue
                        price_eur = to_eur(conn, obs["price"], "USD", obs["observed_at"].date())
                        if price_eur is None:
                            continue
                        inserted += conn.execute(text("""
                            insert into price_observations (card_id, finish, grade, source, price_type,
                                                            price, currency, price_eur, observed_at)
                            values (:c, :f, 'RAW', :src, :pt, :p, 'USD', :pe, :o)
                            on conflict do nothing"""),
                            {"c": card_id, "f": obs["finish"], "src": SOURCE, "pt": obs["price_type"],
                             "p": obs["price"], "pe": price_eur, "o": obs["observed_at"]}).rowcount
            stats.rows_out = inserted
            stats.notes = {"unresolved_numbers": unresolved}
            summary[set_id] = {"cards": len(cards), "observations_new": inserted}
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(run())
