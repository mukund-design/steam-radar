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

-- Added in the surges update. ALTER … IF NOT EXISTS keeps older databases working.
alter table apps   add column if not exists early_access boolean;
alter table apps   add column if not exists dlc_count    int;
alter table apps   add column if not exists hist_at      timestamptz;
alter table events add column if not exists event_type   int;    -- Steam's event type (12 patch, 14 major update, …)
alter table events add column if not exists category     text;   -- release | major_update | update | patch | dlc |
                                                                  -- sale | free_weekend | news | crosspost | …
update events set category = 'news' where kind = 'news' and category is null;

-- Steam's own review history per game, replaced on every fetch:
-- rollups = [[day, up, down], …] per month (or week, for younger games) since launch,
-- recent  = the same per day for the last 30 days.
create table if not exists review_history (
  appid       bigint primary key references apps on delete cascade,
  rollup_type text,
  rollups     jsonb not null default '[]',
  recent      jsonb not null default '[]',
  fetched_at  timestamptz default now()
);

-- Every surge seen, one row per game per week, for the "what causes surges" breakdown.
create table if not exists surge_log (
  appid        bigint not null references apps on delete cascade,
  week         date   not null,
  end_day      date   not null,
  cause        text   not null,
  ratio        numeric,
  week_reviews int,
  primary key (appid, week)
);
create index if not exists surge_log_week_idx on surge_log (week);

-- ───────────────────────────────────────────────────────────── estimates ──
-- Functions that call other functions name them as public.xxx: Postgres 17
-- (Supabase) refreshes cached views with an empty search path.
-- Edit the multipliers here; every view uses these two functions.

create or replace function est_units(reviews int) returns bigint
language sql immutable
set search_path = public, pg_temp as $$
  select (coalesce(reviews, 0) * 30)::bigint     -- reviews × 30 ("Boxleiter", typical 20–60)
$$;

create or replace function est_net_usd(reviews int, price_cents int) returns numeric
language sql immutable
set search_path = public, pg_temp as $$
  -- units × list price × 0.5 (Steam cut, discounts, regional pricing, refunds, tax)
  select round(public.est_units(reviews) * coalesce(price_cents, 0) / 100.0 * 0.5)
$$;

create or replace function est_wishlists(followers int) returns bigint
language sql immutable
set search_path = public, pg_temp as $$
  select (coalesce(followers, 0) * 12)::bigint   -- followers × 12 (typical 7–20)
$$;


-- Generic Steam tags that say nothing about genre. Edit to taste.
create or replace function genre_ignored_tags() returns text[]
language sql immutable
set search_path = public, pg_temp as $$
  select array['Indie', 'Singleplayer', 'Early Access', 'Free to Play', 'Great Soundtrack',
               'Moddable', 'Controller', 'Full controller support', 'Steam Workshop',
               'Steam Trading Cards', 'Steam Achievements', 'Partial Controller Support']
$$;

-- A game's genres: its five most-voted tags, skipping the generic ones above.
create or replace function genre_tags(p_tags text[]) returns text[]
language sql immutable
set search_path = public, pg_temp as $$
  select coalesce(array(select t from unnest(p_tags) with ordinality as u(t, i)
                        where t <> all(public.genre_ignored_tags()) order by i limit 5), '{}')
$$;

-- ─────────────────────────────────────────────────────────────── views ──
-- Views and functions hold no data, so they are dropped and rebuilt on every run.
drop function if exists overview(), genre_detail(text), comparables_for(bigint, int),
                        find_comparables(text[], int, date, bigint, int), refresh_metrics();
drop view if exists v_surge_causes, v_surges, v_genres, v_quiet_earners, v_launches, v_demo_heat,
                    v_breakouts, v_tags, v_app_metrics cascade;
drop materialized view if exists mv_surges cascade;
drop materialized view if exists mv_app_metrics cascade;

-- One row per app: the latest value of each metric, plus the same metric
-- ~7 and ~30 days before its own latest reading. Each metric is looked up on
-- its own because different jobs fill different columns at different times
-- (player counts every few hours, followers daily, the quiet-earner sweep weekly).
--
-- This is a cached table: the collector refreshes it at the end of every run
-- (select refresh_metrics()). Computing it live takes seconds once a year of
-- history has built up, which is longer than Supabase allows public queries.
create materialized view mv_app_metrics as
select
  a.appid, a.name, a.type, a.parent_appid, a.coming_soon, a.release_date,
  a.release_date_text, a.is_free, a.price_cents, a.developers, a.publishers,
  a.genres, a.tags, a.header_image, a.watch, a.first_seen, a.early_access, a.dlc_count,
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
  case when not a.is_free then est_net_usd(r.reviews_total, a.price_cents) end as est_net_usd,
  now() as refreshed_at
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

create unique index mv_app_metrics_appid on mv_app_metrics (appid);
create index mv_app_metrics_kind on mv_app_metrics (type, coming_soon);
create index mv_app_metrics_tags on mv_app_metrics using gin (tags);


-- The public name the dashboard reads.
create or replace view v_app_metrics with (security_invoker = true) as
select * from mv_app_metrics;

-- Pre-release breakouts: 7-day follower growth vs games of similar size.
create or replace view v_breakouts with (security_invoker = true) as
with g as (
  select m.*,
    m.followers - m.followers_7d                                 as followers_gain,
    ln((m.followers + 50.0) / (m.followers_7d + 50.0))           as growth_log,
    least(floor(log(greatest(m.followers_7d, 1)::float8)), 5)::int as size_bucket,
    m.wishlist_rank_7d - m.wishlist_rank                         as rank_climb,
    (m.wishlist_rank is not null and m.wishlist_rank_7d is null) as rank_new_entry
  from mv_app_metrics m
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
  p.coming_soon as parent_coming_soon, p.developers,
  p.followers as parent_followers,
  d.peak_7d, d.peak_prev_7d, d.peak_all,
  case when d.peak_prev_7d > 0
       then round(d.peak_7d::numeric / d.peak_prev_7d, 2) end as week_ratio
from mv_app_metrics d
left join mv_app_metrics p on p.appid = d.parent_appid
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
  from mv_app_metrics m
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
  m.reviews_total - m.reviews_7d  as reviews_last_7d,
  case when m.reviews_30d is not null
       then est_net_usd(m.reviews_total - m.reviews_30d, m.price_cents) end as est_net_last_30d
from mv_app_metrics m
where m.type = 'game' and not coalesce(m.coming_soon, false) and not coalesce(m.is_free, false)
  and m.release_date <= current_date - 365
  and m.reviews_total >= 1000
  and m.pct_positive >= 0.80;

-- ────────────────────────────────────────────────────────────── surges ──
-- Released games suddenly picking up reviews (a sales proxy) or players.
--   reviews: this week's reviews ≥ 2× the weekly average of the 3 weeks before, and ≥ 15
--            (from Steam's daily review counts, so it works from the first fetch)
--   players: peak players this week ≥ 1.5× last week's, and ≥ 50 (games watched daily)
-- Games launched in the last 30 days are left out: they belong to the launch scorecard.
--
-- Likely cause: what happened on Steam from 10 days before the surge week to its end,
-- most specific first. It shows timing, not proof.
create or replace function surge_cause_rank(cause text) returns int
language sql immutable
set search_path = public, pg_temp as $$
  select case cause
    when 'release' then 1 when 'dlc' then 2 when 'major_update' then 3 when 'free_weekend' then 4
    when 'sale' then 5 when 'steam_sale' then 6 when 'update' then 7 when 'page' then 8
    when 'fest' then 9 when 'patch' then 10 else 99 end
$$;

create materialized view mv_surges as
with
rh as (
  select h.appid, (x->>0)::date as day, (x->>1)::int as up, (x->>2)::int as down
  from review_history h, jsonb_array_elements(h.recent) as x
),
ends as (
  select appid, max(day) as end_day,
         least(21, max(day) - 7 - min(day) + 1) as prev_span   -- calendar days of history before this week
  from rh group by appid
),
w as (
  select rh.appid, e.end_day,
    sum(up + down) filter (where rh.day >  e.end_day - 7)                            as week_reviews,
    sum(up)        filter (where rh.day >  e.end_day - 7)                            as week_up,
    sum(up + down) filter (where rh.day <= e.end_day - 7 and rh.day > e.end_day - 28) as prev_reviews,
    sum(up)        filter (where rh.day <= e.end_day - 7 and rh.day > e.end_day - 28) as prev_up,
    e.prev_span as prev_days
  from rh join ends e on e.appid = rh.appid
  group by rh.appid, e.end_day, e.prev_span
),
base as (
  select m.appid, m.name, m.header_image, m.developers, m.tags, m.release_date, m.early_access,
    m.price_cents, m.is_free, m.reviews_total, m.followers, m.peak_7d, m.peak_prev_7d, m.pct_positive,
    w.end_day, w.week_reviews, w.week_up, w.prev_days,
    case when w.prev_days >= 14 then round(coalesce(w.prev_reviews, 0) * 7.0 / w.prev_days, 1) end as usual_week,
    case when w.week_reviews > 0 then round(w.week_up::numeric / w.week_reviews, 3) end as week_pos,
    coalesce(case when w.prev_reviews >= 5 then round(w.prev_up::numeric / w.prev_reviews, 3) end,
             m.pct_positive) as usual_pos
  from mv_app_metrics m
  left join w on w.appid = m.appid
  where m.type = 'game' and not coalesce(m.coming_soon, false)
    and (m.release_date is null or m.release_date < current_date - 30)
),
flag as (
  select b.*,
    case when b.usual_week > 0 then round(b.week_reviews / b.usual_week, 2) end as ratio,
    case when b.peak_prev_7d > 0 then round(b.peak_7d::numeric / b.peak_prev_7d, 2) end as player_ratio,
    (b.end_day >= current_date - 9 and b.week_reviews >= 15 and b.prev_days >= 14
       and b.week_reviews >= 2 * coalesce(b.usual_week, 0)) as review_surge,
    (b.peak_7d >= 50 and b.peak_prev_7d > 0 and b.peak_7d >= 1.5 * b.peak_prev_7d) as player_surge
  from base b
),
s as (
  select f.*,
    case when f.review_surge and f.player_surge then 'both' when f.review_surge then 'reviews' else 'players' end as kind,
    coalesce(case when f.review_surge then f.end_day end, current_date) as surge_end,
    (f.review_surge and f.week_pos < 0.6 and f.usual_pos - f.week_pos >= 0.2) as backlash
  from flag f
  where f.review_surge or f.player_surge
),
ev as (   -- evidence in the window: posts, store-page changes, Steam-wide events
  select s.appid, e.start_day as day, e.category as cause, e.title as detail
  from s join events e on e.appid = s.appid and e.kind = 'news'
   and e.start_day between s.surge_end - 16 and s.surge_end
  union all
  select s.appid, pc.day,
    case pc.field
      when 'early_access' then case when pc.new_value = 'Full release' then 'release' end
      when 'coming_soon'  then case when pc.new_value = 'released' then 'release' end
      when 'dlc'          then 'dlc'
      when 'discount'     then case when pc.new_value is distinct from '0%' then 'sale' end
      when 'capsule' then 'page' when 'trailers' then 'page' when 'tags' then 'page'
      when 'name' then 'page' when 'short_description' then 'page'
    end,
    case pc.field
      when 'early_access' then 'Left Early Access'
      when 'coming_soon' then 'Released'
      when 'dlc' then pc.new_value
      when 'discount' then 'Discount ' || pc.new_value
      when 'capsule' then 'New capsule art'
      when 'trailers' then pc.new_value
      when 'tags' then 'Tags changed'
      else initcap(replace(pc.field, '_', ' ')) || ' changed'
    end
  from s join page_changes pc on pc.appid = s.appid
   and pc.day between s.surge_end - 16 and s.surge_end
  union all
  select s.appid, g.start_day,
    case when g.title ilike '%sale%' then 'steam_sale' else 'fest' end, g.title
  from s join events g on g.appid is null and g.kind = 'steam_event'
   and g.start_day <= s.surge_end and coalesce(g.end_day, g.start_day) >= s.surge_end - 16
),
ranked as (
  select ev.*, public.surge_cause_rank(ev.cause) as rnk from ev
  where ev.cause is not null and public.surge_cause_rank(ev.cause) < 99
)
select s.appid, s.name, s.header_image, s.developers, s.tags, s.release_date, s.early_access,
  s.price_cents, s.is_free, s.reviews_total, s.followers,
  s.kind, s.surge_end as end_day, s.week_reviews, s.usual_week, s.ratio, s.week_pos, s.usual_pos,
  s.backlash, s.peak_7d, s.peak_prev_7d, s.player_ratio,
  coalesce(case when s.backlash then 'backlash' end,   -- mostly negative reviews: say so first
           (select r.cause from ranked r where r.appid = s.appid order by r.rnk, r.day desc limit 1),
           'none') as cause,
  coalesce((select json_agg(x) from (
      select r.day, r.cause, r.detail from ranked r where r.appid = s.appid
      order by r.rnk, r.day desc limit 5) x), '[]'::json) as causes,
  round((case when s.review_surge
         then least(coalesce(s.ratio, 5), 10) * ln(1 + s.week_reviews)
         else least(s.player_ratio, 10) * ln(1 + s.peak_7d) / 2 end)::numeric, 2) as score
from s;

create unique index mv_surges_appid on mv_surges (appid);

create or replace view v_surges with (security_invoker = true) as
select * from mv_surges;

-- What caused surges over the last 30 days (one count per game per week).
create or replace view v_surge_causes with (security_invoker = true) as
select cause, count(*) as surges
from surge_log
where week >= current_date - 30
group by cause
order by count(*) desc;

create or replace function refresh_metrics() returns void
language plpgsql
set search_path = public, pg_temp as $$
begin
  refresh materialized view concurrently public.mv_app_metrics;
  refresh materialized view concurrently public.mv_surges;
  insert into public.surge_log (appid, week, end_day, cause, ratio, week_reviews)
  select appid, date_trunc('week', end_day)::date, end_day, cause, ratio, week_reviews
  from public.mv_surges
  on conflict (appid, week) do update set
    end_day = excluded.end_day, cause = excluded.cause,
    ratio = excluded.ratio, week_reviews = excluded.week_reviews;
end
$$;

-- Tag list with counts, for pickers.
create or replace view v_tags with (security_invoker = true) as
select t as tag, count(*) as games
from apps, unnest(tags) as t
where type = 'game'
group by t
order by games desc;

-- ────────────────────────────────────────────────────────────── genres ──
-- One row per genre (tag), counting each game under its five main tags.
--   growth_median   median 7-day follower growth of its upcoming games
--   growth_vs_all   that median ÷ the median across every upcoming game
--   breakouts       upcoming games with heat ≥ 2 this week
--   hot_demos       demos at 1.5×+ last week's peak players (20+ players)
--   beat/on_track/miss   launch verdicts, last 90 days
--   reviews_week    reviews gained this week by its released, paid games (a sales proxy)
create or replace view v_genres with (security_invoker = true) as
with
up as (
  select b.followers, b.followers_7d, b.heat, genre_tags(b.tags) as g,
         (b.followers - b.followers_7d)::numeric / b.followers_7d as growth
  from v_breakouts b
  where b.followers_7d >= 50
),
all_up as (
  select percentile_cont(0.5) within group (order by growth) as med from up
),
games as (
  select m.coming_soon, m.is_free, m.reviews_total, m.reviews_7d, genre_tags(m.tags) as g
  from mv_app_metrics m
  where m.type = 'game'
),
by_games as (
  select t as tag,
    count(*) as games,
    count(*) filter (where coalesce(coming_soon, false)) as upcoming,
    count(*) filter (where not coalesce(coming_soon, false)) as released,
    sum(reviews_total - reviews_7d) filter (
      where not coalesce(coming_soon, false) and not coalesce(is_free, false)) as reviews_week
  from games, unnest(g) as t
  group by t
),
by_up as (
  select t as tag,
    count(*) as up_n,
    percentile_cont(0.5) within group (order by growth) as growth_median,
    sum(followers - followers_7d) as follower_gain,
    count(*) filter (where heat >= 2) as breakouts
  from up, unnest(g) as t
  group by t
),
by_demo as (
  select t as tag,
    count(*) as demos,
    count(*) filter (where week_ratio >= 1.5 and peak_7d >= 20) as hot_demos
  from v_demo_heat d, unnest(genre_tags(d.tags)) as t
  group by t
),
by_launch as (
  select t as tag,
    count(*) as launches,
    count(*) filter (where verdict = 'beat') as beat,
    count(*) filter (where verdict = 'on track') as on_track,
    count(*) filter (where verdict = 'miss') as miss,
    percentile_cont(0.5) within group (order by vs_peers) as launch_vs_peers
  from v_launches l, unnest(genre_tags(l.tags)) as t
  group by t
),
by_quiet as (
  select t as tag, count(*) as quiet_earners
  from v_quiet_earners q, unnest(genre_tags(q.tags)) as t
  group by t
),
by_surge as (
  select t as tag, count(*) as surges
  from mv_surges x, unnest(genre_tags(x.tags)) as t
  where x.cause <> 'backlash'
  group by t
)
select
  g.tag, g.games, g.upcoming, g.released,
  coalesce(u.up_n, 0) as up_n,
  case when u.up_n >= 3 then round(u.growth_median::numeric, 4) end as growth_median,
  case when u.up_n >= 3 and a.med > 0 then round((u.growth_median / a.med)::numeric, 2) end as growth_vs_all,
  coalesce(u.follower_gain, 0) as follower_gain,
  coalesce(u.breakouts, 0) as breakouts,
  coalesce(d.demos, 0) as demos,
  coalesce(d.hot_demos, 0) as hot_demos,
  coalesce(l.launches, 0) as launches,
  coalesce(l.beat, 0) as beat,
  coalesce(l.on_track, 0) as on_track,
  coalesce(l.miss, 0) as miss,
  round(l.launch_vs_peers::numeric, 2) as launch_vs_peers,
  g.reviews_week,
  coalesce(q.quiet_earners, 0) as quiet_earners,
  coalesce(sg.surges, 0) as surges,
  round(a.med::numeric, 4) as growth_median_all
from by_games g
cross join all_up a
left join by_up u     on u.tag = g.tag
left join by_demo d   on d.tag = g.tag
left join by_launch l on l.tag = g.tag
left join by_quiet q  on q.tag = g.tag
left join by_surge sg on sg.tag = g.tag
where g.games >= 5;

-- Everything the genre panel shows, in one call.
create or replace function genre_detail(p_tag text) returns json
language sql stable security invoker
set search_path = public, pg_temp as $$
  select json_build_object(
    'tag', p_tag,
    'summary', (select row_to_json(g) from v_genres g where g.tag = p_tag),
    'pairs', (select coalesce(json_agg(x order by x.games desc, x.tag), '[]'::json) from (
        select t as tag, count(*) as games
        from mv_app_metrics m, unnest(genre_tags(m.tags)) as t
        where m.type = 'game' and p_tag = any(genre_tags(m.tags)) and t <> p_tag
        group by t order by count(*) desc limit 10) x),
    'breakouts', (select coalesce(json_agg(x order by x.heat desc nulls last), '[]'::json) from (
        select appid, name, header_image, developers, tags, followers, followers_7d,
               followers_gain, heat, est_wishlists, release_date_text, wishlist_rank
        from v_breakouts where p_tag = any(genre_tags(tags))
        order by heat desc nulls last limit 8) x),
    'demos', (select coalesce(json_agg(x order by x.week_ratio desc nulls last, x.peak_7d desc), '[]'::json) from (
        select appid, parent_appid, parent_name as name, parent_header_image as header_image, developers,
               tags, peak_7d, peak_prev_7d, week_ratio, parent_followers
        from v_demo_heat where p_tag = any(genre_tags(tags))
        order by week_ratio desc nulls last, peak_7d desc limit 6) x),
    'launches', (select coalesce(json_agg(x order by x.release_date desc), '[]'::json) from (
        select appid, name, header_image, developers, tags, release_date, days_live, reviews_total,
               pct_positive, followers_prelaunch, vs_peers, verdict, peak_since_launch
        from v_launches where p_tag = any(genre_tags(tags))
        order by release_date desc limit 10) x),
    'quiet', (select coalesce(json_agg(x order by x.reviews_last_30d desc nulls last, x.reviews_total desc), '[]'::json) from (
        select appid, name, header_image, developers, tags, release_date, reviews_total, pct_positive,
               reviews_last_30d, reviews_last_7d, est_net_usd, est_net_last_30d, price_cents
        from v_quiet_earners where p_tag = any(genre_tags(tags))
        order by reviews_last_30d desc nulls last, reviews_total desc limit 6) x),
    'surges', (select coalesce(json_agg(x order by x.score desc), '[]'::json) from (
        select appid, name, header_image, developers, tags, release_date, kind, week_reviews, usual_week,
               ratio, peak_7d, player_ratio, cause, causes, score
        from mv_surges where p_tag = any(genre_tags(tags))
        order by score desc limit 8) x)
  )
$$;

-- ──────────────────────────────────────────────────────────── overview ──
-- The daily digest in one call: what moved, and what changed on Steam
-- around each mover.
create or replace function overview() returns json
language sql stable security invoker
set search_path = public, pg_temp as $$
  with
  b as (select * from v_breakouts),
  gen as (select * from v_genres),
  l as (select * from v_launches),
  d as (select * from v_demo_heat)
  select json_build_object(
    'as_of',      (select max(day) from daily_snapshots),
    'first_day',  (select min(day) from daily_snapshots where followers is not null),
    'refreshed_at', (select max(refreshed_at) from mv_app_metrics),
    'tracked',    (select count(*) from apps where watch),
    'upcoming_tracked', (select count(*) from b),
    'event_now',  (select row_to_json(e) from (
        select title, start_day, end_day from events
        where appid is null and start_day <= current_date and coalesce(end_day, start_day) >= current_date
        order by start_day limit 1) e),
    'event_next', (select row_to_json(e) from (
        select title, start_day, end_day from events
        where appid is null and start_day > current_date
        order by start_day limit 1) e),
    'events_soon', (select coalesce(json_agg(e order by e.start_day), '[]'::json) from (
        select title, start_day, end_day from events
        where appid is null and start_day > current_date and start_day <= current_date + 30
        order by start_day limit 3) e),
    'counts', json_build_object(
        'breakouts',   (select count(*) from b where heat >= 2),
        'new_on_list', (select count(*) from b where rank_new_entry),
        'hot_demos',   (select count(*) from d where week_ratio >= 1.5 and peak_7d >= 20),
        'launches_2w', (select count(*) from l where days_live <= 14),
        'beat',        (select count(*) from l where verdict = 'beat' and days_live <= 30),
        'miss',        (select count(*) from l where verdict = 'miss' and days_live <= 30),
        'judged',      (select count(*) from l where verdict in ('beat', 'on track', 'miss') and days_live <= 30),
        'surges',      (select count(*) from mv_surges where cause <> 'backlash')),
    'surges', (select coalesce(json_agg(x order by x.score desc), '[]'::json) from (
        select appid, name, header_image, tags, release_date, early_access, kind, week_reviews, usual_week,
               ratio, week_pos, usual_pos, peak_7d, player_ratio, cause, causes, score
        from mv_surges order by score desc limit 6) x),
    'surge_causes', (select coalesce(json_agg(x order by x.surges desc), '[]'::json) from (
        select cause, surges from v_surge_causes) x),
    'breakouts', (select coalesce(json_agg(x order by x.heat desc), '[]'::json) from (
        select b.appid, b.name, b.header_image, b.developers, b.tags, b.followers, b.followers_7d,
               b.followers_gain, b.heat, b.wishlist_rank, b.rank_new_entry, b.release_date_text,
               (select coalesce(json_agg(c order by c.day desc), '[]'::json) from (
                  select pc.day, pc.field as kind, pc.old_value, pc.new_value, null::text as title
                  from page_changes pc
                  where pc.appid = b.appid and pc.day >= current_date - 14
                  union all
                  select e.start_day, 'news', null, null, e.title
                  from events e
                  where e.appid = b.appid and e.kind = 'news' and e.start_day >= current_date - 14
                  order by 1 desc limit 4) c) as causes
        from b where b.heat >= 1.5
        order by b.heat desc limit 6) x),
    'new_on_list', (select coalesce(json_agg(x order by x.wishlist_rank), '[]'::json) from (
        select appid, name, header_image, wishlist_rank, followers, tags
        from b where rank_new_entry order by wishlist_rank limit 6) x),
    'demos', (select coalesce(json_agg(x order by x.week_ratio desc nulls last, x.peak_7d desc), '[]'::json) from (
        select appid, parent_appid, parent_name as name, parent_header_image as header_image, tags,
               peak_7d, peak_prev_7d, week_ratio, parent_followers
        from d where peak_7d >= 20
        order by week_ratio desc nulls last, peak_7d desc limit 6) x),
    'launches', (select coalesce(json_agg(x order by x.vs_peers desc nulls last, x.reviews_total desc nulls last), '[]'::json) from (
        select appid, name, header_image, tags, release_date, days_live, reviews_total, pct_positive,
               followers_prelaunch, vs_peers, verdict
        from l where days_live <= 14
        order by vs_peers desc nulls last, reviews_total desc nulls last limit 6) x),
    'genres_up', (select coalesce(json_agg(x order by x.growth_vs_all desc), '[]'::json) from (
        select tag, up_n, growth_median, growth_vs_all, breakouts, hot_demos
        from gen where up_n >= 5 and growth_vs_all is not null
        order by growth_vs_all desc limit 5) x),
    'genres_down', (select coalesce(json_agg(x order by x.growth_vs_all), '[]'::json) from (
        select tag, up_n, growth_median, growth_vs_all, breakouts
        from gen where up_n >= 5 and growth_vs_all is not null
        order by growth_vs_all limit 3) x),
    'changes', (select coalesce(json_agg(x order by x.heat desc nulls last, x.followers desc nulls last), '[]'::json) from (
        select pc.appid, a.name, a.header_image, pc.day, pc.field, pc.old_value, pc.new_value,
               m.followers, bb.heat
        from page_changes pc
        join apps a on a.appid = pc.appid
        left join mv_app_metrics m on m.appid = pc.appid
        left join b bb on bb.appid = pc.appid
        where pc.day >= current_date - 7
          and pc.field in ('capsule', 'trailers', 'tags', 'name', 'short_description')
        order by bb.heat desc nulls last, m.followers desc nulls last limit 8) x),
    'quiet', (select coalesce(json_agg(x order by x.reviews_last_30d desc nulls last, x.reviews_last_7d desc nulls last), '[]'::json) from (
        select appid, name, header_image, tags, release_date, reviews_total, pct_positive,
               reviews_last_30d, reviews_last_7d, est_net_usd, est_net_last_30d, price_cents
        from v_quiet_earners
        order by reviews_last_30d desc nulls last, reviews_last_7d desc nulls last limit 4) x)
  )
$$;

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
language sql stable security invoker
set search_path = public, pg_temp as $$
  with q as (
    select t, i from unnest(p_tags) with ordinality as u(t, i)
  ),
  qn as (
    select sum(1.0 / (1 + 0.15 * (i - 1))) as norm, count(*) as n from q
  ),
  c as (
    select a.appid, a.name, a.release_date, a.price_cents, a.tags, a.is_free,
      a.header_image, a.developers,
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
    m.reviews_total, m.pct_positive,
    est_units(m.reviews_total),
    case when not s.is_free then est_net_usd(m.reviews_total, s.price_cents) end,
    m.peak_all, m.followers,
    s.header_image, s.developers
  from s
  left join mv_app_metrics m on m.appid = s.appid
  order by s.score desc, m.reviews_total desc nulls last
  limit p_limit
$$;

-- Same, seeded from an existing game's top tags and price.
create or replace function comparables_for(p_appid bigint, p_limit int default 25)
returns table (
  appid bigint, name text, release_date date, price_cents int, tags text[],
  shared_tags text[], score numeric, reviews_total int, pct_positive numeric,
  est_units bigint, est_net_usd numeric, peak_ccu int, followers int,
  header_image text, developers text[]
)
language sql stable security invoker
set search_path = public, pg_temp as $$
  select f.*
  from apps a,
       lateral find_comparables(a.tags[1:8], a.price_cents, null, a.appid, p_limit) f
  where a.appid = p_appid
$$;

-- ─────────────────────────────────────────────── read-only public access ──
-- The dashboard uses Supabase's publishable/anon key: it may read, never write.
-- The collector connects as the database owner, which bypasses RLS.

alter table apps            enable row level security;
alter table daily_snapshots enable row level security;
alter table page_state      enable row level security;
alter table page_changes    enable row level security;
alter table events          enable row level security;
alter table review_history  enable row level security;
alter table surge_log       enable row level security;

do $$
declare t text;
begin
  if exists (select 1 from pg_roles where rolname = 'anon') then
    -- Explicit grants, so this works with "Automatically expose new tables" turned off.
    grant usage on schema public to anon, authenticated;
    foreach t in array array['apps','daily_snapshots','page_changes','events','review_history','surge_log'] loop
      execute format('drop policy if exists "public read" on %I', t);
      execute format('create policy "public read" on %I for select to anon, authenticated using (true)', t);
      execute format('grant select on %I to anon, authenticated', t);
    end loop;
    grant select on mv_app_metrics, v_app_metrics, v_breakouts, v_demo_heat, v_launches,
                    v_quiet_earners, v_tags, v_genres, mv_surges, v_surges, v_surge_causes
      to anon, authenticated;
    grant execute on function find_comparables(text[], int, date, bigint, int),
                              comparables_for(bigint, int), genre_detail(text), overview(),
                              genre_tags(text[]), genre_ignored_tags(), surge_cause_rank(text),
                              est_units(int), est_net_usd(int, int), est_wishlists(int)
      to anon, authenticated;
    revoke execute on function refresh_metrics() from public, anon, authenticated;
  end if;
end $$;
