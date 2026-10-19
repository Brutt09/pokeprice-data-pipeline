"""Read-only API over the pipeline's tables.

Run:  uvicorn api.main:app --reload     (docs at http://localhost:8000/docs)
"""
from fastapi import FastAPI, HTTPException, Query
from sqlalchemy import text

from common.db import engine

app = FastAPI(title="PokéPrice API", version="0.1.0",
              description="Pokémon card catalogue, reference prices, listing matches and "
                          "explainable price estimates. Listing estimates are ASKING prices; "
                          "rows with is_synthetic=true are demo data.")


def rows(sql: str, **params) -> list[dict]:
    with engine().connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


@app.get("/health")
def health():
    last = rows("select job_name, status, completed_at from pipeline_runs order by run_id desc limit 1")
    return {"status": "ok", "last_job": last[0] if last else None}


@app.get("/cards")
def search_cards(q: str = Query("", description="name contains"), limit: int = Query(50, le=500)):
    return rows("""select card_id, name, local_id, set_name, rarity, image_url from cards
                   where name ilike :q order by collector_number limit :limit""",
                q=f"%{q}%", limit=limit)


@app.get("/cards/{card_id}")
def get_card(card_id: str):
    card = rows("select * from cards where card_id = :c", c=card_id)
    if not card:
        raise HTTPException(404, "card not found")
    variants = rows("select variant_id, finish, is_special, special_label, cardmarket_id "
                    "from card_variants where card_id = :c order by finish", c=card_id)
    return {**card[0], "variants": variants}


@app.get("/cards/{card_id}/estimates")
def get_estimates(card_id: str, grade: str | None = None):
    return rows("""select card_id, finish, grade, estimate_type, estimated_price, currency, price_low,
                          price_high, n_observations, confidence, method, is_synthetic, calculated_at
                   from v_latest_estimates where card_id = :c and (cast(:g as text) is null or grade = :g)
                   order by finish, grade, estimate_type""", c=card_id, g=grade)


@app.get("/cards/{card_id}/estimates/history")
def get_estimate_history(card_id: str, finish: str, grade: str = "RAW", estimate_type: str = "reference"):
    return rows("""select calculated_at, estimated_price, confidence, n_observations from price_estimates
                   where card_id = :c and finish = :f and grade = :g and estimate_type = :t
                   order by calculated_at""", c=card_id, f=finish, g=grade, t=estimate_type)


@app.get("/cards/{card_id}/observations")
def get_observations(card_id: str, finish: str | None = None):
    return rows("""select o.finish, o.grade, o.source, o.price_type, o.price, o.currency, o.price_eur,
                          o.observed_at, coalesce(v.is_special, false) as is_special_variant
                   from price_observations o left join card_variants v on v.variant_id = o.variant_id
                   where o.card_id = :c and (cast(:f as text) is null or o.finish = :f)
                   order by o.observed_at desc, o.source, o.price_type""", c=card_id, f=finish)


@app.get("/cards/{card_id}/listings")
def get_listings(card_id: str, limit: int = Query(100, le=1000)):
    return rows("""select l.source, l.listing_id, l.title, l.price, l.currency, l.price_eur, l.listed_at,
                          l.is_duplicate, l.is_synthetic, m.finish, m.grade, m.match_score, m.match_method
                   from card_matches m join listings l using (source, listing_id)
                   where m.card_id = :c and m.status = 'auto'
                   order by l.listed_at desc nulls last limit :limit""", c=card_id, limit=limit)


@app.get("/matches/review")
def review_queue(limit: int = Query(100, le=1000)):
    """Listings the matcher was not sure about — the human-in-the-loop queue."""
    return rows("""select m.source, m.listing_id, l.title, m.card_id, m.match_score, m.match_method
                   from card_matches m join listings l using (source, listing_id)
                   where m.status = 'review' order by m.match_score desc limit :limit""", limit=limit)


@app.get("/pipeline/runs")
def pipeline_runs(limit: int = Query(50, le=500)):
    return rows("""select run_id, job_name, status, started_at, completed_at, rows_in, rows_out, error
                   from pipeline_runs order by run_id desc limit :limit""", limit=limit)


@app.get("/quality")
def quality():
    """Latest result of every data-quality check."""
    return rows("""select distinct on (check_name) check_name, table_name, severity, passed, failing_rows, run_at
                   from dq_results order by check_name, run_at desc""")
