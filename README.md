# Steam Radar

Tracks Steam games that are taking off — pre-release breakouts, hot demos,
launches that beat or miss expectations, and quiet earners — plus *why* they
moved: store-page changes, updates and Steam events drawn on every timeline,
and a comparable finder for game ideas. See [PLAN.md](PLAN.md) for the reasoning.

```
collector/   Python job that reads Steam and writes snapshots (runs on GitHub Actions)
db/          schema.sql — tables, detector views, comparables function
web/         index.html — the dashboard (static, no build step)
tools/       make_demo.py — builds the sample data the dashboard shows before you connect
tests/       parser tests + an end-to-end test against a real Postgres
```

Open `web/index.html` in a browser to try it with invented sample data first.

## Setup (about 20 minutes)

### 1. Database
1. Create a free project at [supabase.com](https://supabase.com).
2. Open **SQL Editor**, paste all of `db/schema.sql`, run it. (Re-running it later is safe.)
3. From **Connect**, copy the **Session pooler** connection string (port 5432) and fill
   in your database password. Use the pooler string: GitHub's runners need IPv4, and
   Supabase's direct connection address is IPv6.
4. From **Project Settings → API Keys**, note the **Project URL** and the **publishable** key
   (`sb_publishable_…`). An older `anon` key also works.

### 2. Collector on GitHub Actions
1. Push this folder to a GitHub repository.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   `DATABASE_URL` = the pooler connection string.
3. **Actions** tab → *daily snapshot* → **Run workflow**. The first run discovers
   ~1,000–2,000 games and takes 30–90 minutes.
4. **Actions** → *quiet-earner sweep* → **Run workflow** with command `seed-universe`
   (about an hour; SteamSpy allows one page per minute), then once more with `sweep`.

After that everything is scheduled:

| Workflow | When | What |
|---|---|---|
| daily snapshot | every day 04:17 UTC | feeds → watchlist, wishlist ranks, followers, reviews, store pages, update posts, tags |
| player counts | every 4 hours | concurrent players; the day's peak is kept |
| quiet-earner sweep | Sundays | review counts for released games with 500+ reviews |
| (same workflow) | 1st of the month | refresh that list from SteamSpy |

Public repositories get unlimited Actions minutes. On a private repository the
free plan has a monthly minute allowance; lower `WATCH_LIMIT` (a repository
*variable*, default 2000) if you run short.

### 3. Dashboard
Host the `web/` folder anywhere static: Cloudflare Pages, GitHub Pages, Netlify,
or just open the file. Then either:
- click **Connect your database** and paste the Project URL and publishable key
  (saved in that browser), or
- copy `web/config.example.js` to `web/config.js` and fill it in, so every visitor
  of your deployment sees live data.

The publishable/anon key is read-only: `schema.sql` grants it `select` and nothing else.

## What you'll see, and when

| View | Needs |
|---|---|
| Comparables, quiet earners (totals) | 1st sweep |
| Breakouts, demo heat | 7 days of daily runs |
| Quiet earners "last 30 days" | 4–5 weekly sweeps |
| Launch scorecard | a game you watched before it launched |
| Store page changes | the second check of a page onward |

Steam Next Fest runs 19–26 October 2026. Starting before then gives demo heat
a baseline week.

## Running locally

```bash
pip install -r collector/requirements.txt
python -m collector.run probe 1145360          # prints every signal for one game, no database
export DATABASE_URL="postgresql://..."
python -m collector.run daily
```

Environment settings (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `WATCH_LIMIT` | 2000 | games tracked daily |
| `WISHLIST_PAGES` | 10 | ×100 rows of the most-wishlisted list |
| `NEW_RELEASE_PAGES` | 3 | ×100 newest releases scanned per day |
| `SWEEP_BATCH` | 3000 | quiet-earner candidates refreshed per sweep |
| `UNIVERSE_MIN_REVIEWS` | 500 | SteamSpy cut-off for the quiet-earner list |
| `STEAM_CC` | us | store region (prices are stored in USD; keep `us`) |

Tests: `pytest tests` runs the parser tests. Set `TEST_DATABASE_URL` to an empty,
throwaway Postgres database to also run the end-to-end collector test.

## Tuning the estimates

The multipliers live in three SQL functions at the top of `db/schema.sql`:
`est_wishlists` (followers × 12), `est_units` (reviews × 30) and `est_net_usd`
(units × list price × 0.5). Change them there and re-run the file; every view and
the dashboard follow.

## Adding events

Steam sales and fests through July 2027 are in `collector/global_events.json` and
load on every daily run. Add showcases or anything else you want marked on the
charts in the same format (`"kind": "showcase"` for non-Steam events).

## Limits worth knowing

- Follower counts come from each game's Steam community group. It is a public
  page, not an official API, so it could change.
- Tags come from SteamSpy and lag the store by days to weeks; tag changes in the
  change log are weekly at best.
- Steam's store API allows about 200 page lookups per 5 minutes. Store pages are
  refreshed daily for upcoming games, demos and recent launches, and every three
  days for everything else.
- Reviews are counted across all purchase types, which is what the review-to-sales
  multipliers were calibrated on. Free games get far more reviews per player, so
  they are left out of revenue estimates.
- SteamDB forbids automated access. Use it by hand — game panels link to it.
