"""Market value + buyback quote per product (card, finish, grade).

Market value: the estimates are never averaged together. The value comes from
the best available evidence, in this order:
  1. sales      eBay sold median, if confidence is medium or high
  2. reference  price guide, for RAW cards only
  3. comparable model estimate from similar cards' sales (graded products with
                no reliable sales of their own). Always 'low' here: a human
                checks it, it never makes an automatic offer
  4. listing    real active asking prices (never synthetic), discounted because
                asking prices sit above what buyers pay
Open anomalies lower confidence ('warn') or block automatic offers ('error').

Buyback quote: offer = market value x (1 - margin), the margin growing as
confidence falls. A quote is not issued automatically when confidence is low,
when an 'error' anomaly is open, or for bulk cards below BULK_BELOW_EUR.
These are policy parameters, not facts: POLICY_VERSION is stored on each quote.

Run:  python -m pricing.buyback
"""
import json
from datetime import datetime, timezone

from sqlalchemy import text

from common.db import engine

POLICY_VERSION = "2026-10-20.v2"
MARGIN = {"high": 0.25, "medium": 0.35}   # share of market value kept as margin
LISTING_DISCOUNT = 0.85                   # asking -> expected sale price, when only listings exist
BULK_BELOW_EUR = 2.0
CONFIDENCE_ORDER = ["low", "medium", "high"]


def downgrade(conf: str, steps: int) -> str:
    return CONFIDENCE_ORDER[max(CONFIDENCE_ORDER.index(conf) - steps, 0)]


def choose_market_value(estimates: dict) -> dict | None:
    """estimates: {estimate_type: {price, confidence, n, is_synthetic}} for ONE product, plus
    'grade'. Returns {value, basis, confidence, n} or None when nothing usable exists."""
    sales = estimates.get("sales")
    if sales and sales["confidence"] in ("medium", "high"):
        return {"value": sales["price"], "basis": "sales", "confidence": sales["confidence"], "n": sales["n"]}
    ref = estimates.get("reference")
    if ref and estimates.get("grade") == "RAW":
        return {"value": ref["price"], "basis": "reference", "confidence": ref["confidence"], "n": ref["n"]}
    comp = estimates.get("comparable")
    if comp:
        return {"value": comp["price"], "basis": "comparable", "confidence": "low", "n": comp["n"]}
    listing = estimates.get("listing")
    if listing and not listing["is_synthetic"]:
        return {"value": round(listing["price"] * LISTING_DISCOUNT, 2), "basis": "listing_discounted",
                "confidence": "low", "n": listing["n"]}
    if sales:   # low-confidence sales are still better than nothing, but flagged as low
        return {"value": sales["price"], "basis": "sales", "confidence": "low", "n": sales["n"]}
    return None


def quote(market: dict, anomalies: list[dict]) -> dict:
    """Apply anomalies and the buyback policy to one product's market value."""
    errors = [a for a in anomalies if a["severity"] == "error"]
    warns = [a for a in anomalies if a["severity"] == "warn"]
    confidence = downgrade(market["confidence"], len(warns))
    reasons = [f"market value from {market['basis']} ({market['n']} obs)"]
    reasons += [f"{a['anomaly_type']} ({a['severity']})" for a in anomalies]
    if errors:
        decision, offer = "manual_review", None
        reasons.append("open error anomaly: price not trusted for an automatic offer")
    elif market["value"] < BULK_BELOW_EUR:
        decision, offer = "bulk", None
        reasons.append(f"below €{BULK_BELOW_EUR:.0f}: bought as bulk, not quoted individually")
    elif confidence not in MARGIN:
        decision, offer = "manual_review", None
        reasons.append("low confidence: needs a human price check")
    else:
        decision = "auto"
        offer = round(market["value"] * (1 - MARGIN[confidence]), 2)
        reasons.append(f"margin {MARGIN[confidence]:.0%} for {confidence} confidence")
    return {"market_value": market["value"], "basis": market["basis"], "confidence": confidence,
            "decision": decision, "offer": offer, "reasons": reasons}


def run(stats) -> dict:
    now = datetime.now(timezone.utc)
    with engine().begin() as conn:
        rows = conn.execute(text("""
            select card_id, finish, grade, estimate_type, estimated_price::float as price, confidence,
                   n_observations as n, is_synthetic
            from v_latest_estimates""")).all()
        anomalies = conn.execute(text("""
            select card_id, finish, grade, anomaly_type, severity from v_open_anomalies""")).all()
        products: dict = {}
        for r in rows:
            p = products.setdefault((r.card_id, r.finish, r.grade), {"grade": r.grade})
            p[r.estimate_type] = {"price": r.price, "confidence": r.confidence, "n": r.n,
                                  "is_synthetic": r.is_synthetic}
        open_by_product: dict = {}
        for a in anomalies:
            open_by_product.setdefault((a.card_id, a.finish, a.grade), []).append(dict(a._mapping))
        counts: dict = {}
        for key, ests in products.items():
            market = choose_market_value(ests)
            if not market:
                continue
            q = quote(market, open_by_product.get(key, []))
            counts[q["decision"]] = counts.get(q["decision"], 0) + 1
            conn.execute(text("""
                insert into buyback_quotes (card_id, finish, grade, market_value, basis, confidence,
                                            decision, offer, reasons, policy_version, quoted_at)
                values (:c, :f, :g, :mv, :b, :conf, :d, :o, cast(:r as jsonb), :pv, :now)"""),
                {"c": key[0], "f": key[1], "g": key[2], "mv": q["market_value"], "b": q["basis"],
                 "conf": q["confidence"], "d": q["decision"], "o": q["offer"],
                 "r": json.dumps(q["reasons"]), "pv": POLICY_VERSION, "now": now})
    stats.rows_in = len(products)
    stats.rows_out = sum(counts.values())
    return {"products_quoted": sum(counts.values()), "decisions": counts, "policy": POLICY_VERSION}
