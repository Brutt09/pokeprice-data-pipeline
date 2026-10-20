"""Unit tests: comparable-card estimates and how buyback treats them."""
from pricing.buyback import choose_market_value, quote
from pricing.comparables import (MIN_TRAIN, backtest, build_estimates, comparable_confidence, nearest_comps,
                                 predict)


def ladder(n=30, grade="PSA 10"):
    """n cards from €0.50 to ~€150 raw. Premium falls as raw value rises (grading-fee floor):
    cheap cards sell for ~20x raw in a slab, expensive ones for ~3x."""
    sales, ref = [], {}
    for i in range(n):
        cid, raw = f"c{i:02d}", round(0.5 * 1.22 ** i, 2)
        ref[(cid, "holo")] = raw
        premium = 20.0 if raw < 5 else 8.0 if raw < 30 else 3.0
        sales.append({"card_id": cid, "finish": "holo", "grade": grade, "price": raw * premium,
                      "confidence": "high"})
    return sales, ref


def test_nearest_comps_are_close_in_raw_value_on_log_scale():
    train = [{"card_id": c, "tier": "x", "raw": r, "sold": r} for c, r in [("a", 1), ("b", 2), ("c", 10), ("d", 100)]]
    assert [c["card_id"] for c in nearest_comps(9, train, k=2)] == ["c", "b"]


def test_predict_applies_median_premium_with_min_max_band():
    comps = [{"raw": 10, "sold": 30}, {"raw": 20, "sold": 100}, {"raw": 5, "sold": 20}]
    assert predict(4.0, comps) == (16.0, 12.0, 20.0)   # premiums 3, 5, 4 -> median 4


def test_backtest_never_uses_the_card_itself():
    # Each card has two finishes at the same raw price and premium. If the other finish leaked
    # into the prediction it would be the nearest comp and every error would be 0.
    train = [{"card_id": f"c{i}", "tier": "x", "raw": 10.0 * 4 ** i, "sold": 10.0 * 4 ** i * 3 * 2 ** i}
             for i in range(5) for _ in ("holo", "reverse")]
    bt = backtest(train, k=1)
    assert bt["n"] == 10 and bt["mdape"] >= 0.5


def test_confidence_follows_the_grade_backtest_and_is_never_high():
    assert comparable_confidence(0.15) == "medium"
    assert comparable_confidence(0.35) == "low"
    assert comparable_confidence(None) == "low"


def test_no_estimate_outside_the_training_raw_range():
    sales, ref = ladder()                      # raw values €0.50 .. ~€160
    ref[("pricey", "holo")] = 900.0
    out, report = build_estimates(sales, ref, blocked=set(), rarity={})
    assert out == [] and report["PSA 10"]["skipped_outside_range"] == 1


def test_build_estimates_fills_only_products_without_reliable_sales():
    sales, ref = ladder()
    ref[("new", "holo")] = 2.0                     # cheap card with no sales: should get ~20x
    sales.append({"card_id": "thin", "finish": "holo", "grade": "PSA 10", "price": 999.0, "confidence": "low"})
    ref[("thin", "holo")] = 100.0                  # only low-confidence sales: estimated, not trained on
    out, report = build_estimates(sales, ref, blocked=set(), rarity={})
    by_card = {e["card_id"]: e for e in out}
    assert set(by_card) == {"new", "thin"}
    assert by_card["new"]["estimated_price"] == 40.0
    assert by_card["new"]["estimate_type"] == "comparable" and by_card["new"]["confidence"] != "high"
    assert report["PSA 10"]["n_train"] == 30 and report["PSA 10"]["n_estimates"] == 2
    assert all(c["card_id"] != "thin" for c in by_card["thin"]["inputs"]["comps"])


def test_blocked_products_are_neither_trained_on_nor_estimated():
    sales, ref = ladder()
    ref[("gengar", "holo")] = 2.66
    out, _ = build_estimates(sales, ref, blocked={("gengar", "holo", "PSA 10"), ("c05", "holo", "PSA 10")}, rarity={})
    assert "gengar" not in {e["card_id"] for e in out}
    assert all(c["card_id"] != "c05" for e in out for c in e["inputs"]["comps"])


def test_grades_with_too_few_training_cards_are_skipped():
    sales, ref = ladder(n=MIN_TRAIN - 1, grade="BGS 10")
    ref[("new", "holo")] = 2.0
    out, report = build_estimates(sales, ref, blocked=set(), rarity={})
    assert out == [] and report == {}


def test_buyback_sends_comparable_values_to_a_human():
    est = {"grade": "PSA 10", "comparable": {"price": 40.0, "confidence": "medium", "n": 8, "is_synthetic": False}}
    mv = choose_market_value(est)
    assert (mv["basis"], mv["confidence"], mv["value"]) == ("comparable", "low", 40.0)
    assert quote(mv, [])["decision"] == "manual_review"


def test_reliable_sales_still_beat_comparable():
    est = {"grade": "PSA 10", "sales": {"price": 55.0, "confidence": "medium", "n": 12, "is_synthetic": False},
           "comparable": {"price": 40.0, "confidence": "medium", "n": 8, "is_synthetic": False}}
    assert choose_market_value(est)["basis"] == "sales"


def test_comps_only_come_from_the_same_rarity_tier():
    # Famous commons with big slab premiums must not price an obscure common... unless
    # they are the only commons; and a chase card never prices a common.
    sales, ref = ladder()
    rarity = {f"c{i:02d}": "Special illustration rare" for i in range(30)}
    ref[("caterpie", "normal")] = 0.08
    rarity["caterpie"] = "Common"
    out, report = build_estimates(sales, ref, blocked=set(), rarity=rarity)
    assert out == [] and report["PSA 10"]["skipped_too_few_comps"] == 1
