"""PokemonPriceTracker API: graded (PSA/CGC/BGS/SGC) eBay **sold** data.

This is the only source of sales-based prices in the project. Cards are looked
up by TCGplayer product id, which TCGdex already provides, so linking is an
exact id join, not fuzzy matching.

Free plan: 100 credits/day; includeEbay costs 2 credits per card. The job spends
at most DAILY_CREDIT_BUDGET, picking the most valuable cards whose sold data is
oldest (rotation), so the whole set is covered over a few days.

Terms (reviewed 2026-10-19): hobby/non-revenue use and storing data in our own
DB are allowed; redistributing the raw data is not. So this data never goes
into the git repo or a public deployment.

Response shape (verified on a real response, 2026-10-19):
  data.ebay.updatedAt, data.ebay.salesByGrade.<psa10|cgc9_5|...>.
      {count, medianPrice, averagePrice, minPrice, maxPrice, lastSaleDate, ...}
  metadata.apiCallsConsumed.total  -> credits actually charged
We use the MEDIAN (one high sale skews the average) and keep count + last sale
date so the estimator can lower confidence for thin or stale markets.

Run:  python -m ingestion.pokepricetracker
"""
import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import text

from common.config import POKEPRICETRACKER_API_KEY as API_KEY
from common.config import POKEPRICETRACKER_DAILY_CREDITS as DAILY_CREDIT_BUDGET
from common.db import engine
from ingestion.fx import to_eur
from ingestion.http import get_json, save_raw

log = logging.getLogger(__name__)
SOURCE = "ebay_sold_via_ppt"
BASE_URL = "https://www.pokemonpricetracker.com/api/v2"
CREDITS_PER_CARD = 2          # 1 card + 1 for includeEbay

GRADE_KEY_RE = re.compile(r"^(psa|cgc|bgs|sgc|beckett|ace)[\s_-]?(10|\d(?:[._]5)?)$", re.I)
COMPANY = {"psa": "PSA", "cgc": "CGC", "bgs": "BGS", "beckett": "BGS", "sgc": "SGC", "ace": "ACE"}
PRICE_KEYS = ("medianPrice", "median", "averagePrice", "avgPrice", "average", "avg", "price", "marketPrice")
COUNT_KEYS = ("count", "salesCount", "totalSales", "sales", "n")


def credentials_present() -> bool:
    return bool(API_KEY)


def grade_from_key(key: str) -> str | None:
    m = GRADE_KEY_RE.match(key.replace("_sales", "").strip())
    if not m:
        return None
    return f"{COMPANY[m.group(1).lower()]} {m.group(2).replace('_', '.')}"


def _number(v):
    return float(v) if isinstance(v, (int, float)) and v > 0 else None


def _ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None


def parse_graded(card: dict) -> list[dict]:
    """Return [{grade, price_usd, sample_size, last_sale_at}]. Real shape is
    ebay.salesByGrade (see module doc); older documented shapes are also accepted:
      gradedPrices: {"psa10": 15750.0, ...}
      ebay(_data):  {"psa10": {"averagePrice": .., "count": ..}} or {"psa_10_sales": [{"price": ..}, ...]}
    """
    out = []
    for key, value in (card.get("gradedPrices") or {}).items():
        grade = grade_from_key(key)
        if grade and _number(value):
            out.append({"grade": grade, "price_usd": float(value), "sample_size": None, "last_sale_at": None})
    ebay = card.get("ebay") or card.get("ebay_data") or {}
    blocks = ebay.get("salesByGrade") or ebay
    for key, value in (blocks.items() if isinstance(blocks, dict) else []):
        grade = grade_from_key(key)
        if not grade:
            continue
        last_sale = None
        if isinstance(value, dict):
            price = next((_number(value.get(k)) for k in PRICE_KEYS if _number(value.get(k))), None)
            count = next((int(value[k]) for k in COUNT_KEYS if isinstance(value.get(k), int)), None)
            last_sale = _ts(value.get("lastSaleDate"))
        elif isinstance(value, list):        # list of individual sales
            prices = [p for p in (_number(s.get("price")) for s in value if isinstance(s, dict)) if p]
            prices.sort()
            price = prices[len(prices) // 2] if prices else None
            count = len(prices)
        else:
            continue
        if price:
            out.append({"grade": grade, "price_usd": price, "sample_size": count, "last_sale_at": last_sale})
    # keep the entry with the largest sample per grade (sold data beats a bare graded price)
    best: dict[str, dict] = {}
    for o in out:
        cur = best.get(o["grade"])
        if cur is None or (o["sample_size"] or 0) > (cur["sample_size"] or 0):
            best[o["grade"]] = o
    return list(best.values())


def cards_to_fetch(conn, n: int) -> list:
    """Cards with no / the oldest sold data first, then the most valuable.
    One request per TCGplayer product (normal and reverse share one on TCGdex)."""
    rows = _candidates(conn)
    rows.sort(key=lambda r: (r.last_sold_obs is not None, r.last_sold_obs or datetime.min.replace(tzinfo=timezone.utc),
                             -(float(r.ref_price) if r.ref_price else 0)))
    return rows[:n]


def _candidates(conn) -> list:
    return conn.execute(text("""
        select distinct on (v.tcgplayer_id) c.card_id, v.finish, v.tcgplayer_id,
               (select max(o.observed_at) from price_observations o
                 where o.card_id = c.card_id and o.source = :src) as last_sold_obs,
               (select max(r.price_eur) from price_observations r
                 where r.card_id = c.card_id and r.price_type = 'trend') as ref_price
        from cards c
        join card_variants v on v.card_id = c.card_id and not v.is_special and v.tcgplayer_id is not null
        where c.language = 'en'
        order by v.tcgplayer_id, v.finish"""), {"src": SOURCE}).all()


async def fetch(tcgplayer_ids: list[int]) -> tuple[dict[int, dict | Exception], int]:
    results, credits = {}, 0
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        for tid in tcgplayer_ids:            # sequential: well under the 60 req/min limit
            try:
                doc = await get_json(client, f"{BASE_URL}/cards",
                                     params={"tcgPlayerId": str(tid), "includeEbay": "true"})
                save_raw("pokepricetracker", f"cards?tcgPlayerId={tid}&includeEbay=true", doc)
                results[tid] = doc.get("data", doc) if isinstance(doc, dict) else doc
                credits += int((((doc.get("metadata") or {}).get("apiCallsConsumed") or {}).get("total")) or 0)
            except Exception as exc:
                results[tid] = exc
    return results, credits


def run(stats) -> dict:
    n_cards = DAILY_CREDIT_BUDGET // CREDITS_PER_CARD
    with engine().connect() as conn:
        targets = cards_to_fetch(conn, n_cards)
    fetched, credits_used = asyncio.run(fetch([t.tcgplayer_id for t in targets]))
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    inserted = failed = no_graded = 0
    with engine().begin() as conn:
        for t in targets:
            card = fetched.get(t.tcgplayer_id)
            if isinstance(card, Exception) or card is None:
                failed += 1
                continue
            card = card[0] if isinstance(card, list) and card else card
            graded = parse_graded(card) if isinstance(card, dict) else []
            # snapshot time = when the source last recomputed eBay stats (hour-snapped for idempotency)
            snap = _ts(((card.get("ebay") or {}) if isinstance(card, dict) else {}).get("updatedAt"))
            observed = snap.replace(minute=0, second=0, microsecond=0) if snap else now
            if not graded:
                no_graded += 1
            for g in graded:
                price_eur = to_eur(conn, g["price_usd"], "USD", observed.date())
                if price_eur is None:
                    continue
                inserted += conn.execute(text("""
                    insert into price_observations (card_id, finish, grade, source, price_type, price,
                                                    currency, price_eur, observed_at, sample_size, last_sale_at)
                    values (:c, :f, :g, :src, 'sold_median', :p, 'USD', :pe, :o, :n, :ls)
                    on conflict do nothing"""),
                    {"c": t.card_id, "f": t.finish, "g": g["grade"], "src": SOURCE, "p": g["price_usd"],
                     "pe": price_eur, "o": observed, "n": g["sample_size"], "ls": g["last_sale_at"]}).rowcount
    stats.rows_in = len(targets)
    stats.rows_out = inserted
    return {"cards_requested": len(targets), "credits_used": credits_used,
            "failed": failed, "cards_without_graded_data": no_graded, "observations_new": inserted}
