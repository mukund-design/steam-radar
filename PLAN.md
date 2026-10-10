# Steam Radar — plan

A tool that snapshots public Steam signals every day and surfaces games that are
moving faster than their peers: pre-release breakouts, hot demos, launches that
beat or miss expectations, and quiet earners that sustain small studios.

Steam hides wishlists and sales. The tool tracks public proxies instead and
keeps its own history. **The history is the product** — start collecting early.

---

## 1. Signals

| Signal | Source | Use |
|---|---|---|
| Live players (CCU) | `api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid=` | Engagement; works for demos (own app IDs) |
| Review totals | `store.steampowered.com/appreviews/{appid}?json=1&num_per_page=0` | Sales proxy + sentiment |
| Followers | `steamcommunity.com/games/{appid}/memberslistxml/?xml=1` → `memberCount` | Wishlist proxy |
| Wishlist rank | `store.steampowered.com/search/results/?filter=popularwishlist&json=1&infinite=1` | Rank among most-wishlisted upcoming games |
| Charts | `ISteamChartsService/GetMostPlayedGames`, `store.steampowered.com/api/featuredcategories` | Discovery |
| Store page | `store.steampowered.com/api/appdetails` (~200 req / 5 min) | Metadata + change log |
| Tags | `steamspy.com/api.php?request=appdetails` | Genre/segment grouping, comparables |
| Update posts | `ISteamNews/GetNewsForApp/v2` | Event overlay |

Rough conversions (stored as estimates, never as facts):

- wishlists ≈ followers × 7–20 (median ~12×, GameDiscoverCo 2023)
- units sold ≈ reviews × 20–60 (default 30×, "Boxleiter"); developer net ≈ units × price × 0.5

SteamDB has no public API and forbids scraping — use it for manual checks only.

## 2. Collection tiers

1. **Discovery (daily)** — most played, top sellers, new releases, coming soon,
   popular-wishlist pages, popular-upcoming, newest releases. Anything seen
   joins the watchlist. Demos are discovered from each game's `demos` field.
2. **Watchlist (daily)** — followers, reviews, CCU, store page, wishlist rank.
   CCU is sampled every few hours and the daily peak is kept.
3. **Quiet-earner sweep (weekly)** — released games with 500+ reviews get their
   review totals refreshed even if they fall off the watchlist.

## 3. Detectors

- **Pre-release breakouts** — 7-day follower growth on a log scale, scored as a
  z-score against games in the same follower-size bucket (200→2k beats 50k→55k).
- **Demo heat** — 7-day peak CCU vs the previous 7 days, per demo.
- **Launch scorecard** — reviews and peak CCU after launch vs followers the day
  before launch. Reviews-per-follower vs the peer median gives beat / on track / miss.
- **Quiet earners** — released 1+ year, 1k+ reviews, ≥80% positive, still gaining
  reviews every month. Shows estimated units and net revenue.
- **Genre view** — all detectors filterable by tag.

## 4. New in this revision

### 4a. Store page change log
Every appdetails fetch is compared with the last stored state. Tracked fields:
name, capsule/header art (the `?t=` asset version), short description, price,
discount, release date text, coming-soon flag, trailer IDs, screenshot count,
genres, and (weekly, via SteamSpy) the top-10 tag list. Each difference becomes a
row in `page_changes`, shown on the game page and as markers on its charts.

*Question it answers:* "What did they change the week before followers jumped?"

### 4b. Event overlay
`events` holds:
- **Global events** — Steam sales and fests (seeded from Valve's published
  calendar in `collector/global_events.json`; add showcases yourself).
- **Per-game events** — update/announcement posts from GetNewsForApp, discounts
  and page changes.

Every game chart draws these as vertical markers with hover labels, so each
spike sits next to its likely cause.

### 4c. Comparable finder
`find_comparables(tags, price, release_after, limit)` scores every released game
by weighted tag overlap (top-tag positions weigh more) plus price closeness, and
returns outcomes: reviews, positive %, estimated units/revenue, peak CCU seen,
launch date. Usable from a game page ("games like this") or with a free-form
concept (pick tags + price).

*Question it answers:* "If we make X at $15, what happened to the 20 closest games?"

## 5. Stack

- **Collector:** Python (`collector/`), run by GitHub Actions cron. No server.
- **Database:** Postgres (Supabase free tier works). Detectors are SQL views.
- **Dashboard:** one static `web/index.html` (Cloudflare Pages / GitHub Pages /
  open locally). Reads through Supabase's REST API with the read-only anon key.
  Has a built-in demo mode with sample data.

## 6. Build order

- **Phase 1 (done in this repo):** schema + views, collector for all tiers,
  change log, event overlay, comparables, dashboard.
- **Phase 2:** daily digest (top movers per detector → Discord/email), alert
  thresholds, triage inbox (study / ignore / competitor), notes.
- **Phase 3:** Twitch viewer counts, achievement-based retention proxy, review
  text clustering + language mix, self-calibrating multipliers per genre,
  capsule-art trend board.

Steam Next Fest runs **Oct 19–26, 2026** — get the collector running before then
so the demo-heat detector has a baseline.
