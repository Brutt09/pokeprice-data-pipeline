"""TCGdex ingestion: card catalogue + Cardmarket reference prices.

TCGdex is the master catalogue. Each card payload also carries Cardmarket
price-guide figures per printed variant, which we store as reference-price
observations (raw / ungraded cards, EUR).

Run:  python -m ingestion.tcgdex
"""
import asyncio
import logging
from datetime import datetime

import httpx
from sqlalchemy import text

from common.config import SETS, TCGDEX_BASE_URL
from common.db import engine, pipeline_run
from ingestion.http import FetchError, get_json, save_raw

log = logging.getLogger(__name__)
SOURCE = "tcgdex"
PRICE_SOURCE = "cardmarket_via_tcgdex"
CARDMARKET_PRICE_TYPES = ["trend", "avg", "avg1", "avg7", "avg30", "low"]


def collector_number(local_id: str) -> int | None:
    digits = "".join(ch for ch in local_id if ch.isdigit())
    return int(digits) if digits else None


def parse_card(card: dict) -> dict:
    s = card["set"]
    return {
        "card_id": card["id"],
        "set_id": s["id"],
        "set_name": s["name"],
        "set_official_count": (s.get("cardCount") or {}).get("official"),
        "name": card["name"],
        "local_id": card["localId"],
        "collector_number": collector_number(card["localId"]),
        "rarity": card.get("rarity"),
        "category": card.get("category"),                    # Pokemon / Trainer / Energy
        "types": ", ".join(card.get("types") or []) or None,  # e.g. "Fire"; none for trainers
        "image_url": card.get("image"),
        "source_updated_at": card.get("updated"),
    }


def parse_variants(card: dict) -> list[dict]:
    """One row per printed variant. Falls back to the simple `variants` flags
    (with the card-level pricing block) when `variants_detailed` is absent."""
    rows = []
    detailed = card.get("variants_detailed")
    if detailed:
        for v in detailed:
            special = []
            if v.get("stamp"):
                special.append("stamp:" + "+".join(v["stamp"]))
            if v.get("foil"):
                special.append("foil:" + v["foil"])
            if v.get("size") and v["size"] != "standard":
                special.append("size:" + v["size"])
            third = v.get("thirdParty") or {}
            rows.append({
                # TCGdex variantId identifies a variant *type* and repeats across
                # cards, so the key is scoped to the card.
                "variant_id": f"{card['id']}:{v['variantId']}",
                "card_id": card["id"],
                "finish": v["type"],
                "is_special": bool(special),
                "special_label": ",".join(special) or None,
                "cardmarket_id": third.get("cardmarket"),
                "tcgplayer_id": third.get("tcgplayer"),
                "pricing": v.get("pricing"),
            })
    else:
        for finish, present in (card.get("variants") or {}).items():
            if present and finish != "wPromo":
                rows.append({
                    "variant_id": f"{card['id']}:{finish}",
                    "card_id": card["id"], "finish": finish, "is_special": False,
                    "special_label": None, "cardmarket_id": None, "tcgplayer_id": None,
                    "pricing": card.get("pricing"),
                })
    return rows


def parse_cardmarket_prices(variant: dict) -> list[dict]:
    """Cardmarket's price guide has plain fields for the non-foil product and
    '-holo' fields for its reverse-holo version. Zero/None means 'no data'."""
    cm = (variant.get("pricing") or {}).get("cardmarket")
    if not cm or not cm.get("updated"):
        return []
    suffix = "-holo" if variant["finish"] == "reverse" else ""
    # TCGdex reports the same daily price-guide snapshot with millisecond jitter
    # between requests; snap to the hour so re-runs don't duplicate observations.
    observed = datetime.fromisoformat(cm["updated"].replace("Z", "+00:00")).replace(
        minute=0, second=0, microsecond=0)
    out = []
    for ptype in CARDMARKET_PRICE_TYPES:
        value = cm.get(ptype + suffix)
        if value is None or float(value) <= 0:
            continue
        out.append({
            "card_id": variant["card_id"], "finish": variant["finish"], "grade": "RAW",
            "variant_id": variant["variant_id"], "source": PRICE_SOURCE, "price_type": ptype,
            "price": float(value), "currency": cm.get("unit", "EUR"),
            "observed_at": observed,
        })
    return out


UPSERT_CARD = text("""
insert into cards (card_id, set_id, set_name, set_official_count, name, local_id,
                   collector_number, rarity, category, types, image_url, source_updated_at)
values (:card_id, :set_id, :set_name, :set_official_count, :name, :local_id,
        :collector_number, :rarity, :category, :types, :image_url, :source_updated_at)
on conflict (card_id) do update set
    set_name = excluded.set_name, set_official_count = excluded.set_official_count,
    name = excluded.name, local_id = excluded.local_id,
    collector_number = excluded.collector_number, rarity = excluded.rarity,
    category = excluded.category, types = excluded.types, image_url = excluded.image_url, source_updated_at = excluded.source_updated_at,
    ingested_at = now()
""")
UPSERT_VARIANT = text("""
insert into card_variants (variant_id, card_id, finish, is_special, special_label, cardmarket_id, tcgplayer_id)
values (:variant_id, :card_id, :finish, :is_special, :special_label, :cardmarket_id, :tcgplayer_id)
on conflict (variant_id) do update set
    finish = excluded.finish, is_special = excluded.is_special, special_label = excluded.special_label,
    cardmarket_id = excluded.cardmarket_id, tcgplayer_id = excluded.tcgplayer_id
""")
# Cardmarket figures are EUR, so price_eur = price; non-EUR rows would be converted by fx.
INSERT_OBS = text("""
insert into price_observations (card_id, finish, grade, variant_id, source, price_type,
                                price, currency, price_eur, observed_at)
values (:card_id, :finish, :grade, :variant_id, :source, :price_type,
        :price, :currency, :price, :observed_at)
on conflict do nothing
""")


async def fetch_set(set_id: str, concurrency: int = 8) -> tuple[dict, list[dict], list[str]]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "pokeprice-pipeline/0.1"}) as client:
        set_doc = await get_json(client, f"{TCGDEX_BASE_URL}/sets/{set_id}")
        ids = [c["id"] for c in set_doc.get("cards", [])]

        async def one(card_id):
            try:
                return await get_json(client, f"{TCGDEX_BASE_URL}/cards/{card_id}", sem=sem)
            except FetchError as exc:
                log.error("card %s failed: %s", card_id, exc)
                return exc

        results = await asyncio.gather(*(one(i) for i in ids))
    cards = [r for r in results if isinstance(r, dict)]
    failed = [i for i, r in zip(ids, results) if not isinstance(r, dict)]
    return set_doc, cards, failed


def load(set_id: str, cards: list[dict]) -> dict:
    counts = {"cards": 0, "variants": 0, "observations_new": 0, "raw_new": 0}
    for card in cards:
        counts["raw_new"] += save_raw(SOURCE, f"cards/{card['id']}", card)
    with engine().begin() as conn:
        for card in cards:
            conn.execute(UPSERT_CARD, parse_card(card))
            counts["cards"] += 1
            for v in parse_variants(card):
                conn.execute(UPSERT_VARIANT, {k: v[k] for k in v if k != "pricing"})
                counts["variants"] += 1
                for obs in parse_cardmarket_prices(v):
                    counts["observations_new"] += conn.execute(INSERT_OBS, obs).rowcount
    return counts


def run() -> dict:
    summary = {}
    for set_id in SETS:
        with pipeline_run(f"ingest_tcgdex:{set_id}") as stats:
            set_doc, cards, failed = asyncio.run(fetch_set(set_id))
            save_raw(SOURCE, f"sets/{set_id}", set_doc)
            stats.rows_in = len(set_doc.get("cards", []))
            counts = load(set_id, cards)
            stats.rows_out = counts["cards"]
            stats.notes = {**counts, "failed_cards": failed,
                           "success_rate": round(len(cards) / max(stats.rows_in, 1), 4)}
            if not cards:
                raise FetchError(f"no cards fetched for {set_id}")
            summary[set_id] = stats.notes
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    print(run())
