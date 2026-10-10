"""Build a realistic *synthetic* dataset, run it through the real SQL views, and
export the results for the dashboard's demo mode (web/demo-data.js).

    python tools/make_demo.py postgresql://localhost/radar_demo

The database is wiped. All games, studios and numbers are invented.
"""
from __future__ import annotations

import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DAYS = 75
rng = random.Random(7)

TAGS = ["Cozy", "Building", "Roguelike", "Deckbuilder", "Survival", "Crafting", "Co-op", "Horror",
        "Puzzle", "Platformer", "Pixel Graphics", "Farming Sim", "Automation", "Strategy", "Simulation",
        "Physics", "Multiplayer", "Relaxing", "Cute", "Action", "Shooter", "Metroidvania", "Idle",
        "Incremental", "Base Building", "Open World", "Story Rich", "Exploration", "Management",
        "Card Game", "Diorama", "Fishing", "Tower Defense", "Party Game", "Online Co-Op", "Colony Sim"]
CLUSTERS = [
    ["Cozy", "Building", "Relaxing", "Cute", "Diorama", "Simulation"],
    ["Roguelike", "Deckbuilder", "Card Game", "Strategy", "Pixel Graphics"],
    ["Survival", "Crafting", "Base Building", "Open World", "Online Co-Op", "Exploration"],
    ["Physics", "Co-op", "Party Game", "Multiplayer", "Online Co-Op", "Action"],
    ["Horror", "Co-op", "Online Co-Op", "Exploration", "Story Rich"],
    ["Idle", "Incremental", "Automation", "Management", "Relaxing"],
    ["Metroidvania", "Platformer", "Action", "Pixel Graphics", "Story Rich"],
    ["Farming Sim", "Cozy", "Fishing", "Relaxing", "Cute", "Exploration"],
    ["Colony Sim", "Management", "Base Building", "Strategy", "Simulation"],
    ["Puzzle", "Relaxing", "Story Rich", "Cute"],
    ["Tower Defense", "Strategy", "Roguelike", "Action"],
]
A = ["Moss", "Lantern", "Tide", "Hollow", "Ember", "Quiet", "Copper", "Paper", "Velvet", "Static", "Juniper",
     "Rust", "Pebble", "Hearth", "Signal", "Cinder", "Marrow", "Saltwind", "Burrow", "Glass", "Tin", "Fable",
     "Orbit", "Lumen", "Drift", "Kettle", "Bramble", "Nimbus", "Wax", "Thimble"]
B = ["Keeper", "Garden", "Hollow", "Express", "Protocol", "Harbor", "Archive", "Folk", "Engine", "Atlas",
     "Market", "Kingdom", "Shift", "Parade", "Den", "Wake", "Mill", "Outpost", "Circus", "Crossing",
     "Collapse", "Machine", "Orchard", "Lighthouse", "Workshop", "Spire", "Caravan", "Society"]
STUDIOS = ["Little Fern", "Tallow Games", "Northpine", "Bitwork Co.", "Two Herons", "Kilnworks", "Saltmarsh",
           "Owlbear Interactive", "Gloam", "Pocket Comet", "Sodium Studio", "Fernhall", "Brightwater",
           "Mudlark", "Quartz & Co.", "Halfmoon", "Rookery", "Tinderbox", "Velvet Lynx", "Stubborn Goat"]

used_names: set[str] = set()


def name() -> str:
    while True:
        n = rng.choice([f"{rng.choice(A)} {rng.choice(B)}", f"{rng.choice(A)}{rng.choice(B).lower()}",
                        f"The {rng.choice(A)} {rng.choice(B)}", f"{rng.choice(B)} of {rng.choice(A)}"])
        if n not in used_names:
            used_names.add(n)
            return n


def tags() -> list[str]:
    c = rng.choice(CLUSTERS)
    t = rng.sample(c, k=min(len(c), rng.randint(3, 5)))
    t += [x for x in rng.sample(TAGS, 4) if x not in t][: rng.randint(1, 3)]
    if rng.random() < 0.7:
        t.append("Indie")
    return t


def main(url: str) -> None:
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("drop schema public cascade; create schema public;")
    conn.execute((ROOT / "db" / "schema.sql").read_text())
    today = conn.execute("select current_date").fetchone()[0]
    start = today - timedelta(days=DAYS - 1)
    days = [start + timedelta(days=i) for i in range(DAYS)]

    events = json.loads((ROOT / "collector" / "global_events.json").read_text())["events"]
    with conn.cursor() as cur:
        cur.executemany("""insert into events (appid, start_day, end_day, kind, title, ext_id)
                           values (null, %s, %s, 'steam_event', %s, %s)""",
                        [(e["start"], e["end"], e["title"], e["id"]) for e in events])
    autumn = (date(2026, 10, 1), date(2026, 10, 8))

    apps, snaps, changes, news = [], [], [], []
    appid = 3_100_000

    def new_app(**kw):
        nonlocal appid
        appid += rng.randint(7, 900)
        a = {"appid": appid, "name": name(), "type": "game", "parent_appid": None, "coming_soon": False,
             "release_date": None, "release_date_text": None, "is_free": False,
             "price_cents": rng.choice([499, 999, 1299, 1499, 1499, 1999, 1999, 2499, 2999]),
             "developers": [rng.choice(STUDIOS)], "genres": ["Indie"], "tags": tags(),
             "header_image": None, "first_seen": start, **kw}
        a["publishers"] = a["developers"]
        apps.append(a)
        return a

    def page_change(a, d, field, old, new):
        changes.append((a["appid"], d, field, old, new))

    # ── upcoming games ──
    upcoming = []
    for i in range(130):
        a = new_app(coming_soon=True, release_date_text=rng.choice(
            ["Q4 2026", "Q1 2027", "Q2 2027", "2027", "Coming soon", "Nov 2026", "Dec 2026"]))
        base = int(math.exp(rng.gauss(7.3, 1.25)))
        drift = rng.gauss(0.004, 0.003)
        spike_day, spike_size, spike_len = None, 0, 0
        if i < 12:                                          # breakouts
            spike_day = rng.randint(DAYS - 14, DAYS - 3)
            spike_size = rng.uniform(0.35, 1.3) * (1.5 if base < 1500 else 1)
            spike_len = rng.randint(2, 6)
            why = rng.choice(["capsule", "trailer", "news"])
            sd = days[spike_day]
            if why == "capsule":
                page_change(a, sd - timedelta(days=rng.randint(1, 3)), "capsule",
                            f"header.jpg?t={rng.randint(10**9, 2*10**9)}", f"header.jpg?t={rng.randint(10**9, 2*10**9)}")
            elif why == "trailer":
                page_change(a, sd - timedelta(days=1), "trailers", "1 trailers", "2 trailers (+1 new)")
            elif why == "news":
                news.append((a["appid"], sd, "Our new gameplay trailer is out!", f"n{a['appid']}t"))
            if rng.random() < 0.5:
                page_change(a, sd + timedelta(days=rng.randint(0, 2)), "tags",
                            rng.choice(TAGS), rng.choice(TAGS))
        f = base
        for k, d in enumerate(days):
            f *= math.exp(drift + rng.gauss(0, 0.004))
            if spike_day is not None and spike_day <= k < spike_day + spike_len:
                f *= math.exp(spike_size / spike_len)
            snaps.append({"appid": a["appid"], "day": d, "followers": int(f)})
        a["_f"] = f
        upcoming.append(a)
        if rng.random() < 0.08:
            page_change(a, days[rng.randint(5, DAYS - 1)], "release_date", "Q4 2026", a["release_date_text"])

    # wishlist ranks: top by followers, recomputed every day
    by_day: dict[date, dict[int, int]] = {}
    for s in snaps:
        by_day.setdefault(s["day"], {})[s["appid"]] = s["followers"]
    rank_map = {}
    for d, fol in by_day.items():
        for pos, (aid, _) in enumerate(sorted(fol.items(), key=lambda kv: -kv[1])[:100], 1):
            rank_map[(aid, d)] = pos + (pos // 3) + (aid % 4) * 9   # bundles & big games interleave
    for s in snaps:
        s["wishlist_rank"] = rank_map.get((s["appid"], s["day"]))

    # ── demos for some upcoming games ──
    for i, g in enumerate(rng.sample(upcoming, 28)):
        d = new_app(type="demo", parent_appid=g["appid"], is_free=True, price_cents=None, tags=[],
                    name=f"{g['name']} Demo", developers=g["developers"])
        level = max(5, int(g["_f"] / rng.uniform(20, 120)))
        hot = i < 6
        for k, day in enumerate(days):
            if k < 20:
                continue
            boost = 1.0
            if hot and k > DAYS - 12:
                boost = 1 + (k - (DAYS - 12)) * rng.uniform(0.15, 0.45)
            snaps.append({"appid": d["appid"], "day": day,
                          "ccu_peak": max(1, int(level * boost * (1 + 0.25 * math.sin(k / 3.5)) * rng.uniform(0.85, 1.15)))})

    # ── launches in the last 60 days ──
    for i in range(34):
        rel = days[rng.randint(DAYS - 60, DAYS - 2)]
        a = new_app(release_date=rel, release_date_text=rel.strftime("%b %-d, %Y"))
        pre = int(math.exp(rng.gauss(7.6, 1.1)))
        quality = rng.choice([0.25, 0.5, 0.9, 1.0, 1.1, 1.4, 2.5, 3.5])
        reviews = 0
        f = pre * 0.7
        for k, d in enumerate(days):
            row = {"appid": a["appid"], "day": d}
            if d < rel:
                f = min(pre, f * 1.01 + 3)
                row["followers"] = int(f)
            else:
                age = (d - rel).days
                daily = pre * 0.012 * quality * math.exp(-age / 9) + pre * 0.0015 * quality
                reviews += max(0, int(daily * rng.uniform(0.7, 1.3)))
                f *= 1 + 0.01 * quality * math.exp(-age / 10)
                row.update(followers=int(f), reviews_total=reviews,
                           reviews_positive=int(reviews * rng.uniform(0.72, 0.93)),
                           ccu_peak=int(pre * 0.05 * quality * math.exp(-age / 12) * rng.uniform(0.8, 1.2)) + 3)
            snaps.append(row)
        news.append((a["appid"], rel, "Out now!", f"n{a['appid']}l"))
        if age_ok := (rel < days[-1] - timedelta(days=6)):
            news.append((a["appid"], rel + timedelta(days=rng.randint(3, 6)), "Patch 1.0.1 — fixes and QoL", f"n{a['appid']}p"))
        page_change(a, rel, "coming_soon", "coming soon", "released")

    # ── older released games (quiet earners and the rest) ──
    for i in range(140):
        rel = today - timedelta(days=rng.randint(380, 2200))
        a = new_app(release_date=rel, release_date_text=rel.strftime("%b %-d, %Y"))
        total = int(math.exp(rng.gauss(7.0, 1.2)))
        monthly = total * rng.uniform(0.004, 0.03)
        pos = rng.choice([0.68, 0.78, 0.84, 0.88, 0.91, 0.94, 0.96])
        ccu = max(3, int(total * rng.uniform(0.005, 0.03)))
        fol = int(total * rng.uniform(1.5, 4))
        reviews = total - monthly * DAYS / 30
        for k, d in enumerate(days):
            sale = autumn[0] <= d <= autumn[1]
            reviews += monthly / 30 * (2.5 if sale else 1) * rng.uniform(0.6, 1.4)
            row = {"appid": a["appid"], "day": d, "reviews_total": int(reviews),
                   "reviews_positive": int(reviews * pos)}
            if k % 7 == 0 or k > DAYS - 15:          # some days only swept weekly
                row["followers"] = int(fol + k * monthly / 30)
                row["ccu_peak"] = int(ccu * (1.8 if sale else 1) * rng.uniform(0.85, 1.15))
            snaps.append(row)
        if rng.random() < 0.5:
            page_change(a, autumn[0], "discount", "0%", f"{rng.choice([20, 25, 30, 40, 50])}%")
            page_change(a, autumn[1] + timedelta(days=1), "discount", "40%", "0%")
        if rng.random() < 0.25:
            d = days[rng.randint(0, DAYS - 1)]
            news.append((a["appid"], d, rng.choice(["Major update: new biome", "1.2 is live", "Free content update",
                                                     "Anniversary update"]), f"n{a['appid']}u"))

    # ── write ──
    with conn.cursor() as cur:
        cur.executemany("""
            insert into apps (appid, name, type, parent_appid, coming_soon, release_date, release_date_text,
              is_free, price_cents, developers, publishers, genres, tags, header_image, first_seen, watch)
            values (%(appid)s, %(name)s, %(type)s, %(parent_appid)s, %(coming_soon)s, %(release_date)s,
              %(release_date_text)s, %(is_free)s, %(price_cents)s, %(developers)s, %(publishers)s, %(genres)s,
              %(tags)s, %(header_image)s, %(first_seen)s, true)""", apps)
        cols = ["followers", "reviews_total", "reviews_positive", "ccu_peak", "wishlist_rank"]
        cur.executemany(f"""insert into daily_snapshots (appid, day, {', '.join(cols)})
                            values (%(appid)s, %(day)s, {', '.join('%(' + c + ')s' for c in cols)})""",
                        [{c: s.get(c) for c in ["appid", "day", *cols]} for s in snaps])
        cur.executemany("insert into page_changes (appid, day, field, old_value, new_value) values (%s,%s,%s,%s,%s)",
                        changes)
        cur.executemany("insert into events (appid, start_day, kind, title, ext_id) values (%s,%s,'news',%s,%s)",
                        news)

    export(conn, today)


def _json(o):
    """Match PostgREST: numerics become JSON numbers, dates become ISO strings."""
    from decimal import Decimal
    if isinstance(o, Decimal):
        return float(o)
    return o.isoformat() if hasattr(o, "isoformat") else str(o)


def rows(conn, sql, *args):
    cur = conn.execute(sql, args)
    names = [c.name for c in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def export(conn, today):
    out = {
        "generated_for": today,
        "breakouts": rows(conn, "select * from v_breakouts order by heat desc nulls last limit 150"),
        "demos": rows(conn, "select * from v_demo_heat order by week_ratio desc nulls last"),
        "launches": rows(conn, "select * from v_launches order by release_date desc"),
        "quiet": rows(conn, "select * from v_quiet_earners order by reviews_last_30d desc nulls last"),
        "tags": rows(conn, "select * from v_tags"),
        "apps": rows(conn, "select * from v_app_metrics"),
        "changes": rows(conn, "select appid, day, field, old_value, new_value from page_changes order by day"),
        "events": rows(conn, "select appid, start_day, end_day, kind, title, url from events order by start_day"),
    }
    snaps = rows(conn, """select appid, day, followers, reviews_total, ccu_peak, wishlist_rank
                          from daily_snapshots order by appid, day""")
    series: dict[int, list] = {}
    for s in snaps:
        series.setdefault(s["appid"], []).append(
            [s["day"].isoformat(), s["followers"], s["reviews_total"], s["ccu_peak"], s["wishlist_rank"]])
    out["series"] = series
    # sanity: the comparables function on real SQL, saved for a test in the dashboard
    out["sample_comparables"] = rows(conn, "select * from find_comparables(array['Cozy','Building','Relaxing'], 1499)")
    js = "window.DEMO_DATA = " + json.dumps(out, default=_json, separators=(",", ":")) + ";\n"
    (ROOT / "web" / "demo-data.js").write_text(js)
    print(f"demo-data.js: {len(js)/1024:.0f} KB | breakouts {len(out['breakouts'])} | demos {len(out['demos'])} | "
          f"launches {len(out['launches'])} | quiet {len(out['quiet'])} | apps {len(out['apps'])}")


if __name__ == "__main__":
    main(sys.argv[1])
