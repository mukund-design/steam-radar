"""End-to-end: run the collector against a real Postgres with a fake Steam.

Needs TEST_DATABASE_URL pointing at an empty, throwaway database. Skipped otherwise.
"""
import os
from datetime import date, timedelta
from pathlib import Path

import psycopg
import pytest

from collector import run
from collector.steam import SteamClient

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
SCHEMA = Path(__file__).resolve().parents[1] / "db" / "schema.sql"


class FakeSteam(SteamClient):
    """Two upcoming games, one released game with a demo, one delisted id."""

    def __init__(self):
        super().__init__(delays={k: 0 for k in ("store_api", "store", "community", "webapi", "steamspy")})
        self.capsule = "100"
        self.discount = 0
        self.followers_by_app = {10: 1000, 20: 200, 30: 50000}
        self.tags_by_app = {10: ["Cozy", "Building", "Relaxing"], 20: ["Roguelike", "Action"],
                            30: ["Cozy", "Building", "Simulation"]}

    def most_played(self): return [{"appid": 30, "rank": 1, "peak_in_game": 9000}]
    def featured(self): return {"coming_soon": [10, 20], "top_sellers": [30, 99]}

    def search_feed(self, pages=1, per_page=100, **q):
        return [20, None, 10] if q.get("filter") == "popularwishlist" else []

    def app_details(self, a):
        if a == 99:
            return {"missing": True}
        if a == 31:
            return {"missing": False, "name": "Hearth Demo", "type": "demo", "parent_appid": 30,
                    "coming_soon": False, "release_date": date(2026, 1, 1), "release_date_text": "Jan 1, 2026",
                    "is_free": True, "price_cents": None, "final_cents": None, "discount_pct": 0,
                    "developers": [], "publishers": [], "genres": [], "header_image": None,
                    "capsule_version": None, "short_description": "", "trailer_ids": [],
                    "screenshot_count": 0, "demo_appids": []}
        upcoming = a in (10, 20)
        return {"missing": False, "name": f"Game {a}", "type": "game", "parent_appid": None,
                "coming_soon": upcoming,
                "release_date": None if upcoming else date(2024, 3, 1),
                "release_date_text": "Q1 2027" if upcoming else "Mar 1, 2024",
                "is_free": False, "price_cents": 1999, "final_cents": 1999 * (100 - self.discount) // 100,
                "discount_pct": self.discount, "developers": ["Studio"], "publishers": ["Studio"],
                "genres": ["Indie"], "header_image": f"https://x/{a}/header.jpg?t={self.capsule}",
                "capsule_version": self.capsule, "short_description": "desc",
                "trailer_ids": ["1"], "screenshot_count": 5, "demo_appids": [31] if a == 30 else []}

    def reviews_summary(self, a): return {"total": 1200 if a == 30 else 0, "positive": 1100 if a == 30 else 0}
    def followers(self, a): return self.followers_by_app.get(a)
    def current_players(self, a): return 400 if a in (30, 31) else None
    def news(self, a, count=10): return [{"gid": f"n{a}", "title": "Patch", "url": "u", "day": date(2026, 10, 1)}]
    def steamspy_tags(self, a): return self.tags_by_app.get(a, [])


@pytest.fixture(scope="module")
def conn():
    c = psycopg.connect(URL, autocommit=True)
    c.execute("drop schema public cascade; create schema public;")
    c.execute(SCHEMA.read_text())
    yield c
    c.close()


def q(conn, sql, *args):
    return conn.execute(sql, args).fetchall()


def test_two_days(conn, monkeypatch):
    steam = FakeSteam()
    day1 = date(2026, 10, 1)
    monkeypatch.setattr(run, "today", lambda: day1)
    run.discover(steam, URL)
    run.snapshot(steam, URL)

    # discovery + delisting
    assert q(conn, "select skip_reason, watch from apps where appid = 99") == [("no store page", False)]
    assert q(conn, "select appid, wishlist_rank from daily_snapshots where wishlist_rank is not null order by 1") == [
        (10, 3), (20, 1)]
    # demo found from the parent's store page, global events loaded
    assert q(conn, "select type, parent_appid, watch from apps where appid = 31") == [("demo", 30, True)]
    assert q(conn, "select count(*) from events where appid is null")[0][0] > 20
    news_apps = {r[0] for r in q(conn, "select appid from events where kind = 'news'")}
    assert {10, 20, 30} <= news_apps
    assert q(conn, "select count(*) from page_changes")[0][0] == 0      # first sighting

    # day 8: capsule swap, a sale, a tag change, follower growth
    steam.capsule, steam.discount = "200", 40
    steam.followers_by_app = {10: 1100, 20: 900, 30: 50500}
    steam.tags_by_app[20] = ["Roguelike", "Deckbuilder"]
    day8 = day1 + timedelta(days=7)
    monkeypatch.setattr(run, "today", lambda: day8)
    conn.execute("update apps set details_at = details_at - interval '4 days', tags_at = tags_at - interval '8 days'")
    run.discover(steam, URL)
    run.snapshot(steam, URL)

    changes = q(conn, "select appid, field, old_value, new_value from page_changes where appid = 20 order by field")
    fields = {c[1]: c for c in changes}
    assert set(fields) == {"capsule", "discount", "tags"}     # list price unchanged
    assert fields["discount"][2:] == ("0%", "40%")
    assert fields["tags"][2:] == ("Action", "Deckbuilder")

    # detectors
    b = q(conn, "select appid, followers_gain from v_breakouts order by heat desc nulls last, followers_gain desc")
    assert dict(b) == {20: 700, 10: 100}
    assert q(conn, "select appid, peak_7d from v_demo_heat") == [(31, 400)]
    qe = q(conn, "select appid, est_units from v_quiet_earners")
    assert qe == [(30, 36000)]

    # comparables
    comps = q(conn, "select appid, shared_tags from find_comparables(array['Cozy','Building'], 1500)")
    assert comps == [(30, ["Cozy", "Building"])]
    assert q(conn, "select appid from comparables_for(30)") == []          # 10 is unreleased
    conn.execute("update apps set coming_soon = false, release_date = '2026-01-01' where appid = 10")
    assert [r[0] for r in q(conn, "select appid from comparables_for(30)")] == [10]


def test_launch_and_sweep(conn, monkeypatch):
    # game 20 launches; launch scorecard uses its last pre-launch follower count
    conn.execute("update apps set coming_soon = false, release_date = current_date - 5 where appid = 20")
    conn.execute("insert into daily_snapshots (appid, day, followers) values (20, current_date - 6, 900) "
                 "on conflict (appid, day) do update set followers = 900")
    conn.execute("insert into daily_snapshots (appid, day, reviews_total, reviews_positive) "
                 "values (20, current_date, 450, 400) on conflict (appid, day) do update "
                 "set reviews_total = 450, reviews_positive = 400")
    row = q(conn, "select followers_prelaunch, reviews_per_follower, verdict from v_launches where appid = 20")
    assert row and row[0][0] == 900 and float(row[0][1]) == 0.5 and row[0][2] == "on track"

    # sweep refreshes reviews for apps off the daily watchlist
    conn.execute("insert into apps (appid, name, watch, type) values (500, 'Old Gem', false, 'game')")
    steam = FakeSteam()
    steam.reviews_summary = lambda a: {"total": 5000, "positive": 4800}
    monkeypatch.setattr(run, "today", lambda: date(2026, 10, 9))
    run.sweep(steam, URL)
    assert q(conn, "select reviews_total from daily_snapshots where appid = 500") == [(5000,)]
    assert q(conn, "select name, release_date from apps where appid = 500") == [("Game 500", date(2024, 3, 1))]
