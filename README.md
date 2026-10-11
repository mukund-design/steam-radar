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
4. **Actions** → *older games sweep* → **Run workflow** with command `seed-universe`
   (about an hour; SteamSpy allows one page per minute). The daily sweep takes it from there.

After that everything is scheduled:

| Workflow | When | What |
|---|---|---|
| daily snapshot | every day 04:17 UTC | feeds → watchlist, wishlist ranks, followers, reviews, review history, store pages, typed update posts, tags; then a store-page and post check for every surging game |
| player counts | every 4 hours | concurrent players; the day's peak is kept |
| older games sweep | every day 09:23 UTC | review totals and review history for a seventh of the older games (each one weekly), then the same surge check |
| (same workflow) | 1st of the month | add games that newly passed 500 reviews (nothing is removed) |

The defaults assume a **public** repository, which gets unlimited Actions minutes:
2,000 games tracked daily and 3,500 older games swept a day. Tune with repository *variables*
(Settings → Secrets and variables → Actions → Variables): `WATCH_LIMIT` and `SWEEP_BATCH`.

For a **private** repository (2,000 free minutes a month on GitHub Free), set
`WATCH_LIMIT` to 1000, and change the schedules to every 8 hours in `ccu.yml`
(`41 */8 * * *`) and to weekly in `weekly.yml` (`23 9 * * 0`, with `SWEEP_BATCH` 2000).
That comes to roughly 1,200–1,400 minutes a month, with older games re-checked less often.

### 3. Dashboard
Host the `web/` folder anywhere static: Cloudflare Pages, GitHub Pages, Netlify,
or just open the file. Then either:
- click **Connect your database** and paste the Project URL and publishable key
  (saved in that browser), or
- copy `web/config.example.js` to `web/config.js` and fill it in, so every visitor
  of your deployment sees live data.

The publishable/anon key is read-only: `schema.sql` grants it `select` and nothing else.

## The tabs

| Tab | What it answers |
|---|---|
| **Overview** | The day's digest: what's breaking out and what changed on Steam just before, genres heating up and cooling, hot demos, recent launches, new wishlist-chart entries, capsule/trailer/tag changes, quiet earners. |
| **Genres** | Which genres are doing well. Per genre (a game counts under its five main tags): follower growth of its upcoming games against the typical upcoming game, breakouts, hot demos, launch results, reviews gained this week, quiet earners. Click a genre for its games and the tags it's most often paired with. |
| **Surges** | Released games suddenly picking up reviews or players (2× their usual week or more), with the likely trigger: left Early Access / 1.0, new DLC, major update, free weekend, discount, Steam sale or fest, store page change, patch, a wave of negative reviews, or nothing visible on Steam. A bar on top shows what came before surges over the last 30 days. |
| Breakouts, Demo heat, Launches, Quiet earners | Full lists behind the overview panels. |
| Comparables | How the closest released games did, for any set of tags and a price. |

Click any game for its timeline (with Steam events, update posts sized by importance
and store-page changes marked), including **Since launch**: reviews per month back to
release, from Steam's own review history. Click any tag for its genre panel.

### How surges and causes work
- Steam publishes each game's reviews per day for the last 30 days. A surge is a week
  with at least 15 reviews and at least twice the average of the three weeks before,
  or (for games watched daily) peak players 1.5× last week's and at least 50.
  Games launched in the last 30 days are left to the Launches tab.
- When a game surges, the collector re-reads its store page and its posts the same day.
  Posts carry Steam's own type (patch, update, major update, DLC, sale, free trial…);
  plain news posts are typed from their title.
- The likely cause is the most specific thing that happened from 10 days before the
  surge week to its end, in this order: left Early Access, new DLC, major update,
  free weekend, discount, Steam sale, update, store page change, Steam fest, patch.
  A surge made mostly of negative reviews is labelled that way first.
- These are coincidences in time, not proof. "Nothing on Steam" usually means a video,
  stream or post elsewhere.

## What you'll see, and when

| View | Needs |
|---|---|
| Comparables, quiet earners (totals) | the first sweeps after `seed-universe` |
| Surges, Since launch chart | the first daily run (watched games); within a week for older games |
| Breakouts, demo heat, genre heat | 7 days of daily runs |
| Quiet earners "last 30 days" | about a month |
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
| `SWEEP_BATCH` | 3500 | older games refreshed per daily sweep |
| `UNIVERSE_MIN_REVIEWS` | 500 | SteamSpy cut-off for the quiet-earner list |
| `STEAM_CC` | us | store region (prices are stored in USD; keep `us`) |

Tests: `pytest tests` runs the parser tests. Set `TEST_DATABASE_URL` to an empty,
throwaway Postgres database to also run the end-to-end collector test.

## History and storage

Nothing is ever deleted or replaced. Every reading is a new dated row, the monthly job
only adds games, and Steam's review history goes back to each game's launch. A year of
data at the default settings is roughly 300 MB, inside Supabase's free 500 MB, so about
a year and a half before older daily rows need thinning (for example to weekly).

## Updating to a new version

1. In Supabase's **SQL Editor**, run the new `db/schema.sql`. It keeps all your data
   and rebuilds the views (takes a few seconds).
2. Replace the files in your GitHub repository with the new ones and commit.
3. Reload the dashboard.

If step 1 is skipped, the dashboard keeps working and the newer tabs say what's missing.

## How the dashboard stays fast

Per-game numbers are computed once at the end of every collector run and cached in
`mv_app_metrics` (`select refresh_metrics()`). Every tab reads that cache, so the
page stays quick as history grows. If you load data by hand, run
`select refresh_metrics();` afterwards. Supabase's Security Advisor may list this
cached view as visible to the API; that is intended, since it holds the same public
Steam numbers as the rest of the dashboard.

## Tuning the estimates

The multipliers live in three SQL functions at the top of `db/schema.sql`:
`est_wishlists` (followers × 12), `est_units` (reviews × 30) and `est_net_usd`
(units × list price × 0.5). Change them there and re-run the file; every view and
the dashboard follow.

Generic tags that aren't genres (Indie, Singleplayer, Early Access and so on) are
skipped when grouping games into genres. The list is `genre_ignored_tags()` in the
same file.

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
