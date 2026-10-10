-- Steam Radar schema. Safe to re-run: tables use IF NOT EXISTS, views and
-- functions use CREATE OR REPLACE.
-- Paste into the Supabase SQL editor (or psql) once, and again after updates.

-- ─────────────────────────────────────────────────────────────── tables ──

create table if not exists apps (
  appid             bigint primary key,
  name              text,
  type              text default 'game',        -- game | demo | dlc | ...
  parent_appid      bigint,                      -- demos: the full game
  coming_soon       boolean,
  release_date      date,
  release_date_text text,                        -- raw text ("Q1 2027", "Coming soon")
  is_free           boolean default false,
  price_cents       int,                         -- USD list price (store cc=us)
  developers        text[] default '{}',
  publishers        text[] default '{}',
  genres            text[] default '{}',
  tags              text[] default '{}',         -- SteamSpy user tags, most-voted first
  header_image      text,
  watch             boolean default true,        -- daily tier
  sources           text[] default '{}',         -- discovery feeds that surfaced it
  first_seen        date default (now() at time zone 'utc')::date,
  last_seen         date default (now() at time zone 'utc')::date,  -- last day a feed listed it
  details_at        timestamptz,
  tags_at           timestamptz,
  news_at           timestamptz,
  reviews_at        timestamptz,
  skip_reason       text                         -- set when Steam returns nothing
);
create index if not exists apps_watch_idx on apps (watch) where watch;
create index if not exists apps_tags_gin on apps using gin (tags);
create index if not exists apps_parent_idx on apps (parent_appid);

create table if not exists daily_snapshots (
  appid            bigint not null references apps on delete cascade,
  day              date   not null,
  followers        int,
  reviews_total    int,
  reviews_positive int,
  ccu_peak         int,       -- highest sample that day
  wishlist_rank    int,       -- position in the popular-wishlist search
  price_cents      int,
  discount_pct     int,
  primary key (appid, day)
);
create index if not exists snapshots_day_idx on daily_snapshots (day);

-- Last known store-page state, used to detect changes.
create table if not exists page_state (
  appid      bigint primary key references apps on delete cascade,
  state      jsonb not null,
  updated_at timestamptz default now()
);

-- Store page change log (4a).
create table if not exists page_changes (
  id        bigserial primary key,
  appid     bigint not null references apps on delete cascade,
  day       date   not null,
  field     text   not null,   -- name | capsule | short_description | price | discount |
                               -- release_date | coming_soon | trailers | screenshots | genres | tags
  old_value text,
  new_value text
);
create index if not exists page_changes_app_idx on page_changes (appid, day);
create index if not exists page_changes_day_idx on page_changes (day);

-- Event overlay (4b). appid null = global (Steam sales, fests, showcases).
create table if not exists events (
  id        bigserial primary key,
  appid     bigint references apps on delete cascade,
  start_day date not null,
  end_day   date,
  kind      text not null,     -- steam_event | showcase | news | custom
  title     text not null,
  url       text,
  ext_id    text not null      -- dedupe key (news gid, or slug for global events)
);
create unique index if not exists events_dedupe on events (coalesce(appid, 0), kind, ext_id);
create index if not exists events_app_idx on events (appid, start_day);

-- ───────────────────────────────────────────────────────────── estimates ──
-- Edit the multipliers here; every view uses these two functions.

create or replace function est_units(reviews int) returns bigint
language sql immutable as $$
  select (coalesce(reviews, 0) * 30)::bigint     -- reviews × 30 ("Boxleiter", typical 20–60)
$$;

create or replace function est_net_usd(reviews int, price_cents int) returns numeric
language sql immutable as $$
  -- units × list price × 0.5 (Steam cut, discounts, regional pricing, refunds, tax)
  select round(est_units(reviews) * coalesce(price_cents, 0) / 100.0 * 0.5)
$$;

create or replace function est_wishlists(followers int) returns bigint
language sql immutable as $$
  select (coalesce(followers, 0) * 12)::bigint   -- followers × 12 (typical 7–20)
$$;

-- ─────────────────────────────────────────────────────────────── views ──
-- Views hold no data, so they are dropped and rebuilt on every run.
drop view if exists v_quiet_earners, v_launches, v_demo_heat, v_breakouts,
                    v_tags, v_app_metrics cascade;

-- One row per app: the latest value of each metric, plus the same metric
-- ~7 and ~30 days before its own latest reading. Each metric is looked up on
-- its own because different jobs fill different columns at different times
-- (player counts every few hours, followers daily, the quiet-earner sweep weekly).
create or replace view v_app_metrics with (security_invoker = true) as
select
  a.appid, a.name, a.type, a.parent_appid, a.coming_soon, a.release_date,
  a.release_date_text, a.is_free, a.price_cents, a.developers, a.publishers,
  a.genres, a.tags, a.header_image, a.watch, a.first_seen,
  greatest(f.day, r.day) as snap_day,
  f.followers,
  r.reviews_total,
  r.reviews_positive,
  case when r.reviews_total > 0
       then round(r.reviews_positive::numeric / r.reviews_total, 3) end as pct_positive,
  f.wishlist_rank,
  f7.followers         as followers_7d,
  f7.wishlist_rank     as wishlist_rank_7d,
  r7.reviews_total     as reviews_7d,
  r30.reviews_total    as reviews_30d,
  ccu.peak_7d,
  ccu.peak_prev_7d,
  ccu.peak_all,
  est_wishlists(f.followers)   as est_wishlists,
  est_units(r.reviews_total)   as est_units,
  case when not a.is_free then est_net_usd(r.reviews_total, a.price_cents) end as est_net_usd
from apps a
left join lateral (
  select s.day, s.followers, s.wishlist_rank from daily_snapshots s
  where s.appid = a.appid and s.followers is not null
  order by s.day desc limit 1
) f on true
left join lateral (
  select s.followers, s.wishlist_rank from daily_snapshots s
  where s.appid = a.appid and s.followers is not null
    and s.day between f.day - 10 and f.day - 6
  order by s.day desc limit 1
) f7 on true
left join lateral (
  select s.day, s.reviews_total, s.reviews_positive from daily_snapshots s
  where s.appid = a.appid and s.reviews_total is not null
  order by s.day desc limit 1
) r on true
left join lateral (
  select s.reviews_total from daily_snapshots s
  where s.appid = a.appid and s.reviews_total is not null
    and s.day between r.day - 10 and r.day - 6
  order by s.day desc limit 1
) r7 on true
left join lateral (
  select s.reviews_total from daily_snapshots s
  where s.appid = a.appid and s.reviews_total is not null
    and s.day between r.day - 45 and r.day - 28
  order by s.day desc limit 1
) r30 on true
left join lateral (
  select
    max(s.ccu_peak) filter (where s.day > current_date - 7)                                as peak_7d,
    max(s.ccu_peak) filter (where s.day <= current_date - 7 and s.day > current_date - 14) as peak_prev_7d,
    max(s.ccu_peak)                                                                         as peak_all
  from daily_snapshots s where s.appid = a.appid
) ccu on true;

-- Pre-release breakouts: 7-day follower growth vs games of similar size.
create or replace view v_breakouts with (security_invoker = true) as
with g as (
  select m.*,
    m.followers - m.followers_7d                                 as followers_gain,
    ln((m.followers + 50.0) / (m.followers_7d + 50.0))           as growth_log,
    least(floor(log(greatest(m.followers_7d, 1)::float8)), 5)::int as size_bucket,
    m.wishlist_rank_7d - m.wishlist_rank                         as rank_climb,
    (m.wishlist_rank is not null and m.wishlist_rank_7d is null) as rank_new_entry
  from v_app_metrics m
  where coalesce(m.coming_soon, false) and m.type = 'game'
    and m.followers is not null and m.followers_7d is not null
)
select g.*,
  round(((g.growth_log - avg(g.growth_log) over b)
         / nullif(stddev_samp(g.growth_log) over b, 0))::numeric, 2) as heat,
  count(*) over b as bucket_size
from g
window b as (partition by g.size_bucket);

-- Demo heat: peak players this week vs last week.
create or replace view v_demo_heat with (security_invoker = true) as
select
  d.appid, d.name, d.parent_appid,
  p.name as parent_name, p.tags, p.header_image as parent_header_image,
  p.coming_soon as parent_coming_soon,
  (select s.followers from daily_snapshots s
    where s.appid = d.parent_appid and s.followers is not null
    order by s.day desc limit 1) as parent_followers,
  d.peak_7d, d.peak_prev_7d, d.peak_all,
  case when d.peak_prev_7d > 0
       then round(d.peak_7d::numeric / d.peak_prev_7d, 2) end as week_ratio
from v_app_metrics d
left join apps p on p.appid = d.parent_appid
where d.type = 'demo' and d.peak_7d is not null;

-- Launch scorecard: games released in the last 90 days.
create or replace view v_launches with (security_invoker = true) as
with l as (
  select m.*,
    (current_date - m.release_date) as days_live,
    (select s.followers from daily_snapshots s
      where s.appid = m.appid and s.day < m.release_date and s.followers is not null
      order by s.day desc limit 1) as followers_prelaunch,
    (select max(s.ccu_peak) from daily_snapshots s
      where s.appid = m.appid and s.day >= m.release_date) as peak_since_launch
  from v_app_metrics m
  where m.type = 'game' and not coalesce(m.coming_soon, false)
    and m.release_date between current_date - 90 and current_date
),
r as (
  select l.*,
    case when l.days_live <= 7 then 'week 1' when l.days_live <= 30 then 'month 1' else 'month 2–3' end as age_bucket,
    case when l.followers_prelaunch > 0
         then l.reviews_total::numeric / l.followers_prelaunch end as reviews_per_follower
  from l
),
med as (
  select age_bucket,
         percentile_cont(0.5) within group (order by reviews_per_follower) as median_rpf
  from r where reviews_per_follower is not null
  group by age_bucket
)
select r.*,
  round((r.reviews_per_follower / nullif(med.median_rpf, 0))::numeric, 2) as vs_peers,
  case
    when r.reviews_per_follower is null then 'no pre-launch data'
    when r.reviews_per_follower >= 2   * med.median_rpf then 'beat'
    when r.reviews_per_follower <= 0.5 * med.median_rpf then 'miss'
    else 'on track'
  end as verdict
from r
left join med on med.age_bucket = r.age_bucket;

-- Quiet earners: 1+ year old, 1k+ reviews, 80%+ positive, still selling.
create or replace view v_quiet_earners with (security_invoker = true) as
select m.*,
  m.reviews_total - m.reviews_30d as reviews_last_30d,
  case when m.reviews_30d is not null
       then est_net_usd(m.reviews_total - m.reviews_30d, m.price_cents) end as est_net_last_30d
from v_app_metrics m
where m.type = 'game' and not coalesce(m.coming_soon, false) and not coalesce(m.is_free, false)
  and m.release_date <= current_date - 365
  and m.reviews_total >= 1000
  and m.pct_positive >= 0.80;

-- Tag list with counts, for pickers.
create or replace view v_tags with (security_invoker = true) as
select t as tag, count(*) as games
from apps, unnest(tags) as t
where type = 'game'
group by t
order by games desc;

-- ─────────────────────────────────────────────────── comparable finder ──
-- Scores released games by weighted tag overlap (earlier tags weigh more on
-- both sides) plus price closeness, and returns their outcomes.

create or replace function find_comparables(
  p_tags           text[],
  p_price_cents    int     default null,
  p_released_after date    default null,
  p_exclude        bigint  default null,
  p_limit          int     default 25
)
returns table (
  appid bigint, name text, release_date date, price_cents int, tags text[],
  shared_tags text[], score numeric, reviews_total int, pct_positive numeric,
  est_units bigint, est_net_usd numeric, peak_ccu int, followers int,
  header_image text, developers text[]
)
language sql stable security invoker as $$
  with q as (
    select t, i from unnest(p_tags) with ordinality as u(t, i)
  ),
  qn as (
    select sum(1.0 / (1 + 0.15 * (i - 1))) as norm, count(*) as n from q
  ),
  c as (
    select a.*,
      (select coalesce(sum((1.0 / (1 + 0.15 * (q.i - 1))) * (1.0 / (1 + 0.15 * (ct.ci - 1)))), 0)
         from unnest(a.tags[1:15]) with ordinality as ct(t, ci)
         join q on q.t = ct.t) as tag_raw,
      array(select ct.t from unnest(a.tags) with ordinality as ct(t, ci)
             where ct.t = any(p_tags) order by ct.ci) as shared
    from apps a
    where a.type = 'game' and not coalesce(a.coming_soon, false)
      and a.tags && p_tags
      and a.appid is distinct from p_exclude
      and (p_released_after is null or a.release_date >= p_released_after)
  ),
  s as (
    select c.*,
      case when p_price_cents is null then c.tag_raw / qn.norm
           else 0.75 * c.tag_raw / qn.norm
              + 0.25 * exp(-abs(ln((coalesce(c.price_cents, 0) + 100.0) / (p_price_cents + 100.0))))
      end as score
    from c, qn
    where cardinality(c.shared) >= least(2, qn.n)
  )
  select s.appid, s.name, s.release_date, s.price_cents, s.tags, s.shared,
    round(s.score::numeric, 3),
    snap.reviews_total,
    case when snap.reviews_total > 0
         then round(snap.reviews_positive::numeric / snap.reviews_total, 3) end,
    est_units(snap.reviews_total),
    case when not s.is_free then est_net_usd(snap.reviews_total, s.price_cents) end,
    (select max(d.ccu_peak) from daily_snapshots d where d.appid = s.appid),
    snap.followers,
    s.header_image, s.developers
  from s
  left join lateral (
    select d.reviews_total, d.reviews_positive, d.followers
    from daily_snapshots d
    where d.appid = s.appid and d.reviews_total is not null
    order by d.day desc limit 1
  ) snap on true
  order by s.score desc, snap.reviews_total desc nulls last
  limit p_limit
$$;

-- Same, seeded from an existing game's top tags and price.
drop function if exists comparables_for(bigint, int);
create or replace function comparables_for(p_appid bigint, p_limit int default 25)
returns table (
  appid bigint, name text, release_date date, price_cents int, tags text[],
  shared_tags text[], score numeric, reviews_total int, pct_positive numeric,
  est_units bigint, est_net_usd numeric, peak_ccu int, followers int,
  header_image text, developers text[]
)
language sql stable security invoker as $$
  select f.*
  from apps a,
       lateral find_comparables(a.tags[1:8], a.price_cents, null, a.appid, p_limit) f
  where a.appid = p_appid
$$;

-- ─────────────────────────────────────────────── read-only public access ──
-- The dashboard uses Supabase's anon key: it may read, never write.
-- The collector connects as the database owner, which bypasses RLS.

alter table apps            enable row level security;
alter table daily_snapshots enable row level security;
alter table page_state      enable row level security;
alter table page_changes    enable row level security;
alter table events          enable row level security;

do $$
declare t text;
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    foreach t in array array['apps','daily_snapshots','page_changes','events'] loop
      execute format('drop policy if exists "public read" on %I', t);
      execute format('create policy "public read" on %I for select to anon, authenticated using (true)', t);
      execute format('grant select on %I to anon, authenticated', t);
    end loop;
    grant select on v_app_metrics, v_breakouts, v_demo_heat, v_launches,
                    v_quiet_earners, v_tags to anon, authenticated;
    grant execute on function find_comparables(text[], int, date, bigint, int),
                              comparables_for(bigint, int),
                              est_units(int), est_net_usd(int, int), est_wishlists(int)
      to anon, authenticated;
  end if;
end $$;
