# IMD Radar 500km Karaikal test

A standalone preview build, forked out of
[`Chennai-Rains/IMD_Radar_Updates`](https://github.com/Chennai-Rains/IMD_Radar_Updates),
to try Karaikal on its real 500km-range PPZ product (`kkl_ppz`) instead of
`kkl_maxz`'s ~250km range, while NIOT and Kochi stay on their normal
products/ranges. This is a test product, not the live nowcast bot -- it
never touches `storm_forecast_map.html` or any of that repo's state.

## Running it

The workflow (`.github/workflows/nowcast_500km_test.yml`) is
**manual-dispatch only** for now -- trigger it from the Actions tab
whenever you want a fresh build. It's not on a recurring schedule yet; once
you're happy with a few days of manual checks, this can be switched to a
15-minute cron-job.org pinger the same way the live bot's `nowcast.yml`
is, with its own cron-job.org job pointed at this repo's
`workflow_dispatch` API endpoint.

Each run builds `output/storm_forecast_map_500km_test.html` from scratch
(its own isolated `state_500km_test/`/`archive_500km_test/`, never
committed or reused between runs) and uploads just that one file over FTP
to the same `plots.chennairains.com` directory the live bot uses --
alongside, not instead of, `storm_forecast_map.html`.

## Required repo secrets

Same FTP credentials as the live repo, added under **Settings → Secrets
and variables → Actions** here too:

- `FTP_HOST`
- `FTP_USERNAME`
- `FTP_PASSWORD`
- `FTP_REMOTE_DIR`

## Files

- `nowcast_bot.py` -- full pipeline, copied from `IMD_Radar_Updates` as of
  the Karaikal-timestamp-fix commit (`8cfef21`). `kkl_ppz`'s 500km
  calibration was already in here; this repo is just what wires it into a
  build.
- `test_500km_pipeline.py` -- the entry point: overrides
  `POLLED_PRODUCTS`, state/archive paths and the output filename, then
  calls `nowcast_bot.run_pipeline()`.
