"""Data-quality checks written as SQL assertions. Each query returns the number
of failing rows; results go to dq_results so quality is tracked over time.
'error' checks fail the pipeline run, 'warn' checks are reported only.

Run:  python -m transformation.validation
"""
import json

from sqlalchemy import text

from common.db import engine

CHECKS = [
    # (name, table, severity, failing-rows SQL)
    ("cards_required_fields", "cards", "error",
     "select count(*) from cards where name is null or local_id is null or set_id is null"),
    ("cards_complete_vs_set", "cards", "warn", """
        select count(*) from (
          select r.payload->>'id' set_id, jsonb_array_length(r.payload->'cards') expected,
                 (select count(*) from cards c where c.set_id = r.payload->>'id') actual
          from (select distinct on (endpoint) payload from raw_ingestions
                where source = 'tcgdex' and endpoint like 'sets/%' order by endpoint, fetched_at desc) r
        ) s where actual < expected"""),
    ("cards_without_reference_price", "price_observations", "warn", """
        select count(*) from cards c where c.language = 'en'
        and not exists (select 1 from price_observations o where o.card_id = c.card_id)"""),
    ("reference_prices_stale", "price_observations", "warn", """
        select case when max(observed_at) < now() - interval '3 days' then 1 else 0 end
        from price_observations"""),
    ("jp_cards_without_english_link", "card_equivalents", "warn", """
        select count(*) from cards c where c.language = 'ja'
        and not exists (select 1 from card_equivalents e where e.card_id = c.card_id)"""),
    ("observations_eur_mismatch", "price_observations", "error",
     "select count(*) from price_observations where currency = 'EUR' and price <> price_eur"),
    ("listings_unknown_currency", "listings", "error", """
        select count(*) from listings where price is not null
        and (currency is null or currency not in (select quote from fx_rates union select 'EUR'))"""),
    ("listings_missing_price", "listings", "warn",
     "select count(*) from listings where price_eur is null and not is_excluded"),
    ("listings_duplicate_rate_over_10pct", "listings", "warn", """
        select case when avg(is_duplicate::int) > 0.10 then 1 else 0 end from listings"""),
    ("matches_grade_consistent", "card_matches", "error", """
        select count(*) from card_matches m join listings l using (source, listing_id)
        where m.status <> 'rejected' and m.grade <> l.parsed_grade"""),
    ("match_auto_rate_under_80pct", "card_matches", "warn", """
        select case when avg((m.status = 'auto')::int) < 0.80 then 1 else 0 end
        from card_matches m join listings l using (source, listing_id) where not l.is_excluded"""),
    ("estimates_listing_vs_reference_3x_n3", "price_estimates", "warn", """
        select count(*) from v_latest_estimates a join v_latest_estimates r
          on (a.card_id, a.finish, a.grade) = (r.card_id, r.finish, r.grade)
        where a.estimate_type = 'listing' and r.estimate_type = 'reference' and a.n_observations >= 3
          and (a.estimated_price > 3 * r.estimated_price or a.estimated_price < r.estimated_price / 3)"""),
]


def run(stats) -> dict:
    results = []
    with engine().begin() as conn:
        for name, table, severity, sql in CHECKS:
            failing = int(conn.execute(text(sql)).scalar() or 0)
            results.append({"check": name, "severity": severity, "passed": failing == 0, "failing": failing})
            conn.execute(text("""insert into dq_results (check_name, table_name, severity, passed, failing_rows, details)
                                 values (:n, :t, :s, :p, :f, cast(:d as jsonb))"""),
                         {"n": name, "t": table, "s": severity, "p": failing == 0, "f": failing,
                          "d": json.dumps({"sql": " ".join(sql.split())})})
    stats.rows_in = len(results)
    stats.rows_out = sum(r["passed"] for r in results)
    errors = [r for r in results if r["severity"] == "error" and not r["passed"]]
    summary = {"passed": stats.rows_out, "total": len(results),
               "failed": [r["check"] for r in results if not r["passed"]]}
    if errors:
        raise AssertionError(f"data-quality errors: {[e['check'] for e in errors]}")
    return summary
