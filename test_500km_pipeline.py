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

Also now polling cni_maxz -- IMD's own Chennai DWR (S-band), caz_cni.gif,
which came back online 2026-09-30 after being off-air. Added here first as
one more product alongside the existing four, NOT as a replacement for
anything yet: per explicit instruction, whether to eventually drop the
Karaikal 500km extension in favor of Chennai (S-band, so potentially
better long-range resolution than Karaikal's own radar) is a call to make
once this has run for a few real days, not now. See its PRODUCTS entry in
nowcast_bot.py for the calibration (measured from the frame's own lat/lon
gridlines, same method as Kochi's) and the exclude_colors/despeckle_min_px/
label_exclude_boxes fields added specifically for it -- this radar's first
live frame showed heavy false echo (its open-water basemap texture
coincidentally renders in colors close enough to real LUT swatches to read
as rain across almost the whole visible sea surface) that needed real
cleanup, not just the usual per-radar calibration.

Also now dropping any product's data from the rendered map entirely once
it's more than STALE_OBS_MINUTES (60) old, rather than just flagging it in
the banner -- see build_forecast_map's fresh_products filtering in
nowcast_bot.py. Chennai coming back online made this worth testing for
real: a radar that was recently off-air is exactly the case where a stale
cached frame could otherwise sit on the map looking like a live read.

Also now polling cni_ppz -- Chennai's own extended-range PPZ product,
genuinely rendered to 600km (more than Karaikal's 500km kkl_ppz). It
cedes its own inner 250km disc to cni_maxz via mask_within_km=250.0,
same hybrid pattern as kkl_ppz ceding to kkl_maxz. This is the piece
that lets Chennai be directly compared against the Karaikal hybrid as a
candidate 500km-extended-range source -- see its PRODUCTS entry in
nowcast_bot.py for the calibration and the national-emblem
label_exclude_boxes fix its first real frame needed.

Also now polling mlr_maxz -- IMD's Mangalore CAZ MAXZ (caz_mlr.gif), 250km
range, added per explicit instruction ("add CAZ ... to the test version.
Once it works well then we can shift to production version") -- test-repo
ONLY for now, deliberately not touched in the production repo yet. A
third distinct panel layout (circular plan-view with radial spokes and
concentric range rings, no printed lat/lon gridlines) alongside the two
families already here -- see its PRODUCTS entry and the
RADAR_SITES["mangalore"] comment in nowcast_bot.py for the full
calibration methodology (site_px found by extrapolating along the
100/200km range-ring labels rather than from a literal site marker, after
an initial attempt mistook a coastline-dash artifact for one) and for the
UNVERIFIED status of its site_lat/site_lon (a public estimate, not yet
measured from the image's own geometry the way kochi/chennai's were).
"""
from pathlib import Path

import nowcast_bot as nb

# koc_maxz (Kochi MAXZ) dropped per explicit instruction -- that radar is
# under maintenance, so polling it would just serve an increasingly stale
# (or outright broken) frame under a "live" banner. Re-add once it's back.
TEST_PRODUCTS = ("maxz", "kkl_maxz", "kkl_ppz", "cni_maxz", "cni_ppz", "mlr_maxz")

nb.POLLED_PRODUCTS = TEST_PRODUCTS
nb.ARCHIVE_DIR = Path("archive_500km_test")
nb.STATE_FILE = Path("state_500km_test/poll_state.json")
nb.CELLS_STATE_FILE = Path("state_500km_test/prev_cells.json")
nb.OBS_TIME_STATE_FILE = Path("state_500km_test/prev_obs_time.json")
nb.OUTPUT_HTML = Path("output/storm_forecast_map_500km_test.html")
# Bot data export (see the BOT DATA EXPORT block in nowcast_bot.py) -- same
# "_500km_test" suffix as the map, so these never collide with the live
# pipeline's files once that starts writing its own.
nb.OUTPUT_BOT_JSON = Path("output/nowcast_bot_500km_test.json")
nb.OUTPUT_BOT_GRID = Path("output/nowcast_bot_grid_500km_test.bin.gz")
# Mangaluru stays on the test map but is left out of the bot's data: its
# frames keep showing large echo that does not move from one frame to the
# next (a ~20,000 sq km patch over the sea west of the coast on 2 Oct 2026),
# which reads as ground/sea clutter rather than rain, and the bot would
# report it as rain there. Put it back once that has been cleaned up.
nb.BOT_EXCLUDED_PRODUCTS = ("mlr_maxz",)
nb.ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
nb.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
nb.OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)

# run_pipeline() rebuilds _prev_cells/_last_obs_time_seen itself from
# load_prev_cells()/load_prev_obs_time() (which read POLLED_PRODUCTS,
# already patched above), so no need to touch those globals separately.
nb.run_pipeline()

# Social-media storm alerts, SHADOW MODE: drafts a post (text + image) when
# strong echo is over/heading for Chennai and sends it ONLY to a private
# Telegram chat for review -- nothing is published publicly. Reads the bot
# export run_pipeline() just wrote; never allowed to affect the map or state.
# Needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID repo secrets to actually send
# (without them it just prints the draft). See social_alerts.py.
try:
    import social_alerts
    social_alerts.run(nb.OUTPUT_BOT_JSON, nb.OUTPUT_BOT_GRID, Path("state_social_test"),
                      tile_url=getattr(nb, "CARTO_VOYAGER_URL", None))
except Exception as e:
    print(f"[social] failed (the map and tracking above are unaffected): {e!r}")
