"""Thin, polite client for the public Steam endpoints Steam Radar uses.

Every host has its own rate limiter, so phases that hit different hosts can run
in parallel threads without tripping each other's limits. Parsing lives in
plain functions (parse_*) so it can be tested against saved responses.
"""
from __future__ import annotations

import html
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from typing import Any

import requests

log = logging.getLogger("steam")

UA = "SteamRadar/0.1 (personal market research; github.com/steam-radar)"

# Seconds between requests, per host. appdetails is the strict one
# (~200 requests per 5 minutes), so it gets its own bucket.
DEFAULT_DELAYS = {
    "store_api": 1.6,     # store.steampowered.com/api/appdetails
    "store": 0.8,         # other store.steampowered.com pages (reviews, search, charts)
    "community": 1.0,     # steamcommunity.com
    "webapi": 0.25,       # api.steampowered.com
    "steamspy": 1.1,      # steamspy.com (1 req/s; "all" pages are 1/min)
    "store_hist": 0.8,    # store review history (appreviewhistogram)
    "store_events": 1.0,  # store event posts (ajaxgetadjacentpartnerevents)
}


class RateLimiter:
    def __init__(self, delay: float):
        self.delay = delay
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
            self._next = time.monotonic() + self.delay

    def back_off(self, seconds: float) -> None:
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


class SteamClient:
    def __init__(self, cc: str = "us", delays: dict[str, float] | None = None,
                 timeout: float = 20.0, max_tries: int = 4):
        self.cc = cc
        self.timeout = timeout
        self.max_tries = max_tries
        d = {**DEFAULT_DELAYS, **(delays or {})}
        self.limiters = {k: RateLimiter(v) for k, v in d.items()}
        self._local = threading.local()

    # One requests.Session per thread.
    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = UA
            # Skip the store's age gate for mature games.
            s.cookies.set("birthtime", "283993201", domain="store.steampowered.com")
            s.cookies.set("mature_content", "1", domain="store.steampowered.com")
            self._local.session = s
        return s

    def get(self, bucket: str, url: str, params: dict | None = None) -> requests.Response | None:
        """GET with rate limiting and retries. Returns None after giving up."""
        limiter = self.limiters[bucket]
        for attempt in range(1, self.max_tries + 1):
            limiter.wait()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                log.warning("%s %s: %s (try %d)", bucket, url, e, attempt)
                limiter.back_off(5 * attempt)
                continue
            if r.status_code == 200:
                return r
            if r.status_code in (429, 403, 500, 502, 503, 504):
                wait = 60 if r.status_code in (429, 403) else 10 * attempt
                log.warning("%s %s -> HTTP %d, backing off %ds", bucket, url, r.status_code, wait)
                limiter.back_off(wait)
                continue
            log.info("%s %s -> HTTP %d", bucket, url, r.status_code)
            return None
        return None

    def get_json(self, bucket: str, url: str, params: dict | None = None) -> Any:
        r = self.get(bucket, url, params)
        if r is None:
            return None
        try:
            return r.json()
        except ValueError:
            log.info("non-JSON from %s", url)
            return None

    # ── discovery ────────────────────────────────────────────────────────

    def most_played(self) -> list[dict]:
        """Weekly most-played chart: [{appid, rank, peak_in_game}]"""
        j = self.get_json("webapi", "https://api.steampowered.com/ISteamChartsService/GetMostPlayedGames/v1/")
        return parse_most_played(j)

    def featured(self) -> dict[str, list[int]]:
        """Store front charts: {'top_sellers': [...], 'new_releases': [...], 'coming_soon': [...], ...}"""
        j = self.get_json("store", "https://store.steampowered.com/api/featuredcategories",
                          {"cc": self.cc, "l": "english"})
        return parse_featured(j)

    def search_feed(self, pages: int = 1, per_page: int = 100, **query) -> list[int]:
        """Store search as an ordered app id list. Position = rank.

        query examples: filter='popularwishlist' | 'popularcomingsoon' | 'topsellers',
                        sort_by='Released_DESC', category1='998' (games only)
        Bundles/packages keep their position (they are part of Steam's ranking)
        but are returned as None.
        """
        out: list[int | None] = []
        for p in range(pages):
            params = {"json": 1, "infinite": 1, "start": p * per_page, "count": per_page,
                      "cc": self.cc, "l": "english", "ignore_preferences": 1, **query}
            j = self.get_json("store", "https://store.steampowered.com/search/results/", params)
            if not j or not j.get("results_html"):
                break
            rows = parse_search_results(j["results_html"])
            out.extend(rows)
            if len(rows) < per_page or p * per_page + len(rows) >= int(j.get("total_count") or 0):
                break
        return out

    # ── per app ──────────────────────────────────────────────────────────

    def app_details(self, appid: int) -> dict | None:
        j = self.get_json("store_api", "https://store.steampowered.com/api/appdetails",
                          {"appids": appid, "cc": self.cc, "l": "english"})
        return parse_app_details(j, appid)

    def reviews_summary(self, appid: int) -> dict | None:
        j = self.get_json("store", f"https://store.steampowered.com/appreviews/{appid}",
                          {"json": 1, "language": "all", "purchase_type": "all",
                           "num_per_page": 0, "filter": "all"})
        return parse_reviews(j)

    def followers(self, appid: int) -> int | None:
        r = self.get("community", f"https://steamcommunity.com/games/{appid}/memberslistxml/",
                     {"xml": 1})
        return parse_members_xml(r.text) if r is not None else None

    def current_players(self, appid: int) -> int | None:
        j = self.get_json("webapi", "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/",
                          {"appid": appid})
        return parse_current_players(j)

    def news(self, appid: int, count: int = 10) -> list[dict]:
        """Developer announcements only (patch notes, updates, events)."""
        j = self.get_json("webapi", "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
                          {"appid": appid, "count": count, "maxlength": 1,
                           "feeds": "steam_community_announcements"})
        return parse_news(j)

    def review_histogram(self, appid: int) -> dict | None:
        """Reviews per month (or week) since launch, plus daily counts for the last 30 days."""
        j = self.get_json("store_hist", f"https://store.steampowered.com/appreviewhistogram/{appid}",
                          {"l": "english", "review_score_preference": 0})
        return parse_histogram(j)

    def partner_events(self, appid: int, count: int = 30) -> list[dict] | None:
        """The developer's event posts, newest first, each with Steam's event type
        (patch, update, major update, DLC, sale, free weekend, ...). None if the request failed."""
        j = self.get_json("store_events", "https://store.steampowered.com/events/ajaxgetadjacentpartnerevents/",
                          {"appid": appid, "count_before": 0, "count_after": count, "lang_list": 0})
        return parse_partner_events(j, appid)

    def steamspy_tags(self, appid: int) -> list[str] | None:
        j = self.get_json("steamspy", "https://steamspy.com/api.php",
                          {"request": "appdetails", "appid": appid})
        return parse_steamspy_tags(j)

    def steamspy_all_page(self, page: int) -> list[dict]:
        """~1000 games per page, sorted by owners. SteamSpy allows 1 call per minute."""
        self.limiters["steamspy"].back_off(60)
        j = self.get_json("steamspy", "https://steamspy.com/api.php", {"request": "all", "page": page})
        if not isinstance(j, dict):
            return []
        return [v for v in j.values() if isinstance(v, dict) and v.get("appid")]


# ── parsers (pure functions) ─────────────────────────────────────────────

def parse_most_played(j: Any) -> list[dict]:
    ranks = ((j or {}).get("response") or {}).get("ranks") or []
    out = []
    for r in ranks:
        if r.get("appid"):
            out.append({"appid": int(r["appid"]), "rank": r.get("rank"),
                        "peak_in_game": r.get("peak_in_game")})
    return out


def parse_featured(j: Any) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    if not isinstance(j, dict):
        return out
    for key in ("top_sellers", "new_releases", "coming_soon", "specials"):
        items = (j.get(key) or {}).get("items") or []
        ids = [int(i["id"]) for i in items if isinstance(i, dict) and i.get("id") and i.get("type", 0) == 0]
        out[key] = list(dict.fromkeys(ids))
    return out


_ROW_RE = re.compile(r"<a\b[^>]*\bclass=\"[^\"]*search_result_row[^\"]*\"[^>]*>", re.I)
_ATTR_RE = re.compile(r'(data-ds-appid|data-ds-packageid|data-ds-bundleid)="([^"]*)"', re.I)


def parse_search_results(results_html: str) -> list[int | None]:
    """App id per search row, in order. Package/bundle rows become None."""
    out: list[int | None] = []
    for tag in _ROW_RE.findall(results_html or ""):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        if "data-ds-packageid" in attrs or "data-ds-bundleid" in attrs:
            out.append(None)
            continue
        first = (attrs.get("data-ds-appid") or "").split(",")[0].strip()
        out.append(int(first) if first.isdigit() else None)
    return out


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_release_date(text: str | None) -> date | None:
    """Steam shows 'Sep 17, 2020', '17 Sep, 2020', 'September 2026', 'Q1 2027', '2027', 'Coming soon'.
    Only full day-precision dates become a date; vaguer text stays text."""
    if not text:
        return None
    t = text.strip().replace(".", "")
    for fmt in ("%b %d, %Y", "%d %b, %Y", "%B %d, %Y", "%d %B, %Y", "%Y-%m-%d", "%b %d %Y", "%d %b %Y"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            pass
    return None


def parse_app_details(j: Any, appid: int) -> dict | None:
    """Normalise appdetails into the fields Steam Radar stores.
    Returns {'missing': True} when Steam has no store page for the id."""
    entry = (j or {}).get(str(appid)) if isinstance(j, dict) else None
    if entry is None:
        return None                       # request failed; try again later
    if not entry.get("success"):
        return {"missing": True}
    d = entry.get("data") or {}
    rel = d.get("release_date") or {}
    price = d.get("price_overview") or {}
    fullgame = d.get("fullgame") or {}
    header = d.get("header_image") or ""
    m = re.search(r"[?&]t=(\d+)", header)
    return {
        "missing": False,
        "name": html.unescape(d.get("name") or "") or None,
        "type": d.get("type") or "game",
        "parent_appid": int(fullgame["appid"]) if str(fullgame.get("appid", "")).isdigit() else None,
        "coming_soon": bool(rel.get("coming_soon")),
        "release_date_text": rel.get("date") or None,
        "release_date": parse_release_date(rel.get("date")),
        "is_free": bool(d.get("is_free")),
        # USD list price only when the store answered in USD
        "price_cents": price.get("initial") if price.get("currency") in (None, "USD") else None,
        "final_cents": price.get("final"),
        "discount_pct": price.get("discount_percent") or 0,
        "developers": d.get("developers") or [],
        "publishers": d.get("publishers") or [],
        "genres": [g.get("description") for g in d.get("genres") or [] if g.get("description")],
        "header_image": header or None,
        "capsule_version": m.group(1) if m else None,
        "short_description": html.unescape(d.get("short_description") or ""),
        "trailer_ids": sorted(str(mv.get("id")) for mv in d.get("movies") or [] if mv.get("id")),
        "screenshot_count": len(d.get("screenshots") or []),
        "demo_appids": [int(x["appid"]) for x in d.get("demos") or [] if str(x.get("appid", "")).isdigit()],
        # Steam lists Early Access as genre 70; when it disappears the game has hit 1.0
        "early_access": any(str(g.get("id")) == "70" or g.get("description") == "Early Access"
                            for g in d.get("genres") or []),
        "dlc_count": len(d.get("dlc") or []),
    }


def parse_reviews(j: Any) -> dict | None:
    if not isinstance(j, dict) or not j.get("success"):
        return None
    q = j.get("query_summary") or {}
    if "total_reviews" not in q:
        return None
    return {"total": int(q.get("total_reviews") or 0),
            "positive": int(q.get("total_positive") or 0),
            "score_desc": q.get("review_score_desc")}


def parse_members_xml(text: str | None) -> int | None:
    """Follower count = member count of the game's community group."""
    if not text or "<memberList" not in text:
        return None
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        m = re.search(r"<memberCount>(\d+)</memberCount>", text)
        return int(m.group(1)) if m else None
    for path in ("groupDetails/memberCount", "memberCount"):
        el = root.find(path)
        if el is not None and (el.text or "").strip().isdigit():
            return int(el.text.strip())
    return None


def parse_current_players(j: Any) -> int | None:
    resp = (j or {}).get("response") if isinstance(j, dict) else None
    if not resp or resp.get("result") != 1:
        return None                       # unreleased / no data
    return int(resp.get("player_count") or 0)


def parse_news(j: Any) -> list[dict]:
    items = (((j or {}).get("appnews") or {}).get("newsitems")) if isinstance(j, dict) else None
    out = []
    for n in items or []:
        if not n.get("gid") or not n.get("date"):
            continue
        out.append({
            "gid": str(n["gid"]),
            "title": html.unescape(n.get("title") or "Update"),
            "url": n.get("url"),
            "day": datetime.fromtimestamp(int(n["date"]), tz=timezone.utc).date(),
            "event_type": None,
            "category": classify_event(None, n.get("title")),
        })
    return out


def parse_steamspy_tags(j: Any) -> list[str] | None:
    if not isinstance(j, dict) or not j.get("appid"):
        return None
    tags = j.get("tags")
    if isinstance(tags, dict):
        return [t for t, _ in sorted(tags.items(), key=lambda kv: -int(kv[1] or 0))]
    return []          # SteamSpy returns [] when a game has no tags


def _day(ts) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def parse_histogram(j: Any) -> dict | None:
    """{'rollup_type': 'month'|'week', 'rollups': [[day, up, down], ...], 'recent': [[day, up, down], ...]}"""
    if not isinstance(j, dict) or j.get("success") != 1 or not isinstance(j.get("results"), dict):
        return None
    r = j.get("results") or {}

    def rows(items):
        out = []
        for x in items or []:
            d = _day(x.get("date")) if isinstance(x, dict) else None
            if d:
                out.append([d, int(x.get("recommendations_up") or 0), int(x.get("recommendations_down") or 0)])
        return sorted(out)

    return {"rollup_type": r.get("rollup_type") or "month",
            "rollups": rows(r.get("rollups")), "recent": rows(r.get("recent"))}


# Steam's event types, from EProtoClanEventType in Steam's published protobufs.
EVENT_TYPES = {
    10: "release", 12: "patch", 13: "update", 14: "major_update", 15: "dlc",
    16: "announcement", 20: "sale", 21: "sale", 29: "update", 30: "major_update",
    31: "free_weekend", 32: "major_update", 33: "update", 34: "crosspost",
    11: "stream", 17: "stream", 18: "stream", 19: "stream",
    22: "in_game_event", 23: "in_game_event", 24: "in_game_event", 25: "in_game_event",
    26: "in_game_event", 35: "in_game_event", 28: "news",
}

_KEYWORDS = [   # used only for plain news posts and posts without a type
    ("release", re.compile(r"(\bv?1\.0(?![.\d])|\bfull release|\bleav(es|ing) early access|\bout of early access|"
                           r"\b(launch(es|ed)?|releas(es|ed)) (today|on steam)\b)", re.I)),
    ("free_weekend", re.compile(r"free (weekend|to play this weekend|play days?)|play (it )?for free", re.I)),
    ("dlc", re.compile(r"\b(dlc|expansion|soundtrack|season pass)\b", re.I)),
    ("sale", re.compile(r"\b(sale|\d{1,2}\s?% off|discount(ed)?)\b", re.I)),
    ("major_update", re.compile(r"\b(major|big|huge|content|free) update\b|\bupdate \d|\bnew (chapter|biome|season|map|mode)\b", re.I)),
    ("patch", re.compile(r"\b(hot ?fix|patch(notes)?|bug ?fix(es)?)\b", re.I)),
    ("update", re.compile(r"\bupdate\b", re.I)),
]


def classify_event(event_type: int | None, title: str | None) -> str:
    """Category of a developer post: Steam's own type first, the title as a fallback."""
    cat = EVENT_TYPES.get(event_type or 0)
    if cat and cat != "news":
        return cat
    for name, rx in _KEYWORDS:
        if rx.search(title or ""):
            return name
    return "news"


def parse_partner_events(j: Any, appid: int) -> list[dict] | None:
    if not isinstance(j, dict) or j.get("success") != 1:
        return None
    out = []
    for e in j.get("events") or []:
        if not isinstance(e, dict) or e.get("hidden") or e.get("published") == 0:
            continue
        if e.get("appid") and int(e["appid"]) != appid:
            continue
        body = e.get("announcement_body") or {}
        title = html.unescape(body.get("headline") or e.get("event_name") or "Update")
        day = _day(e.get("rtime32_start_time") or body.get("posttime"))
        gid = str(body.get("gid") or e.get("gid") or "")
        if not day or not gid:
            continue
        etype = int(e["event_type"]) if str(e.get("event_type", "")).isdigit() else None
        out.append({
            "gid": gid, "title": title, "day": date.fromisoformat(day), "event_type": etype,
            "category": classify_event(etype, title),
            "url": f"https://store.steampowered.com/news/app/{appid}/view/{gid}",
        })
    return out
