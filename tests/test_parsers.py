"""Parsers against response shapes captured from the live endpoints (Oct 2026)."""
from datetime import date

from collector.changes import diff_states, diff_tags, page_state
from collector.steam import (parse_app_details, parse_current_players, parse_featured,
                             parse_members_xml, parse_most_played, parse_news, parse_release_date,
                             parse_reviews, parse_search_results, parse_steamspy_tags)

MEMBERS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<memberList>
  <groupID64>103582791465318237</groupID64>
  <groupDetails>
    <groupName><![CDATA[Hades]]></groupName>
    <groupURL><![CDATA[]]></groupURL>
    <headline><![CDATA[]]></headline>
    <summary><![CDATA[No information given.]]></summary>
    <memberCount>308216</memberCount>
    <membersInChat>695</membersInChat>
    <membersInGame>8244</membersInGame>
    <membersOnline>65449</membersOnline>
  </groupDetails>
  <memberCount>315240</memberCount>
  <totalPages>316</totalPages>
  <currentPage>1</currentPage>
  <startingMember>0</startingMember>
  <members><steamID64>76561197960287930</steamID64></members>
</memberList>"""

SEARCH_HTML = """
<a href="https://store.steampowered.com/app/1422450/Deadlock/?snr=1_7_7_7000_150_1"
   data-ds-appid="1422450" data-ds-itemkey="App_1422450" data-ds-tagids="[1,2]"
   data-ds-crtrids="[1]" onmouseover="x" class="search_result_row ds_collapse_flag " data-search-page="1">
  <div class="responsive_search_name_combined">Deadlock</div></a>
<a href="https://store.steampowered.com/sub/1686522/" data-ds-packageid="1686522"
   data-ds-appid="367500,2054970" class="search_result_row ds_collapse_flag ">bundle</a>
<a href="https://store.steampowered.com/app/2244130/Light_No_Fire/" class="search_result_row"
   data-ds-appid="2244130" data-ds-itemkey="App_2244130">Light No Fire</a>
"""

DETAILS = {"1145360": {"success": True, "data": {
    "type": "game", "name": "Hades", "steam_appid": 1145360, "is_free": False,
    "short_description": "Defy the god of the dead &amp; hack and slash out of the Underworld.",
    "header_image": "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1145360/header.jpg?t=1758127023",
    "developers": ["Supergiant Games"], "publishers": ["Supergiant Games"],
    "price_overview": {"currency": "USD", "initial": 2499, "final": 1249, "discount_percent": 50,
                       "initial_formatted": "$24.99", "final_formatted": "$12.49"},
    "genres": [{"id": "1", "description": "Action"}, {"id": "23", "description": "Indie"},
               {"id": "3", "description": "RPG"}],
    "release_date": {"coming_soon": False, "date": "Sep 17, 2020"},
    "movies": [{"id": 256785037}, {"id": 256785038}],
    "screenshots": [{"id": 0}, {"id": 1}, {"id": 2}],
    "demos": [{"appid": 1234560, "description": ""}],
}}}


def test_members_xml_prefers_group_details():
    assert parse_members_xml(MEMBERS_XML) == 308216
    assert parse_members_xml("<html>error</html>") is None
    assert parse_members_xml(None) is None


def test_search_results_keep_positions():
    assert parse_search_results(SEARCH_HTML) == [1422450, None, 2244130]


def test_app_details():
    d = parse_app_details(DETAILS, 1145360)
    assert d["name"] == "Hades" and d["type"] == "game"
    assert d["release_date"] == date(2020, 9, 17) and d["coming_soon"] is False
    assert d["price_cents"] == 2499 and d["final_cents"] == 1249 and d["discount_pct"] == 50
    assert d["capsule_version"] == "1758127023"
    assert d["genres"] == ["Action", "Indie", "RPG"]
    assert d["trailer_ids"] == ["256785037", "256785038"] and d["screenshot_count"] == 3
    assert d["demo_appids"] == [1234560]
    assert "&amp;" not in d["short_description"]
    assert parse_app_details({"5": {"success": False}}, 5) == {"missing": True}
    assert parse_app_details(None, 5) is None


def test_demo_details_link_parent():
    j = {"9": {"success": True, "data": {"type": "demo", "name": "X Demo",
                                         "fullgame": {"appid": "8", "name": "X"},
                                         "release_date": {"coming_soon": False, "date": "Oct 1, 2026"}}}}
    d = parse_app_details(j, 9)
    assert d["type"] == "demo" and d["parent_appid"] == 8


def test_release_dates():
    assert parse_release_date("Sep 17, 2020") == date(2020, 9, 17)
    assert parse_release_date("17 Sep, 2020") == date(2020, 9, 17)
    assert parse_release_date("Q1 2027") is None
    assert parse_release_date("Coming soon") is None
    assert parse_release_date("") is None


def test_reviews_players_charts_news_tags():
    assert parse_reviews({"success": 1, "query_summary": {
        "num_reviews": 0, "review_score": 9, "review_score_desc": "Overwhelmingly Positive",
        "total_positive": 303134, "total_negative": 6197, "total_reviews": 309331}}) == {
        "total": 309331, "positive": 303134, "score_desc": "Overwhelmingly Positive"}
    assert parse_reviews({"success": 2}) is None

    assert parse_current_players({"response": {"player_count": 42, "result": 1}}) == 42
    assert parse_current_players({"response": {"result": 42}}) is None

    assert parse_most_played({"response": {"rollup_date": 1, "ranks": [
        {"rank": 1, "appid": 730, "last_week_rank": 1, "peak_in_game": 1500000}]}}) == [
        {"appid": 730, "rank": 1, "peak_in_game": 1500000}]

    f = parse_featured({"top_sellers": {"items": [{"id": 1, "type": 0}, {"id": 2, "type": 1}, {"id": 1, "type": 0}]},
                        "coming_soon": {"items": []}, "status": 1})
    assert f["top_sellers"] == [1] and f["coming_soon"] == []

    n = parse_news({"appnews": {"newsitems": [{"gid": "55", "title": "Patch 1.1 &amp; more",
                                               "url": "u", "date": 1760000000}]}})
    assert n[0]["title"] == "Patch 1.1 & more" and n[0]["day"] == date(2025, 10, 9)

    assert parse_steamspy_tags({"appid": 1, "tags": {"Indie": 50, "Roguelike": 900, "Action": 300}}) == [
        "Roguelike", "Action", "Indie"]
    assert parse_steamspy_tags({"appid": 1, "tags": []}) == []
    assert parse_steamspy_tags(None) is None


def test_page_change_detection():
    a = page_state(parse_app_details(DETAILS, 1145360))
    assert diff_states(None, a) == []          # first sighting logs nothing
    assert diff_states(a, dict(a)) == []

    b = dict(a, capsule="1760000000",
             header_image=a["header_image"].replace("1758127023", "1760000000"),
             discount=0, trailers=a["trailers"] + ["999"], name="Hades: Remastered")
    fields = {r["field"]: r for r in diff_states(a, b)}
    assert set(fields) == {"capsule", "discount", "trailers", "name"}
    assert fields["discount"]["old_value"] == "50%" and fields["discount"]["new_value"] == "0%"
    assert fields["trailers"]["new_value"] == "3 trailers (+1 new)"
    assert "1760000000" in fields["capsule"]["new_value"]

    # trailer reorder is not a change
    assert diff_states(a, dict(a, trailers=list(reversed(a["trailers"])))) == []


def test_tag_diff():
    old = ["Roguelike", "Action", "Indie"]
    assert diff_tags(old, ["Action", "Roguelike", "Indie"]) is None
    assert diff_tags(old, ["Roguelike", "Action", "Cozy"]) == {
        "field": "tags", "old_value": "Indie", "new_value": "Cozy"}
    assert diff_tags(None, old) is None


# ── surges update ───────────────────────────────────────────────────────

from collector.steam import classify_event, parse_histogram, parse_partner_events


def test_histogram():
    j = {"success": 1, "results": {"start_date": 1, "end_date": 2, "weeks": [], "rollup_type": "month",
         "rollups": [{"date": 1577836800, "recommendations_up": 4820, "recommendations_down": 76},
                     {"date": 1575158400, "recommendations_up": 5652, "recommendations_down": 65}],
         "recent": [{"date": 1788220800, "recommendations_up": 30, "recommendations_down": 0}]}}
    h = parse_histogram(j)
    assert h["rollup_type"] == "month"
    assert h["rollups"] == [["2019-12-01", 5652, 65], ["2020-01-01", 4820, 76]]   # sorted by day
    assert h["recent"] == [["2026-09-01", 30, 0]]
    assert parse_histogram({"success": 2}) is None


def test_partner_events():
    j = {"success": 1, "events": [
        {"gid": "8", "appid": 1145360, "event_type": 12, "event_name": "Post-Launch Patch",
         "rtime32_start_time": 1644962700, "announcement_body": {"gid": "9", "headline": "Post-Launch Patch &amp; fixes"}},
        {"gid": "6", "appid": 1145360, "event_type": 34, "event_name": "Hades II v1.0 Is Now Available!",
         "rtime32_start_time": 1758816660, "announcement_body": {"gid": "7", "headline": "Hades II v1.0 Is Now Available!"}},
        {"gid": "4", "appid": 1145360, "event_type": 14, "hidden": 1, "rtime32_start_time": 1, "announcement_body": {"gid": "5"}},
    ]}
    ev = parse_partner_events(j, 1145360)
    assert [(e["gid"], e["category"]) for e in ev] == [("9", "patch"), ("7", "crosspost")]
    assert ev[0]["title"] == "Post-Launch Patch & fixes" and ev[0]["day"] == date(2022, 2, 15)
    assert parse_partner_events({"success": 0}, 1) is None


def test_classify_event():
    assert classify_event(14, "anything") == "major_update"
    assert classify_event(15, "x") == "dlc" and classify_event(31, "x") == "free_weekend"
    assert classify_event(28, "Version 1.0 is out now!") == "release"
    assert classify_event(28, "Patch v1.0.3") == "patch"
    assert classify_event(28, "Content update out now") == "major_update"
    assert classify_event(28, "Autumn Sale: 40% off") == "sale"
    assert classify_event(None, "We won an award") == "news"


def test_early_access_and_dlc():
    j = {"7": {"success": True, "data": {"type": "game", "name": "X", "dlc": [1, 2],
          "genres": [{"id": "23", "description": "Indie"}, {"id": "70", "description": "Early Access"}],
          "release_date": {"coming_soon": False, "date": "Mar 1, 2024"}}}}
    d = parse_app_details(j, 7)
    assert d["early_access"] is True and d["dlc_count"] == 2
    a = page_state(d)
    assert a["genres"] == ["Indie"]                       # Early Access has its own field
    b = page_state({**d, "early_access": False, "dlc_count": 3, "genres": ["Indie"]})
    rows = {r["field"]: r for r in diff_states(a, b)}
    assert set(rows) == {"early_access", "dlc"}
    assert rows["early_access"]["new_value"] == "Full release"
    assert rows["dlc"]["new_value"] == "3 DLC (+1 new)"
    # a state saved by an older version has no early_access/dlc and kept EA in genres: no false changes
    old = {k: v for k, v in a.items() if k not in ("early_access", "dlc")}
    old["genres"] = ["Early Access", "Indie"]
    assert diff_states(old, a) == []
