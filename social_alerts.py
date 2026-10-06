"""Tamil Nadu storm alerts for social media -- SHADOW MODE.

Reads the bot data export the pipeline already writes every cycle
(nowcast_bot*.json + the gzip'd reflectivity grid with its 10-minute-step
"moved along the measured motion" layers) and decides, region by region,
whether strong echo is over or about to reach it. Regions are Chennai (a 40 km
zone, as before) plus every Tamil Nadu district (outlines in
assets/tn_districts.geojson, used only to say which district an echo lies in).

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
posts are at least GLOBAL_MIN_GAP_MIN apart, and there is a daily cap.
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

# --- where / how strong ------------------------------------------------------
CHENNAI = (13.0827, 80.2707)
CHENNAI_ZONE_KM = 40.0     # "Chennai": city + immediate suburbs (the old single zone)
ALERT_DBZ = 35.0           # grid node counts as strong echo at/above this
MIN_REGION_NODES = 8       # grid step is ~2.2 km (~5 km2/node): ~40 km2 of strong echo. 6 nodes on an edge was a real false alarm (5 Oct)
MAX_LEAD_MIN = 90          # the export carries layers out to +90 min

# Names as the audience writes them (the boundary file uses other spellings).
RENAME = {"Thiruvallur": "Tiruvallur", "Viluppuram": "Villupuram", "Thoothukkudi": "Thoothukudi",
          "Tirupathur": "Tirupattur", "Thiruvarur": "Tiruvarur"}
SKIP_DISTRICTS = {"Chennai"}   # covered by the Chennai zone, which is the same ground

# --- trust: when NOT to say anything -----------------------------------------
MAX_EXPORT_AGE_MIN = 30    # export itself older than this -> pipeline is not running properly
MAX_RADAR_AGE_MIN = 40     # freshest radar frame older than this -> nowcast is not "now"
MIN_REGION_COVERAGE = 0.5  # share of a region inside radar coverage; no coverage != dry, so stay quiet there

# --- how often ----------------------------------------------------------------
CLEAR_AFTER_MIN = 45       # a region must be quiet this long before a new storm there is a NEW episode
MIN_GAP_MIN = 30           # minimum time between two posts about the same region
GLOBAL_MIN_GAP_MIN = 20    # minimum time between any two posts
MAX_POSTS_PER_DAY = 12     # safety cap while in shadow mode (IST calendar day)
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


def build_regions(lats, lons, geojson_path: Path = TN_GEOJSON) -> list[Region]:
    """Chennai zone first, then the districts that have at least one node on the grid."""
    regions = []
    dlat = CHENNAI_ZONE_KM / 111.0
    dlon = CHENNAI_ZONE_KM / (111.0 * math.cos(math.radians(CHENNAI[0])))
    rs, cs = _box(lats, lons, CHENNAI[0] - dlat, CHENNAI[0] + dlat, CHENNAI[1] - dlon, CHENNAI[1] + dlon)
    if rs.stop > rs.start and cs.stop > cs.start:
        LAT, LON = np.meshgrid(lats[rs], lons[cs], indexing="ij")
        sub = haversine_km_arr(LAT, LON, CHENNAI[0], CHENNAI[1]) <= CHENNAI_ZONE_KM
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


@dataclass
class Assessment:
    ok: bool
    reason: str
    hits: list = field(default_factory=list)
    uncovered: list = field(default_factory=list)
    n_regions: int = 0


def assess(exp: Export, now_utc: datetime, regions: list[Region] | None = None) -> Assessment:
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

    hits, uncovered = [], []
    for reg in regions:
        first_layer = exp.grid[idx[0][1]][reg.rs, reg.cs][reg.sub]
        if (first_layer != 255).mean() < MIN_REGION_COVERAGE:
            uncovered.append(reg.name)
            continue
        first, nodes_first, max_dbz, cen = None, 0, 0.0, None
        for lead, i in idx:
            v = exp.grid[i][reg.rs, reg.cs]
            strong = (v >= ALERT_DBZ) & (v < 255) & reg.sub
            n = int(strong.sum())
            if n >= reg.min_nodes:
                if first is None:
                    first, nodes_first = lead, n
                    ys, xs = np.where(strong)
                    cen = (float(exp.lats[reg.rs][ys].mean()), float(exp.lons[reg.cs][xs].mean()))
                max_dbz = max(max_dbz, float(v[strong].max()))
        if first is not None:
            hits.append(Hit(reg.name, "over" if first == 0 else "approaching", first, max_dbz, nodes_first,
                            exp.generated_utc + timedelta(minutes=first), cen[0], cen[1]))
    return Assessment(True, "ok", hits, uncovered, len(regions))


# --- episode / dedup state ---------------------------------------------------------
def load_state(path: Path) -> dict:
    try:
        st = json.loads(Path(path).read_text())
        if isinstance(st.get("regions"), dict):
            return st
    except Exception:
        pass
    return {"regions": {}, "last_post_utc": None, "posts": {}}


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def _mins(now, then):
    return (now - then).total_seconds() / 60.0 if then else 1e9


def decide(hits: list[Hit], state: dict, now_utc: datetime) -> tuple[str, str, set]:
    """("send"|"skip", reason, names of regions that are new or upgraded).
    Mutates state only for bookkeeping that does not depend on whether a post
    went out (last hit time, episode end); the caller records the post itself
    once it succeeded."""
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
    new, upgraded = [], []
    for h in hits:
        st = regs[h.name]
        if not st.get("active"):
            new.append(h.name); changed.add(h.name); continue
        _, rank = severity_label(h.max_dbz)
        went_over = h.status == "over" and st.get("last_status") == "approaching"
        if (went_over or rank > st.get("last_rank", 0)) and _mins(now_utc, _dt(st.get("last_post"))) >= MIN_GAP_MIN:
            upgraded.append(h.name); changed.add(h.name)
    if not changed:
        return "skip", f"already alerted for the {len(hits)} active region(s)", set()
    gap = _mins(now_utc, _dt(state.get("last_post_utc")))
    if gap < GLOBAL_MIN_GAP_MIN:
        return "skip", f"{len(changed)} change(s) waiting, last post was {gap:.0f} min ago (< {GLOBAL_MIN_GAP_MIN})", set()
    today = now_utc.astimezone(IST).strftime("%Y-%m-%d")
    if state.get("posts", {}).get(today, 0) >= MAX_POSTS_PER_DAY:
        return "skip", f"daily cap of {MAX_POSTS_PER_DAY} posts reached", set()
    return "send", f"{len(new)} new, {len(upgraded)} upgraded", changed


# --- the wording -----------------------------------------------------------------------
def overall_motion(exp: Export, hits: list[Hit]) -> str:
    """"moving NE ~25 km/h" from the strong rain areas near the listed regions,
    only when they agree on direction; otherwise nothing (better silent than wrong)."""
    vecs, wts = [], []
    for ar in exp.doc.get("rain_areas", []):
        sp, br = ar.get("speed_kmh"), ar.get("bearing_deg")
        if not ar.get("has_strong_core") or ar.get("max_dbz", 0) < ALERT_DBZ or sp is None or br is None or sp < 5:
            continue
        if min(float(haversine_km_arr(ar["lat"], ar["lon"], h.lat, h.lon)) for h in hits) > 120:
            continue
        vecs.append((sp * math.sin(math.radians(br)), sp * math.cos(math.radians(br))))
        wts.append(max(ar.get("area_km2", 1.0), 1.0))
    if not vecs:
        return ""
    w = np.array(wts) / sum(wts)
    u = float((np.array([v[0] for v in vecs]) * w).sum()); v = float((np.array([v[1] for v in vecs]) * w).sum())
    mean_speed = float((np.hypot([x[0] for x in vecs], [x[1] for x in vecs]) * w).sum())
    speed = math.hypot(u, v)
    if speed < 8 or speed < 0.85 * mean_speed:     # slow, or the areas disagree on direction
        return ""
    return f"Moving {compass(math.degrees(math.atan2(u, v)) % 360)} ~{round(speed / 5) * 5:.0f} km/h."


def order_hits(hits: list[Hit], changed: set) -> list[Hit]:
    """New/upgraded first (they are why we are posting), then Chennai, 'over' before
    'heading for', then strongest."""
    return sorted(hits, key=lambda h: (h.name not in changed, h.name != "Chennai", h.status != "over",
                                       -h.max_dbz, -h.nodes))


def build_text(hits_ordered: list[Hit], exp: Export) -> tuple[str, list[Hit]]:
    """Digest of at most MAX_NAMED_REGIONS names that fits 280 characters; returns the
    text and the hits it names. The others are summarised as "+N more districts"."""
    asof = fmt_time(exp.generated_utc)

    def compose(sel: list[Hit], dropped: int, with_motion: bool) -> str:
        sev, _ = severity_label(max(h.max_dbz for h in sel))
        over = [h.name for h in sel if h.status == "over"]
        groups: dict[int, list[str]] = {}
        for h in sel:
            if h.status == "approaching":
                groups.setdefault(h.lead_min, []).append(h.name)
        appr = "; ".join(f"{', '.join(names)} (~{fmt_time(exp.generated_utc + timedelta(minutes=lead))})"
                         for lead, names in sorted(groups.items()))
        if over and appr:
            body = f"{sev} echoes over {', '.join(over)}; heading for {appr}."
        elif over:
            body = f"{sev} echoes over {', '.join(over)}."
        else:
            body = f"{sev} echoes heading for {appr}."
        if dropped:
            body += f" (+{dropped} more districts)"
        motion = overall_motion(exp, sel) if with_motion else ""
        return " ".join(x for x in (f"Radar nowcast ({asof} IST):", body, motion, HASHTAGS) if x)

    for n in range(min(len(hits_ordered), MAX_NAMED_REGIONS), 0, -1):
        for with_motion in (True, False):
            text = compose(hits_ordered[:n], len(hits_ordered) - n, with_motion)
            if len(text) <= 280:
                return text, hits_ordered[:n]
    return compose(hits_ordered[:1], len(hits_ordered) - 1, False)[:280], hits_ordered[:1]


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


VIEW_MIN_KM, VIEW_MAX_KM = 45.0, 260.0   # half-width of the map


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


def render_image(exp: Export, hits: list[Hit], path: Path, tile_url: str | None = None,
                 view_hits: list[Hit] | None = None) -> Path:
    """One large square map, readable on a phone: filled colours are the echo
    now, the dashed outline is where strong echo is expected at the horizon,
    and each listed region is named where its echo is."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe
    from matplotlib.colors import BoundaryNorm, ListedColormap

    names = {n: i for i, n in enumerate(exp.layer_names)}
    clat, clon, half = view_box(exp, view_hits or hits, names)
    dlat = half / 111.0
    dlon = half / (111.0 * math.cos(math.radians(clat)))
    west, east, south, north = clon - dlon, clon + dlon, clat - dlat, clat + dlat

    bg, bg_ext = None, None
    if tile_url:
        try:
            zoom = 9 if half <= 55 else (8 if half <= 130 else 7)
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
            ax.contour(X, Y, strong, levels=[0.5], colors="black", linewidths=3.0, linestyles="--", zorder=4)

    if any(h.name == "Chennai" for h in hits):
        t = np.linspace(0, 2 * math.pi, 120)
        ax.plot(_mx(CHENNAI[1] + (CHENNAI_ZONE_KM / (111.0 * math.cos(math.radians(CHENNAI[0])))) * np.cos(t)),
                _my(CHENNAI[0] + (CHENNAI_ZONE_KM / 111.0) * np.sin(t)), color="#1a1a1a", lw=2.0, ls=":", zorder=3)
        ax.plot([_mx(CHENNAI[1])], [_my(CHENNAI[0])], marker="o", ms=9, mfc="white", mec="black", mew=2.2, zorder=5)
    # Direction arrows, same look as the radar page: a dark triangle with a white edge,
    # placed a little ahead of each moving strong rain area, pointing along its bearing.
    from matplotlib.markers import MarkerStyle
    tri = np.array([[0, 1.0], [-0.73, -0.73], [0, -0.27], [0.73, -0.73], [0, 1.0]])
    n_arrows = 0
    for ar in exp.doc.get("rain_areas", []):
        sp, br = ar.get("speed_kmh"), ar.get("bearing_deg")
        if sp is None or br is None or sp < 5 or ar.get("max_dbz", 0) < 30:
            continue
        a_lat = ar["lat"] + (sp * 0.4 * math.cos(math.radians(br))) / 111.0
        a_lon = ar["lon"] + (sp * 0.4 * math.sin(math.radians(br))) / (111.0 * math.cos(math.radians(ar["lat"])))
        if not (west < a_lon < east and south < a_lat < north):
            continue
        ax.plot([_mx(a_lon)], [_my(a_lat)], marker=MarkerStyle(tri).rotated(deg=-br), ms=19,
                mfc="#222222", mec="white", mew=1.6, linestyle="none", zorder=7)
        n_arrows += 1
    placed = []                      # label centres already drawn, as fractions of the map box
    for h in hits[:8]:
        fx = (_mx(h.lon) - _mx(west)) / (_mx(east) - _mx(west))
        fy = (_my(h.lat) - _my(south)) / (_my(north) - _my(south))
        if any(abs(fx - px) < 0.24 and abs(fy - py) < 0.09 for px, py in placed):
            continue                 # would sit on top of another label; the text of the post still names it
        placed.append((fx, fy))
        label = h.name if h.status == "over" else f"{h.name}\n~{fmt_time(h.when_utc)}"
        ax.annotate(label, (_mx(h.lon), _my(h.lat)), xytext=(0, -22), textcoords="offset points", ha="center", va="top", fontsize=14 if half < 120 else 12,
                    fontweight="bold", zorder=6, path_effects=halo)
    ax.set_xlim(_mx(west), _mx(east))
    ax.set_ylim(_my(south), _my(north))
    ax.set_aspect("equal")
    ax.set_xticks([]); ax.set_yticks([])
    for sp in ax.spines.values():
        sp.set_linewidth(1.5)

    sev, _ = severity_label(max(h.max_dbz for h in hits))
    where = "Chennai" if all(h.name == "Chennai" for h in hits) else "Tamil Nadu"
    fig.suptitle(f"{where} radar nowcast: {sev} echoes", fontsize=17.5, fontweight="bold", y=0.985)
    ax.set_title(f"Colours: now {fmt_time(exp.generated_utc)}   Dashed: ~"
                 f"{fmt_time(exp.generated_utc + timedelta(minutes=lead_then))}"
                 + ("   Arrows: motion" if n_arrows else ""), fontsize=12.5, pad=8)
    cb = fig.colorbar(mesh, ax=ax, orientation="horizontal", fraction=0.045, pad=0.025, ticks=[20, 30, 40, 50])
    cb.ax.tick_params(labelsize=13)
    cb.set_label("radar echo strength (dBZ)", fontsize=13)
    fig.text(0.5, 0.008,
             "Extrapolated from current storm motion, not a guaranteed forecast.  chennairains.com\n"
             "IMD radar data. Map: © OpenStreetMap contributors © CARTO",
             ha="center", fontsize=10, color="#333", linespacing=1.4)
    fig.subplots_adjust(left=0.03, right=0.97, top=0.91, bottom=0.15)
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


def send_telegram(caption: str, image_path: Path | None) -> bool:
    """True only if Telegram accepted it. No credentials -> False (a dry run,
    never an error), so state is not marked as posted."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("TELEGRAM_CHAT_ID", chat)) if not v]
        msg = f"secret(s) empty or not visible to this repo's workflow: {', '.join(missing)}"
        print(f"[social] {msg} -- draft only, nothing sent")
        _record_telegram(False, msg)
        return False
    caption = caption[:1020]
    try:
        if image_path and Path(image_path).exists():
            with open(image_path, "rb") as fh:
                r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                                  data={"chat_id": chat, "caption": caption},
                                  files={"photo": fh}, timeout=30)
        else:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": caption}, timeout=30)
        ok = r.ok and r.json().get("ok", False)
        detail = "sent" if ok else f"Telegram refused: HTTP {r.status_code} {r.text[:200]}"
        if not ok:
            print(f"[social] {detail}")
        _record_telegram(bool(ok), detail)
        return bool(ok)
    except Exception as e:
        print(f"[social] Telegram send failed: {e!r}")
        _record_telegram(False, f"request failed: {type(e).__name__}")
        return False


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
    names.insert(0, "observed"); leads.insert(0, 0); layers.insert(0, layers[0])
    doc = {"generated_utc": now.isoformat(timespec="seconds"),
           "radars": [{"product": "kkl_maxz", "radar": "karaikal", "used": True, "age_min": 10.0}],
           "rain_areas": areas, "motion": {}}
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
        png = render_image(exp, listed, Path(tempfile.mkdtemp()) / "test_draft.png", tile_url)
    except Exception as e:
        print(f"[social] test image failed: {e!r}")
    ok = sender("TEST DRAFT -- made-up storms, only checking the Telegram link and map background. "
                "Nothing here is real weather.\n\n" + text, png)
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
        for ext in (".txt", ".png"):
            try:
                old.with_suffix(ext).unlink()
            except OSError:
                pass


def run(json_path, grid_path, state_dir, now_utc: datetime | None = None,
        sender=send_telegram, tile_url: str | None = None) -> dict:
    """One cycle. Returns what it decided (also appended to decision_log.jsonl)."""
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
    a = assess(exp, now_utc) if exp is not None else Assessment(False, err)

    if not a.ok:
        action, reason, changed = "skip", a.reason, set()
    else:
        action, reason, changed = decide(a.hits, state, now_utc)

    entry = {"t": now_utc.isoformat(timespec="seconds"), "action": action, "reason": reason,
             "over": [h.name for h in a.hits if h.status == "over"],
             "heading": {h.name: h.lead_min for h in a.hits if h.status == "approaching"},
             "max_dbz": round(max((h.max_dbz for h in a.hits), default=0.0), 1)}
    if a.uncovered:
        entry["no_coverage"] = len(a.uncovered)

    if action == "send":
        text, listed = build_text(order_hits(a.hits, changed), exp)
        stamp = now_utc.astimezone(IST).strftime("%Y%m%d_%H%M%S")
        drafts = state_dir / "drafts"
        drafts.mkdir(exist_ok=True)
        png = None
        try:
            png = render_image(exp, listed, drafts / f"{stamp}.png", tile_url, view_hits=a.hits)
        except Exception as e:
            print(f"[social] image failed, sending text only: {e!r}")
        why = "\n".join(f"- {h.name}: {'over now' if h.status == 'over' else f'+{h.lead_min} min'}, "
                        f"{h.max_dbz:.0f} dBZ, {h.nodes} strong nodes" for h in listed[:10])
        if len(a.hits) > len(listed):
            why += f"\n- ...and {len(a.hits) - len(listed)} more: " + ", ".join(
                h.name for h in order_hits(a.hits, changed) if h not in listed)
        caption = (f"SHADOW DRAFT (not posted anywhere public)\n\n{text}\n\nWhy ({reason}):\n{why}")
        (drafts / f"{stamp}.txt").write_text(text + "\n")
        _prune_drafts(drafts)
        print(f"[social] DRAFT: {text}")
        sent = sender(caption, png)
        entry["sent"] = bool(sent)
        entry["text"] = text
        entry["listed"] = [h.name for h in listed]
        if sent:
            regs = state.setdefault("regions", {})
            for h in a.hits:       # everything summarised in this post (named or "+N more") counts as announced
                _, rank = severity_label(h.max_dbz)
                regs[h.name].update(active=True, last_post=now_utc.isoformat(), last_status=h.status, last_rank=rank)
            state["last_post_utc"] = now_utc.isoformat()
            today = now_utc.astimezone(IST).strftime("%Y-%m-%d")
            posts = state.setdefault("posts", {})
            posts[today] = posts.get(today, 0) + 1
            for k in sorted(posts)[:-7]:
                del posts[k]
    else:
        print(f"[social] no post: {reason}")

    state_path.write_text(json.dumps(state, indent=1))
    _append_log(log_path, entry)
    return entry
