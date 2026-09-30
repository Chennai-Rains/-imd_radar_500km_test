# IMD Radar 500km Karaikal test

A standalone preview build, forked out of
[`Chennai-Rains/IMD_Radar_Updates`](https://github.com/Chennai-Rains/IMD_Radar_Updates),
to try Karaikal as a HYBRID of its two real IMD products: `kkl_maxz`
(full resolution, 0-250km) for the inner disc, plus `kkl_ppz` (coarser,
500km) for just the 250-500km ring `kkl_maxz`'s own image can't reach --
so the map gets genuine 500km reach without giving up `kkl_maxz`'s sharper
resolution close in. NIOT and Kochi stay on their normal products/ranges.
This is a test product, not the live nowcast bot -- it never touches
`storm_forecast_map.html` or any of that repo's state.

`kkl_ppz`'s `PRODUCTS` entry carries `mask_within_km: 250.0`, which makes
`decode_reflectivity()` NaN out its own inner 250km disc before it ever
reaches the map or cell extraction -- so it only ever contributes the
extended ring, never duplicates/competes with `kkl_maxz` in the region
they both technically cover. On the page, that masked product shows up
labeled "Karaikal Extended Radar".

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

- `nowcast_bot.py` -- full pipeline, kept in sync with `IMD_Radar_Updates`
  (copy it over again after any live-repo change, same as this file's own
  history shows). `kkl_ppz`'s 500km calibration and `mask_within_km`
  support were already in here; this repo is just what wires them into a
  build.
- `test_500km_pipeline.py` -- the entry point: overrides
  `POLLED_PRODUCTS` (now `maxz`, `kkl_maxz`, `kkl_ppz`, `koc_maxz`),
  state/archive paths and the output filename, then calls
  `nowcast_bot.run_pipeline()`.
