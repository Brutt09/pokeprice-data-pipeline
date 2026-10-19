"""Suspicious-price detection. Runs after the estimates and writes one batch of
findings per run to price_anomalies; v_open_anomalies is the latest batch.

Detectors (each one is a pure function on plain rows, so it is unit-tested):
  premium_outlier         sold(grade) / raw reference is far outside what other
                          cards show for that grade (log-MAD robust z); only cards
                          worth >= €2 raw, where the grading fee doesn't set the price
  variant_pooling         premium_outlier on a card that also has a much pricier
                          special print (e.g. galaxy foil): sold data probably
                          mixes the two versions -> severity error
  grade_step_outlier      price jump between adjacent grades (PSA 9 -> PSA 10)
                          far outside the typical jump; needs no reference price
  grade_ladder_inversion  a higher grade sells for clearly less than a lower one
  source_disagreement     Cardmarket vs TCGplayer differ by more than 3x
  price_spike             a price series moved > 50% since its previous snapshot
  listing_far_from_sold   real asking prices far from real sold prices

Severity 'error' blocks automatic buyback offers; 'warn' lowers confidence.

Run:  python -m pricing.anomalies
"""
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from statistics import median

from sqlalchemy import text

from common.db import engine

ROBUST_Z = 3.5                 # log-MAD z-score threshold
MIN_PEERS = 10                 # cards needed before "unusual compared to peers" means anything
MIN_SALES = 10                 # per grade, for ladder checks
SPECIAL_VARIANT_FACTOR = 3.0   # special print worth > 3x the base print
# Below this raw price the grading fee, not the card, sets the graded price
# (a €0.14 common still sells for €8+ in a slab), so premium ratios mean nothing.
MIN_RAW_FOR_PREMIUM = 2.0
SOURCE_RATIO_LIMIT = 3.0
SOURCE_MIN_EUR = 2.0           # ignore penny cards: ratios are meaningless there
SPIKE_CHANGE = 0.5
SPIKE_MIN_EUR = 5.0
GRADE_RE = re.compile(r"^(PSA|CGC|BGS|SGC|ACE) (\d+(?:\.5)?)$")


def robust_z(values: dict) -> dict:
    """{key: value>0} -> {key: z} on the log scale, using median and MAD."""
    logs = {k: math.log(v) for k, v in values.items() if v and v > 0}
    if len(logs) < MIN_PEERS:
        return {}
    med = median(logs.values())
    mad = median(abs(x - med) for x in logs.values()) or 1e-9
    return {k: (x - med) / (1.4826 * mad) for k, x in logs.items()}


def finding(card_id, finish, grade, kind, severity, observed, expected, **details):
    return {"card_id": card_id, "finish": finish, "grade": grade, "anomaly_type": kind,
            "severity": severity, "observed_value": round(observed, 2) if observed is not None else None,
            "expected_value": round(expected, 2) if expected is not None else None, "details": details}


def detect_premium_outliers(sales: list[dict], raw_ref: dict, special_prices: dict) -> list[dict]:
    """sales: [{card_id, finish, grade, price, n}]; raw_ref: {(card_id, finish): eur};
    special_prices: {card_id: max raw price of a special print}."""
    out = []
    by_grade = defaultdict(dict)
    for s in sales:
        ref = raw_ref.get((s["card_id"], s["finish"]))
        if ref and ref >= MIN_RAW_FOR_PREMIUM and s["n"] >= MIN_SALES:
            by_grade[s["grade"]][(s["card_id"], s["finish"])] = (s["price"] / ref, s["price"], ref)
    for grade, items in by_grade.items():
        z = robust_z({k: v[0] for k, v in items.items()})
        typical = median(v[0] for v in items.values()) if items else None
        for (card_id, finish), score in z.items():
            if score <= ROBUST_Z:
                continue
            premium, price, ref = items[(card_id, finish)]
            special = special_prices.get(card_id)
            if special and special > SPECIAL_VARIANT_FACTOR * ref:
                out.append(finding(card_id, finish, grade, "variant_pooling", "error", price, ref * typical,
                                   premium=round(premium, 1), typical_premium=round(typical, 1),
                                   robust_z=round(score, 1), base_raw_eur=ref, special_print_raw_eur=special,
                                   hypothesis="sold data likely mixes the base card with its pricier special print"))
            else:
                out.append(finding(card_id, finish, grade, "premium_outlier", "warn", price, ref * typical,
                                   premium=round(premium, 1), typical_premium=round(typical, 1),
                                   robust_z=round(score, 1)))
    return out


def grade_key(grade: str):
    m = GRADE_RE.match(grade or "")
    return (m.group(1), float(m.group(2))) if m else None


def detect_grade_ladder(sales: list[dict]) -> list[dict]:
    """Within one card + grading company: inversions, and unusual top-grade jumps."""
    out = []
    ladders = defaultdict(list)
    for s in sales:
        g = grade_key(s["grade"])
        if g and s["n"] >= MIN_SALES:
            ladders[(s["card_id"], s["finish"], g[0])].append((g[1], s["price"], s["grade"]))
    steps = defaultdict(dict)
    for (card_id, finish, company), rungs in ladders.items():
        rungs.sort()
        for (g_lo, p_lo, name_lo), (g_hi, p_hi, name_hi) in zip(rungs, rungs[1:]):
            if p_hi < 0.9 * p_lo:
                out.append(finding(card_id, finish, name_hi, "grade_ladder_inversion", "warn", p_hi, p_lo,
                                   lower_grade=name_lo, lower_grade_price=p_lo))
            steps[(name_lo, name_hi)][(card_id, finish)] = p_hi / p_lo
    for (name_lo, name_hi), ratios in steps.items():
        typical = median(ratios.values()) if ratios else None
        for (card_id, finish), score in robust_z(ratios).items():
            if score > ROBUST_Z:
                out.append(finding(card_id, finish, name_hi, "grade_step_outlier", "warn",
                                   ratios[(card_id, finish)], typical,
                                   step=f"{name_lo} -> {name_hi}", robust_z=round(score, 1)))
    return out


def detect_source_disagreement(pairs: list[dict]) -> list[dict]:
    """pairs: [{card_id, finish, cardmarket, tcgplayer}] in EUR."""
    out = []
    for p in pairs:
        a, b = p["cardmarket"], p["tcgplayer"]
        if min(a, b) < SOURCE_MIN_EUR:
            continue
        ratio = b / a
        if ratio > SOURCE_RATIO_LIMIT or ratio < 1 / SOURCE_RATIO_LIMIT:
            out.append(finding(p["card_id"], p["finish"], "RAW", "source_disagreement", "warn", b, a,
                               ratio=round(ratio, 2), sources="tcgplayer vs cardmarket"))
    return out


def detect_spikes(series: list[dict]) -> list[dict]:
    """series: [{card_id, finish, grade, source, price_type, latest, previous}]."""
    out = []
    for s in series:
        prev, cur = s["previous"], s["latest"]
        if not prev or max(prev, cur) < SPIKE_MIN_EUR:
            continue
        change = (cur - prev) / prev
        if abs(change) > SPIKE_CHANGE:
            out.append(finding(s["card_id"], s["finish"], s["grade"], "price_spike", "warn", cur, prev,
                               change_pct=round(100 * change, 1), source=s["source"], price_type=s["price_type"]))
    return out


def detect_listing_vs_sold(pairs: list[dict]) -> list[dict]:
    """pairs: [{card_id, finish, grade, listing, sold}] using REAL listings only."""
    out = []
    for p in pairs:
        ratio = p["listing"] / p["sold"]
        if ratio < 0.5 or ratio > 2.5:
            out.append(finding(p["card_id"], p["finish"], p["grade"], "listing_far_from_sold", "warn",
                               p["listing"], p["sold"], ratio=round(ratio, 2)))
    return out


# ---------------------------------------------------------------- data access
def load_inputs(conn) -> dict:
    sales = [dict(r._mapping) for r in conn.execute(text("""
        select card_id, finish, grade, estimated_price::float as price, n_observations as n
        from v_latest_estimates where estimate_type = 'sales'"""))]
    raw_ref = {(r.card_id, r.finish): float(r.p) for r in conn.execute(text("""
        select card_id, finish, estimated_price as p from v_latest_estimates
        where estimate_type = 'reference' and grade = 'RAW'"""))}
    special = {r.card_id: float(r.p) for r in conn.execute(text("""
        select o.card_id, max(o.price_eur) as p
        from price_observations o join card_variants v on v.variant_id = o.variant_id and v.is_special
        where o.price_type = 'trend' group by o.card_id"""))}
    pairs = [dict(r._mapping) for r in conn.execute(text("""
        select cm.card_id, cm.finish, cm.p::float as cardmarket, tp.p::float as tcgplayer
        from (select distinct on (o.card_id, o.finish) o.card_id, o.finish, o.price_eur p
              from price_observations o join card_variants v on v.variant_id = o.variant_id and not v.is_special
              where o.source = 'cardmarket_via_tcgdex' and o.price_type = 'trend'
              order by o.card_id, o.finish, o.observed_at desc) cm
        join (select distinct on (card_id, finish) card_id, finish, price_eur p from price_observations
              where source = 'tcgplayer_via_pokemontcg' and price_type = 'market'
              order by card_id, finish, observed_at desc) tp using (card_id, finish)"""))]
    series = [dict(r._mapping) for r in conn.execute(text("""
        with ranked as (
            select card_id, finish, grade, source, price_type, variant_id, price_eur,
                   row_number() over (partition by card_id, finish, grade, source, price_type, variant_id
                                      order by observed_at desc) rn
            from price_observations where price_type in ('trend', 'market', 'sold_median'))
        select a.card_id, a.finish, a.grade, a.source, a.price_type,
               a.price_eur::float as latest, b.price_eur::float as previous
        from ranked a join ranked b using (card_id, finish, grade, source, price_type, variant_id)
        where a.rn = 1 and b.rn = 2"""))]
    listing_vs_sold = [dict(r._mapping) for r in conn.execute(text("""
        select l.card_id, l.finish, l.grade, l.estimated_price::float as listing, s.estimated_price::float as sold
        from v_latest_estimates l join v_latest_estimates s using (card_id, finish, grade)
        where l.estimate_type = 'listing' and not l.is_synthetic
          and s.estimate_type = 'sales' and s.confidence <> 'low'"""))]
    return {"sales": sales, "raw_ref": raw_ref, "special": special, "pairs": pairs,
            "series": series, "listing_vs_sold": listing_vs_sold}


def detect_all(inp: dict) -> list[dict]:
    return (detect_premium_outliers(inp["sales"], inp["raw_ref"], inp["special"])
            + detect_grade_ladder(inp["sales"])
            + detect_source_disagreement(inp["pairs"])
            + detect_spikes(inp["series"])
            + detect_listing_vs_sold(inp["listing_vs_sold"]))


def run(stats) -> dict:
    now = datetime.now(timezone.utc)
    with engine().begin() as conn:
        inp = load_inputs(conn)
        found = detect_all(inp)
        for f in found:
            conn.execute(text("""
                insert into price_anomalies (card_id, finish, grade, anomaly_type, severity,
                                             observed_value, expected_value, details, detected_at)
                values (:card_id, :finish, :grade, :anomaly_type, :severity,
                        :observed_value, :expected_value, cast(:details as jsonb), :now)"""),
                {**f, "details": json.dumps(f["details"]), "now": now})
        # an empty batch must still close the previous one
        conn.execute(text("insert into anomaly_runs (detected_at, n_found) values (:now, :n)"),
                     {"now": now, "n": len(found)})
    by_type = defaultdict(int)
    for f in found:
        by_type[f"{f['anomaly_type']}:{f['severity']}"] += 1
    stats.rows_in = len(inp["sales"]) + len(inp["pairs"]) + len(inp["series"])
    stats.rows_out = len(found)
    return {"anomalies": len(found), "by_type": dict(by_type)}
