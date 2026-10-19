"""Explainable price estimates per product (card_id, finish, grade).

Two estimates are kept apart on purpose:
  reference  weighted median of reference price-guide figures (Cardmarket,
             TCGplayer) for RAW cards. Weights by price type and source.
  listing    weighted median of matched ACTIVE listings (asking prices, not
             sales). Outliers removed with a log-MAD test; weights by match
             score and freshness.
Every estimate stores its inputs so the number can be traced back.

Run:  python -m pricing.estimator
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import text

from common.db import engine

SOURCE_WEIGHT = {"cardmarket_via_tcgdex": 1.0, "tcgplayer_via_pokemontcg": 1.0}
# 'low' is the cheapest listing on the platform: kept as data, not used for value.
PRICE_TYPE_WEIGHT = {"trend": 3.0, "avg7": 2.0, "avg30": 1.5, "avg1": 1.0, "avg": 1.0, "market": 3.0, "mid": 1.5}
REFERENCE_MAX_AGE_DAYS = 7
LISTING_WINDOW_DAYS = 30
FRESHNESS_HALF_LIFE_DAYS = 14
MAD_Z = 3.0


def weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def mad_outliers(prices: list[float]) -> list[bool]:
    """True where a price is an outlier on the log scale (robust z > 3).
    Needs at least 4 points; otherwise nothing is flagged."""
    if len(prices) < 4:
        return [False] * len(prices)
    logs = [math.log(p) for p in prices]
    med = median(logs)
    mad = median(abs(x - med) for x in logs)
    if mad == 0:
        return [abs(x - med) > 0.5 for x in logs]
    return [abs(x - med) / (1.4826 * mad) > MAD_Z for x in logs]


def reference_confidence(values: list[float], n_sources: int, age_days: float) -> str:
    mid = median(values)
    spread = (max(values) - min(values)) / mid if mid else 1
    if age_days > 3:
        return "low" if spread > 0.6 else "medium"
    if spread <= 0.25 and len(values) >= 3:
        return "high"
    return "medium" if spread <= 0.6 else "low"


def listing_confidence(prices: list[float]) -> str:
    n = len(prices)
    s = sorted(prices)
    q1, q3 = s[int(0.25 * (n - 1))], s[int(0.75 * (n - 1))]
    rel_iqr = (q3 - q1) / median(s) if median(s) else 1
    if n >= 8 and rel_iqr <= 0.35:
        return "high"
    if n >= 4 and rel_iqr <= 0.6:
        return "medium"
    return "low"


def reference_estimates(conn, now: datetime) -> list[dict]:
    rows = conn.execute(text("""
        select distinct on (o.source, o.card_id, o.finish, o.price_type)
               o.card_id, o.finish, o.grade, o.source, o.price_type, o.price_eur, o.observed_at
        from price_observations o
        left join card_variants v on v.variant_id = o.variant_id
        where o.grade = 'RAW' and coalesce(v.is_special, false) = false
          and o.observed_at >= now() - make_interval(days => :d)
        order by o.source, o.card_id, o.finish, o.price_type, o.observed_at desc"""),
        {"d": REFERENCE_MAX_AGE_DAYS}).all()
    groups = defaultdict(list)
    for r in rows:
        if PRICE_TYPE_WEIGHT.get(r.price_type):
            groups[(r.card_id, r.finish, r.grade)].append(r)
    out = []
    for (card_id, finish, grade), obs in groups.items():
        values = [float(o.price_eur) for o in obs]
        weights = [PRICE_TYPE_WEIGHT[o.price_type] * SOURCE_WEIGHT.get(o.source, 0.5) for o in obs]
        newest = max(o.observed_at for o in obs)
        out.append({
            "card_id": card_id, "finish": finish, "grade": grade, "estimate_type": "reference",
            "estimated_price": round(weighted_median(values, weights), 2), "currency": "EUR",
            "price_low": min(values), "price_high": max(values), "n_observations": len(obs),
            "confidence": reference_confidence(values, len({o.source for o in obs}),
                                               (now - newest).total_seconds() / 86400),
            "method": "weighted_median(price_guide; weights=price_type*source)",
            "inputs": [{"source": o.source, "type": o.price_type, "eur": float(o.price_eur),
                        "observed_at": o.observed_at.isoformat()} for o in obs],
            "is_synthetic": False,
        })
    return out


def listing_estimates(conn, now: datetime) -> tuple[list[dict], int]:
    rows = conn.execute(text("""
        select m.card_id, m.finish, m.grade, m.match_score, l.source, l.listing_id,
               l.price_eur, coalesce(l.listed_at, l.fetched_at) as seen_at, l.is_synthetic
        from card_matches m
        join listings l using (source, listing_id)
        where m.status = 'auto' and not l.is_excluded and not l.is_duplicate
          and l.price_eur > 0
          and coalesce(l.listed_at, l.fetched_at) >= now() - make_interval(days => :d)"""),
        {"d": LISTING_WINDOW_DAYS}).all()
    groups = defaultdict(list)
    for r in rows:
        groups[(r.card_id, r.finish, r.grade)].append(r)
    out, outliers_total = [], 0
    for (card_id, finish, grade), items in groups.items():
        prices = [float(i.price_eur) for i in items]
        flags = mad_outliers(prices)
        kept = [(i, p) for i, p, f in zip(items, prices, flags) if not f]
        outliers_total += sum(flags)
        if not kept:
            continue
        weights = [float(i.match_score) / 100 *
                   0.5 ** (max((now - i.seen_at).total_seconds(), 0) / 86400 / FRESHNESS_HALF_LIFE_DAYS)
                   for i, _ in kept]
        values = [p for _, p in kept]
        out.append({
            "card_id": card_id, "finish": finish, "grade": grade, "estimate_type": "listing",
            "estimated_price": round(weighted_median(values, weights), 2), "currency": "EUR",
            "price_low": min(values), "price_high": max(values), "n_observations": len(kept),
            "confidence": listing_confidence(values),
            "method": "weighted_median(active asking prices; log-MAD outliers removed; "
                      "weights=match_score*freshness)",
            "inputs": {"listing_ids": [f"{i.source}:{i.listing_id}" for i, _ in kept],
                       "outliers_removed": [f"{i.source}:{i.listing_id}"
                                            for i, f in zip(items, flags) if f]},
            "is_synthetic": any(i.is_synthetic for i, _ in kept),
        })
    return out, outliers_total


def sales_confidence(n_sales: int, days_since_last_sale: int) -> str:
    """Thin or stale markets get less confidence, however exact the number looks."""
    if n_sales >= 10 and days_since_last_sale <= 30:
        return "high"
    if n_sales >= 3 and days_since_last_sale <= 90:
        return "medium"
    return "low"


def sales_estimates(conn) -> list[dict]:
    """Sold-price estimate per (card, finish, grade) from eBay sold averages.
    Snapshots in the window are combined by a median weighted by sample size."""
    rows = conn.execute(text("""
        select card_id, finish, grade, price_eur, sample_size, observed_at, last_sale_at
        from price_observations
        where source = 'ebay_sold_via_ppt' and price_type = 'sold_median'
          and observed_at >= now() - make_interval(days => :d)"""), {"d": LISTING_WINDOW_DAYS}).all()
    groups = defaultdict(list)
    for r in rows:
        groups[(r.card_id, r.finish, r.grade)].append(r)
    out = []
    for (card_id, finish, grade), obs in groups.items():
        values = [float(o.price_eur) for o in obs]
        weights = [max(o.sample_size or 1, 1) for o in obs]
        latest = max(obs, key=lambda o: o.observed_at)
        n_sales = latest.sample_size or 0
        days_since_sale = ((datetime.now(timezone.utc) - latest.last_sale_at).days
                           if latest.last_sale_at else 9999)
        out.append({
            "card_id": card_id, "finish": finish, "grade": grade, "estimate_type": "sales",
            "estimated_price": round(weighted_median(values, weights), 2), "currency": "EUR",
            "price_low": min(values), "price_high": max(values), "n_observations": sum(weights),
            "confidence": sales_confidence(n_sales, days_since_sale),
            "method": "weighted_median(eBay sold medians per grade via PokemonPriceTracker; weights=sample_size)",
            "inputs": [{"eur": float(o.price_eur), "n": o.sample_size, "observed_at": o.observed_at.isoformat(),
                        "last_sale_at": o.last_sale_at.isoformat() if o.last_sale_at else None} for o in obs],
            "is_synthetic": False,
        })
    return out


def run(stats) -> dict:
    now = datetime.now(timezone.utc)
    with engine().begin() as conn:
        refs = reference_estimates(conn, now)
        lists, outliers = listing_estimates(conn, now)
        sales = sales_estimates(conn)
        for e in refs + lists + sales:
            conn.execute(text("""
                insert into price_estimates (card_id, finish, grade, estimate_type, estimated_price, currency,
                    price_low, price_high, n_observations, confidence, method, inputs, is_synthetic, calculated_at)
                values (:card_id, :finish, :grade, :estimate_type, :estimated_price, :currency,
                    :price_low, :price_high, :n_observations, :confidence, :method, cast(:inputs as jsonb),
                    :is_synthetic, :now)"""), {**e, "inputs": json.dumps(e["inputs"]), "now": now})
    stats.rows_out = len(refs) + len(lists) + len(sales)
    conf = defaultdict(int)
    for e in refs + lists + sales:
        conf[f"{e['estimate_type']}:{e['confidence']}"] += 1
    return {"reference_estimates": len(refs), "listing_estimates": len(lists), "sales_estimates": len(sales),
            "listing_outliers_removed": outliers, "confidence": dict(conf)}
