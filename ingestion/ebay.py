"""eBay Browse API ingestion: active listings (asking prices, NOT sold prices).

Needs EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in .env (free developer account at
developer.ebay.com). Without keys the job is recorded as 'skipped'.

Run:  python -m ingestion.ebay
"""
import asyncio
import base64
import logging
from datetime import datetime

import httpx
from sqlalchemy import text

from common.config import EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_MARKETPLACE, SETS
from common.db import engine
from ingestion.http import get_json, save_raw

log = logging.getLogger(__name__)
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
CCG_SINGLES_CATEGORY = "183454"   # Collectible Card Games > CCG Individual Cards
MAX_CARDS = 50                    # query the N most valuable cards per set
MAX_ITEMS_PER_QUERY = 200


def credentials_present() -> bool:
    return bool(EBAY_CLIENT_ID and EBAY_CLIENT_SECRET)


async def get_token(client: httpx.AsyncClient) -> str:
    basic = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    resp = await client.post(TOKEN_URL, headers={"Authorization": f"Basic {basic}"},
                             data={"grant_type": "client_credentials",
                                   "scope": "https://api.ebay.com/oauth/api_scope"})
    resp.raise_for_status()
    return resp.json()["access_token"]


def parse_item(item: dict) -> dict:
    ship = (item.get("shippingOptions") or [{}])[0].get("shippingCost") or {}
    price = item.get("price") or {}
    created = item.get("itemCreationDate")
    return {
        "source": "ebay", "listing_id": item["itemId"], "title": item.get("title", ""),
        "price": float(price["value"]) if price.get("value") else None,
        "currency": price.get("currency"),
        "shipping": float(ship["value"]) if ship.get("value") else None,
        "seller": (item.get("seller") or {}).get("username"),
        "condition_raw": item.get("condition"), "url": item.get("itemWebUrl"),
        "listed_at": datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None,
    }


def search_queries(set_id: str) -> list[str]:
    with engine().connect() as conn:
        rows = conn.execute(text("""
            select c.name, c.local_id, c.set_official_count, c.set_name
            from cards c
            join price_observations o on o.card_id = c.card_id and o.price_type = 'trend'
            where c.set_id = :s
            group by c.card_id, c.name, c.local_id, c.set_official_count, c.set_name
            order by max(o.price_eur) desc limit :n"""), {"s": set_id, "n": MAX_CARDS}).all()
    return [f"pokemon {r.name} {r.local_id}/{r.set_official_count}" for r in rows]


async def fetch(queries: list[str]) -> list[dict]:
    items = []
    async with httpx.AsyncClient(timeout=30) as client:
        token = await get_token(client)
        headers = {"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": EBAY_MARKETPLACE}
        for q in queries:
            doc = await get_json(client, SEARCH_URL, headers=headers,
                                 params={"q": q, "category_ids": CCG_SINGLES_CATEGORY,
                                         "limit": MAX_ITEMS_PER_QUERY})
            save_raw("ebay", f"search?q={q}", doc)
            items.extend(doc.get("itemSummaries", []))
    return items


INSERT_LISTING = text("""
insert into listings (source, listing_id, title, price, currency, shipping, seller,
                      condition_raw, url, listed_at, is_synthetic)
values (:source, :listing_id, :title, :price, :currency, :shipping, :seller,
        :condition_raw, :url, :listed_at, false)
on conflict (source, listing_id) do update set
    title = excluded.title, price = excluded.price, currency = excluded.currency,
    shipping = excluded.shipping, fetched_at = now(), normalized_at = null
""")


def run(stats) -> dict:
    """Called inside a pipeline_run by the orchestrator."""
    rows = []
    for set_id in SETS:
        items = asyncio.run(fetch(search_queries(set_id)))
        stats.rows_in += len(items)
        rows.extend(parse_item(i) for i in items)
    with engine().begin() as conn:
        for row in rows:
            conn.execute(INSERT_LISTING, row)
    stats.rows_out = len(rows)
    return {"listings": len(rows)}
