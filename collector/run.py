"""Steam Radar collector.

    python -m collector.run discover        # feeds -> watchlist, wishlist ranks, global events
    python -m collector.run snapshot        # daily signals for every watched game
    python -m collector.run ccu             # player-count sample (run every few hours)
    python -m collector.run sweep           # daily: review totals + history for a seventh of the older games
    python -m collector.run seed-universe   # monthly: pull games with 500+ reviews from SteamSpy
    python -m collector.run daily           # discover + snapshot
    python -m collector.run probe 1145360   # print every parsed signal for one app (no database)

Settings come from environment variables (see README).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from . import db
from .steam import SteamClient

log = logging.getLogger("radar")
HERE = Path(__file__).resolve().parent


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def today():
    return datetime.now(timezone.utc).date()


def client() -> SteamClient:
    return SteamClient(cc=os.environ.get("STEAM_CC", "us"))


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set (see README).")
    return url


# ── discover ─────────────────────────────────────────────────────────────

def discover(steam: SteamClient, url: str) -> None:
    day = today()
    wishlist_pages = env_int("WISHLIST_PAGES", 10)
    new_pages = env_int("NEW_RELEASE_PAGES", 3)
    conn = db.connect(url)

    feeds: dict[str, list] = {}
    feeds["most_played"] = [r["appid"] for r in steam.most_played()]
    for k, ids in steam.featured().items():
        feeds[k] = ids
    feeds["popular_upcoming"] = steam.search_feed(pages=2, filter="popularcomingsoon", category1="998")
    feeds["popular_new"] = steam.search_feed(pages=2, filter="popularnew", category1="998")
    feeds["newest"] = steam.search_feed(pages=new_pages, sort_by="Released_DESC", category1="998")
    wishlist = steam.search_feed(pages=wishlist_pages, filter="popularwishlist")
    feeds["top_wishlisted"] = wishlist

    for name, ids in feeds.items():
        n = db.mark_seen(conn, [i for i in ids if i], name, day)
        log.info("feed %-16s %4d apps", name, n)

    ranks = {appid: pos for pos, appid in enumerate(wishlist, 1) if appid}
    db.upsert_snapshot(conn, day, "wishlist_rank", ranks)
    log.info("wishlist ranks recorded: %d", len(ranks))

    events = json.loads((HERE / "global_events.json").read_text())["events"]
    db.add_global_events(conn, events)
    log.info("demoted to weekly sweep: %d", db.demote_stale(conn, day))


# ── snapshot ─────────────────────────────────────────────────────────────

def _phase(name: str, url: str, ids: list[int], fn) -> None:
    """Run fn(conn, appid) for each id on its own connection; log progress."""
    if not ids:
        log.info("[%s] nothing to do", name)
        return
    conn = db.connect(url)
    t0, done, failed = time.time(), 0, 0
    for i, appid in enumerate(ids, 1):
        try:
            fn(conn, appid)
            done += 1
        except Exception as e:                       # keep going; one bad app must not stop a run
            failed += 1
            log.warning("[%s] %s: %s", name, appid, e)
            if conn.closed:
                conn = db.connect(url)
        if i % 250 == 0:
            log.info("[%s] %d/%d (%.0fs)", name, i, len(ids), time.time() - t0)
    log.info("[%s] done: %d ok, %d failed in %.0fs", name, done, failed, time.time() - t0)


def snapshot(steam: SteamClient, url: str) -> None:
    day = today()
    limit = env_int("WATCH_LIMIT", 2000)
    conn = db.connect(url)
    watched = db.fetch_ids(conn, """
        select appid from apps where watch and skip_reason is null
        order by coalesce(coming_soon, true) desc, last_seen desc nulls last, appid
        limit %(limit)s""", {"limit": limit})
    # Store pages: hot apps daily, settled ones every 3 days (appdetails is the slow endpoint).
    need_details = db.fetch_ids(conn, """
        select appid from apps where appid = any(%(ids)s) and (
          details_at is null
          or (details_at < now() - interval '20 hours'
              and (coalesce(coming_soon, true) or type = 'demo' or release_date > current_date - 60))
          or details_at < now() - interval '70 hours')
        order by details_at nulls first""", {"ids": watched})
    need_tags = db.fetch_ids(conn, """
        select appid from apps where appid = any(%(ids)s) and type = 'game'
          and (tags_at is null or tags_at < now() - interval '7 days')
        order by tags_at nulls first""", {"ids": watched})
    need_news = db.fetch_ids(conn, """
        select appid from apps where appid = any(%(ids)s) and type = 'game'
          and (news_at is null or news_at < now() - interval '3 days')""", {"ids": watched})
    games = db.fetch_ids(conn, "select appid from apps where appid = any(%(ids)s) and type <> 'demo'",
                         {"ids": watched})
    # Review history (daily counts for 30 days) for released games, to spot surges.
    need_hist = db.fetch_ids(conn, """
        select appid from apps where appid = any(%(ids)s) and type = 'game'
          and not coalesce(coming_soon, false)
          and (hist_at is null or hist_at < now() - interval '20 hours')""", {"ids": watched})
    conn.close()
    log.info("watched %d | details %d | tags %d | posts %d | review history %d", len(watched),
             len(need_details), len(need_tags), len(need_news), len(need_hist))

    def details(c, a):
        changes = db.apply_details(c, a, steam.app_details(a), day)
        for ch in changes:
            log.info("page change %s %s: %s -> %s", a, ch["field"], ch["old_value"], ch["new_value"])

    def reviews(c, a):
        db.upsert_reviews(c, day, {a: steam.reviews_summary(a)})

    def followers(c, a):
        db.upsert_snapshot(c, day, "followers", {a: steam.followers(a)})

    def ccu(c, a):
        db.upsert_snapshot(c, day, "ccu_peak", {a: steam.current_players(a)})

    def news(c, a):
        db.add_news(c, a, fetch_posts(steam, a))

    def history(c, a):
        db.save_histogram(c, a, steam.review_histogram(a))

    def tags(c, a):
        db.apply_tags(c, a, steam.steamspy_tags(a), day)

    # Each phase talks to a different host, so they run side by side.
    # (Reviews share the store host with details but use a separate, lighter bucket.)
    phases = [("details", need_details, details), ("reviews", watched, reviews),
              ("followers", games, followers), ("ccu", watched, ccu),
              ("posts", need_news, news), ("tags", need_tags, tags),
              ("history", need_hist, history)]
    with ThreadPoolExecutor(max_workers=len(phases)) as pool:
        for f in [pool.submit(_phase, n, url, ids, fn) for n, ids, fn in phases]:
            f.result()

    # Demos found while reading store pages get their first snapshot next run.


def ccu_sample(steam: SteamClient, url: str) -> None:
    day = today()
    conn = db.connect(url)
    ids = db.fetch_ids(conn, """
        select a.appid from apps a
        where a.watch and a.skip_reason is null
          and (a.type = 'demo' or not coalesce(a.coming_soon, true)
               or exists (select 1 from daily_snapshots s where s.appid = a.appid and s.ccu_peak > 0))
        limit %(limit)s""", {"limit": env_int("WATCH_LIMIT", 2000)})
    conn.close()
    _phase("ccu", url, ids, lambda c, a: db.upsert_snapshot(c, day, "ccu_peak", {a: steam.current_players(a)}))


def fetch_posts(steam: SteamClient, appid: int) -> list[dict]:
    """Developer posts with Steam's event type; plain news feed if the event feed fails."""
    posts = steam.partner_events(appid)
    return posts if posts is not None else steam.news(appid)


def investigate_surges(steam: SteamClient, url: str) -> None:
    """For every game surging right now, re-read its store page and posts so the likely
    cause (1.0 launch, DLC, update, discount...) is on record. Only movers are checked,
    which keeps this well inside Steam's rate limits."""
    db.refresh_metrics(url)
    day = today()
    conn = db.connect(url)
    try:
        need_details = db.fetch_ids(conn, """
            select s.appid from mv_surges s join apps a using (appid)
            where a.details_at is null or a.details_at < now() - interval '20 hours'
            limit 400""")
        need_posts = db.fetch_ids(conn, """
            select s.appid from mv_surges s join apps a using (appid)
            where a.news_at is null or a.news_at < now() - interval '20 hours'
            limit 400""")
    except Exception as e:                      # schema not updated yet
        log.warning("surge check skipped: %s", e)
        return
    finally:
        conn.close()
    log.info("surging games to check: %d store pages, %d post lists", len(need_details), len(need_posts))
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_phase, "surge-details", url, need_details,
                        lambda c, x: db.apply_details(c, x, steam.app_details(x), day))
        b = pool.submit(_phase, "surge-posts", url, need_posts,
                        lambda c, x: db.add_news(c, x, fetch_posts(steam, x)))
        a.result(), b.result()


# ── quiet-earner universe ────────────────────────────────────────────────

def seed_universe(steam: SteamClient, url: str) -> None:
    day = today()
    min_reviews = env_int("UNIVERSE_MIN_REVIEWS", 500)
    pages = env_int("UNIVERSE_PAGES", 40)
    conn = db.connect(url)
    total = 0
    for page in range(pages):
        rows = steam.steamspy_all_page(page)
        if not rows:
            break
        keep = [{"appid": int(r["appid"]), "name": r.get("name"),
                 "price": int(r["initialprice"]) if str(r.get("initialprice", "")).isdigit() else None}
                for r in rows if int(r.get("positive") or 0) + int(r.get("negative") or 0) >= min_reviews]
        total += db.add_universe(conn, keep, day)
        log.info("SteamSpy page %d: %d rows, %d kept (total %d)", page, len(rows), len(keep), total)


def sweep(steam: SteamClient, url: str) -> None:
    day = today()
    batch = env_int("SWEEP_BATCH", 3500)
    conn = db.connect(url)
    ids = db.fetch_ids(conn, """
        select appid from apps
        where not watch and skip_reason is null and type = 'game'
          and (reviews_at is null or reviews_at < now() - interval '6 days')
        order by reviews_at nulls first limit %(b)s""", {"b": batch})
    conn.close()

    # Review totals and review history use separate rate limits, so they run side by side.
    with ThreadPoolExecutor(max_workers=2) as pool:
        r = pool.submit(_phase, "sweep-reviews", url, ids,
                        lambda c, a: db.upsert_reviews(c, day, {a: steam.reviews_summary(a)}))
        h = pool.submit(_phase, "sweep-history", url, ids,
                        lambda c, a: db.save_histogram(c, a, steam.review_histogram(a)))
        r.result(), h.result()

    conn = db.connect(url)
    # Release date + price + tags are needed to qualify as a quiet earner.
    need_details = db.fetch_ids(conn, """
        select a.appid from apps a
        join lateral (select reviews_total from daily_snapshots s where s.appid = a.appid
                      and reviews_total is not null order by day desc limit 1) r on true
        where a.appid = any(%(ids)s) and a.details_at is null and r.reviews_total >= 1000""", {"ids": ids})
    need_tags = db.fetch_ids(conn, """
        select appid from apps where appid = any(%(ids)s) and tags_at is null""", {"ids": need_details})
    conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(_phase, "sweep-details", url, need_details,
                        lambda c, x: db.apply_details(c, x, steam.app_details(x), day))
        b = pool.submit(_phase, "sweep-tags", url, need_tags,
                        lambda c, x: db.apply_tags(c, x, steam.steamspy_tags(x), day))
        a.result(), b.result()


# ── probe ────────────────────────────────────────────────────────────────

def probe(steam: SteamClient, appid: int) -> None:
    """Fetch every signal for one app and print it. Good first test after setup."""
    out = {
        "details": steam.app_details(appid),
        "reviews": steam.reviews_summary(appid),
        "followers": steam.followers(appid),
        "current_players": steam.current_players(appid),
        "tags": (steam.steamspy_tags(appid) or [])[:10],
        "posts": (fetch_posts(steam, appid) or [])[:5],
        "review_history": (lambda h: h and {"rollup_type": h["rollup_type"], "months_or_weeks": len(h["rollups"]),
                                             "last_30_days": h["recent"][-7:]})(steam.review_histogram(appid)),
    }
    print(json.dumps(out, indent=2, default=str))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description="Steam Radar collector")
    p.add_argument("command", choices=["discover", "snapshot", "ccu", "sweep", "seed-universe", "daily", "probe"])
    p.add_argument("appid", nargs="?", type=int, help="for probe")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    steam = client()
    if a.command == "probe":
        if not a.appid:
            sys.exit("usage: probe APPID")
        return probe(steam, a.appid)
    url = database_url()
    if a.command in ("discover", "daily"):
        discover(steam, url)
    if a.command in ("snapshot", "daily"):
        snapshot(steam, url)
        investigate_surges(steam, url)
    if a.command == "ccu":
        ccu_sample(steam, url)
    if a.command == "sweep":
        sweep(steam, url)
        investigate_surges(steam, url)
    if a.command == "seed-universe":
        seed_universe(steam, url)
    db.refresh_metrics(url)


if __name__ == "__main__":
    main()
