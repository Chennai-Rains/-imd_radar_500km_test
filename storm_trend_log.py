"""Storm intensity-trend log (14 Oct 2026). Test-repo only; never touches the map or the alerts.

Every run it takes the OBSERVED layer of the bot export, cuts out storm blobs (>= 35 dBZ), matches each blob to the blobs
of the previous runs (predicted position from the measured storm motion) so the same storm keeps one track id, and
logs one JSON line per run to state_social_test/storm_trend.jsonl:

  per blob: track id, centroid, node counts at >=35/40/45/50 dBZ, strongest value, p90 dBZ, "mass" (sum of rain rate
  from Z=200 R^1.6 over the blob's nodes, mm/h x nodes -- an area x intensity measure far steadier than a single peak),
  whether it touches a no-coverage / grid edge, and the trend estimated from the track's last 5 points (log-slope of mass,
  n35 and n45 as %-per-10-min, plus the raw class intensifying / steady / weakening and a "persist" flag set when the
  last 3 raw classes agree).

storm_trend_eval.py later scores those calls against what the same tracks actually did 30 and 60 minutes on."""
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
from scipy import ndimage

LOG = Path("state_social_test/storm_trend.jsonl")
HIST_RUNS = 8            # previous runs read back from the log to continue tracks
MIN_DBZ = 35.0
MIN_NODES = 6            # blob must hold this many >=35 dBZ nodes (~30 km2 at 0.02 deg)
MATCH_KM = 25.0
MAX_GAP_MIN = 20.0       # no continuation across a longer hole in the record
SLOPE_PTS = 5
MIN_PTS = 3
MIN_SPAN_MIN = 15.0
UP_PCT, DOWN_PCT = 15.0, -15.0     # %-per-10-min on mass beyond which a track counts as intensifying / weakening
THRESH = (35, 40, 45, 50)


def _rain_rate(dbz):
    return (10.0 ** (dbz / 10.0) / 200.0) ** (1.0 / 1.6)


def _km(lat1, lon1, lat2, lon2):
    dy = (lat2 - lat1) * 111.0
    dx = (lon2 - lon1) * 111.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def _blobs(grid, lats, lons):
    g = grid.astype(np.int16)
    cover = g == 255
    strong = (g >= MIN_DBZ) & ~cover
    lab, n = ndimage.label(ndimage.binary_closing(strong, structure=np.ones((3, 3))), structure=np.ones((3, 3)))
    near_cov = ndimage.binary_dilation(cover, iterations=3)
    near_cov[:3, :] = near_cov[-3:, :] = True
    near_cov[:, :3] = near_cov[:, -3:] = True
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        m = (lab[sl] == i) & strong[sl]
        if m.sum() < MIN_NODES:
            continue
        vals = g[sl][m].astype(float)
        ys, xs = np.nonzero(m)
        ys = ys + sl[0].start
        xs = xs + sl[1].start
        out.append({
            "lat": round(float(lats[ys].mean()), 3), "lon": round(float(lons[xs].mean()), 3),
            "n": {str(t): int((vals >= t).sum()) for t in THRESH},
            "max": int(vals.max()), "p90": round(float(np.percentile(vals, 90)), 1),
            "mass": round(float(_rain_rate(vals).sum()), 1),
            "edge": bool(near_cov[ys, xs].any()),
        })
    return out


def _slope_pct(pts, key):
    """%-per-10-min log-slope of key over the last SLOPE_PTS points of a track; None if too short."""
    pts = pts[-SLOPE_PTS:]
    if len(pts) < MIN_PTS:
        return None
    t = np.array([p["tmin"] for p in pts], float)
    if t[-1] - t[0] < MIN_SPAN_MIN:
        return None
    y = np.log(np.array([key(p) for p in pts], float) + 1.0)
    b = np.polyfit(t - t[0], y, 1)[0]
    return round((math.exp(10.0 * b) - 1.0) * 100.0, 1)


def _cls(pct):
    if pct is None:
        return None
    return "intensifying" if pct >= UP_PCT else "weakening" if pct <= DOWN_PCT else "steady"


def _read_history():
    if not LOG.exists():
        return []
    rows = []
    for line in LOG.read_text().strip().splitlines()[-HIST_RUNS:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def log_trends(json_path, grid_path):
    import social_alerts
    ex = social_alerts.load_export(Path(json_path), Path(grid_path))
    if ex is None or "observed" not in ex.layer_names:
        return None
    version = ex.doc.get("version")
    hist = _read_history()
    if hist and hist[-1].get("version") == version:
        return None                      # this export is already logged
    t = ex.generated_utc
    mot = ex.doc.get("motion") or {}
    speed, bearing = mot.get("overall_speed_kmh"), mot.get("overall_bearing_deg")
    blobs = _blobs(ex.grid[ex.layer_names.index("observed")], ex.lats, ex.lons)

    # per-track history from the log: track id -> list of points (oldest first)
    tracks = {}
    for row in hist:
        tm = datetime.fromisoformat(row["t"])
        for b in row["blobs"]:
            tracks.setdefault(b["track"], []).append({
                "tmin": tm.timestamp() / 60.0, "lat": b["lat"], "lon": b["lon"], "mass": b["mass"],
                "n35": b["n"]["35"], "n45": b["n"]["45"], "raw": b.get("raw"), "t": row["t"]})
    next_id = 1 + max([int(k) for k in tracks] + [int(b["track"]) for r in hist for b in r["blobs"]] + [0])
    tnow = t.timestamp() / 60.0

    # greedy nearest matching of blobs to the last point of each live track
    cand = []
    for tid, pts in tracks.items():
        last = pts[-1]
        dt = tnow - last["tmin"]
        if dt <= 0 or dt > MAX_GAP_MIN:
            continue
        la, lo = last["lat"], last["lon"]
        if speed is not None and bearing is not None:
            d = speed * dt / 60.0
            la += d * math.cos(math.radians(bearing)) / 111.0
            lo += d * math.sin(math.radians(bearing)) / (111.0 * math.cos(math.radians(last["lat"])))
        for j, b in enumerate(blobs):
            dist = _km(la, lo, b["lat"], b["lon"])
            if dist <= MATCH_KM:
                cand.append((dist, tid, j))
    cand.sort()
    used_t, used_b, assign = set(), set(), {}
    for dist, tid, j in cand:
        if tid in used_t or j in used_b:
            continue
        used_t.add(tid)
        used_b.add(j)
        assign[j] = tid
    for j, b in enumerate(blobs):
        if j in assign:
            b["track"] = assign[j]
            b["new"] = False
        else:
            b["track"] = str(next_id)
            next_id += 1
            b["new"] = True
        pts = list(tracks.get(b["track"], [])) + [{"tmin": tnow, "mass": b["mass"], "n35": b["n"]["35"], "n45": b["n"]["45"]}]
        b["pts"] = len(pts)
        b["slope_mass"] = _slope_pct(pts, lambda p: p["mass"])
        b["slope_n35"] = _slope_pct(pts, lambda p: p["n35"])
        b["slope_n45"] = _slope_pct(pts, lambda p: p["n45"])
        b["raw"] = _cls(b["slope_mass"])
        prev_raw = [p.get("raw") for p in pts[:-1]][-2:]
        b["persist"] = bool(b["raw"] and len(prev_raw) == 2 and all(r == b["raw"] for r in prev_raw))
    rec = {"t": t.isoformat(timespec="seconds"), "version": version,
           "motion": {"kmh": speed, "bearing": bearing}, "n_blobs": len(blobs), "blobs": blobs}
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec
