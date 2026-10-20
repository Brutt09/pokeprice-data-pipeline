"""PokéPrice dashboard: a window onto the pipeline for non-data staff (pricing, buyback).

Three tabs:
  Value a card        find a card, see every version (finish x grade) priced separately, and what to pay
  What makes this...  live evidence of the pipeline's work: sold data, EN<->JP links, caught anomalies,
                      backtested comparable estimates, title matching
  Pipeline health     runs, data-quality checks, matcher review queue
Every number is read from the database or latest_run.json; nothing is typed in by hand.

Run:  streamlit run dashboard/app.py
"""
import json
import re
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common.config import ROOT  # noqa: E402
from common.db import engine  # noqa: E402

SERIES_LISTING = "#2a78d6"     # categorical slot 1
SERIES_REFERENCE = "#eb6834"   # categorical slot 2

FINISH = {"normal": "Normal", "holo": "Holo", "reverse": "Reverse holo", "metal": "Metal"}
BASIS = {"sales": "Real eBay sales", "reference": "Price guide", "comparable": "Similar cards (model)",
         "listing_discounted": "Asking prices −15%"}
DECISION = {"auto": "✅ Make offer", "manual_review": "👀 Human check", "bulk": "📦 Bulk"}
ANOMALY = {"variant_pooling": "Sales mixed with a rarer special print",
           "premium_outlier": "Graded price far out of line with similar cards",
           "grade_ladder_inversion": "Lower grade selling above a higher grade",
           "source_disagreement": "Price guides disagree with each other",
           "price_spike": "Sudden price jump since yesterday",
           "listing_far_from_sold": "Asking prices far from sold prices"}
COMPANIES = ["PSA", "CGC", "BGS", "SGC", "ACE"]
RARITIES = ["Common", "Uncommon", "Rare", "Double rare", "Ultra Rare", "Illustration rare",
            "Special illustration rare", "Hyper rare"]   # cheapest to most chased
CATEGORY = {"Pokemon": "Pokémon", "Trainer": "Trainer", "Energy": "Energy"}
GUIDE = """
**A card's value depends on three things, and this dashboard never mixes them:**

1. **Rarity: how hard it is to pull from a pack.** The symbol at the bottom of the card.

   | Rarity | What it means |
   |---|---|
   | Common / Uncommon | Everyday cards, usually worth cents |
   | Rare | One per pack, a little more |
   | Double rare | The "ex" cards: stronger Pokémon, a few euros |
   | Ultra Rare | Full-art versions: the art covers the whole card |
   | Illustration rare | Special artwork, the Pokémon in a scene |
   | Special illustration rare | The chase cards: full-scene art, often the most valuable |
   | Hyper rare | Gold cards, very rare |

2. **Version (finish): how the card is printed.** *Normal* = no shine · *Holo* = the picture is shiny ·
   *Reverse holo* = the border is shiny instead.

3. **Condition (grade).** *Ungraded (raw)* = straight from the pack. Collectors pay a company
   (**PSA**, **CGC**, **BGS**, **SGC**, **ACE**) to inspect a card, score it **1 to 10** and seal it
   in a plastic case. A **10** is perfect and can be worth many times the ungraded card.

**Card types:** *Pokémon* (the creatures, each with an element like Fire or Water) ·
*Trainer* (people and items, e.g. Erika's Invitation) · *Energy* (power cards).
"""

st.set_page_config(page_title="PokéPrice", page_icon="🃏", layout="wide")


@st.cache_data(ttl=300)
def q(sql: str, **params) -> pd.DataFrame:
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


def latest_run() -> dict:
    f = ROOT / "data" / "metrics" / "latest_run.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def grade_label(grade: str) -> str:
    return "Ungraded (raw)" if grade == "RAW" else grade


def grade_order(grade: str) -> tuple:
    """Ungraded first, then by grading company, best grade first."""
    if grade == "RAW":
        return (0, 0, 0)
    m = re.match(r"(\w+) ([\d.]+)", grade)
    company, score = (m.group(1), float(m.group(2))) if m else (grade, 0)
    return (1, COMPANIES.index(company) if company in COMPANIES else 9, -score)


def euro(v) -> str:
    return "—" if v is None or pd.isna(v) else f"€{v:,.2f}"


st.title("🃏 PokéPrice")
st.caption("What is a Pokémon card worth, and what should we pay for it? · Scarlet & Violet 151 · "
           "prices in EUR · refreshed by a daily pipeline")

tab_card, tab_diff, tab_ops = st.tabs(["🔎 Value a card", "⭐ What makes this different (whole set)",
                                      "🛠 Pipeline health (behind the scenes)"])

# ============================================================== value a card
with tab_card:
    with st.expander("🎓 New to Pokémon cards? Read this first"):
        st.markdown(GUIDE)
    cards = q("""
        select c.card_id, c.name, c.local_id, c.set_name, c.rarity, c.category, c.types, c.image_url,
               c.collector_number,
               min(qt.market_value) filter (where qt.grade = 'RAW')                              as raw_value,
               max(qt.market_value) filter (where qt.grade = 'PSA 10' and a.card_id is null)     as psa10_value,
               coalesce(bool_or(qt.basis = 'sales'), false)                                      as has_sales
        from cards c
        left join v_latest_quotes qt on qt.card_id = c.card_id
        left join v_open_anomalies a on a.card_id = qt.card_id and a.finish = qt.finish
             and a.grade = qt.grade and a.severity = 'error'
        where c.language = 'en'
        group by c.card_id""")
    if cards.empty:
        st.warning("No data yet — run `python pipeline.py` first.")
        st.stop()

    f1, f2, f3, f4 = st.columns([3, 2, 2, 2])
    search = f1.text_input("Search", placeholder="Name or number, e.g. charizard, pikachu, 199")
    kinds = f2.multiselect("Card type", list(CATEGORY), format_func=CATEGORY.get, placeholder="All types")
    elements = f3.multiselect("Element", sorted(cards.types.dropna().unique()), placeholder="All elements")
    rarities = f4.multiselect("Rarity", [r for r in RARITIES if r in set(cards.rarity)],
                              placeholder="All rarities")
    g1, g2 = st.columns([2, 6], vertical_alignment="bottom")
    sort = g1.selectbox("Sort by", ["Most valuable", "Card number", "Name A–Z"])
    only_sales = g2.toggle("Only cards with real eBay sales", help="Hide cards priced only from guides or models")

    found = cards
    if search.strip():
        term = search.strip().lower().lstrip("#")
        found = found[found.name.str.lower().str.contains(term, regex=False)
                      | (found.local_id.str.lstrip("0") == term.lstrip("0"))]
    if kinds:
        found = found[found.category.isin(kinds)]
    if elements:
        found = found[found.types.isin(elements)]
    if rarities:
        found = found[found.rarity.isin(rarities)]
    if only_sales:
        found = found[found.has_sales]
    found = {"Most valuable": found.sort_values("raw_value", ascending=False, na_position="last"),
             "Card number": found.sort_values("collector_number"),
             "Name A–Z": found.sort_values("name")}[sort].reset_index(drop=True)
    if found.empty:
        st.info("No card matches these filters. Try a shorter search or clear a filter.")
        st.stop()

    st.caption(f"**{len(found)}** of {len(cards)} cards · click a row to see its prices below")
    picked = st.dataframe(
        pd.DataFrame({"#": found.local_id, "Card": found.name,
                      "Type": found.category.map(lambda c: CATEGORY.get(c, c)),
                      "Element": found.types.fillna("—"), "Rarity": found.rarity,
                      "Ungraded from": found.raw_value.map(euro), "PSA 10": found.psa10_value.map(euro),
                      "Real sales": found.has_sales.map(lambda h: "✅" if h else "—")}),
        hide_index=True, width="stretch", height=280, on_select="rerun", selection_mode="single-row",
        # new filters = new table, so a row picked in an older, longer list is never reused
        key=f"card_table:{search}:{kinds}:{elements}:{rarities}:{sort}:{only_sales}")
    if picked.selection.rows and picked.selection.rows[0] < len(found):
        st.session_state.card_id = found.card_id[picked.selection.rows[0]]
    if st.session_state.get("card_id") not in set(found.card_id):
        st.session_state.card_id = found.card_id[0]
    card_id = st.session_state.card_id
    card = cards.set_index("card_id").loc[card_id]
    st.divider()

    ladder = q("""
        with e as (
            select finish, grade,
                   max(estimated_price) filter (where estimate_type = 'sales')      as sold,
                   max(n_observations)  filter (where estimate_type = 'sales')      as n_sales,
                   max(estimated_price) filter (where estimate_type = 'reference')  as guide,
                   max(estimated_price) filter (where estimate_type = 'comparable') as similar,
                   max(estimated_price) filter (where estimate_type = 'listing')    as asking,
                   bool_or(is_synthetic) filter (where estimate_type = 'listing')   as asking_demo
            from v_latest_estimates where card_id = :c group by finish, grade)
        select e.*, qt.market_value, qt.basis, qt.confidence, qt.decision, qt.offer, qt.reasons
        from e left join v_latest_quotes qt on qt.card_id = :c and qt.finish = e.finish and qt.grade = e.grade""",
               c=card_id)
    jp = q("""select jp.card_id, jp.name, jp.local_id, jp.rarity from card_equivalents e
              join cards jp on jp.card_id = e.card_id where e.equivalent_card_id = :c""", c=card_id)

    col_img, col_main = st.columns([1, 3], gap="large")
    with col_img:
        if card.image_url:
            st.image(f"{card.image_url}/high.webp", width="stretch")
    with col_main:
        st.subheader(card["name"])
        kind = CATEGORY.get(card.category, card.category or "")
        st.markdown(f"**#{card.local_id}** · {card.rarity} · {kind}"
                    + (f" · {card.types}" if isinstance(card.types, str) else "") + f" · set {card.set_name}")
        if jp.empty:
            st.caption("🇯🇵 No Japanese print linked to this card.")
        else:
            j = jp.iloc[0]
            st.caption(f"🇯🇵 Japanese print: **{j['name']}** #{j.local_id} · {j.rarity} (`{j.card_id}`). "
                       "A separate product; Japanese prices are not collected yet.")
        if ladder.empty:
            st.info("No prices for this card yet.")
            st.stop()

        finishes = sorted(ladder.finish.unique(), key=lambda f: -len(ladder[ladder.finish == f]))
        finish = (st.segmented_control("Version", finishes, default=finishes[0], format_func=lambda f: FINISH.get(f, f))
                  if len(finishes) > 1 else finishes[0]) or finishes[0]
        rows = ladder[ladder.finish == finish].copy()
        rows["order"] = rows.grade.map(grade_order)
        rows = rows.sort_values("order")

        by_grade = rows.set_index("grade")
        blocked = set(q("""select grade from v_open_anomalies
                           where card_id = :c and finish = :f and severity = 'error'""", c=card_id, f=finish).grade)
        t1, t2, t3, t4 = st.columns(4)
        for col, g in ((t1, "RAW"), (t2, "PSA 10"), (t3, "PSA 9")):
            mv = by_grade.market_value.get(g)
            col.metric(grade_label(g), euro(mv))
            if g in blocked:
                col.caption("🛑 Flagged as suspicious: not trusted, see below")
            elif mv is not None and not pd.isna(mv):
                col.caption(f"{BASIS.get(by_grade.basis[g], by_grade.basis[g])} · {by_grade.confidence[g]} confidence")
        raw, psa10 = by_grade.market_value.get("RAW"), by_grade.market_value.get("PSA 10")
        if raw and psa10 and not pd.isna(raw) and not pd.isna(psa10) and not blocked & {"RAW", "PSA 10"}:
            t4.metric("PSA 10 vs ungraded", f"{psa10 / raw:,.1f}×")
            t4.caption("How much a perfect grade multiplies the value")

    st.markdown(f"#### Every condition of **{FINISH.get(finish, finish)}** {card['name']}, priced separately")
    st.caption("Grades and versions are never mixed: each row is its own product with its own evidence.")
    demo = bool(rows.asking_demo.fillna(False).any())
    # Shown as text so empty cells read "—" instead of "None" for non-data readers.
    table = pd.DataFrame({
        "Condition": rows.grade.map(grade_label),
        "Market value": rows.market_value.map(euro),
        "Based on": rows.basis.map(lambda b: BASIS.get(b, b) if isinstance(b, str) else "—"),
        "Confidence": rows.confidence.fillna("—"),
        "We'd pay": rows.offer.map(euro),
        "Decision": rows.decision.map(lambda d: DECISION.get(d, "—") if isinstance(d, str) else "—"),
        "Real sales": rows.sold.map(euro),
        "# sales": rows.n_sales.map(lambda n: "—" if pd.isna(n) else f"{int(n):,}"),
        "Price guide": rows.guide.map(euro),
        "Similar cards": rows.similar.map(euro),
        "Asking (demo)" if demo else "Asking": rows.asking.map(euro),
    })
    st.dataframe(table, hide_index=True, width="stretch")
    with st.expander("What do these columns mean?"):
        st.markdown(
            "- **Real eBay sales**: what buyers actually paid, per grade (PokemonPriceTracker).\n"
            "- **Price guide**: Cardmarket and TCGplayer guide prices. They only cover ungraded cards.\n"
            "- **Similar cards (model)**: for versions with no sales of their own, estimated from the 8 most "
            "similar cards of the same rarity. Never used for an automatic offer.\n"
            "- **Asking**: what sellers ask on marketplaces. Asking is not selling."
            + (" **These listings are synthetic demo data** until eBay API access is approved." if demo else "")
            + "\n- **Market value** uses the best evidence available, in that order. **We'd pay** keeps a margin "
            "that grows as confidence falls; weak or suspicious evidence goes to a human instead.")

    chart_rows = rows.dropna(subset=["market_value"]).assign(
        condition=lambda d: d.grade.map(grade_label),
        based_on=lambda d: d.basis.map(lambda b: BASIS.get(b, b)))
    if len(chart_rows) > 1:
        st.markdown("**Market value by condition**")
        st.caption("Faded bars rest on thin evidence (low confidence): treat them as a rough guide.")
        bars = alt.Chart(chart_rows).mark_bar(color=SERIES_LISTING, cornerRadiusEnd=4, height=14).encode(
            x=alt.X("market_value:Q", title=None,
                    axis=alt.Axis(orient="top", tickCount=5, gridOpacity=0.35, domain=False,
                                  labelExpr="'€' + format(datum.value, ',.0f')")),
            y=alt.Y("condition:N", sort=list(chart_rows.condition), title=None,
                    axis=alt.Axis(grid=False, ticks=False, domain=False)),
            opacity=alt.condition(alt.datum.confidence == "low", alt.value(0.3), alt.value(1.0)),
            tooltip=[alt.Tooltip("condition:N", title="Condition"),
                     alt.Tooltip("market_value:Q", format=",.2f", title="Market value €"),
                     alt.Tooltip("based_on:N", title="Based on"),
                     alt.Tooltip("confidence:N", title="Confidence"),
                     alt.Tooltip("n_sales:Q", title="# sales")])
        st.altair_chart(bars.properties(height=26 * len(chart_rows) + 30), width="stretch")

    # ---- one product in detail
    st.divider()
    st.markdown("#### Look closer at one condition")
    grade = st.selectbox("Condition", list(rows.grade), format_func=grade_label, label_visibility="collapsed")
    r = by_grade.loc[grade]
    m1, m2 = st.columns(2)
    m1.metric("Market value (used for the decision)", euro(r.market_value))
    if isinstance(r.basis, str):
        m1.caption(f"From **{BASIS.get(r.basis, r.basis)}** · confidence **{r.confidence}**")
        m2.metric(DECISION.get(r.decision, "Decision"), euro(r.offer) if r.decision == "auto" else "—")
        m2.caption(" · ".join(r.reasons[1:]) if isinstance(r.reasons, list) else "")
    flags = q("""select anomaly_type, severity, observed_value, expected_value from v_open_anomalies
                 where card_id = :c and finish = :f and grade = :g""", c=card_id, f=finish, g=grade)
    for f in flags.itertuples():
        (st.error if f.severity == "error" else st.warning)(
            f"⚠ **{ANOMALY.get(f.anomaly_type, f.anomaly_type)}**: observed {euro(f.observed_value)}, "
            f"typical about {euro(f.expected_value)}")

    listings = q("""select l.listed_at, l.price_eur, l.title, l.source, l.is_synthetic, m.match_score
                    from card_matches m join listings l using (source, listing_id)
                    where m.card_id = :c and m.finish = :f and m.grade = :g and m.status = 'auto'
                      and not l.is_duplicate and l.price_eur > 0""", c=card_id, f=finish, g=grade)
    if not listings.empty:
        demo_note = " — ⚠ synthetic demo listings" if listings.is_synthetic.any() else ""
        st.markdown(f"**Marketplace listings matched to this exact version** (asking prices{demo_note})")
        dots = alt.Chart(listings).mark_circle(size=90, color=SERIES_LISTING, opacity=0.85,
                                               stroke="white", strokeWidth=2).encode(
            x=alt.X("listed_at:T", title=None),
            y=alt.Y("price_eur:Q", title="EUR", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("title:N", title="Listing"), alt.Tooltip("price_eur:Q", format=",.2f", title="€"),
                     alt.Tooltip("listed_at:T", title="Listed"), alt.Tooltip("match_score:Q", title="Match"),
                     alt.Tooltip("source:N")])
        layers = [dots]
        if not pd.isna(r.guide):
            ref = pd.DataFrame({"y": [float(r.guide)], "label": ["Price guide"]})
            layers.append(alt.Chart(ref).mark_rule(color=SERIES_REFERENCE, strokeWidth=2, strokeDash=[6, 4])
                          .encode(y="y:Q", tooltip=[alt.Tooltip("y:Q", format=",.2f", title="Price guide €")]))
            layers.append(alt.Chart(ref).mark_text(align="left", dx=4, dy=-8, color=SERIES_REFERENCE)
                          .encode(y="y:Q", x=alt.value(0), text="label:N"))
        st.altair_chart(alt.layer(*layers).properties(height=280), width="stretch")
        with st.expander("Listings table"):
            st.dataframe(listings.sort_values("listed_at", ascending=False), width="stretch")

    with st.expander("Raw price figures behind these numbers (every source, every day)"):
        st.dataframe(q("""select o.source, o.grade, o.price_type, o.price, o.currency, o.price_eur,
                                 o.sample_size as n_sales, o.observed_at
                          from price_observations o left join card_variants v on v.variant_id = o.variant_id
                          where o.card_id = :c and o.finish = :f and o.grade = :g
                            and coalesce(v.is_special, false) = false
                          order by o.observed_at desc, o.price_type""", c=card_id, f=finish, g=grade),
                     hide_index=True, width="stretch")

# ============================================================== what makes this different
with tab_diff:
    st.info("This tab covers **the whole set, not one card**, so it looks the same whichever card you picked. "
            "It shows what the pipeline does that a simple price lookup can't. "
            "Every number is computed live from the database and changes after each daily run.")
    counts = q("""select
        (select count(*) from cards where language = 'en') as en_cards,
        (select count(*) from cards where language = 'ja') as jp_cards,
        (select count(distinct card_id) from v_latest_estimates where estimate_type = 'sales') as sold_cards,
        (select coalesce(sum(n_observations), 0) from v_latest_estimates where estimate_type = 'sales') as n_sales,
        (select count(*) from v_latest_quotes) as products,
        (select count(*) filter (where passed) from dq_results
          where run_at = (select max(run_at) from dq_results)) as dq_passed,
        (select count(*) from dq_results where run_at = (select max(run_at) from dq_results)) as dq_total""").iloc[0]
    a, b, c, d = st.columns(4)
    a.metric("Cards tracked", f"{counts.en_cards} EN + {counts.jp_cards} JP")
    b.metric("Cards with real sold prices", f"{counts.sold_cards} / {counts.en_cards}")
    b.caption(f"{int(counts.n_sales):,} eBay sales behind them")
    c.metric("Versions priced", f"{counts.products:,}")
    c.caption("card × finish × grade, never mixed")
    d.metric("Data-quality checks passing", f"{counts.dq_passed} / {counts.dq_total}")

    st.subheader("1 · Suspicious prices are caught before anyone pays them")
    st.caption("Automatic detectors run on every pipeline run. A serious flag blocks the automatic offer "
               "for that product; a warning lowers its confidence.")
    anomalies = q("""select c.name || ' #' || c.local_id as card, a.finish, a.grade, a.anomaly_type, a.severity,
                            a.observed_value, a.expected_value
                     from v_open_anomalies a join cards c using (card_id)
                     order by a.severity, a.observed_value desc""")
    if anomalies.empty:
        st.success("No open flags today.")
    else:
        st.dataframe(pd.DataFrame({
            "Card": anomalies.card, "Version": anomalies.finish.map(lambda f: FINISH.get(f, f)),
            "Condition": anomalies.grade.map(grade_label),
            "Problem": anomalies.anomaly_type.map(lambda t: ANOMALY.get(t, t)),
            "Severity": anomalies.severity.map({"error": "🛑 blocks offer", "warn": "⚠ lowers confidence"}),
            "Observed €": anomalies.observed_value, "Typical €": anomalies.expected_value}),
            hide_index=True, width="stretch",
            column_config={k: st.column_config.NumberColumn(format="€%.2f") for k in ("Observed €", "Typical €")})

    st.subheader("2 · Grading doesn't always pay")
    st.caption("Real sold prices show some low-grade slabs selling for LESS than the ungraded card "
               "(a PSA 8 signals a flaw). This matters for buyback and for deciding what to send for grading.")
    below = q("""select c.name || ' #' || c.local_id as card, s.grade, s.estimated_price as graded,
                        r.estimated_price as raw, s.n_observations as n_sales
                 from v_latest_estimates s
                 join v_latest_estimates r on r.card_id = s.card_id and r.finish = s.finish
                  and r.grade = 'RAW' and r.estimate_type = 'reference'
                 join cards c on c.card_id = s.card_id
                 where s.estimate_type = 'sales' and s.estimated_price < r.estimated_price and s.n_observations >= 10
                 order by s.estimated_price / r.estimated_price limit 10""")
    if below.empty:
        st.info("No graded product currently sells below its ungraded price.")
    else:
        st.dataframe(below.rename(columns={"card": "Card", "grade": "Graded as", "graded": "Graded sells for €",
                                           "raw": "Ungraded worth €", "n_sales": "# sales"}),
                     hide_index=True, width="stretch",
                     column_config={k: st.column_config.NumberColumn(format="€%.2f")
                                    for k in ("Graded sells for €", "Ungraded worth €")})

    st.subheader("3 · Prices for versions nobody has sold yet, with measured accuracy")
    st.caption("A version with no sales is estimated from the 8 most similar cards of the same rarity. "
               "Accuracy is measured every run by hiding each known card and predicting it from the others. "
               "No estimate is made when there are too few similar cards.")
    bt = q("""select notes from pipeline_runs where job_name = 'comparable_estimates' and status = 'success'
              order by run_id desc limit 1""")
    by_grade_bt = (bt.notes.iloc[0] or {}).get("by_grade", {}) if not bt.empty else {}
    if by_grade_bt:
        bt_df = pd.DataFrame([{"Grade": g, "Cards learned from": v["n_train"],
                               "Typical error": v["mdape"], "Estimates made": v["n_estimates"],
                               "Skipped (no fair comparison)": v["skipped_too_few_comps"]
                               + v.get("skipped_outside_range", 0)}
                              for g, v in by_grade_bt.items()]).sort_values("Grade", key=lambda s: s.map(grade_order))
        st.dataframe(bt_df, hide_index=True, width="stretch",
                     column_config={"Typical error": st.column_config.NumberColumn(
                         format="percent", help="Median absolute error in the leave-one-card-out test")})
    else:
        st.info("Comparable estimates have not run yet.")

    st.subheader("4 · English and Japanese prints linked, never merged")
    jp_stats = q("""select (select count(*) from card_equivalents) as linked,
                           (select count(*) from cards where language = 'ja'
                              and card_id not in (select card_id from card_equivalents)) as exclusive""").iloc[0]
    st.caption(f"{jp_stats.linked} Japanese cards (scraped from Bulbapedia) are linked to their English print by "
               f"name and rarity; {jp_stats.exclusive} exist only in Japanese. They stay separate products, "
               "because language changes the price.")
    st.dataframe(q("""select en.name || ' #' || en.local_id as "English print", en.rarity as "Rarity",
                             jp.card_id as "Japanese print", jp.rarity as "Japanese rarity"
                      from card_equivalents e join cards jp on jp.card_id = e.card_id
                      join cards en on en.card_id = e.equivalent_card_id
                      order by en.collector_number desc limit 8"""), hide_index=True, width="stretch")

    st.subheader("5 · Messy listing titles matched to the exact card")
    ev = latest_run().get("stages", {}).get("match", {}).get("result", {}).get("evaluation", {})
    if ev:
        x, y, z = st.columns(3)
        x.metric("Precision*", f"{ev['precision_product']:.1%}")
        y.metric("Recall*", f"{ev['recall_product']:.1%}")
        z.metric("Sent to human review*", f"{ev['review_rate']:.1%}")
        st.caption("Titles like *“PSA 10 Charizard ex 199/165 SIR 151 🔥”* are parsed for card, number, finish, "
                   "grade and language, then matched. *Measured on a labelled SYNTHETIC benchmark, not on real "
                   "marketplace titles yet (eBay API access pending).")

# ============================================================== pipeline health
with tab_ops:
    st.info("The **engine room**, for the data team: did today's run work, are the automatic quality checks "
            "passing, and which listings does the matcher need a human to confirm. Also set-wide, not per card.")
    m = latest_run()
    if m:
        ev = m["stages"].get("match", {}).get("result", {}).get("evaluation", {})
        a, b, c, d = st.columns(4)
        a.metric("Last full run (s)", m.get("runtime_seconds"))
        if ev:
            b.metric("Match precision*", f"{ev['precision_product']:.1%}")
            c.metric("Match recall*", f"{ev['recall_product']:.1%}")
            d.metric("Sent to review*", f"{ev['review_rate']:.1%}")
            st.caption("*Measured on the labelled synthetic benchmark, not on real marketplace titles.")
    st.markdown("**Data-quality checks (latest)**")
    st.dataframe(q("""select check_name, severity, passed, failing_rows, run_at from dq_results
                      where run_at = (select max(run_at) from dq_results) order by check_name"""), width="stretch")
    st.markdown("**Recent pipeline runs**")
    st.dataframe(q("""select run_id, job_name, status, started_at, completed_at, rows_in, rows_out, error
                      from pipeline_runs order by run_id desc limit 30"""), width="stretch")
    st.markdown("**Review queue** (matcher unsure)")
    st.dataframe(q("""select l.title, m.card_id, m.match_score, m.match_method
                      from card_matches m join listings l using (source, listing_id)
                      where m.status = 'review' order by m.match_score desc limit 50"""),
                 width="stretch")
