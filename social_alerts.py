"""Tamil Nadu storm alerts for social media -- SHADOW MODE.

Reads the bot data export the pipeline already writes every cycle
(nowcast_bot*.json + the gzip'd reflectivity grid with its 10-minute-step
"moved along the measured motion" layers) and decides, region by region,
whether strong echo is over or about to reach it. Regions are Chennai (a 40 km
zone, as before) plus every Tamil Nadu district (outlines in
assets/tn_districts.geojson, used only to say which district an echo lies in).
When assets/tn_places.json exists, regions are OSM towns and cities instead (a disc around each).

Each cycle produces AT MOST ONE post: a digest of every region currently
affected, e.g. "strong echoes over Villupuram, Cuddalore; heading for
Thanjavur (~4:10 PM)". Text is <= 280 characters so the same wording works on
X, Facebook and a WhatsApp Channel, and comes with one map cropped to just
the storms involved. In shadow mode the draft goes only to a private
Telegram chat for review -- nothing is published anywhere public. Posting to
X / Facebook comes later, behind its own switch, after the calls have been
checked against real storm days.

How a widespread day stays readable: a region is announced once per
"episode" (until it has been quiet for CLEAR_AFTER_MIN), later posts only
happen for new regions or when one is upgraded from "heading for" to "over",
posts are at least GLOBAL_MIN_GAP_MIN apart and at most MAX_POSTS_PER_HOUR in any hour, except Very heavy spells (no daily cap).
Regions that don't fit in one post stay un-announced and are picked up by the
next one.

Deliberately decoupled from nowcast_bot.py: it only reads the export files,
so it can be run, tested and tuned without touching detection or the map,
and run() can never take the pipeline down (the caller wraps it in
try/except as well). It never claims rain, only "strong radar echoes".
Everything tunable is a constant below.

Every cycle appends one line to decision_log.jsonl (what it saw, what it
decided, why) so "why didn't it fire?" can be answered from the repo.
"""
from __future__ import annotations

import gzip
import io
import json
import math
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import requests

IST = timezone(timedelta(hours=5, minutes=30))
HERE = Path(__file__).resolve().parent
TN_GEOJSON = HERE / "assets" / "tn_districts.geojson"
TN_PLACES = HERE / "assets" / "tn_places.json"   # towns and cities from OpenStreetMap (tools/fetch_osm_places.py)

# --- where / how strong ------------------------------------------------------
CHENNAI = (13.0827, 80.2707)
CHENNAI_ZONE_KM = 40.0     # "Chennai": city + immediate suburbs (the old single zone)
CHENNAI_LAND_ONLY = True   # ...counting only the land inside that circle (echo over the sea there is not "Chennai")
CORE_DBZ = 45.0            # core of a storm, for the Intense category
ALERT_DBZ = 35.0           # grid node counts as strong echo at/above this
MIN_REGION_NODES = 8       # grid step is ~2.2 km (~5 km2/node): ~40 km2 of strong echo. 6 nodes on an edge was a real false alarm (5 Oct)
PERSIST_NODES = 6          # a smaller cell (6-7 strong nodes, ~30 km2) counts only when the district also had >= this many in the previous cycle
PERSIST_MAX_AGE_MIN = 15   # ...and that previous cycle must be this recent (cycles are ~5 min apart)
QUIET_START_HOUR, QUIET_END_HOUR = 23, 4   # IST: from 23:00 to 03:59 only Very heavy spells are announced (a manual trigger overrides)
QUIET_MIN_CATEGORY = 3
MAX_LEAD_MIN = 90          # the export carries layers out to +90 min

# Names as the audience writes them (the boundary file uses other spellings).
RENAME = {"Thiruvallur": "Tiruvallur", "Viluppuram": "Villupuram", "Thoothukkudi": "Thoothukudi",
          "Tirupathur": "Tirupattur", "Thiruvarur": "Tiruvarur"}
USE_PLACES = False   # districts are what the audience expects (7 Oct); place mode stays available (assets/tn_places.json)
PLACE_RENAME = {"Kallakuruchi": "Kallakurichi", "Kanchipuram": "Kancheepuram", "Viluppuram": "Villupuram",
                "Thiruvarur": "Tiruvarur", "Giingee": "Gingee", "Tiruppattur": "Tirupattur",
                "Tiruppur": "Tirupur", "Udhagamandalam": "Ooty (Udhagamandalam)"}
PLACE_RADIUS_KM = {"city": 12.0, "town": 8.0}   # a place is "hit" when strong echo covers enough of this disc
PLACE_CHENNAI_ZONE_KM = 20.0   # in place mode "Chennai" is the city only, not the whole 40 km zone
SKIP_DISTRICTS = {"Chennai"}   # covered by the Chennai zone, which is the same ground

# --- trust: when NOT to say anything -----------------------------------------
MAX_EXPORT_AGE_MIN = 30    # export itself older than this -> pipeline is not running properly
MAX_RADAR_AGE_MIN = 40     # freshest radar frame older than this -> nowcast is not "now"
MIN_REGION_COVERAGE = 0.5  # share of a region inside radar coverage; no coverage != dry, so stay quiet there

# --- how often ----------------------------------------------------------------
# 21 Oct 2026 (user): alerts should WARN. 83 % of "over" mentions (15-20 Oct) were repeats of rain already announced, so
# continuing rain no longer triggers a post by itself; it is carried as "continuing over ..." when a post goes out for a
# new reason (a district newly approaching, rain newly starting over a district, or a step up to Heavy).
CLEAR_AFTER_MIN = 60       # a region must be quiet this long before rain there is a NEW episode (was 30: pauses re-announced the same rain)
DISTRICT_MIN_GAP_MIN = 60  # a district already announced is upgraded (Moderate -> Heavy) at most once an hour ...
GLOBAL_MIN_GAP_MIN = 20    # minimum time between any two posts (was 30) ...
PRIORITY_GAP_MIN = 10      # ... but a NEWLY APPROACHING district (arrival >= IMMINENT_MIN away) may post 10 min after the last post
MAX_POSTS_PER_HOUR = 2     # at most this many posts in any rolling 60 minutes ...
PRIORITY_MAX_PER_HOUR = 3  # ... a newly approaching district may use a third
GROUP_RADIUS_KM = 40.0     # a newly approaching district whose rain is within this distance of rain in a district announced
GROUP_HOLD_MIN = 30        # in the last hour is the same band: it waits until this long after the last post (rides along meanwhile)
# 21 Oct 2026, from 14-21 Oct data (storm-trend notes): rain from a medium/large storm peaking >= 50 dBZ was still there an hour
# later 71-76 % of the time, from a small cell below 45 dBZ only 45 % -- so the text says how long it is likely to last.
# Far approaching calls from small weak rain arrived only 52-57 % of the time (others 71-76 %), so they wait until < 30 min.
LASTING_MEDIUM_NODES, LASTING_LARGE_NODES = 15, 50
BRIEF_MAX_NODES, BRIEF_MAX_PEAK = 15, 45.0
WEAK_FAR_LEAD_MIN = 30
IMMINENT_MIN = 15          # arrival this soon: "likely to begin shortly" (the radar picture is ~30 min old; no window that fine)
# ... EXCEPT a Very heavy spell (a district newly Very heavy, or upgraded to it), which is always announced at once
MAX_POSTS_PER_DAY = None   # no daily cap (removed 7 Oct: it blocked the afternoon storms); set a number to re-enable
MAX_NAMED_REGIONS = 6      # a post names at most this many; the rest become "+N more districts" (and count as announced)

DECISION_LOG_MAX_LINES = 1500
DRAFTS_KEEP = 30
HASHTAGS = "#TNRains #ChennaiRains"

SEVERITY = [(50.0, "very strong", 3), (40.0, "strong", 2), (35.0, "moderate-to-strong", 1)]


def severity_label(max_dbz: float) -> tuple[str, int]:
    for floor, label, rank in SEVERITY:
        if max_dbz >= floor:
            return label, rank
    return "moderate", 0


# --- small geo helpers ---------------------------------------------------------
_COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg: float) -> str:
    return _COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]


def haversine_km_arr(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(lon2) - np.radians(lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def fmt_time(dt: datetime) -> str:
    return dt.astimezone(IST).strftime("%I:%M %p").lstrip("0")


# 18 Oct 2026: arrival is given as a time WINDOW, not "in N minutes" -- the radar picture is 20-30 min old and the motion
# behind the moved-forward layers is uncertain, so a single minute figure overstates what is known. The window is centred
# on the estimated arrival and widens with the lead (+-10 min up to 30 min ahead, +-15 up to 60, +-20 beyond), snapped to 5.
def window_text(h: "Hit", now_utc: datetime) -> str:
    pad = 10 if h.lead_min <= 30 else 15 if h.lead_min <= 60 else 20
    snap = lambda d: d - timedelta(minutes=d.minute % 5, seconds=d.second, microseconds=d.microsecond)
    start = max(snap(h.when_utc - timedelta(minutes=pad)), snap(now_utc))
    end = snap(h.when_utc + timedelta(minutes=pad))
    s, e = start.astimezone(IST), end.astimezone(IST)
    st, et = s.strftime("%I:%M").lstrip("0"), e.strftime("%I:%M").lstrip("0")
    if s.strftime("%p") == e.strftime("%p"):
        return f"{st}-{et} {e.strftime('%p')}"
    return f"{st} {s.strftime('%p')}-{et} {e.strftime('%p')}"


# --- reading the export --------------------------------------------------------
@dataclass
class Export:
    doc: dict
    grid: np.ndarray            # (layers, rows, cols) uint8: 0 no echo, 255 no coverage, else dBZ
    lats: np.ndarray
    lons: np.ndarray
    layer_names: list
    layer_leads: list
    generated_utc: datetime


def load_export(json_path: Path, grid_path: Path) -> Export | None:
    doc = json.loads(Path(json_path).read_text())
    g = doc.get("grid")
    if not g:
        return None
    raw = gzip.decompress(Path(grid_path).read_bytes())
    nl, nr, nc = len(g["layers"]), g["nrows"], g["ncols"]
    arr = np.frombuffer(raw, dtype=np.uint8)
    if arr.size != nl * nr * nc:
        raise ValueError(f"grid size {arr.size} != {nl}x{nr}x{nc}")
    step = g["step_deg"]
    return Export(
        doc=doc,
        grid=arr.reshape(nl, nr, nc),
        lats=g["lat_north"] - np.arange(nr) * step,
        lons=g["lon_west"] + np.arange(nc) * step,
        layer_names=[l["name"] for l in g["layers"]],
        layer_leads=[l.get("lead_min", 0) for l in g["layers"]],
        generated_utc=datetime.fromisoformat(doc["generated_utc"]),
    )


# --- regions ---------------------------------------------------------------------
@dataclass
class Region:
    name: str
    rs: slice                  # rows / cols of the bounding box on the export grid
    cs: slice
    sub: np.ndarray            # bool mask inside that box
    n: int                     # nodes in the region

    @property
    def min_nodes_persist(self) -> int:
        return max(3, min(PERSIST_NODES, int(0.25 * self.n)))

    @property
    def min_nodes(self) -> int:
        # a small region can't hold 8 strong nodes without being all echo
        return max(3, min(MIN_REGION_NODES, int(0.25 * self.n)))


def _box(lats, lons, south, north, west, east):
    # lats run north -> south
    r0 = int(np.searchsorted(-lats, -north, side="left"))
    r1 = int(np.searchsorted(-lats, -south, side="right"))
    c0 = int(np.searchsorted(lons, west, side="left"))
    c1 = int(np.searchsorted(lons, east, side="right"))
    return slice(r0, r1), slice(c0, c1)


def _polygon_mask(polys, LAT, LON):
    from matplotlib.path import Path as MPath
    pts = np.column_stack([LON.ravel(), LAT.ravel()])
    out = np.zeros(LAT.size, dtype=bool)
    for rings in polys:
        m = MPath(np.asarray(rings[0])).contains_points(pts)
        for hole in rings[1:]:
            m &= ~MPath(np.asarray(hole)).contains_points(pts)
        out |= m
    return out.reshape(LAT.shape)


def build_place_regions(lats, lons, places_path: Path = TN_PLACES) -> list[Region]:
    """Chennai (20 km disc) first, then one disc per OSM town/city that has nodes on the grid."""
    regions = []
    places = json.loads(Path(places_path).read_text())
    spec = [("Chennai", CHENNAI[0], CHENNAI[1], PLACE_CHENNAI_ZONE_KM)]
    for p in places:
        if p["name"] == "Chennai" or haversine_km(p["lat"], p["lon"], CHENNAI[0], CHENNAI[1]) < PLACE_CHENNAI_ZONE_KM:
            continue
        spec.append((PLACE_RENAME.get(p["name"], p["name"]), p["lat"], p["lon"], PLACE_RADIUS_KM.get(p["kind"], 8.0)))
    for name, lat, lon, rad in spec:
        dlat = rad / 111.0
        dlon = rad / (111.0 * math.cos(math.radians(lat)))
        rs, cs = _box(lats, lons, lat - dlat, lat + dlat, lon - dlon, lon + dlon)
        if rs.stop <= rs.start or cs.stop <= cs.start:
            continue
        LAT, LON = np.meshgrid(lats[rs], lons[cs], indexing="ij")
        sub = haversine_km_arr(LAT, LON, lat, lon) <= rad
        if sub.any():
            regions.append(Region(name, rs, cs, sub, int(sub.sum())))
    return regions


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    return float(haversine_km_arr(np.array(lat1), np.array(lon1), lat2, lon2))


def build_regions(lats, lons, geojson_path: Path = TN_GEOJSON) -> list[Region]:
    """Places (OSM towns and cities) when assets/tn_places.json exists, else districts."""
    if USE_PLACES and TN_PLACES.exists() and geojson_path == TN_GEOJSON:
        return build_place_regions(lats, lons)
    return build_district_regions(lats, lons, geojson_path)


def build_district_regions(lats, lons, geojson_path: Path = TN_GEOJSON) -> list[Region]:
    """Chennai zone first, then the districts that have at least one node on the grid."""
    regions = []
    dlat = CHENNAI_ZONE_KM / 111.0
    dlon = CHENNAI_ZONE_KM / (111.0 * math.cos(math.radians(CHENNAI[0])))
    rs, cs = _box(lats, lons, CHENNAI[0] - dlat, CHENNAI[0] + dlat, CHENNAI[1] - dlon, CHENNAI[1] + dlon)
    if rs.stop > rs.start and cs.stop > cs.start:
        LAT, LON = np.meshgrid(lats[rs], lons[cs], indexing="ij")
        sub = haversine_km_arr(LAT, LON, CHENNAI[0], CHENNAI[1]) <= CHENNAI_ZONE_KM
        if CHENNAI_LAND_ONLY and geojson_path == TN_GEOJSON and TN_GEOJSON.exists():
            # Only the land part of the circle counts (19 Oct 2026: two Chennai alerts came from a band 20-30 km out
            # at sea that was moving along the coast, not onto it). Strong echo now over land, or moved onto land by
            # any forecast layer up to +90 min, still triggers; a band that stays at sea does not.
            sub &= land_mask_grid(lats, lons)[rs, cs]
        if sub.any():
            regions.append(Region("Chennai", rs, cs, sub, int(sub.sum())))
    gj = json.loads(Path(geojson_path).read_text())
    for f in gj["features"]:
        raw = f["properties"]["district"]
        if raw in SKIP_DISTRICTS:
            continue
        geom = f["geometry"]
        polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
        pts = np.array([p for poly in polys for p in poly[0]])
        rs, cs = _box(lats, lons, pts[:, 1].min(), pts[:, 1].max(), pts[:, 0].min(), pts[:, 0].max())
        if rs.stop <= rs.start or cs.stop <= cs.start:
            continue
        LAT, LON = np.meshgrid(lats[rs], lons[cs], indexing="ij")
        sub = _polygon_mask(polys, LAT, LON)
        if sub.any():
            regions.append(Region(RENAME.get(raw, raw), rs, cs, sub, int(sub.sum())))
    return regions


# --- the assessment ----------------------------------------------------------------
@dataclass
class Hit:
    name: str
    status: str                # "over" | "approaching"
    lead_min: int              # first lead at which strong echo is in the region (0 = now)
    max_dbz: float
    nodes: int                 # strong nodes at that lead
    when_utc: datetime
    lat: float                 # centre of the strong echo in the region at that lead
    lon: float
    core45: int = 0            # most nodes at/above CORE_DBZ at any lead (a real core, not one spike)
    end_lead: int = 0          # last lead (min) at which the region still holds strong echo
    vh_ok: bool = True         # Very heavy allowed: seen as intense in the previous cycle too, and not a small (< min_nodes) cell
    heavy_ok: bool = True      # Heavy allowed: the district looked Heavy in the previous cycle too (20 Oct 2026)
    storm_nodes: int = 0       # size of the whole storm (connected >= ALERT_DBZ nodes) that brings the rain, at the first lead
    storm_peak: float = 0.0    # its peak dBZ


@dataclass
class Assessment:
    ok: bool
    reason: str
    hits: list = field(default_factory=list)
    uncovered: list = field(default_factory=list)
    offshore: list = field(default_factory=list)   # approaching hits dropped: only an offshore band, farther than OFFSHORE_MAX_KM
    n_regions: int = 0
    weak: list = field(default_factory=list)       # districts with >= PERSIST_NODES strong nodes at some lead (for next cycle's persistence test)
    intense: list = field(default_factory=list)    # districts that look Very heavy this cycle (confirmed or not), for next cycle's confirmation
    persisted: list = field(default_factory=list)  # hits that exist only because the same small cell was also seen last cycle
    heavy: list = field(default_factory=list)      # districts that look Heavy this cycle (confirmed or not), for next cycle's confirmation


OFFSHORE_MAX_KM = 25.0     # a band that is still over the sea is only announced once it is this close to the district
# 21 Oct 2026 (user): Chennai may be warned about a sea band from 50 km -- but only when the band is coming in at
# >= 20 km/h (distance / forecast arrival time), so a slow band drifting offshore is not announced hours early.
OFFSHORE_MAX_KM_REGION = {"Chennai": 50.0}
OFFSHORE_EXT_MIN_SPEED_KMH = 20.0
LAND_NEAR_KM = 150.0       # ...while echo already over land is announced from further away
_LAND_CACHE: dict = {}


def land_mask_grid(lats, lons) -> np.ndarray:
    """Grid cells inside any district outline (Tamil Nadu, Chennai, Puducherry, Karaikal) = land."""
    key = (len(lats), len(lons), float(lats[0]), float(lons[0]))
    if key not in _LAND_CACHE:
        land = np.zeros((len(lats), len(lons)), dtype=bool)
        dists, _ = _map_features()
        from matplotlib.path import Path as MPath
        for _name, rings in dists:
            for r in rings:
                rs, cs = _box(lats, lons, r[:, 1].min(), r[:, 1].max(), r[:, 0].min(), r[:, 0].max())
                if rs.stop <= rs.start or cs.stop <= cs.start:
                    continue
                LAT, LON = np.meshgrid(lats[rs], lons[cs], indexing="ij")
                m = MPath(r).contains_points(np.column_stack([LON.ravel(), LAT.ravel()])).reshape(LAT.shape)
                land[rs, cs] |= m
        _LAND_CACHE[key] = land
    return _LAND_CACHE[key]


def land_mask(exp: Export) -> np.ndarray:
    return land_mask_grid(exp.lats, exp.lons)


def _offshore_only(exp: Export, reg: Region, idx0: int, lead_min: int | None = None) -> bool:
    """True when the echo about to reach this region is only a band over the sea that is still
    farther than OFFSHORE_MAX_KM away (and no echo over land is within LAND_NEAR_KM). For the regions in
    OFFSHORE_MAX_KM_REGION the limit is wider when the band approaches at >= OFFSHORE_EXT_MIN_SPEED_KMH."""
    layer = exp.grid[idx0]
    pad = 2.0
    rs, cs = _box(exp.lats, exp.lons, exp.lats[reg.rs].min() - pad, exp.lats[reg.rs].max() + pad,
                  exp.lons[reg.cs].min() - pad, exp.lons[reg.cs].max() + pad)
    sub = layer[rs, cs]
    ys, xs = np.where((sub >= ALERT_DBZ) & (sub < 255))
    if len(ys) == 0:
        return False                      # nothing to judge by; leave the hit alone
    plat, plon = exp.lats[rs][ys], exp.lons[cs][xs]
    on_land = land_mask(exp)[rs, cs][ys, xs]
    ry, rx = np.where(reg.sub)
    rlat, rlon = exp.lats[reg.rs][ry], exp.lons[reg.cs][rx]
    if len(rlat) > 150:
        k = np.linspace(0, len(rlat) - 1, 150).astype(int)
        rlat, rlon = rlat[k], rlon[k]
    def nearest(mask):
        if not mask.any():
            return float("inf")
        la, lo = plat[mask], plon[mask]
        if len(la) > 600:
            k = np.linspace(0, len(la) - 1, 600).astype(int)
            la, lo = la[k], lo[k]
        return float(haversine_km_arr(la[:, None], lo[:, None], rlat[None, :], rlon[None, :]).min())
    d_land, d_sea = nearest(on_land), nearest(~on_land)
    limit = OFFSHORE_MAX_KM
    ext = OFFSHORE_MAX_KM_REGION.get(reg.name)
    if ext and lead_min and d_sea < float("inf") and d_sea / (lead_min / 60.0) >= OFFSHORE_EXT_MIN_SPEED_KMH:
        limit = max(limit, ext)
    return not (d_land <= LAND_NEAR_KM or d_sea <= limit)


def assess(exp: Export, now_utc: datetime, regions: list[Region] | None = None, prev_weak: set | None = None, prev_vh: set | None = None,
           prev_heavy: set | None = None) -> Assessment:
    doc = exp.doc
    age = (now_utc - exp.generated_utc).total_seconds() / 60.0
    if age > MAX_EXPORT_AGE_MIN:
        return Assessment(False, f"export is {age:.0f} min old")
    used = [r for r in doc.get("radars", []) if r.get("used")]
    if not used:
        return Assessment(False, "no radar in use this cycle")
    ages = [r["age_min"] if r.get("age_min") is not None else 0.0 for r in used]
    if min(ages) > MAX_RADAR_AGE_MIN:
        return Assessment(False, f"freshest radar frame is {min(ages):.0f} min old")

    regions = regions if regions is not None else build_regions(exp.lats, exp.lons)
    if not regions:
        return Assessment(False, "grid does not cover Tamil Nadu")

    # Layers to read: the moved-forward ones (plus_N) when the export has
    # motion, else just the observed picture (can say "over", not "approaching").
    idx = [(lead, i) for i, (n, lead) in enumerate(zip(exp.layer_names, exp.layer_leads))
           if n.startswith("plus_") and lead <= MAX_LEAD_MIN]
    if not idx:
        idx = [(0, exp.layer_names.index("observed"))]
    idx.sort()

    hits, uncovered, offshore, weak, persisted, intense, heavy = [], [], [], [], [], [], []
    from scipy import ndimage as _ndi
    _labels = {}

    def storm_at(i_layer, reg, strong_mask):
        """(size, peak) of the largest connected >= ALERT_DBZ storm that has nodes in this region's strong mask."""
        if i_layer not in _labels:
            v = exp.grid[i_layer]
            m = (v >= ALERT_DBZ) & (v < 255)
            _labels[i_layer] = (_ndi.label(m, structure=np.ones((3, 3)))[0], v)
        lab, v = _labels[i_layer]
        ids = np.unique(lab[reg.rs, reg.cs][strong_mask])
        ids = ids[ids > 0]
        best = (0, 0.0)
        for k in ids:
            mk = lab == k
            n = int(mk.sum())
            if n > best[0]:
                best = (n, float(v[mk].max()))
        return best
    prev_weak = prev_weak or set()
    for reg in regions:
        first_layer = exp.grid[idx[0][1]][reg.rs, reg.cs][reg.sub]
        if (first_layer != 255).mean() < MIN_REGION_COVERAGE:
            uncovered.append(reg.name)
            continue
        first, nodes_first, max_dbz, cen, core, last = None, 0, 0.0, None, 0, 0
        storm = (0, 0.0)
        thr = reg.min_nodes_persist if reg.name in prev_weak else reg.min_nodes   # small cells need to have been seen last cycle too
        seen_weak = False
        for lead, i in idx:
            v = exp.grid[i][reg.rs, reg.cs]
            strong = (v >= ALERT_DBZ) & (v < 255) & reg.sub
            n = int(strong.sum())
            if n >= reg.min_nodes_persist:
                seen_weak = True
            if n >= thr:
                last = lead
                if first is None:
                    first, nodes_first = lead, n
                    storm = storm_at(i, reg, strong)
                    ys, xs = np.where(strong)
                    cen = (float(exp.lats[reg.rs][ys].mean()), float(exp.lons[reg.cs][xs].mean()))
                max_dbz = max(max_dbz, float(v[strong].max()))
                core = max(core, int(((v >= CORE_DBZ) & (v < 255) & reg.sub).sum()))
        if seen_weak:
            weak.append(reg.name)
        if first is not None and first > 0 and _offshore_only(exp, reg, idx[0][1], first):
            offshore.append(reg.name)
            continue
        if first is not None:
            if nodes_first < reg.min_nodes:
                persisted.append(reg.name)
            h = Hit(reg.name, "over" if first == 0 else "approaching", first, max_dbz, nodes_first,
                    exp.generated_utc + timedelta(minutes=first), cen[0], cen[1], core, last,
                    storm_nodes=storm[0], storm_peak=storm[1])
            if is_heavy_raw(h):
                heavy.append(reg.name)
                # Heavy must show in two cycles running (20 Oct 2026, user's choice): cores pulsing around 45 dBZ made
                # the label flip Heavy -> Moderate -> Heavy every scan (NIOT 6:08-6:46 AM: 48, 38, 51, 39 dBZ over Chennai).
                h.heavy_ok = prev_heavy is None or reg.name in prev_heavy
            if is_intense(h):
                intense.append(reg.name)
                # Very heavy is the claim we must not get wrong: the district has to have looked Very heavy in
                # the previous cycle too (a one-frame artefact does not repeat), and the cell has to be a full
                # size one, not a 6-7 node cell let in by the small-cell rule.
                h.vh_ok = nodes_first >= reg.min_nodes and (prev_vh is None or reg.name in prev_vh)
            hits.append(h)
    return Assessment(True, "ok", hits, uncovered, offshore, len(regions), weak, intense, persisted, heavy)


# --- episode / dedup state ---------------------------------------------------------
def load_state(path: Path) -> dict:
    try:
        st = json.loads(Path(path).read_text())
        if isinstance(st.get("regions"), dict):
            return st
    except Exception:
        pass
    return {"regions": {}, "last_post_utc": None, "posts": {}}


def in_quiet_hours(now_utc: datetime) -> bool:
    h = now_utc.astimezone(IST).hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def _mins(now, then):
    return (now - then).total_seconds() / 60.0 if then else 1e9


def _same_band_as_announced(h: Hit, regs: dict, now_utc: datetime) -> bool:
    """The district's approaching rain lies within GROUP_RADIUS_KM of the rain in a district announced in the last hour."""
    for st in regs.values():
        if st.get("active") and st.get("last_lat") is not None and _mins(now_utc, _dt(st.get("last_post"))) < 60:
            if haversine_km(h.lat, h.lon, st["last_lat"], st["last_lon"]) <= GROUP_RADIUS_KM:
                return True
    return False


def decide(hits: list[Hit], state: dict, now_utc: datetime, global_gap: float = None, max_per_hour: int = None,
           vh_exception: bool = True, district_gap: float = None, refresh_gap: float = None) -> tuple[str, str, set]:
    """("send"|"skip", reason, names of regions that are new or upgraded).
    Triggers (21 Oct 2026): a district NEWLY approaching or NEWLY under rain (not announced in this episode), or a step up
    in category. Rain arriving in a district already warned about, and rain simply continuing, never trigger a post on
    their own -- they ride along as "continuing" in the next post. Mutates state only for bookkeeping that does not
    depend on whether a post went out; the caller records the post once it succeeded."""
    global_gap = GLOBAL_MIN_GAP_MIN if global_gap is None else global_gap
    max_per_hour = MAX_POSTS_PER_HOUR if max_per_hour is None else max_per_hour
    district_gap = DISTRICT_MIN_GAP_MIN if district_gap is None else district_gap
    regs = state.setdefault("regions", {})
    hit_names = {h.name for h in hits}
    for h in hits:
        regs.setdefault(h.name, {"active": False, "last_post": None, "last_status": None, "last_rank": 0})
        regs[h.name]["last_hit"] = now_utc.isoformat()
    for name, st in regs.items():
        if st.get("active") and name not in hit_names and _mins(now_utc, _dt(st.get("last_hit"))) >= CLEAR_AFTER_MIN:
            st["active"] = False

    if not hits:
        return "skip", "no strong echo over or heading for any region", set()

    changed = set()
    warn, new, upgraded, vh, grouped, held_weak = [], [], [], [], [], []
    for h in hits:
        st = regs[h.name]
        rank = category(h)     # Moderate -> Heavy -> Very heavy counts as an upgrade
        if not st.get("active"):
            if weak_far(h):
                held_weak.append(h.name)    # small weak rain still >= 30 min away: no post of its own yet
                continue
            if h.status == "approaching" and _same_band_as_announced(h, regs, now_utc) \
                    and _mins(now_utc, _dt(state.get("last_post_utc"))) < GROUP_HOLD_MIN:
                grouped.append(h.name)      # same rainband as a district just announced: no post of its own yet
                continue
            changed.add(h.name)
            if h.status == "approaching" and h.lead_min >= IMMINENT_MIN:
                warn.append(h.name)
            else:
                new.append(h.name)
            if rank == 3 and vh_exception:
                vh.append(h.name)
            continue
        if rank == 3 and st.get("last_rank", 0) < 3 and vh_exception:       # turned Very heavy: never held back
            upgraded.append(h.name); changed.add(h.name); vh.append(h.name)
        elif rank > st.get("last_rank", 0) and _mins(now_utc, _dt(st.get("last_post"))) >= district_gap:
            upgraded.append(h.name); changed.add(h.name)
    if not changed:
        why = f"nothing new: {len(hits)} active region(s) already announced (rain continuing)"
        if grouped:
            why += f"; held as the same band: {', '.join(grouped)}"
        if held_weak:
            why += f"; small weak rain still far: {', '.join(held_weak)}"
        return "skip", why, set()
    summary = f"{len(warn)} newly approaching, {len(new)} new rain, {len(upgraded)} upgraded"
    if vh:
        return "send", f"VERY HEAVY: {', '.join(vh)} ({summary})", changed
    gap = _mins(now_utc, _dt(state.get("last_post_utc")))
    need_gap, cap = (min(global_gap, PRIORITY_GAP_MIN), max(max_per_hour, PRIORITY_MAX_PER_HOUR)) if warn else (global_gap, max_per_hour)
    if gap < need_gap:
        return "skip", f"{len(changed)} change(s) waiting, last post was {gap:.0f} min ago (< {need_gap:.0f})", set()
    recent = [t for t in state.get("post_times", []) if _mins(now_utc, _dt(t)) < 60]
    if len(recent) >= cap:
        return "skip", f"{len(changed)} change(s) waiting, {len(recent)} posts already in the last hour", set()
    today = now_utc.astimezone(IST).strftime("%Y-%m-%d")
    if MAX_POSTS_PER_DAY is not None and state.get("posts", {}).get(today, 0) >= MAX_POSTS_PER_DAY:
        return "skip", f"daily cap of {MAX_POSTS_PER_DAY} posts reached", set()
    return "send", summary, changed


# --- the wording -----------------------------------------------------------------------
def motion_bearing(exp: Export, hits: list[Hit]) -> float | None:
    """Direction (degrees, towards) the strong rain areas near these regions are moving,
    only when they agree and are not slow; otherwise None (better silent than wrong)."""
    vecs, wts = [], []
    for ar in exp.doc.get("rain_areas", []):
        sp, br = ar.get("speed_kmh"), ar.get("bearing_deg")
        if not ar.get("has_strong_core") or ar.get("max_dbz", 0) < ALERT_DBZ or sp is None or br is None or sp < 5:
            continue
        if not hits or min(float(haversine_km_arr(ar["lat"], ar["lon"], h.lat, h.lon)) for h in hits) > 120:
            continue
        vecs.append((sp * math.sin(math.radians(br)), sp * math.cos(math.radians(br))))
        wts.append(max(ar.get("area_km2", 1.0), 1.0))
    if not vecs:
        return None
    w = np.array(wts) / sum(wts)
    u = float((np.array([v[0] for v in vecs]) * w).sum()); v = float((np.array([v[1] for v in vecs]) * w).sum())
    mean_speed = float((np.hypot([x[0] for x in vecs], [x[1] for x in vecs]) * w).sum())
    speed = math.hypot(u, v)
    if speed < 8 or speed < 0.85 * mean_speed:     # slow, or the areas disagree on direction
        return None
    return math.degrees(math.atan2(u, v)) % 360


def overall_motion(exp: Export, hits: list[Hit]) -> str:
    b = motion_bearing(exp, hits)
    return "" if b is None else f"Moving {compass(b)}."


def order_hits(hits: list[Hit], changed: set) -> list[Hit]:
    """New/upgraded first (they are why we are posting), then Chennai, 'over' before
    'heading for', then strongest."""
    return sorted(hits, key=lambda h: (h.name not in changed, h.status != "approaching", h.name != "Chennai",
                                       -h.max_dbz, -h.nodes))


def region_of_tn(lat: float, lon: float) -> str:
    """Rough part of Tamil Nadu, for 'isolated storms around X district in North TN'."""
    if lat >= 12.2:
        return "North TN"
    if lat < 9.8:
        return "South TN"
    if lon < 77.6:
        return "West TN"
    if lat >= 10.2 and lon >= 78.9:
        return "Delta region"
    return "Central TN"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _districts(names: list[str]) -> str:
    return f"{_join(names)} district" + ("" if len(names) == 1 else "s")


# "Very heavy spells": peak at/above INTENSE_DBZ AND a real core (INTENSE_CORE_NODES nodes at/above
# CORE_DBZ, about 15 km2), so one stray bright node does not promote a district. At 52 dBZ (13 Oct 2026) a replay
# of 10-13 Oct shows about 3.5% of district-hits and 4% of cycles (about 6 episodes in 3 days); at 48 it was 11.6% / 18%.
INTENSE_DBZ = 52.0         # raised from 48 on 13 Oct 2026: 52 dBZ is ~65 mm/h (Z = 200 R^1.6), i.e. 6 cm/h or more
INTENSE_CORE_NODES = 3
ISOLATED_MAX_NODES = 20    # an "over" region with fewer strong nodes than this, away from the main storms, is isolated
ISOLATED_MIN_KM = 80.0


def is_intense(h: Hit) -> bool:
    """Very heavy spell: peak at/above INTENSE_DBZ with a real core (see INTENSE_DBZ)."""
    return h.max_dbz >= INTENSE_DBZ and h.core45 >= INTENSE_CORE_NODES


# "Spells of rain" in three categories from radar intensity (12 Oct 2026). Rough rain rates from the
# standard Z = 200 R^1.6: 35 dBZ ~ 6 mm/h, 45 dBZ ~ 24 mm/h, 48 dBZ ~ 37 mm/h. Across 281 alert cycles
# (6-12 Oct, 817 district-hits) this splits about 64% Moderate, 28% Heavy, 9% Very heavy.
HEAVY_DBZ = 45.0
HEAVY_CORE_NODES = 2       # nodes at/above HEAVY_DBZ, so one bright node does not make a spell Heavy
CATEGORY_NAME = {1: "Moderate", 2: "Heavy", 3: "Very heavy"}


# Very heavy alerts are held back (14 Oct 2026): the spells so far gave about 2 cm/h (Moderate/Heavy depending on how long
# they last), so a Very heavy label overstates them. While this is False nothing is ever called Very heavy (the strongest
# spells go out as Heavy), the Very heavy exceptions to the posting limits never fire, and -- because night-time posting is
# limited to Very heavy -- nothing is posted 23:00-04:00 IST. Set it to True to switch them back on.
VERY_HEAVY_ENABLED = False


def category(h: Hit) -> int:
    """1 Moderate, 2 Heavy, 3 Very heavy spell of rain (3 only while VERY_HEAVY_ENABLED)."""
    if VERY_HEAVY_ENABLED and is_intense(h) and h.vh_ok:
        return 3
    if is_heavy_raw(h) and h.heavy_ok:
        return 2
    return 1


def persistence(h: Hit) -> str | None:
    """'lasting' / 'brief' / None from the size and peak of the storm bringing the rain."""
    if (h.storm_nodes >= LASTING_MEDIUM_NODES and h.storm_peak >= 50) or (h.storm_nodes >= LASTING_LARGE_NODES and h.storm_peak >= 45):
        return "lasting"
    if h.storm_nodes < BRIEF_MAX_NODES and h.storm_peak < BRIEF_MAX_PEAK:
        return "brief"
    return None


def weak_far(h: Hit) -> bool:
    """Approaching rain that is small and weak in the district and still far off: arrived only ~55 % of the time."""
    # judged on the WHOLE storm (a big storm's leading edge touching the district is not "small weak rain")
    # 31 Oct 2026: the peak no longer rescues a tiny storm. A 9-node, 47 dBZ cell was called for Pudukkottai 90 min
    # ahead (10:46 PM, 30 Oct) and was gone 15 min later. Replay 25-31 Oct: storms under 15 nodes called 30+ min ahead
    # arrived 30-36 % of the time whatever their peak (vs 60-74 % when under 30 min away).
    return (h.status == "approaching" and h.lead_min >= WEAK_FAR_LEAD_MIN
            and h.storm_nodes < BRIEF_MAX_NODES)


def impact_key(h: Hit) -> tuple:
    """22 Oct 2026 (user): larger clusters and more intense storms take precedence -- category, then the size of the storm
    bringing the rain, then its peak, then how much of it is in the district."""
    return (-category(h), -h.storm_nodes, -h.storm_peak, -h.max_dbz, -h.nodes)


def is_heavy_raw(h: Hit) -> bool:
    """Heavy-looking this cycle, before the two-cycle confirmation."""
    return h.max_dbz >= HEAVY_DBZ and h.core45 >= HEAVY_CORE_NODES


DISCLAIMER = ("Disclaimer: Experimental, automated nowcast based on IMD radar. The above alert is subject to storms "
              "retaining their intensity and movement. Storms may intensify / weaken in real time. Not an official forecast.")


def build_text(hits_ordered: list[Hit], exp: Export, header: str = "COMK Automated Rainfall Nowcast",
               continuing: set | None = None, changed: set | None = None) -> tuple[str, list[Hit]]:
    """Warning-first wording (21 Oct 2026): districts the rain is APPROACHING (with the arrival window), then where it is
    likely to begin shortly, then where it has newly started, then where it is continuing (already announced), the way
    the bands move, and any isolated storms elsewhere. `continuing` = districts announced earlier in this episode.
    Returns the text and the hits it names; the others become "+N more districts"."""
    asof = fmt_time(exp.generated_utc)
    continuing = continuing or set()
    over_all = sorted((h for h in hits_ordered if h.status == "over"), key=impact_key)
    changed = changed or set()
    appr_all = sorted((h for h in hits_ordered if h.status == "approaching"), key=impact_key)
    main, iso = [], []
    if over_all:
        top = over_all[0]
        for h in over_all:
            far = float(haversine_km_arr(h.lat, h.lon, top.lat, top.lon)) > ISOLATED_MIN_KM
            (iso if (h is not top and far and h.nodes < ISOLATED_MAX_NODES) else main).append(h)

    def compose(n_main: int, n_appr: int, n_iso: int) -> tuple[str, list[Hit]]:
        m, a, i = main[:n_main], appr_all[:n_appr], iso[:n_iso]
        # 22 Oct 2026 (user): heaviest impact first -- each sentence is ranked by its most impactful district
        units = []
        soon = [h for h in a if h.lead_min < IMMINENT_MIN]
        later = [h for h in a if h.lead_min >= IMMINENT_MIN]
        if later:
            parts = [f"{h.name} ({window_text(h, exp.generated_utc)})" for h in later]
            units.append((min(map(impact_key, later)), f"Rainbands approaching {_join(parts)} district{'s' if len(later) > 1 else ''}."))
        if soon:
            units.append((min(map(impact_key, soon)), f"Rains likely to begin shortly over parts of {_districts([h.name for h in soon])}."))
        fresh = [h for h in m if h.name not in continuing]
        cont = [h for h in m if h.name in continuing]
        for cat in (3, 2, 1):
            hs = [h for h in fresh if category(h) == cat]
            if hs:
                units.append((min(map(impact_key, hs)), f"{CATEGORY_NAME[cat]} spell of rains over parts of {_districts([h.name for h in hs])}."))
            hs = [h for h in cont if category(h) == cat]
            if hs:
                units.append((min(map(impact_key, hs)), f"{CATEGORY_NAME[cat]} rains continuing over parts of {_districts([h.name for h in hs])}."))
        units.sort(key=lambda u: u[0])
        sents = [u[1] for u in units]
        lasting = [h.name for h in m if persistence(h) == "lasting"]
        brief = [h.name for h in m if persistence(h) == "brief"]
        if lasting:
            sents.append(f"Rains over {_join(lasting)} likely to continue for the next hour.")
        if brief:
            sents.append(f"Rains over {_join(brief)} may be brief.")
        b = motion_bearing(exp, m + a) if (m or a) else None
        if b is not None:
            sents.append(f"Rainbands roughly moving {compass((b + 180) % 360)} to {compass(b)}.")
        if i:
            where = region_of_tn(i[0].lat, i[0].lon)
            sents.append(f"{'Meanwhile isolated' if sents else 'Isolated'} rains around {_districts([h.name for h in i])} in {where}.")
        listed = a + m + i
        dropped = len(hits_ordered) - len(listed)
        body = " ".join(sents)
        if dropped:
            body += f" (+{dropped} more districts)"
        return f"{header} ({asof} IST): {body} {HASHTAGS}", listed

    n_m, n_a, n_i = min(len(main), MAX_NAMED_REGIONS), min(len(appr_all), 4), min(len(iso), 2)
    while True:
        text, listed = compose(n_m, n_a, n_i)
        if len(text) <= 600 or (n_m + n_a + n_i) <= 1:   # 600 (was 400, 22 Oct): the persistence sentence repeats names; Telegram takes 1024, X is fitted separately
            return text, listed
        if n_m > 3:
            n_m -= 1
        elif n_a > 2:
            n_a -= 1
        elif n_i > 1:
            n_i -= 1
        elif n_m > 1:
            n_m -= 1
        elif n_a > 1:
            n_a -= 1
        elif n_i:
            n_i -= 1
        else:
            n_m -= 1


def _duration_note(h: Hit) -> str:
    """Rough 'may last' from how long the moved-forward layers keep strong echo in the district."""
    horizon = max(MAX_LEAD_MIN, 1)
    if h.end_lead >= horizon:
        return f"likely to continue through the next {horizon} min"
    dur = h.end_lead - h.lead_min + 10
    return f"may last about {max(dur, 10)} min"


# 14 Oct 2026: the caption carries only the overview (which districts, which way the bands move) and the disclaimer; the
# per-district timeline is on the image (each district is labelled with its category and time). Set True to add the
# "- District Dt.: ..." lines back under the overview.
SHOW_DISTRICT_LINES = False
CAPTION_LIMIT = 1020   # Telegram allows 1024 characters in a photo caption


def fit_caption(text: str, details: str, disclaimer: str = None, limit: int = CAPTION_LIMIT) -> str:
    """Header + per-district lines + disclaimer inside the caption limit: when it is too long the last district lines are
    replaced by "(+N more districts)", so the disclaimer is never the part that gets cut."""
    disclaimer = DISCLAIMER if disclaimer is None else disclaimer
    lines = details.split("\n") if details else []
    dropped = 0
    while True:
        body = "\n".join(lines + ([f"- ...and {dropped} more district(s)"] if dropped else []))
        cap = f"{text}\n\n{body}\n\n{disclaimer}" if body else f"{text}\n\n{disclaimer}"
        if len(cap) <= limit or not lines:
            return cap
        lines.pop()
        dropped += 1


def build_details(listed: list[Hit], all_ordered: list[Hit], exp_time: datetime = None) -> str:
    """Per district: the category of the spell and the estimated timeline (when it starts and
    roughly how long it may last). No dBZ or node counts (those stay in the decision log)."""
    exp_time = exp_time or datetime.now(timezone.utc)
    now_ = sorted((h for h in listed if h.status == "over"), key=lambda h: (-category(h), -h.nodes))
    soon = sorted((h for h in listed if h.status == "approaching"), key=lambda h: (h.lead_min, -category(h)))
    lines = [f"- {h.name} Dt.: {CATEGORY_NAME[category(h)]} spell of rains now" for h in now_]
    lines += [f"- {h.name} Dt.: rainbands approaching; {CATEGORY_NAME[category(h)]} spell of rains likely between "
              f"{window_text(h, exp_time)}" for h in soon]
    rest = [h.name for h in sorted((h for h in all_ordered if h not in listed),
                                   key=lambda h: (h.status != "over", h.lead_min))]
    if rest:
        shown = rest[:10]   # keep the caption inside Telegram's length limit on big days
        more = f" (+{len(rest) - len(shown)} others)" if len(rest) > len(shown) else ""
        lines.append(f"- ...and {len(rest)} more districts: " + ", ".join(shown) + more)
    return "\n".join(lines)


# --- the picture ---------------------------------------------------------------------
_R = 6378137.0


def _mx(lon):
    return np.radians(lon) * _R


def _my(lat):
    return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2)) * _R


def _tile_mosaic(west, south, east, north, zoom, url_tpl):
    """Basemap tiles covering the box, as (RGB array, (x0,x1,y0,y1) in mercator metres).
    Any tile that fails to load is left blank rather than failing the post."""
    from PIL import Image
    n = 2 ** zoom

    def tx(lon):
        return int((lon + 180.0) / 360.0 * n)

    def ty(lat):
        r = math.radians(lat)
        return int((1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * n)

    x0, x1, y0, y1 = tx(west), tx(east), ty(north), ty(south)
    canvas = Image.new("RGB", ((x1 - x0 + 1) * 256, (y1 - y0 + 1) * 256), (226, 232, 236))
    ok = 0
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            try:
                url = url_tpl.format(s="abcd"[(x + y) % 4], z=zoom, x=x, y=y)
                r = requests.get(url, timeout=10, headers={"User-Agent": "ChennaiRains-nowcast/1.0"})
                r.raise_for_status()
                canvas.paste(Image.open(io.BytesIO(r.content)).convert("RGB"), ((x - x0) * 256, (y - y0) * 256))
                ok += 1
            except Exception:
                pass
    world = 2 * math.pi * _R
    ext = (x0 / n * world - world / 2, (x1 + 1) / n * world - world / 2,
           world / 2 - (y1 + 1) / n * world, world / 2 - y0 / n * world)
    return np.asarray(canvas), ext, ok


VIEW_MIN_KM, VIEW_MAX_KM = 60.0, 260.0   # half-width of the map


def view_box(exp: Export, hits: list[Hit], names: dict) -> tuple[float, float, float]:
    """(centre lat, centre lon, half-width km): just enough to hold the listed
    regions' echo and the storms heading for them, not all of Tamil Nadu."""
    obs = exp.grid[names["plus_0"] if "plus_0" in names else names["observed"]]
    ys, xs = np.where((obs >= ALERT_DBZ) & (obs < 255))
    plat, plon = exp.lats[ys], exp.lons[xs]
    keep = np.zeros(len(ys), dtype=bool)
    for h in hits:
        keep |= haversine_km_arr(plat, plon, h.lat, h.lon) <= 45.0 + 0.7 * h.lead_min
    la = list(plat[keep]) + [h.lat for h in hits]
    lo = list(plon[keep]) + [h.lon for h in hits]
    if any(h.name == "Chennai" for h in hits):
        la.append(CHENNAI[0]); lo.append(CHENNAI[1])
    south, north, west, east = min(la), max(la), min(lo), max(lo)
    clat, clon = (south + north) / 2, (west + east) / 2
    span_km = max((north - south) * 111.0, (east - west) * 111.0 * math.cos(math.radians(clat)))
    return clat, clon, float(np.clip(span_km / 2 * 1.25 + 15.0, VIEW_MIN_KM, VIEW_MAX_KM))


MAX_IMAGES = 4
ARROW_MIN_KMH = 8.0            # slower storms get no arrow: direction is noise
ARROW_MAX_DISAGREE_DEG = 60.0  # arrow must roughly agree with the area motion the outline is based on
IMAGE_MERGE_KM = 90.0     # regions closer than this share one picture


def split_views(hits: list[Hit], max_views: int = MAX_IMAGES) -> list[list[Hit]]:
    """Groups the regions into at most max_views spatial clusters, one picture
    each, so a statewide system is shown as a few zoomed-in maps instead of one
    crowded one. Regions within IMAGE_MERGE_KM of each other stay together."""
    if len(hits) <= 1:
        return [list(hits)] if hits else []
    from scipy.cluster.hierarchy import fcluster, linkage
    km_lat = 111.0
    pts = np.array([[h.lat * km_lat, h.lon * km_lat * math.cos(math.radians(h.lat))] for h in hits])
    Z = linkage(pts, method="complete")
    lab = fcluster(Z, t=IMAGE_MERGE_KM, criterion="distance")
    if lab.max() > max_views:
        lab = fcluster(Z, t=max_views, criterion="maxclust")
    groups: dict[int, list[Hit]] = {}
    for h, l in zip(hits, lab):
        groups.setdefault(int(l), []).append(h)
    # strongest group first, so the first picture (the one with the caption) matters most
    return sorted(groups.values(), key=lambda g: -max(h.max_dbz for h in g))


GRID_KM = 100.0                       # fixed map cells: the state is cut into 100 x 100 km squares
GRID_LAT0, GRID_LON0 = 8.0, 76.2      # south-west corner of the grid (covers all of Tamil Nadu)
GRID_REF_LAT = 10.8                   # latitude at which a cell is 100 km wide; cells are lat/lon aligned
CELL_PAD_KM = 10.0                    # frame = cell + this margin, so echo on a cell edge is not cut off
MAP_TOWNS_MAX = 7
_GRID_DLAT = GRID_KM / 111.195
_GRID_DLON = GRID_KM / (111.195 * math.cos(math.radians(GRID_REF_LAT)))


def grid_cell(lat: float, lon: float) -> tuple[int, int]:
    return int(math.floor((lat - GRID_LAT0) / _GRID_DLAT)), int(math.floor((lon - GRID_LON0) / _GRID_DLON))


def cell_bounds(cell: tuple[int, int]) -> tuple[float, float, float, float]:
    """(south, north, west, east) of the cell, including the margin."""
    i, j = cell
    pad_lat = CELL_PAD_KM / 111.195
    pad_lon = CELL_PAD_KM / (111.195 * math.cos(math.radians(GRID_REF_LAT)))
    return (GRID_LAT0 + i * _GRID_DLAT - pad_lat, GRID_LAT0 + (i + 1) * _GRID_DLAT + pad_lat,
            GRID_LON0 + j * _GRID_DLON - pad_lon, GRID_LON0 + (j + 1) * _GRID_DLON + pad_lon)


def split_cells(hits: list[Hit], max_views: int = MAX_IMAGES) -> list[tuple[tuple[int, int], list[Hit]]]:
    """One picture per grid cell that holds an alerted region's echo, strongest first.
    The same cell always gets the same frame, so readers learn the maps."""
    groups: dict[tuple[int, int], list[Hit]] = {}
    for h in hits:
        groups.setdefault(grid_cell(h.lat, h.lon), []).append(h)
    # 22 Oct 2026: only the districts the text names are passed in; the cell holding the most intense / largest storm
    # comes first (user: larger clusters and more intense storms take precedence)
    ordered = sorted(groups.items(), key=lambda kv: min(impact_key(h) for h in kv[1]))
    return ordered[:max_views]


def render_images(exp: Export, hits: list[Hit], base_path: Path, tile_url: str | None = None) -> list[Path]:
    """One fixed 100 km grid-cell map per cell holding alerted echo (see split_cells)."""
    base_path = Path(base_path)
    views = split_cells(hits)
    out = []
    for i, (cell, grp) in enumerate(views, 1):
        path = base_path.with_name(f"{base_path.stem}_{i}{base_path.suffix}") if len(views) > 1 else base_path
        out.append(render_image(exp, hits, path, tile_url, view_hits=grp, cell=cell))
    return out


_MAP_FEATURES = {}


def _map_features():
    """District outer rings (lon/lat arrays) and towns, loaded once."""
    if not _MAP_FEATURES:
        gj = json.loads(TN_GEOJSON.read_text()) if TN_GEOJSON.exists() else {"features": []}
        dists = []
        for f in gj["features"]:
            g = f["geometry"]
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            name = RENAME.get(f["properties"]["district"], f["properties"]["district"])
            dists.append((name, [np.asarray(p[0], dtype=float) for p in polys]))
        _MAP_FEATURES["districts"] = dists
        _MAP_FEATURES["towns"] = json.loads(TN_PLACES.read_text()) if TN_PLACES.exists() else []
    return _MAP_FEATURES["districts"], _MAP_FEATURES["towns"]


def _frame_towns(south, north, west, east, limit=MAP_TOWNS_MAX):
    """The biggest towns inside the frame: cities first, then district headquarters, then the rest."""
    _, towns = _map_features()
    inside = [p for p in towns if south + 0.03 < p["lat"] < north - 0.03 and west + 0.04 < p["lon"] < east - 0.04]
    def rank(p):
        name = PLACE_RENAME.get(p["name"], p["name"])
        hq = RENAME.get(p.get("district", ""), p.get("district", "")) == name
        return (0 if p["kind"] == "city" else (1 if hq else 2), p["name"])
    return [(PLACE_RENAME.get(p["name"], p["name"]), p["lat"], p["lon"]) for p in sorted(inside, key=rank)][:limit]


def render_image(exp: Export, hits: list[Hit], path: Path, tile_url: str | None = None,
                 view_hits: list[Hit] | None = None, cell: tuple[int, int] | None = None) -> Path:
    """One large square map, readable on a phone: filled colours are the echo
    now, the dashed outline is where strong echo is expected at the horizon,
    and each listed region is named where its echo is."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from matplotlib.colors import BoundaryNorm, ListedColormap

    names = {n: i for i, n in enumerate(exp.layer_names)}
    if cell is None:
        cell = grid_cell(*(((view_hits or hits)[0].lat), ((view_hits or hits)[0].lon)))
    south, north, west, east = cell_bounds(cell)
    clat, clon = (south + north) / 2, (west + east) / 2
    half = GRID_KM / 2 + CELL_PAD_KM

    bg, bg_ext = None, None
    if tile_url:
        try:
            zoom = 9 if half <= 65 else (8 if half <= 130 else 7)
            # Basemap without its own place names: the map draws its own towns, so the two never double up.
            plain = tile_url.replace("/voyager/", "/voyager_nolabels/")
            bg, bg_ext, ok_n = _tile_mosaic(west - 0.05, south - 0.05, east + 0.05, north + 0.05, zoom, plain)
            if ok_n == 0 and plain != tile_url:
                bg, bg_ext, _ = _tile_mosaic(west - 0.05, south - 0.05, east + 0.05, north + 0.05, zoom, tile_url)
        except Exception:
            bg = None

    cmap = ListedColormap(["#a8e6a1", "#5fcf6a", "#f4e04d", "#f7a936", "#ee6a2e", "#d62828", "#a4133c", "#7b2cbf"])
    norm = BoundaryNorm([20, 25, 30, 35, 40, 45, 50, 55, 70], cmap.N)

    approaching = [h.lead_min for h in hits if h.status == "approaching"]
    lead_then = max(approaching) if approaching else 30
    then_key = f"plus_{lead_then}"
    now_layer = exp.grid[names["plus_0"] if "plus_0" in names else names["observed"]].astype(float)
    then_layer = exp.grid[names[then_key]].astype(float) if then_key in names else None

    halo = [pe.withStroke(linewidth=4, foreground="white")]
    fig, ax = plt.subplots(figsize=(7.2, 7.8), dpi=150)
    if bg is not None:
        ax.imshow(bg, extent=bg_ext, origin="upper", interpolation="bilinear", zorder=0)
    else:
        ax.set_facecolor("#e2e8ec")
    X, Y = _mx(exp.lons), _my(exp.lats)
    vals = np.ma.masked_where((now_layer < 20) | (now_layer >= 255), now_layer)
    mesh = ax.pcolormesh(X, Y, vals, cmap=cmap, norm=norm, shading="nearest", alpha=0.85, zorder=2)
    if then_layer is not None:
        strong = ((then_layer >= ALERT_DBZ) & (then_layer < 255)).astype(float)
        if strong.any():
            ax.contour(X, Y, strong, levels=[0.5], colors="black", linewidths=1.6, linestyles="--", zorder=4)

    # District boundaries (alerted ones heavier) and the biggest towns of this frame.
    alerted_names = {h.name for h in hits}
    dist_labels = []
    try:
        dists, _ = _map_features()
        for dname, rings in dists:
            hot = dname in alerted_names
            inside = []
            for r in rings:
                if r[:, 0].max() < west or r[:, 0].min() > east or r[:, 1].max() < south or r[:, 1].min() > north:
                    continue
                ax.plot(_mx(r[:, 0]), _my(r[:, 1]), color="#1a1a1a" if hot else "#555555",
                        lw=2.0 if hot else 1.0, alpha=0.9 if hot else 0.75, zorder=3)
                m = (r[:, 0] > west) & (r[:, 0] < east) & (r[:, 1] > south) & (r[:, 1] < north)
                inside.append(r[m])
            pts = np.concatenate(inside) if inside else np.zeros((0, 2))
            if hot or len(pts) < 6:
                continue
            # label spot: middle of the district's part inside the frame (outline vertices clipped to it)
            x, y = float(pts[:, 0].mean()), float(pts[:, 1].mean())
            if (west + 0.08 < x < east - 0.08) and (south + 0.06 < y < north - 0.06):
                dist_labels.append((dname, x, y))
    except Exception as e:
        print(f"[social] district outlines skipped: {e!r}")
    town_cands = _frame_towns(south, north, west, east)
    if any(h.name == "Chennai" for h in hits):
        t = np.linspace(0, 2 * math.pi, 120)
        ax.plot(_mx(CHENNAI[1] + (CHENNAI_ZONE_KM / (111.0 * math.cos(math.radians(CHENNAI[0])))) * np.cos(t)),
                _my(CHENNAI[0] + (CHENNAI_ZONE_KM / 111.0) * np.sin(t)), color="#1a1a1a", lw=2.0, ls=":", zorder=3)
        ax.plot([_mx(CHENNAI[1])], [_my(CHENNAI[0])], marker="o", ms=9, mfc="white", mec="black", mew=2.2, zorder=5)
    # Direction arrows, same look as the radar page: a dark triangle with a white edge,
    # placed a little ahead of each moving strong rain area, pointing along its bearing.
    from matplotlib.markers import MarkerStyle
    tri = np.array([[0, 1.0], [-0.73, -0.73], [0, -0.27], [0.73, -0.73], [0, 1.0]])
    n_arrows, drawn = 0, []
    # Exactly the cells the radar page draws arrows for (export "cells", motion_reliable),
    # at the same spot: 16.5 min (0.55 x the 30 min lead) ahead of the cell along its bearing.
    for c in sorted(exp.doc.get("cells", []), key=lambda c: -c.get("area_km2", 0)):
        sp, br = c.get("speed_kmh"), c.get("bearing_deg")
        if not c.get("motion_reliable") or sp is None or br is None or sp < ARROW_MIN_KMH:
            continue                 # too slow for a direction to mean anything
        # The dashed outline comes from the area-wide motion; an arrow that points a very
        # different way (or where the area itself is barely moving) would contradict it.
        near = [(float(haversine_km_arr(ar["lat"], ar["lon"], c["lat"], c["lon"])), ar)
                for ar in exp.doc.get("rain_areas", []) if ar.get("speed_kmh") is not None]
        near = [x for x in near if x[0] <= 60.0]
        if near:
            ar = min(near, key=lambda x: x[0])[1]
            diff = abs((br - ar["bearing_deg"] + 180.0) % 360.0 - 180.0)
            if ar["speed_kmh"] < 5.0 or diff > ARROW_MAX_DISAGREE_DEG:
                continue
        a_lat = c["lat"] + (sp * 0.275 * math.cos(math.radians(br))) / 111.0
        a_lon = c["lon"] + (sp * 0.275 * math.sin(math.radians(br))) / (111.0 * math.cos(math.radians(c["lat"])))
        if not (west < a_lon < east and south < a_lat < north):
            continue
        fx = (_mx(a_lon) - _mx(west)) / (_mx(east) - _mx(west))
        fy = (_my(a_lat) - _my(south)) / (_my(north) - _my(south))
        if any(abs(fx - px) < 0.035 and abs(fy - py) < 0.035 for px, py in drawn):
            continue                 # thinned like the page does when zoomed out
        drawn.append((fx, fy))
        ax.plot([_mx(a_lon)], [_my(a_lat)], marker=MarkerStyle(tri).rotated(deg=-br), ms=14,
                mfc="#333333", mec="white", mew=1.4, linestyle="none", zorder=7)
        n_arrows += 1
    # Labels are placed one after another into free space (alerts first, then towns, then
    # district names); a label that finds no free spot is left out (the post text names it).
    fig_w, fig_h = 7.2 * 0.94, 7.8 * 0.70          # approx. axes size in inches

    def _fx(lon): return (_mx(lon) - _mx(west)) / (_mx(east) - _mx(west))
    def _fy(lat): return (_my(lat) - _my(south)) / (_my(north) - _my(south))

    boxes = []
    def _box_of(cx, cy, text, fs):
        lines = text.split("\n")
        w = 0.62 * fs / 72.0 * max(len(l) for l in lines) / fig_w
        h = 1.35 * fs / 72.0 * len(lines) / fig_h
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
    def _free(b):
        if b[0] < 0.01 or b[2] > 0.99 or b[1] < 0.01 or b[3] > 0.99:
            return False
        return not any(b[0] < o[2] and b[2] > o[0] and b[1] < o[3] and b[3] > o[1] for o in boxes)
    def _put(fx, fy, text, fs, offsets, **kw):
        """Try the label at each (dx, dy) offset (axes fractions) from the anchor; True when drawn."""
        for dx, dy in offsets:
            cx, cy = fx + dx, fy + dy
            b = _box_of(cx, cy, text, fs)
            if _free(b):
                boxes.append(b)
                ax.text(cx, cy, text, transform=ax.transAxes, ha="center", va="center", fontsize=fs, zorder=kw.get("z", 6),
                        fontweight=kw.get("weight", "normal"), color=kw.get("color", "black"), alpha=kw.get("alpha", 1.0),
                        path_effects=[pe.withStroke(linewidth=kw.get("halo", 3.5), foreground="white")])
                return True
        return False

    drawn_dots = []
    for h in sorted(hits[:16], key=lambda h: (-category(h), h.lead_min)):
        fx, fy = _fx(h.lon), _fy(h.lat)
        if not (0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0):
            continue                 # outside this panel's view
        label = (f"{h.name}\n{CATEGORY_NAME[category(h)]}" if h.status == "over"
                 else f"{h.name}\n{CATEGORY_NAME[category(h)]}\n{window_text(h, exp.generated_utc)}")
        fs = 12.5
        offs = [(0, -0.075), (0, 0.075), (0.2, 0), (-0.2, 0), (0.2, -0.075), (-0.2, -0.075), (0.2, 0.075), (-0.2, 0.075),
                (0, -0.16), (0, 0.16)]
        _put(fx, fy, label, fs, offs, weight="bold", z=8, halo=4)
    for tname, tlat, tlon in town_cands:
        fx, fy = _fx(tlon), _fy(tlat)
        if not (0.02 < fx < 0.98 and 0.02 < fy < 0.98):
            continue
        n_before = len(boxes)
        if _put(fx, fy, tname, 10, [(0, 0.03), (0, -0.03), (0.07, 0), (-0.07, 0)], z=5, halo=3):
            ax.plot([_mx(tlon)], [_my(tlat)], marker="o", ms=5, mfc="#222222", mec="white", mew=1.0, zorder=5)
    for dname, x, y in dist_labels:
        _put(_fx(x), _fy(y), dname.upper(), 8.5, [(0, 0), (0, 0.04), (0, -0.04)], weight="bold", color="#555555", alpha=0.85, z=3, halo=2.5)
    ax.set_xlim(_mx(west), _mx(east))
    ax.set_ylim(_my(south), _my(north))
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_linewidth(1.5)

    where = ("Chennai" if all(h.name == "Chennai" for h in hits) else
             "Chennai and neighbouring districts" if all(h.name in ("Chennai", "Tiruvallur", "Chengalpattu", "Kancheepuram") for h in hits)
             else "Tamil Nadu and Puducherry")
    fig.suptitle("Experimental Rainfall Nowcast based on IMD Radar", fontsize=15.5, fontweight="bold", y=0.985)
    ax.set_title(f"{where}: rainbands\nColours: now {fmt_time(exp.generated_utc)}   Dashed: ~"
                 f"{fmt_time(exp.generated_utc + timedelta(minutes=lead_then))}"
                 + ("   Arrows: motion" if n_arrows else ""), fontsize=12.5, pad=8)
    cb = fig.colorbar(mesh, ax=ax, orientation="horizontal", fraction=0.045, pad=0.025, ticks=[20, 30, 40, 50])
    cb.ax.tick_params(labelsize=13)
    cb.set_label("radar echo strength (dBZ)    Moderate 35+   Heavy 45+" + (f"   Very heavy {INTENSE_DBZ:.0f}+" if VERY_HEAVY_ENABLED else ""), fontsize=11.5)
    fig.text(0.5, 0.008,
             "Extrapolated from current storm motion, not a guaranteed forecast.  chennairains.com\n"
             "IMD radar data. Map: © OpenStreetMap contributors © CARTO",
             ha="center", fontsize=10, color="#333", linespacing=1.4)
    fig.subplots_adjust(left=0.03, right=0.97, top=0.875, bottom=0.15)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return Path(path)


# --- sending ---------------------------------------------------------------------------
TELEGRAM_STATUS_FILE = Path("state_social_test/telegram_last.json")


def _record_telegram(ok: bool, detail: str) -> None:
    """Leaves the last Telegram outcome in the repo (no secrets) so a missing
    message can be diagnosed without access to the run log."""
    try:
        TELEGRAM_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        TELEGRAM_STATUS_FILE.write_text(json.dumps(
            {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": ok, "detail": detail}, indent=1))
    except Exception:
        pass


def _send_one(token: str, chat: str, caption: str, images: list) -> tuple[bool, str]:
    """One chat (group, channel or person). Returns (accepted, detail)."""
    try:
        if len(images) > 1:
            handles = [open(x, "rb") for x in images[:10]]
            try:
                media = [{"type": "photo", "media": f"attach://p{i}", **({"caption": caption} if i == 0 else {})}
                         for i in range(len(handles))]
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMediaGroup",
                                  data={"chat_id": chat, "media": json.dumps(media)},
                                  files={f"p{i}": fh for i, fh in enumerate(handles)}, timeout=60)
            finally:
                for fh in handles:
                    fh.close()
        elif images:
            with open(images[0], "rb") as fh:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                                  data={"chat_id": chat, "caption": caption},
                                  files={"photo": fh}, timeout=30)
        else:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": caption}, timeout=30)
        ok = r.ok and r.json().get("ok", False)
        return bool(ok), "sent" if ok else f"Telegram refused: HTTP {r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, f"request failed: {type(e).__name__}"


def send_telegram(caption: str, image_path: Path | None) -> bool:
    """TELEGRAM_CHAT_ID may hold several chat ids separated by commas (e.g. a group and a
    channel); the draft goes to each. True if Telegram accepted it for at least one of them.
    No credentials -> False (a dry run, never an error), so state is not marked as posted."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").replace(";", ",").split(",") if c.strip()]
    if not token or not chats:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chats)) if not v]
        msg = f"secret(s) empty or not visible to this repo's workflow: {', '.join(missing)}"
        print(f"[social] {msg} -- draft only, nothing sent")
        _record_telegram(False, msg)
        return False
    caption = caption[:1020]
    images = [Path(x) for x in (image_path if isinstance(image_path, (list, tuple)) else [image_path]) if x]
    images = [x for x in images if x.exists()]
    results = [(c, *_send_one(token, c, caption, images)) for c in dict.fromkeys(chats)]
    for c, ok, detail in results:
        if not ok:
            print(f"[social] chat {c[:6]}...: {detail}")   # id shortened in logs
    n_ok = sum(1 for _, ok, _ in results if ok)
    detail = "sent" if n_ok == len(results) else (f"sent to {n_ok} of {len(results)} chats; " +
             "; ".join(d for _, ok, d in results if not ok))
    _record_telegram(n_ok > 0, detail)
    return n_ok > 0


# --- synthetic data (link check + tests) --------------------------------------------------
def synthetic_export(now: datetime, storms: list[dict]) -> Export:
    """A made-up Tamil-Nadu-wide export. storms: dicts with lat, lon, toward (deg), speed (km/h),
    peak (dBZ) and radius (km, where echo falls to 20 dBZ)."""
    step = 0.02
    lats = 14.0 - np.arange(int(6.5 / step)) * step          # 14.0N .. 7.5N
    lons = 76.0 + np.arange(int(5.0 / step)) * step          # 76E .. 81E
    LAT, LON = np.meshgrid(lats, lons, indexing="ij")
    names, leads, layers, areas = [], [], [], []
    for lead in range(0, 100, 10):
        g = np.zeros(LAT.shape, dtype=np.uint8)
        for s in storms:
            d = s["speed"] * lead / 60.0
            cy = s["lat"] + d * math.cos(math.radians(s["toward"])) / 111.0
            cx = s["lon"] + d * math.sin(math.radians(s["toward"])) / (111.0 * math.cos(math.radians(s["lat"])))
            dist = np.hypot((LAT - cy) * 111.0, (LON - cx) * 111.0 * math.cos(math.radians(s["lat"])))
            core = 20 + (s["peak"] - 20) * (1 - dist / s["radius"])
            g = np.maximum(g, np.where(core >= 20, core, 0).astype(np.uint8))
        names.append(f"plus_{lead}"); leads.append(lead); layers.append(g)
    for s in storms:
        areas.append({"lat": s["lat"], "lon": s["lon"], "area_km2": math.pi * s["radius"] ** 2, "max_dbz": s["peak"],
                      "has_strong_core": True, "speed_kmh": s["speed"], "bearing_deg": s["toward"]})
    cells = [{"lat": a["lat"], "lon": a["lon"], "area_km2": a["area_km2"], "max_dbz": a["max_dbz"],
              "speed_kmh": a["speed_kmh"], "bearing_deg": a["bearing_deg"], "motion_reliable": True} for a in areas]
    names.insert(0, "observed"); leads.insert(0, 0); layers.insert(0, layers[0])
    doc = {"generated_utc": now.isoformat(timespec="seconds"),
           "radars": [{"product": "kkl_maxz", "radar": "karaikal", "used": True, "age_min": 10.0}],
           "rain_areas": areas, "cells": cells, "motion": {}}
    return Export(doc, np.stack(layers), lats, lons, names, leads, now)


def send_test_draft(tile_url: str | None = None, sender=send_telegram) -> bool:
    """Sends a clearly-labelled draft built from made-up storms (one over the
    Cuddalore/Villupuram coast, one west of Tiruchirappalli), to check the
    Telegram link and the map background end to end without waiting for real
    weather. Touches no state and no log."""
    import tempfile
    now = datetime.now(timezone.utc)
    exp = synthetic_export(now, [
        dict(lat=11.75, lon=79.65, toward=20, speed=25, peak=55, radius=38),
        dict(lat=10.8, lon=77.7, toward=80, speed=30, peak=50, radius=34)])
    a = assess(exp, now)
    if not a.hits:
        print("[social] test draft: made-up storms produced no hits (boundary file missing?)")
        return False
    text, listed = build_text(order_hits(a.hits, {h.name for h in a.hits}), exp)
    png = None
    try:
        png = render_images(exp, order_hits(a.hits, {h.name for h in a.hits}), Path(tempfile.mkdtemp()) / "test_draft.png", tile_url)
    except Exception as e:
        print(f"[social] test image failed: {e!r}")
    ok = sender("TEST DRAFT -- made-up storms, only checking the Telegram link and map background. "
                "Nothing here is real weather.\n\n" + text + "\n\n" + build_details(listed, order_hits(a.hits, {h.name for h in a.hits})), png)
    print(f"[social] test draft {'sent' if ok else 'NOT sent'}")
    return bool(ok)


# --- orchestration -----------------------------------------------------------------------
def _append_log(path: Path, entry: dict) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    lines.append(json.dumps(entry, separators=(",", ":")))
    path.write_text("\n".join(lines[-DECISION_LOG_MAX_LINES:]) + "\n")


def _prune_drafts(folder: Path) -> None:
    files = sorted(folder.glob("*.txt"))
    for old in files[:-DRAFTS_KEEP]:
        victims = [old, *old.parent.glob(f"{old.stem}*.png")]
        for f in victims:
            try:
                f.unlink()
            except OSError:
                pass


# --- public platforms --------------------------------------------------------------------
PUBLIC_POSTING = True                       # set False to switch the whole public-posting step off
PUBLIC_REGIONS = ("Chennai", "Tiruvallur", "Chengalpattu", "Kancheepuram")
# The handle also carries the owner's own posts, so the public wording does not say "automated" (Telegram keeps it).
PUBLIC_HEADER = "COMK Rainfall Nowcast"
PUBLIC_DISCLAIMER = DISCLAIMER.replace("Experimental, automated nowcast", "Experimental Nowcast").replace("IMD radar.", "IMD Radar.", 1)
X_DISCLAIMER = "Experimental Nowcast based on IMD Radar."   # "Updates at most once an hour" dropped 21 Oct: X follows the alerts now
# Facebook (19 Oct 2026): posts go to the main Chennai Rains Page, which has no automated-account label (X does), so the
# Facebook text says so itself, in the header and the disclaimer.
FB_HEADER = "COMK Experimental Automated Rainfall Nowcast"
FB_DISCLAIMER = PUBLIC_DISCLAIMER.replace("Experimental Nowcast", "Experimental Automated Nowcast", 1)


def _lead_with(text: str, names: list) -> str:
    """X only has room for 2-3 sentences: the sentences naming the Chennai-area districts that caused this post go first,
    the rest keep their (impact) order; Telegram keeps the pure impact order."""
    head, _, rest = text.partition("): ")
    body = rest.replace(HASHTAGS, "").strip()
    sents = [x.strip() for x in body.split(". ") if x.strip()]
    sents = [x if x.endswith(".") or x.endswith(")") else x + "." for x in sents]
    first = [x for x in sents if any(n in x for n in names) and not x.startswith("Rains over")]
    out = first + [x for x in sents if x not in first]
    return f"{head}): {' '.join(out)} {HASHTAGS}"


def _public_post(tg_text: str, png, changed: set, a: "Assessment", state: dict, entry: dict, now_utc: datetime) -> None:
    import social_publish
    pub = state.setdefault("public", {})
    involved = sorted(n for n in changed if n in PUBLIC_REGIONS)
    if not involved:
        entry["public"] = {"action": "skip", "reason": "no Chennai-area district among the changes"}
        return
    text = tg_text.replace("COMK Automated Rainfall Nowcast", PUBLIC_HEADER, 1)
    image = list(png) if isinstance(png, (list, tuple)) else png    # all the Telegram images (21 Oct 2026), not just the first
    x_text = social_publish.fit_x_text(_lead_with(text, involved), X_DISCLAIMER, HASHTAGS)
    fb_text = f"{text.replace(PUBLIC_HEADER, FB_HEADER, 1)}\n\n{FB_DISCLAIMER}"
    stamp = now_utc.astimezone(IST).strftime("%Y%m%d_%H%M%S")
    res = social_publish.publish(x_text, fb_text, image, stamp)
    entry["public"] = {"action": "send", "involved": involved, "mode": res["mode"], "x": res["x"], "facebook": res["facebook"]}
    if res["any_ok"]:
        pub["last_post_utc"] = now_utc.isoformat()


def run(json_path, grid_path, state_dir, now_utc: datetime | None = None,
        sender=send_telegram, tile_url: str | None = None, force: bool = False) -> dict:
    """One cycle. Returns what it decided (also appended to decision_log.jsonl).
    force=True is the manual trigger: post what is active right now, ignoring the quiet
    hours, the minimum gaps and 'already alerted'."""
    now_utc = now_utc or datetime.now(timezone.utc)
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path, log_path = state_dir / "alert_state.json", state_dir / "decision_log.jsonl"
    state = load_state(state_path)

    try:
        exp = load_export(json_path, grid_path)
        err = "export has no grid this cycle"
    except Exception as e:
        exp, err = None, f"could not read the export: {e!r}"
    ws = state.get("weak_seen") or {}
    prev_weak = set(ws.get("names", [])) if _mins(now_utc, _dt(ws.get("t"))) <= PERSIST_MAX_AGE_MIN else set()
    vs = state.get("vh_seen") or {}
    prev_vh = set(vs.get("names", [])) if _mins(now_utc, _dt(vs.get("t"))) <= PERSIST_MAX_AGE_MIN else set()
    hs = state.get("heavy_seen") or {}
    prev_heavy = set(hs.get("names", [])) if _mins(now_utc, _dt(hs.get("t"))) <= PERSIST_MAX_AGE_MIN else set()
    a = assess(exp, now_utc, prev_weak=prev_weak, prev_vh=prev_vh, prev_heavy=prev_heavy) if exp is not None else Assessment(False, err)
    if a.ok:
        state["weak_seen"] = {"t": now_utc.isoformat(), "names": a.weak}
        state["vh_seen"] = {"t": now_utc.isoformat(), "names": a.intense}
        state["heavy_seen"] = {"t": now_utc.isoformat(), "names": a.heavy}

    quiet_held = []
    if a.ok and not force and in_quiet_hours(now_utc):
        quiet_held = [h.name for h in a.hits if category(h) < QUIET_MIN_CATEGORY]
        a.hits = [h for h in a.hits if category(h) >= QUIET_MIN_CATEGORY]

    if not a.ok:
        action, reason, changed = "skip", a.reason, set()
    elif force:
        action, reason, changed = (("send", "manual trigger", {h.name for h in a.hits}) if a.hits
                                   else ("skip", "manual trigger: nothing active right now", set()))
        regs0 = state.setdefault("regions", {})
        for h in a.hits:
            regs0.setdefault(h.name, {"active": False, "last_post": None, "last_status": None, "last_rank": 0})
            regs0[h.name]["last_hit"] = now_utc.isoformat()
    else:
        action, reason, changed = decide(a.hits, state, now_utc)

    entry = {"t": now_utc.isoformat(timespec="seconds"), "action": action, "reason": reason,
             "over": [h.name for h in a.hits if h.status == "over"],
             "heading": {h.name: h.lead_min for h in a.hits if h.status == "approaching"},
             "max_dbz": round(max((h.max_dbz for h in a.hits), default=0.0), 1),
             "hits": {h.name: [h.status[0], h.max_dbz, h.core45, h.nodes, h.storm_nodes, round(h.storm_peak)] for h in a.hits}}
    # status, peak dBZ in the district, core nodes, strong nodes in the district, whole-storm size, whole-storm peak
    if a.uncovered:
        entry["no_coverage"] = len(a.uncovered)
    if quiet_held:
        entry["quiet_held"] = quiet_held       # 23:00-03:59 IST: below Very heavy, not announced
    h_unconf = [h.name for h in a.hits if is_heavy_raw(h) and not h.heavy_ok]
    if h_unconf:
        entry["heavy_unconfirmed"] = h_unconf   # looked Heavy this cycle only: announced as Moderate until the next cycle agrees
    unconf = [h.name for h in a.hits if is_intense(h) and not h.vh_ok]
    if unconf:
        entry["vh_unconfirmed"] = unconf        # looked Very heavy but not yet confirmed (second cycle / full-size cell): announced as Heavy at most
    if a.persisted:
        entry["persisted_small"] = a.persisted  # 6-7 node cells accepted because they were also seen last cycle
    if force:
        entry["manual"] = True
    if a.offshore:
        entry["offshore_held"] = a.offshore   # approaching, but the band is still over the sea and beyond OFFSHORE_MAX_KM

    regs_now = state.get("regions", {})
    continuing = {h.name for h in a.hits if h.name not in changed and regs_now.get(h.name, {}).get("active")
                  and regs_now.get(h.name, {}).get("last_post")}
    png = None
    # 22 Oct 2026: small weak rain still >= 30 min away is not named (it arrived only ~55 % of the time) and is not marked
    # as announced, so it can still trigger its own post once it is close
    shown = [h for h in a.hits if not weak_far(h)]
    if a.hits and not shown:
        shown = a.hits
    if action == "send":
        text, listed = build_text(order_hits(shown, changed), exp, continuing=continuing, changed=changed)
        stamp = now_utc.astimezone(IST).strftime("%Y%m%d_%H%M%S")
        drafts = state_dir / "drafts"
        drafts.mkdir(exist_ok=True)
        try:
            png = render_images(exp, listed, drafts / f"{stamp}.png", tile_url)
        except Exception as e:
            print(f"[social] image failed, sending text only: {e!r}")
        ordered = order_hits(shown, changed)
        details = build_details(listed, ordered, exp.generated_utc)
        caption = fit_caption(text, details if SHOW_DISTRICT_LINES else "")
        (drafts / f"{stamp}.txt").write_text(text + "\n")
        _prune_drafts(drafts)
        print(f"[social] DRAFT: {text}")
        sent = sender(caption, png)
        entry["sent"] = bool(sent)
        entry["text"] = text
        entry["listed"] = [h.name for h in listed]
        if sent:
            regs = state.setdefault("regions", {})
            for h in shown:        # everything summarised in this post (named or "+N more") counts as announced
                rank = category(h)     # Moderate -> Heavy -> Very heavy counts as an upgrade
                regs[h.name].update(active=True, last_post=now_utc.isoformat(), last_status=h.status, last_rank=rank,
                                    last_lat=round(h.lat, 3), last_lon=round(h.lon, 3))
            state["last_post_utc"] = now_utc.isoformat()
            state["post_times"] = [t for t in state.get("post_times", []) if _mins(now_utc, _dt(t)) < 120] + [now_utc.isoformat()]
            today = now_utc.astimezone(IST).strftime("%Y-%m-%d")
            posts = state.setdefault("posts", {})
            posts[today] = posts.get(today, 0) + 1
            for k in sorted(posts)[:-7]:
                del posts[k]
    else:
        print(f"[social] no post: {reason}")

    # ---- X (Twitter) / Facebook (21 Oct 2026): the SAME decision and the SAME text as Telegram, posted in the same cycle
    # whenever the post involves Chennai / Tiruvallur / Chengalpattu / Kancheepuram (so a Telegram post copied to social
    # media matches what X shows). Manual triggers stay Telegram-only. Dry run until PUBLIC_POST_ENABLED=true.
    if a.ok and not force and PUBLIC_POSTING and action == "send":
        try:
            _public_post(text, png, changed, a, state, entry, now_utc)
        except Exception as e:
            entry["public"] = {"action": "error", "reason": f"{type(e).__name__}: {e}"[:200]}
            print(f"[social] public posting failed (Telegram and the map are unaffected): {e!r}")

    state_path.write_text(json.dumps(state, indent=1))
    _append_log(log_path, entry)
    return entry
