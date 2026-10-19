"""Unit tests: anomaly detectors and the market-value / buyback policy."""
from pricing.anomalies import (detect_grade_ladder, detect_listing_vs_sold, detect_premium_outliers,
                               detect_source_disagreement, detect_spikes, robust_z)
from pricing.buyback import choose_market_value, downgrade, quote


def peers(n=12, premium=5.0, grade="PSA 10"):
    """n normal cards worth €10 raw whose PSA 10 sells for `premium` x raw (with small spread)."""
    sales, ref = [], {}
    for i in range(n):
        cid = f"c{i:02d}"
        ref[(cid, "holo")] = 10.0
        sales.append({"card_id": cid, "finish": "holo", "grade": grade,
                      "price": 10.0 * premium * (1 + 0.03 * (i % 5)), "n": 50})
    return sales, ref


# ---------- detectors ----------
def test_robust_z_needs_enough_peers():
    assert robust_z({i: 1.0 + i for i in range(5)}) == {}


def test_premium_outlier_flags_the_card_out_of_line():
    sales, ref = peers()
    sales.append({"card_id": "odd", "finish": "holo", "grade": "PSA 10", "price": 840.0, "n": 64})
    ref[("odd", "holo")] = 10.0
    out = detect_premium_outliers(sales, ref, special_prices={})
    assert [(f["card_id"], f["anomaly_type"], f["severity"]) for f in out] == [("odd", "premium_outlier", "warn")]


def test_premium_outlier_with_pricier_special_print_is_variant_pooling_error():
    # The Gengar pattern: base print €2.66, galaxy-foil print €45.81, PSA 10 sells ~84x base.
    sales, ref = peers()
    sales.append({"card_id": "gengar", "finish": "holo", "grade": "PSA 10", "price": 224.38, "n": 64})
    ref[("gengar", "holo")] = 2.66
    out = detect_premium_outliers(sales, ref, special_prices={"gengar": 45.81})
    assert len(out) == 1 and out[0]["anomaly_type"] == "variant_pooling" and out[0]["severity"] == "error"
    assert out[0]["details"]["special_print_raw_eur"] == 45.81


def test_cheap_cards_are_not_judged_on_premium():
    # A €0.14 common in a slab sells for €8+ because of grading fees: not an anomaly.
    sales, ref = peers()
    sales.append({"card_id": "bulk", "finish": "normal", "grade": "PSA 10", "price": 9.0, "n": 40})
    ref[("bulk", "normal")] = 0.14
    assert detect_premium_outliers(sales, ref, special_prices={"bulk": 60.0}) == []


def test_thin_sales_are_ignored():
    sales, ref = peers()
    sales.append({"card_id": "thin", "finish": "holo", "grade": "PSA 10", "price": 900.0, "n": 2})
    ref[("thin", "holo")] = 10.0
    assert detect_premium_outliers(sales, ref, {}) == []


def test_grade_ladder_inversion():
    sales = [{"card_id": "x", "finish": "holo", "grade": "PSA 7", "price": 111.0, "n": 20},
             {"card_id": "x", "finish": "holo", "grade": "PSA 8", "price": 88.0, "n": 30}]
    out = detect_grade_ladder(sales)
    assert [(f["grade"], f["anomaly_type"]) for f in out] == [("PSA 8", "grade_ladder_inversion")]


def test_grade_step_outlier_needs_no_reference_price():
    sales = []
    for i in range(12):
        sales += [{"card_id": f"c{i}", "finish": "holo", "grade": "PSA 9", "price": 20.0, "n": 50},
                  {"card_id": f"c{i}", "finish": "holo", "grade": "PSA 10", "price": 20.0 * (4 + 0.1 * (i % 4)), "n": 50}]
    sales += [{"card_id": "g", "finish": "holo", "grade": "PSA 9", "price": 24.66, "n": 77},
              {"card_id": "g", "finish": "holo", "grade": "PSA 10", "price": 224.38, "n": 64}]
    out = [f for f in detect_grade_ladder(sales) if f["anomaly_type"] == "grade_step_outlier"]
    assert [f["card_id"] for f in out] == ["g"]


def test_source_disagreement_ignores_penny_cards():
    out = detect_source_disagreement([
        {"card_id": "a", "finish": "holo", "cardmarket": 10.0, "tcgplayer": 40.0},   # 4x -> flag
        {"card_id": "b", "finish": "holo", "cardmarket": 10.0, "tcgplayer": 12.0},   # fine
        {"card_id": "c", "finish": "normal", "cardmarket": 0.05, "tcgplayer": 0.5},  # pennies -> ignore
    ])
    assert [f["card_id"] for f in out] == ["a"]


def test_price_spike():
    out = detect_spikes([
        {"card_id": "a", "finish": "holo", "grade": "RAW", "source": "s", "price_type": "trend", "latest": 30.0, "previous": 15.0},
        {"card_id": "b", "finish": "holo", "grade": "RAW", "source": "s", "price_type": "trend", "latest": 16.0, "previous": 15.0},
        {"card_id": "c", "finish": "holo", "grade": "RAW", "source": "s", "price_type": "trend", "latest": 2.0, "previous": 0.5},
    ])
    assert [(f["card_id"], f["details"]["change_pct"]) for f in out] == [("a", 100.0)]


def test_listing_far_from_sold():
    out = detect_listing_vs_sold([{"card_id": "a", "finish": "holo", "grade": "PSA 10", "listing": 40.0, "sold": 100.0},
                                  {"card_id": "b", "finish": "holo", "grade": "PSA 10", "listing": 110.0, "sold": 100.0}])
    assert [f["card_id"] for f in out] == ["a"]


# ---------- market value + buyback ----------
def est(price, confidence, n=20, synthetic=False):
    return {"price": price, "confidence": confidence, "n": n, "is_synthetic": synthetic}


def test_market_value_prefers_reliable_sales():
    mv = choose_market_value({"grade": "PSA 10", "sales": est(1288.55, "high", 594), "listing": est(900, "medium")})
    assert (mv["basis"], mv["value"]) == ("sales", 1288.55)


def test_raw_card_falls_back_to_reference_when_sales_are_thin():
    mv = choose_market_value({"grade": "RAW", "sales": est(400, "low", 2), "reference": est(327.72, "medium")})
    assert (mv["basis"], mv["value"]) == ("reference", 327.72)


def test_synthetic_listings_never_set_a_market_value():
    assert choose_market_value({"grade": "PSA 10", "listing": est(500, "high", synthetic=True)}) is None


def test_real_listings_are_discounted():
    mv = choose_market_value({"grade": "PSA 9", "listing": est(100, "medium")})
    assert (mv["basis"], mv["value"], mv["confidence"]) == ("listing_discounted", 85.0, "low")


def test_quote_auto_with_margin_by_confidence():
    q = quote({"value": 1288.55, "basis": "sales", "confidence": "high", "n": 594}, [])
    assert (q["decision"], q["offer"]) == ("auto", 966.41)


def test_error_anomaly_blocks_automatic_offer():
    q = quote({"value": 224.38, "basis": "sales", "confidence": "high", "n": 64},
              [{"anomaly_type": "variant_pooling", "severity": "error"}])
    assert (q["decision"], q["offer"]) == ("manual_review", None)


def test_warn_anomaly_lowers_confidence_and_raises_margin():
    q = quote({"value": 100.0, "basis": "sales", "confidence": "high", "n": 50},
              [{"anomaly_type": "grade_step_outlier", "severity": "warn"}])
    assert (q["confidence"], q["offer"]) == ("medium", 65.0)


def test_bulk_and_low_confidence_are_not_auto_quoted():
    assert quote({"value": 1.5, "basis": "reference", "confidence": "high", "n": 6}, [])["decision"] == "bulk"
    assert quote({"value": 50.0, "basis": "sales", "confidence": "low", "n": 2}, [])["decision"] == "manual_review"


def test_downgrade_floors_at_low():
    assert downgrade("high", 1) == "medium" and downgrade("medium", 5) == "low"
