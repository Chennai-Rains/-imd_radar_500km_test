"""One-off test build: Karaikal shown as a HYBRID of its two real IMD
products -- kkl_maxz (250km, full resolution) for the inner disc, and
kkl_ppz (500km, coarser) for the 250-500km ring kkl_maxz's own image
simply doesn't reach -- instead of the live map's kkl_maxz-only 250km.
NIOT stays on maxz (250km, unchanged) and Kochi stays on koc_maxz (250km,
unchanged, per explicit instruction) -- this is Karaikal-only.

Both Karaikal products are polled every cycle. kkl_ppz's PRODUCTS entry
carries mask_within_km=250.0, so decode_reflectivity() NaNs out its own
inner 250km disc before this ever reaches the map or cell extraction --
kkl_maxz alone covers that ground, at its own higher resolution, so
there's no double-rendering or duplicate storm markers where the two
products' coverage would otherwise overlap. See that field's comment in
nowcast_bot.py for the full reasoning, and PRODUCT_STYLE's "Karaikal
Extended Radar" label for how the masked (250-500km-only) product reads
on the page.

Deliberately NOT touching the live pipeline's state/archive/output paths
or POLLED_PRODUCTS: everything below runs against its own isolated
state_500km_test/ and archive_500km_test/ directories and writes to a
differently-named output file, so this can be run (and re-run) without
any risk to the live storm_forecast_map.html or its tracking state. See
the accompanying nowcast_500km_test.yml workflow for how this gets
fetched with real network access (the sandbox this was developed in can't
reach mausam.imd.gov.in directly) and uploaded to a new filename on the
same host, next to (not replacing) the live map.

state_500km_test/ and archive_500km_test/ ARE now committed back to this
repo between runs (nowcast_500km_test.yml has its own git-auto-commit
step, isolated from the live repo's) -- needed so track_cells() has a
previous cycle to diff against and can compute velocity_kmh; without that,
no direction arrow could ever be drawn. Still completely separate from
the live repo's state/archive, so there's no risk of collision there.

Why kkl_ppz specifically for the extension, not just bumping kkl_maxz's
range_km number: range_km isn't a zoom/display setting -- IMD's own
kkl_maxz image is pixel-calibrated to really only show real echo out to
~250km (that's the actual extent the source image renders at), so
raising its declared range_km would only relax the "discard anything
beyond the radar's own stated range as noise" safety filter without the
image actually containing any real data further out -- exactly the
false-positive pattern already fixed for Kochi earlier. kkl_ppz is a
genuinely different IMD product: its own site_px/km_per_px calibration
comment says it was "fit from 200/300/400/500km range-ring labels" on the
real image, meaning it's really rendered at that range by IMD, not
stretched by us -- just at coarser resolution than kkl_maxz, which is
exactly why it's only used for the ring kkl_maxz can't cover at all,
rather than replacing kkl_maxz outright.
"""
from pathlib import Path

import nowcast_bot as nb

TEST_PRODUCTS = ("maxz", "kkl_maxz", "kkl_ppz", "koc_maxz")

nb.POLLED_PRODUCTS = TEST_PRODUCTS
nb.ARCHIVE_DIR = Path("archive_500km_test")
nb.STATE_FILE = Path("state_500km_test/poll_state.json")
nb.CELLS_STATE_FILE = Path("state_500km_test/prev_cells.json")
nb.OBS_TIME_STATE_FILE = Path("state_500km_test/prev_obs_time.json")
nb.OUTPUT_HTML = Path("output/storm_forecast_map_500km_test.html")
nb.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
nb.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
nb.OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)

# run_pipeline() rebuilds _prev_cells/_last_obs_time_seen itself from
# load_prev_cells()/load_prev_obs_time() (which read POLLED_PRODUCTS,
# already patched above), so no need to touch those globals separately.
nb.run_pipeline()
