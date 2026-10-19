-- PokéPrice schema. Idempotent: safe to run on every pipeline start.
-- Layers: raw (raw_ingestions) -> clean (cards, card_variants, listings,
-- price_observations) -> derived (card_matches, price_estimates) + ops tables.

-- ---------- ops ----------
create table if not exists pipeline_runs (
    run_id        bigserial primary key,
    job_name      text        not null,
    status        text        not null check (status in ('running', 'success', 'failed', 'skipped')),
    started_at    timestamptz not null default now(),
    completed_at  timestamptz,
    rows_in       integer     default 0,
    rows_out      integer     default 0,
    notes         jsonb,
    error         text
);
create index if not exists ix_pipeline_runs_job on pipeline_runs (job_name, started_at desc);

create table if not exists dq_results (
    check_id      bigserial primary key,
    run_at        timestamptz not null default now(),
    check_name    text        not null,
    table_name    text        not null,
    severity      text        not null check (severity in ('error', 'warn')),
    passed        boolean     not null,
    failing_rows  integer     not null,
    details       jsonb
);
create index if not exists ix_dq_results_run on dq_results (run_at desc);

-- ---------- raw ----------
create table if not exists raw_ingestions (
    ingestion_id  bigserial primary key,
    source        text        not null,
    endpoint      text        not null,
    fetched_at    timestamptz not null default now(),
    payload_hash  text        not null,
    payload       jsonb       not null,
    unique (source, endpoint, payload_hash)   -- identical payloads are stored once
);
create index if not exists ix_raw_source_time on raw_ingestions (source, fetched_at desc);

-- ---------- catalogue ----------
create table if not exists cards (
    card_id           text primary key,          -- TCGdex id, e.g. sv03.5-006
    set_id            text not null,
    set_name          text not null,
    set_official_count integer,
    name              text not null,
    local_id          text not null,             -- printed number as text, e.g. '006'
    collector_number  integer,                   -- numeric part, e.g. 6
    rarity            text,
    language          text not null default 'en',
    image_url         text,
    source_updated_at timestamptz,
    ingested_at       timestamptz not null default now()
);
create index if not exists ix_cards_set_number on cards (set_id, collector_number);

-- One row per printed version of a card. "finish" is what matching and pricing key on;
-- special versions (stamped, cosmos foil, ...) are kept but flagged.
create table if not exists card_variants (
    variant_id     text primary key,             -- '<card_id>:<TCGdex variantId>'
    card_id        text not null references cards (card_id),
    finish         text not null,                -- normal | reverse | holo | firstEdition
    is_special     boolean not null default false,
    special_label  text,                         -- e.g. 'stamp:set-logo', 'foil:cosmos'
    cardmarket_id  integer,
    tcgplayer_id   integer
);
create index if not exists ix_variants_card on card_variants (card_id, finish);

-- ---------- reference prices ----------
-- Product key = (card_id, finish, grade). Reference prices are for raw (ungraded) cards.
create table if not exists price_observations (
    obs_id       bigserial primary key,
    card_id      text not null references cards (card_id),
    finish       text not null,
    grade        text not null default 'RAW',
    variant_id   text not null default '',       -- '' when the source has no variant id
    source       text not null,                  -- e.g. cardmarket_via_tcgdex
    price_type   text not null,                  -- trend | avg | avg1 | avg7 | avg30 | low | market ...
    price        numeric(12, 2) not null check (price > 0),
    currency     text not null,
    price_eur    numeric(12, 2) not null,
    observed_at  timestamptz not null,           -- when the source computed the price
    ingested_at  timestamptz not null default now(),
    unique (source, card_id, finish, grade, variant_id, price_type, observed_at)
);
create index if not exists ix_obs_product on price_observations (card_id, finish, grade, observed_at desc);

-- ---------- marketplace listings ----------
create table if not exists listings (
    listing_pk        bigserial primary key,
    source            text not null,             -- ebay | synthetic
    listing_id        text not null,
    title             text not null,
    price             numeric(12, 2),
    currency          text,
    price_eur         numeric(12, 2),
    shipping          numeric(12, 2),
    seller            text,
    condition_raw     text,
    url               text,
    listed_at         timestamptz,
    fetched_at        timestamptz not null default now(),
    is_synthetic      boolean not null default false,
    -- filled by transformation.normalize
    title_clean       text,
    parsed_number     integer,
    parsed_set_total  integer,
    parsed_grade      text,                      -- 'RAW' or e.g. 'PSA 10'
    parsed_finish     text,
    parsed_language   text,
    flags             text[],                    -- lot, proxy, japanese, ...
    is_excluded       boolean not null default false,
    is_duplicate      boolean not null default false,
    duplicate_of      bigint,
    normalized_at     timestamptz,
    unique (source, listing_id)
);
create index if not exists ix_listings_number on listings (parsed_number);

-- Ground truth for synthetic listings, used only to measure matching precision/recall.
create table if not exists listing_labels (
    source        text not null,
    listing_id    text not null,
    true_card_id  text not null,
    true_finish   text not null,
    true_grade    text not null,
    noise_kind    text,
    primary key (source, listing_id)
);

create table if not exists card_matches (
    source        text not null,
    listing_id    text not null,
    card_id       text references cards (card_id),
    finish        text,
    grade         text,
    match_score   numeric(5, 2) not null,
    match_method  text not null,
    status        text not null check (status in ('auto', 'review', 'rejected')),
    matched_at    timestamptz not null default now(),
    primary key (source, listing_id)
);
create index if not exists ix_matches_product on card_matches (card_id, finish, grade);

create table if not exists fx_rates (
    rate_date  date    not null,
    base       text    not null,
    quote      text    not null,
    rate       numeric(14, 6) not null check (rate > 0),
    primary key (rate_date, base, quote)
);

-- ---------- estimates ----------
create table if not exists price_estimates (
    estimate_id      bigserial primary key,
    card_id          text not null references cards (card_id),
    finish           text not null,
    grade            text not null,
    estimate_type    text not null check (estimate_type in ('reference', 'listing', 'sales')),
    estimated_price  numeric(12, 2) not null,
    currency         text not null,
    price_low        numeric(12, 2),
    price_high       numeric(12, 2),
    n_observations   integer not null,
    confidence       text not null check (confidence in ('high', 'medium', 'low')),
    method           text not null,
    inputs           jsonb,
    is_synthetic     boolean not null default false,
    calculated_at    timestamptz not null default now()
);
create index if not exists ix_estimates_product on price_estimates (card_id, finish, grade, estimate_type, calculated_at desc);

create or replace view v_latest_estimates as
select distinct on (card_id, finish, grade, estimate_type) *
from price_estimates
order by card_id, finish, grade, estimate_type, calculated_at desc;
