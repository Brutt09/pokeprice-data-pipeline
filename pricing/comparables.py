"""Comparable-card estimates: a graded price for products with no reliable sales.

For a target card and grade, take the K cards closest in RAW value (log scale)
that DO have medium/high-confidence sales at that grade, and apply the median
of their grade premiums to the target's raw reference price:

    estimate = raw_ref(target) x median(sold_grade(comp) / raw_ref(comp))

Closeness in raw value matters because the premium is not constant: a €0.10
common in a PSA 10 slab sells for ~100x raw (grading fees set a floor), a
€100 chase card for a few x.

Comps come only from the same rarity tier. The cheap cards that DO have graded
sales are mostly famous Pokémon (Pikachu, Charmander): without the tier rule a
PSA 10 Caterpie was priced off Pikachu's premium (€87). A tier with fewer than
K comps gets no estimate: a blank beats a confident wrong number. Same for a
card whose raw value is outside its tier's training range: a premium learned
on €0.50 rares, applied to a €7.73 reverse holo, gave a €200 CGC 9.

Accuracy is measured on every run by leave-one-card-out on the training cards
(each card predicted from the others, never from itself) and stored with each
estimate. A grade is only estimated when it has MIN_TRAIN training cards.
Confidence is never 'high': this is a model, not a transaction.

Run:  python -m pricing.comparables
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import text

from common.db import engine

K = 8                    # comparables per estimate (backtested: K=5 and K=8 err about the same)
MIN_TRAIN = 20           # training cards a grade needs before it is estimated at all
MEDIUM_MAX_MDAPE = 0.20  # backtest median error at or below this -> 'medium', else 'low'
TIER = {"Common": "base", "Uncommon": "base", "Rare": "rare",
        "Double rare": "chase", "Ultra Rare": "chase", "Illustration rare": "chase",
        "Special illustration rare": "chase", "Hyper rare": "chase"}


def nearest_comps(target_raw: float, train: list[dict], k: int = K) -> list[dict]:
    """The k training products closest in raw value, on the log scale."""
    return sorted(train, key=lambda t: abs(math.log(t["raw"]) - math.log(target_raw)))[:k]


def predict(target_raw: float, comps: list[dict]) -> tuple[float, float, float]:
    """(estimate, low, high): target raw x median / min / max of the comps' premiums."""
    ratios = [c["sold"] / c["raw"] for c in comps]
    return target_raw * median(ratios), target_raw * min(ratios), target_raw * max(ratios)


def backtest(train: list[dict], k: int = K) -> dict:
    """Leave-one-card-out: predict every training card from the other cards in its tier.
    Split by card, not by row, so a card's other finish never leaks into its own test."""
    errs = []
    for t in train:
        others = [o for o in train if o["card_id"] != t["card_id"] and o["tier"] == t["tier"]]
        if len(others) < k:
            continue
        est, _, _ = predict(t["raw"], nearest_comps(t["raw"], others, k))
        errs.append(abs(est - t["sold"]) / t["sold"])
    if not errs:
        return {"n": 0, "mdape": None, "p80": None}
    errs.sort()
    return {"n": len(errs), "mdape": round(median(errs), 3), "p80": round(errs[int(0.8 * (len(errs) - 1))], 3)}


def comparable_confidence(mdape: float | None) -> str:
    """'medium' only when the grade backtests well. Never 'high'."""
    return "medium" if mdape is not None and mdape <= MEDIUM_MAX_MDAPE else "low"


def build_estimates(sales: list[dict], raw_ref: dict, blocked: set, rarity: dict) -> tuple[list[dict], dict]:
    """sales: [{card_id, finish, grade, price, confidence}] latest sales estimates.
    raw_ref: {(card_id, finish): raw reference EUR}. blocked: {(card_id, finish, grade)}
    with an open error anomaly (e.g. variant pooling): never trained on, never estimated.
    rarity: {card_id: rarity}; unknown rarities form their own tier."""
    tier = lambda card_id: TIER.get(rarity.get(card_id), "other")
    reliable = [s for s in sales if s["confidence"] in ("medium", "high")]
    covered = {(s["card_id"], s["finish"], s["grade"]) for s in reliable}
    train_by_grade = defaultdict(list)
    for s in reliable:
        key = (s["card_id"], s["finish"])
        if key in raw_ref and (*key, s["grade"]) not in blocked:
            train_by_grade[s["grade"]].append({"card_id": s["card_id"], "finish": s["finish"],
                                               "tier": tier(s["card_id"]), "raw": raw_ref[key],
                                               "sold": s["price"]})
    out, report = [], {}
    for grade, train in sorted(train_by_grade.items()):
        if len(train) < MIN_TRAIN:
            continue
        bt = backtest(train)
        report[grade] = {"n_train": len(train), **bt, "n_estimates": 0, "skipped_too_few_comps": 0,
                         "skipped_outside_range": 0}
        for (card_id, finish), raw in raw_ref.items():
            if (card_id, finish, grade) in covered or (card_id, finish, grade) in blocked:
                continue
            pool = [t for t in train if t["card_id"] != card_id and t["tier"] == tier(card_id)]
            if len(pool) < K:
                report[grade]["skipped_too_few_comps"] += 1
                continue
            if not min(t["raw"] for t in pool) <= raw <= max(t["raw"] for t in pool):
                report[grade]["skipped_outside_range"] += 1
                continue
            comps = nearest_comps(raw, pool)
            est, low, high = predict(raw, comps)
            out.append({
                "card_id": card_id, "finish": finish, "grade": grade, "estimate_type": "comparable",
                "estimated_price": round(est, 2), "currency": "EUR",
                "price_low": round(low, 2), "price_high": round(high, 2), "n_observations": len(comps),
                "confidence": comparable_confidence(bt["mdape"]),
                "method": f"raw_reference x median(sold/raw) of the {K} same-tier cards closest in raw value "
                          f"with reliable {grade} sales",
                "inputs": {"raw_reference_eur": raw, "tier": tier(card_id),
                           "backtest": {"method": "leave-one-card-out", **bt},
                           "comps": [{"card_id": c["card_id"], "finish": c["finish"], "raw_eur": c["raw"],
                                      "sold_eur": c["sold"], "premium": round(c["sold"] / c["raw"], 2)}
                                     for c in comps]},
                "is_synthetic": False,
            })
            report[grade]["n_estimates"] += 1
    return out, report


def run(stats) -> dict:
    now = datetime.now(timezone.utc)
    with engine().begin() as conn:
        sales = [dict(r._mapping) for r in conn.execute(text("""
            select card_id, finish, grade, estimated_price::float as price, confidence
            from v_latest_estimates where estimate_type = 'sales'"""))]
        raw_ref = {(r.card_id, r.finish): float(r.p) for r in conn.execute(text("""
            select card_id, finish, estimated_price as p from v_latest_estimates
            where estimate_type = 'reference' and grade = 'RAW'"""))}
        blocked = {(r.card_id, r.finish, r.grade) for r in conn.execute(text("""
            select card_id, finish, grade from v_open_anomalies where severity = 'error'"""))}
        rarity = dict(conn.execute(text("select card_id, rarity from cards")).all())
        found, report = build_estimates(sales, raw_ref, blocked, rarity)
        for e in found:
            conn.execute(text("""
                insert into price_estimates (card_id, finish, grade, estimate_type, estimated_price, currency,
                    price_low, price_high, n_observations, confidence, method, inputs, is_synthetic, calculated_at)
                values (:card_id, :finish, :grade, :estimate_type, :estimated_price, :currency,
                    :price_low, :price_high, :n_observations, :confidence, :method, cast(:inputs as jsonb),
                    :is_synthetic, :now)"""), {**e, "inputs": json.dumps(e["inputs"]), "now": now})
    conf = defaultdict(int)
    for e in found:
        conf[e["confidence"]] += 1
    stats.rows_in = len(sales)
    stats.rows_out = len(found)
    stats.notes = {"backtest": report}
    return {"comparable_estimates": len(found), "confidence": dict(conf), "by_grade": report}


if __name__ == "__main__":
    from common.db import pipeline_run

    with pipeline_run("comparable_estimates") as s:
        print(json.dumps(run(s), indent=2))
