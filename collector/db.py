"""Database writes. Each collector phase opens its own connection."""
from __future__ import annotations

import json
from datetime import date
from typing import Iterable

import psycopg
from psycopg.types.json import Jsonb

from .changes import diff_states, diff_tags, page_state

SNAPSHOT_COLS = {"followers", "reviews_total", "reviews_positive", "ccu_peak",
                 "wishlist_rank", "price_cents", "discount_pct"}


def connect(url: str) -> psycopg.Connection:
    # prepare_threshold=None keeps it working through Supabase's transaction pooler.
    return psycopg.connect(url, autocommit=True, prepare_threshold=None)


def mark_seen(conn, appids: Iterable[int], source: str, day: date) -> int:
    """Add apps surfaced by a discovery feed and put them on the daily watchlist."""
    ids = sorted({int(a) for a in appids if a})
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute("""
            insert into apps (appid, sources, first_seen, last_seen, watch)
            select x, array[%(src)s], %(day)s, %(day)s, true from unnest(%(ids)s::bigint[]) x
            on conflict (appid) do update set
              last_seen = excluded.last_seen,
              sources   = (select array(select distinct unnest(apps.sources || excluded.sources))),
              watch     = apps.skip_reason is null
        """, {"ids": ids, "src": source, "day": day})
    return len(ids)


def add_universe(conn, rows: list[dict], day: date) -> int:
    """Quiet-earner universe from SteamSpy: known apps, not on the daily watchlist."""
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany("""
            insert into apps (appid, name, price_cents, sources, first_seen, last_seen, watch)
            values (%(appid)s, %(name)s, %(price)s, array['steamspy_universe'], %(day)s, %(day)s, false)
            on conflict (appid) do update set
              name = coalesce(apps.name, excluded.name),
              price_cents = coalesce(apps.price_cents, excluded.price_cents),
              sources = (select array(select distinct unnest(apps.sources || excluded.sources)))
        """, [{**r, "day": day} for r in rows])
    return len(rows)


def upsert_snapshot(conn, day: date, column: str, values: dict[int, int | None]) -> None:
    """Write one metric for many apps. CCU keeps the day's maximum."""
    if column not in SNAPSHOT_COLS:
        raise ValueError(column)
    vals = [(a, day, v) for a, v in values.items() if v is not None]
    if not vals:
        return
    merge = f"greatest(daily_snapshots.{column}, excluded.{column})" if column == "ccu_peak" else f"excluded.{column}"
    with conn.cursor() as cur:
        cur.executemany(f"""
            insert into daily_snapshots (appid, day, {column}) values (%s, %s, %s)
            on conflict (appid, day) do update set {column} = {merge}
        """, vals)


def upsert_reviews(conn, day: date, values: dict[int, dict]) -> None:
    rows = [(a, day, v["total"], v["positive"]) for a, v in values.items() if v]
    if not rows:
        return
    with conn.cursor() as cur:
        cur.executemany("""
            insert into daily_snapshots (appid, day, reviews_total, reviews_positive) values (%s, %s, %s, %s)
            on conflict (appid, day) do update set
              reviews_total = excluded.reviews_total, reviews_positive = excluded.reviews_positive
        """, rows)
        cur.execute("update apps set reviews_at = now() where appid = any(%s)", ([r[0] for r in rows],))


def apply_details(conn, appid: int, d: dict | None, day: date) -> list[dict]:
    """Store parsed appdetails, log page changes, add demos. Returns the change rows."""
    if d is None:
        return []
    with conn.cursor() as cur:
        if d.get("missing"):
            cur.execute("update apps set skip_reason = 'no store page', watch = false, details_at = now() "
                        "where appid = %s", (appid,))
            return []
        cur.execute("""
            update apps set name = %(name)s, type = %(type)s,
              parent_appid = coalesce(%(parent_appid)s, parent_appid),
              coming_soon = %(coming_soon)s, release_date = %(release_date)s,
              release_date_text = %(release_date_text)s, is_free = %(is_free)s,
              price_cents = coalesce(%(price_cents)s, price_cents),
              developers = %(developers)s, publishers = %(publishers)s, genres = %(genres)s,
              header_image = %(header_image)s, details_at = now(), skip_reason = null
            where appid = %(appid)s
        """, {**d, "appid": appid})

        new_state = page_state(d)
        cur.execute("select state from page_state where appid = %s", (appid,))
        row = cur.fetchone()
        changes = diff_states(row[0] if row else None, new_state)
        cur.execute("""
            insert into page_state (appid, state, updated_at) values (%s, %s, now())
            on conflict (appid) do update set state = excluded.state, updated_at = now()
        """, (appid, Jsonb(new_state)))
        if changes:
            cur.executemany("""
                insert into page_changes (appid, day, field, old_value, new_value)
                values (%s, %s, %s, %s, %s)
            """, [(appid, day, c["field"], c["old_value"], c["new_value"]) for c in changes])

        if d.get("price_cents") is not None:
            cur.execute("""
                insert into daily_snapshots (appid, day, price_cents, discount_pct) values (%s, %s, %s, %s)
                on conflict (appid, day) do update set
                  price_cents = excluded.price_cents, discount_pct = excluded.discount_pct
            """, (appid, day, d["final_cents"] if d.get("final_cents") is not None else d["price_cents"],
                  d.get("discount_pct") or 0))

        for demo in d.get("demo_appids") or []:
            cur.execute("""
                insert into apps (appid, type, parent_appid, sources, first_seen, last_seen, watch)
                values (%s, 'demo', %s, array['demo_of_watched'], %s, %s, true)
                on conflict (appid) do update set parent_appid = excluded.parent_appid,
                  last_seen = excluded.last_seen, watch = apps.skip_reason is null
            """, (demo, appid, day, day))
    return changes


def apply_tags(conn, appid: int, tags: list[str] | None, day: date) -> dict | None:
    if tags is None:
        return None
    with conn.cursor() as cur:
        cur.execute("select tags from apps where appid = %s", (appid,))
        row = cur.fetchone()
        change = diff_tags(row[0] if row else None, tags)
        cur.execute("update apps set tags = %s, tags_at = now() where appid = %s", (tags, appid))
        if change:
            cur.execute("""
                insert into page_changes (appid, day, field, old_value, new_value)
                values (%s, %s, %s, %s, %s)
            """, (appid, day, change["field"], change["old_value"], change["new_value"]))
    return change


def add_news(conn, appid: int, items: list[dict]) -> int:
    with conn.cursor() as cur:
        if items:
            cur.executemany("""
                insert into events (appid, start_day, kind, title, url, ext_id)
                values (%s, %s, 'news', %s, %s, %s)
                on conflict do nothing
            """, [(appid, n["day"], n["title"][:300], n["url"], n["gid"]) for n in items])
        cur.execute("update apps set news_at = now() where appid = %s", (appid,))
    return len(items)


def add_global_events(conn, events: list[dict]) -> int:
    with conn.cursor() as cur:
        cur.executemany("""
            insert into events (appid, start_day, end_day, kind, title, url, ext_id)
            values (null, %(start)s, %(end)s, %(kind)s, %(title)s, %(url)s, %(id)s)
            on conflict (coalesce(appid, 0), kind, ext_id) do update set
              start_day = excluded.start_day, end_day = excluded.end_day,
              title = excluded.title, url = excluded.url
        """, [{"end": None, "url": None, "kind": "steam_event", **e} for e in events])
    return len(events)


def demote_stale(conn, day: date) -> int:
    """Released 180+ days ago and absent from every feed for 30 days: weekly sweep only."""
    with conn.cursor() as cur:
        cur.execute("""
            update apps set watch = false
            where watch and type = 'game' and not coalesce(coming_soon, false)
              and release_date < %(day)s::date - 180 and last_seen < %(day)s::date - 30
        """, {"day": day})
        return cur.rowcount


def fetch_ids(conn, sql: str, params: dict | None = None) -> list[int]:
    with conn.cursor() as cur:
        cur.execute(sql, params or {})
        return [r[0] for r in cur.fetchall()]


def dumps(o) -> str:
    return json.dumps(o, default=str)
