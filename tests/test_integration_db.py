"""Integration tests: real SQL against a throwaway PostgreSQL database.

Runs the estimate -> anomalies -> buyback -> data-quality stages on a small
fixture (12 normal cards + one card with the Gengar pattern) and checks the
decisions end to end. No network.

Needs TEST_DATABASE_URL (a database that may be wiped), e.g.
  postgresql+psycopg://pokeprice:pokeprice@localhost:5433/pokeprice_test
Skipped when it is not set.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

TEST_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="TEST_DATABASE_URL not set")


class Stats:
    rows_in = rows_out = 0
    notes: dict = {}


@pytest.fixture(scope="module")
def db():
    import common.db as cdb

    eng = create_engine(TEST_URL, future=True)
    with eng.begin() as conn:
        conn.exec_driver_sql("drop schema if exists public cascade; create schema public;")
    original = cdb.DATABASE_URL
    cdb.DATABASE_URL = TEST_URL          # point every stage at the test database
    cdb.engine.cache_clear()
    cdb.apply_schema()
    load_fixture(eng)
    yield eng
    cdb.DATABASE_URL = original
    cdb.engine.cache_clear()


def load_fixture(eng):
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    last_sale = now - timedelta(days=5)
    with eng.begin() as conn:
        cards = [(f"t-{i:03d}", f"Card {i}", 10.0) for i in range(12)] + [("t-094", "Gengar", 2.66)]
        for cid, name, raw in cards:
            conn.execute(text("""insert into cards (card_id, set_id, set_name, set_official_count, name,
                                 local_id, collector_number, rarity) values (:c, 'tst', 'Test', 200, :n, :l, :num, 'Rare')"""),
                         {"c": cid, "n": name, "l": cid[-3:], "num": int(cid[-3:])})
            conn.execute(text("insert into card_variants (variant_id, card_id, finish) values (:v, :c, 'holo')"),
                         {"v": f"{cid}:holo", "c": cid})
            for ptype, factor in (("trend", 1.0), ("avg7", 1.02), ("avg30", 0.98)):
                conn.execute(text("""insert into price_observations (card_id, finish, grade, variant_id, source,
                                     price_type, price, currency, price_eur, observed_at)
                                     values (:c, 'holo', 'RAW', :v, 'cardmarket_via_tcgdex', :t, :p, 'EUR', :p, :o)"""),
                             {"c": cid, "v": f"{cid}:holo", "t": ptype, "p": round(raw * factor, 2), "o": now})
            psa10 = 224.38 if cid == "t-094" else raw * 5 * (1 + 0.03 * (int(cid[-3:]) % 5))
            for grade, price, n in (("PSA 9", raw * 1.2 if cid != "t-094" else 24.66, 60), ("PSA 10", psa10, 64)):
                conn.execute(text("""insert into price_observations (card_id, finish, grade, source, price_type, price,
                                     currency, price_eur, observed_at, sample_size, last_sale_at)
                                     values (:c, 'holo', :g, 'ebay_sold_via_ppt', 'sold_median', :p, 'EUR', :p, :o, :n, :ls)"""),
                             {"c": cid, "g": grade, "p": round(price, 2), "o": now, "n": n, "ls": last_sale})
        # Gengar's special print (galaxy foil): worth far more than the base card
        conn.execute(text("""insert into card_variants (variant_id, card_id, finish, is_special, special_label)
                             values ('t-094:galaxy', 't-094', 'holo', true, 'foil:galaxy')"""))
        conn.execute(text("""insert into price_observations (card_id, finish, grade, variant_id, source, price_type,
                             price, currency, price_eur, observed_at)
                             values ('t-094', 'holo', 'RAW', 't-094:galaxy', 'cardmarket_via_tcgdex', 'trend',
                                     45.81, 'EUR', 45.81, :o)"""), {"o": now})


def run_stages():
    from pricing import anomalies, buyback, estimator
    from transformation import validation
    out = {}
    for name, fn in (("estimate", estimator.run), ("anomalies", anomalies.run),
                     ("buyback", buyback.run), ("validate", validation.run)):
        out[name] = fn(Stats())
    return out


def test_end_to_end_decisions(db):
    out = run_stages()
    assert out["estimate"]["sales_estimates"] == 26
    with db.connect() as conn:
        flags = conn.execute(text("""select card_id, grade, anomaly_type, severity from v_open_anomalies
                                     where severity = 'error'""")).all()
        # every Gengar grade is inflated relative to its base print, as in the real data
        assert sorted((f.card_id, f.grade, f.anomaly_type) for f in flags) == [
            ("t-094", "PSA 10", "variant_pooling"), ("t-094", "PSA 9", "variant_pooling")]

        q = {(r.card_id, r.grade): r for r in conn.execute(text("select * from v_latest_quotes")).all()}
        assert q[("t-094", "PSA 10")].decision == "manual_review" and q[("t-094", "PSA 10")].offer is None
        normal = q[("t-001", "PSA 10")]
        assert normal.decision == "auto" and 0 < normal.offer < normal.market_value
        assert q[("t-094", "RAW")].basis == "reference"     # the special print never leaks into the base price

        failed_errors = conn.execute(text("""select check_name from dq_results where severity = 'error'
                                             and not passed""")).scalars().all()
        assert failed_errors == []


def test_rerun_is_idempotent_for_open_state(db):
    run_stages()
    with db.connect() as conn:
        first = conn.execute(text("select count(*) from v_open_anomalies")).scalar_one()
        quotes_first = conn.execute(text("select count(*) from v_latest_quotes")).scalar_one()
    run_stages()
    with db.connect() as conn:
        assert conn.execute(text("select count(*) from v_open_anomalies")).scalar_one() == first
        assert conn.execute(text("select count(*) from v_latest_quotes")).scalar_one() == quotes_first


def test_dq_catches_an_auto_offer_on_a_flagged_price(db):
    """Tamper with a quote: the data-quality stage must refuse it."""
    from transformation import validation
    with db.begin() as conn:
        conn.execute(text("""update buyback_quotes set decision = 'auto', offer = 100
                             where quote_id = (select quote_id from v_latest_quotes
                                               where card_id = 't-094' and grade = 'PSA 10')"""))
    with pytest.raises(AssertionError, match="auto_offer_on_flagged_price"):
        validation.run(Stats())
