"""PokéPrice dashboard. Kept minimal on purpose: the pipeline is the product.

Run:  streamlit run dashboard/app.py
"""
import json
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

st.set_page_config(page_title="PokéPrice", layout="wide")


@st.cache_data(ttl=300)
def q(sql: str, **params) -> pd.DataFrame:
    with engine().connect() as conn:
        return pd.read_sql(text(sql), conn, params=params)


tab_card, tab_ops = st.tabs(["Card prices", "Pipeline health"])

with tab_card:
    cards = q("select card_id, name, local_id, set_name, rarity, image_url from cards order by collector_number")
    if cards.empty:
        st.warning("No data yet — run `python pipeline.py` first.")
        st.stop()
    labels = {r.card_id: f"{r.name} · {r.local_id} · {r.rarity}" for r in cards.itertuples()}
    ids = list(labels)
    default = ids.index("sv03.5-199") if "sv03.5-199" in ids else 0
    card_id = st.selectbox("Card", ids, index=default, format_func=labels.get)
    card = cards.set_index("card_id").loc[card_id]

    est = q("""select finish, grade, estimate_type, estimated_price, price_low, price_high,
                      n_observations, confidence, is_synthetic, calculated_at
               from v_latest_estimates where card_id = :c""", c=card_id)
    products = (est[["finish", "grade"]].drop_duplicates()
                .assign(graded=lambda d: d.grade != "RAW").sort_values(["graded", "finish", "grade"]))
    col_img, col_main = st.columns([1, 3])
    with col_img:
        if card.image_url:
            st.image(f"{card.image_url}/high.webp", width="stretch")
        st.caption(f"{card.set_name} · {card_id}")
    with col_main:
        st.subheader(card["name"])
        if products.empty:
            st.info("No estimates for this card yet.")
            st.stop()
        options = [f"{p.finish} · {p.grade}" for p in products.itertuples()]
        choice = st.radio("Product (finish · grade) — never mixed", options, horizontal=True)
        finish, grade = [s.strip() for s in choice.split("·")]
        sel = est[(est.finish == finish) & (est.grade == grade)].set_index("estimate_type")

        c1, c2, c3 = st.columns(3)
        if "reference" in sel.index:
            r = sel.loc["reference"]
            c1.metric("Reference estimate (price guide)", f"€{r.estimated_price:,.2f}")
            c1.caption(f"Confidence: **{r.confidence}** · {r.n_observations} figures · "
                       f"range €{r.price_low:,.2f}–€{r.price_high:,.2f}")
        else:
            c1.metric("Reference estimate", "—")
            c1.caption("Price guides only cover raw (ungraded) cards.")
        if "listing" in sel.index:
            l = sel.loc["listing"]
            c2.metric("Active-listing estimate (asking)", f"€{l.estimated_price:,.2f}")
            c2.caption(f"Confidence: **{l.confidence}** · {l.n_observations} listings")
            if l.is_synthetic:
                c3.warning("**Demo data.** The listing estimate uses SYNTHETIC listings "
                           "(no eBay keys yet). The reference estimate is real Cardmarket data.")
        else:
            c2.metric("Active-listing estimate", "—")
        st.caption("Asking prices are not sold prices. Sold-price estimates need a sold-data source.")

    listings = q("""select l.listed_at, l.price_eur, l.title, l.source, l.is_synthetic, m.match_score
                    from card_matches m join listings l using (source, listing_id)
                    where m.card_id = :c and m.finish = :f and m.grade = :g and m.status = 'auto'
                      and not l.is_duplicate and l.price_eur > 0""", c=card_id, f=finish, g=grade)
    if not listings.empty:
        st.markdown(f"**Matched listings — {finish} · {grade}** (EUR, asking price)")
        dots = alt.Chart(listings).mark_circle(size=90, color=SERIES_LISTING, opacity=0.85,
                                               stroke="white", strokeWidth=2).encode(
            x=alt.X("listed_at:T", title=None),
            y=alt.Y("price_eur:Q", title="EUR", scale=alt.Scale(zero=False)),
            tooltip=[alt.Tooltip("title:N", title="Listing"), alt.Tooltip("price_eur:Q", format=",.2f", title="€"),
                     alt.Tooltip("listed_at:T", title="Listed"), alt.Tooltip("match_score:Q", title="Match"),
                     alt.Tooltip("source:N")])
        layers = [dots]
        if "reference" in sel.index:
            ref = pd.DataFrame({"y": [float(sel.loc["reference"].estimated_price)],
                                "label": ["Reference estimate"]})
            layers.append(alt.Chart(ref).mark_rule(color=SERIES_REFERENCE, strokeWidth=2, strokeDash=[6, 4])
                          .encode(y="y:Q", tooltip=[alt.Tooltip("y:Q", format=",.2f", title="Reference €")]))
            layers.append(alt.Chart(ref).mark_text(align="left", dx=4, dy=-8, color=SERIES_REFERENCE)
                          .encode(y="y:Q", x=alt.value(0), text="label:N"))
        st.altair_chart(alt.layer(*layers).properties(height=320), width="stretch")
        with st.expander("Listings table"):
            st.dataframe(listings.sort_values("listed_at", ascending=False), width="stretch")

    with st.expander("Reference price figures behind the estimate"):
        st.dataframe(q("""select o.source, o.price_type, o.price, o.currency, o.price_eur, o.observed_at
                          from price_observations o left join card_variants v on v.variant_id = o.variant_id
                          where o.card_id = :c and o.finish = :f and coalesce(v.is_special, false) = false
                          order by o.observed_at desc, o.price_type""", c=card_id, f=finish),
                     width="stretch")

with tab_ops:
    metrics_file = ROOT / "data" / "metrics" / "latest_run.json"
    if metrics_file.exists():
        m = json.loads(metrics_file.read_text(encoding="utf-8"))
        ev = m["stages"].get("match", {}).get("result", {}).get("evaluation", {})
        a, b, c, d = st.columns(4)
        a.metric("Last run (s)", m.get("runtime_seconds"))
        if ev:
            b.metric("Match precision*", f"{ev['precision_product']:.1%}")
            c.metric("Match recall*", f"{ev['recall_product']:.1%}")
            d.metric("Sent to review*", f"{ev['review_rate']:.1%}")
            st.caption("*Measured on the labelled synthetic benchmark, not on real marketplace titles.")
    st.markdown("**Data-quality checks (latest)**")
    st.dataframe(q("""select distinct on (check_name) check_name, severity, passed, failing_rows, run_at
                      from dq_results order by check_name, run_at desc"""), width="stretch")
    st.markdown("**Recent pipeline runs**")
    st.dataframe(q("""select run_id, job_name, status, started_at, completed_at, rows_in, rows_out, error
                      from pipeline_runs order by run_id desc limit 30"""), width="stretch")
    st.markdown("**Review queue** (matcher unsure)")
    st.dataframe(q("""select l.title, m.card_id, m.match_score, m.match_method
                      from card_matches m join listings l using (source, listing_id)
                      where m.status = 'review' order by m.match_score desc limit 50"""),
                 width="stretch")
