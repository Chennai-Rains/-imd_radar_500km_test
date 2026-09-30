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

Direction arrows are restricted to Karaikal cells within 250km of the
site (`ARROW_MAX_RANGE_KM` in `nowcast_bot.py`) -- a cell out on the
coarser 250-500km ring still gets a storm marker and tooltip, just no
arrow, since a bearing off a coarse-pixel centroid track is noisier than
it looks once drawn as a confident-looking arrow. Range-boundary circles
are also drawn thin/faint on both this build and the live map, since with
multiple radars' rings now often overlapping they were stacking into
visual clutter over the actual reflectivity data.

## Running it

The workflow (`.github/workflows/nowcast_500km_test.yml`) runs on a
**15-minute cron-job.org pinger**, the same pattern as the live bot's
`nowcast.yml` -- see "Setting up the 15-minute pinger" below.
`workflow_dispatch` also still lets you trigger a run manually from the
Actions tab any time.

Each run builds `output/storm_forecast_map_500km_test.html` and uploads
just that one file over FTP to the same `plots.chennairains.com`
directory the live bot uses -- alongside, not instead of,
`storm_forecast_map.html`. Its own `state_500km_test/`/
`archive_500km_test/` ARE now committed back to this repo between runs
(this build's own `git-auto-commit` step, isolated from the live repo's
`state/`/`archive/`) -- needed so consecutive runs can see motion between
cycles and draw direction arrows at all.

### Setting up the 15-minute pinger

Same setup as the live repo, pointed at this repo instead:

1. On [cron-job.org](https://cron-job.org), create a new cron job.
2. URL: `https://api.github.com/repos/Chennai-Rains/-imd_radar_500km_test/actions/workflows/nowcast_500km_test.yml/dispatches`
3. Request method: `POST`.
4. Headers: `Authorization: Bearer <a GitHub token with repo workflow-dispatch access>`, `Accept: application/vnd.github+json`.
5. Request body: `{"ref":"main"}`.
6. Schedule: every 15 minutes.

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
