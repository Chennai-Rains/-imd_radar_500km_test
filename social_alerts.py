"""Chennai-area storm alerts for social media -- SHADOW MODE.

Reads the bot data export the pipeline already writes every cycle
(nowcast_bot*.json + the gzip'd reflectivity grid with its 10-minute-step
"moved along the measured motion" layers), decides whether strong echo is
over or about to reach Chennai, and if so drafts ONE short post (text <= 280
characters, so the same wording works on X, Facebook and a WhatsApp Channel)
plus a two-panel image (now / expected). In shadow mode the draft goes only
to a private Telegram chat for review -- nothing is published anywhere
public. Posting to X / Facebook comes later, behind its own switch, once the
calls have been checked against real storm days.

Deliberately decoupled from nowcast_bot.py: it only reads the export files,
so it can be run, tested and tuned without touching detection or the map,
and run() can never take the pipeline down (the caller wraps it in
try/except as well).

What it does NOT do: it never claims rain, only "strong radar echoes"; the
dBZ -> wording is in severity_label(). Everything tunable is a constant
right below, so tuning after a few storm days is a one-line change.

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

# --- where / how strong ------------------------------------------------------
CHENNAI = (13.0827, 80.2707)
ZONE_RADIUS_KM = 40.0      # "Chennai area": city + immediate suburbs
ALERT_DBZ = 35.0           # grid node counts as strong echo at/above this
MIN_ZONE_NODES = 3         # grid step is ~2.2 km (~5 km2/node); fewer nodes is a speck, not a storm
MAX_LEAD_MIN = 90          # the export carries layers out to +90 min

# --- trust: when NOT to say anything -----------------------------------------
MAX_EXPORT_AGE_MIN = 30    # export itself older than this -> pipeline is not running properly
MAX_RADAR_AGE_MIN = 40     # freshest radar frame older than this -> nowcast is not "now"
MIN_ZONE_COVERAGE = 0.5    # share of the zone inside radar coverage; no coverage != dry, so stay quiet

# --- how often ----------------------------------------------------------------
CLEAR_AFTER_MIN = 45       # zone must be quiet this long before the next storm counts as a NEW episode
MIN_GAP_MIN = 30           # minimum time between two posts inside one episode

DECISION_LOG_MAX_LINES = 1500
DRAFTS_KEEP = 30

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


def bearing_deg(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def fmt_time(dt: datetime) -> str:
    s = dt.astimezone(IST).strftime("%I:%M %p").lstrip("0")
    return s


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


# --- the decision ---------------------------------------------------------------
@dataclass
class Assessment:
    status: str                       # "none" | "approaching" | "over" | "no_data"
    reason: str
    lead_min: int | None = None       # first lead at which strong echo is in the zone (0 = now)
    max_dbz: float = 0.0
    zone_nodes: int = 0
    when_utc: datetime | None = None
    from_dir: str | None = None       # where it is coming from, e.g. "SW"
    toward_dir: str | None = None
    speed_kmh: float | None = None
    extra: dict = field(default_factory=dict)


def assess(exp: Export, now_utc: datetime) -> Assessment:
    doc = exp.doc
    age = (now_utc - exp.generated_utc).total_seconds() / 60.0
    if age > MAX_EXPORT_AGE_MIN:
        return Assessment("no_data", f"export is {age:.0f} min old")
    used = [r for r in doc.get("radars", []) if r.get("used")]
    if not used:
        return Assessment("no_data", "no radar in use this cycle")
    ages = [r["age_min"] if r.get("age_min") is not None else 0.0 for r in used]
    if min(ages) > MAX_RADAR_AGE_MIN:
        return Assessment("no_data", f"freshest radar frame is {min(ages):.0f} min old")

    d = haversine_km_arr(exp.lats[:, None], exp.lons[None, :], CHENNAI[0], CHENNAI[1])
    zone = d <= ZONE_RADIUS_KM
    if not zone.any():
        return Assessment("no_data", "grid does not cover Chennai")

    # Layers to read: the moved-forward ones (plus_N) when the export has
    # motion, else just the observed picture (can say "over", not "approaching").
    idx = [(lead, i) for i, (n, lead) in enumerate(zip(exp.layer_names, exp.layer_leads))
           if n.startswith("plus_") and lead <= MAX_LEAD_MIN]
    if not idx:
        idx = [(0, exp.layer_names.index("observed"))]
    idx.sort()

    cover = (exp.grid[idx[0][1]][zone] != 255).mean()
    if cover < MIN_ZONE_COVERAGE:
        return Assessment("no_data", f"only {cover:.0%} of the Chennai zone is inside radar coverage")

    first = None
    max_dbz = 0.0
    nodes_at_first = 0
    for lead, i in idx:
        v = exp.grid[i][zone]
        strong = (v >= ALERT_DBZ) & (v < 255)
        n = int(strong.sum())
        if n >= MIN_ZONE_NODES:
            if first is None:
                first, nodes_at_first = lead, n
            max_dbz = max(max_dbz, float(v[strong].max()))
    if first is None:
        return Assessment("none", "no strong echo in or heading for the zone",
                          extra={"coverage": round(float(cover), 2)})

    a = Assessment("over" if first == 0 else "approaching", "strong echo in zone",
                   lead_min=first, max_dbz=max_dbz, zone_nodes=nodes_at_first,
                   when_utc=exp.generated_utc + timedelta(minutes=first),
                   extra={"coverage": round(float(cover), 2)})

    # Where is it coming from / going: the nearest strong rain area's own motion.
    best = None
    for ar in doc.get("rain_areas", []):
        if not ar.get("has_strong_core") or ar.get("max_dbz", 0) < ALERT_DBZ:
            continue
        dist = float(haversine_km_arr(ar["lat"], ar["lon"], CHENNAI[0], CHENNAI[1]))
        if dist <= 250 and (best is None or dist < best[0]):
            best = (dist, ar)
    if best is not None:
        dist, ar = best
        if dist > 10 and first > 0:
            a.from_dir = compass(bearing_deg(CHENNAI[0], CHENNAI[1], ar["lat"], ar["lon"]))
        sp, br = ar.get("speed_kmh"), ar.get("bearing_deg")
        if sp is not None and br is not None and sp >= 5:
            a.speed_kmh, a.toward_dir = float(sp), compass(float(br))
    if a.speed_kmh is None:
        m = doc.get("motion", {})
        if m.get("available") and (m.get("overall_speed_kmh") or 0) >= 5:
            a.speed_kmh, a.toward_dir = float(m["overall_speed_kmh"]), compass(float(m["overall_bearing_deg"]))
    return a


# --- the wording -----------------------------------------------------------------
def build_text(a: Assessment, exp: Export) -> str:
    sev, _ = severity_label(a.max_dbz)
    asof = fmt_time(exp.generated_utc)
    motion = ""
    if a.speed_kmh is not None and a.toward_dir:
        motion = f", moving {a.toward_dir} at ~{round(a.speed_kmh / 5) * 5:.0f} km/h"
    if a.status == "over":
        text = (f"Radar nowcast ({asof} IST): {sev} echoes (up to ~{a.max_dbz:.0f} dBZ) "
                f"are over the Chennai area now{motion}. #ChennaiRains")
    else:
        src = f" from the {a.from_dir}" if a.from_dir else ""
        text = (f"Radar nowcast ({asof} IST): {sev} echoes (up to ~{a.max_dbz:.0f} dBZ) "
                f"approaching Chennai{src}{motion}. May reach the city around "
                f"{fmt_time(a.when_utc)} IST. #ChennaiRains")
    if len(text) > 280:
        text = text.replace(motion, "") if motion else text
    return text


# --- episode / dedup state ---------------------------------------------------------
def load_state(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {"episode_active": False, "last_post_utc": None, "last_hit_utc": None,
                "last_status": None, "last_rank": 0}


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def decide(a: Assessment, state: dict, now_utc: datetime) -> tuple[str, str]:
    """("send"|"skip", reason). Mutates state only for bookkeeping that does
    not depend on whether the post actually went out (hit time, episode end);
    the caller records the post itself once it succeeded."""
    if a.status == "no_data":
        return "skip", a.reason
    last_hit = _dt(state.get("last_hit_utc"))
    if a.status == "none":
        if state.get("episode_active") and last_hit and (now_utc - last_hit).total_seconds() / 60 >= CLEAR_AFTER_MIN:
            state["episode_active"] = False
            return "skip", "episode ended (zone quiet long enough)"
        return "skip", a.reason

    state["last_hit_utc"] = now_utc.isoformat()
    _, rank = severity_label(a.max_dbz)
    if not state.get("episode_active"):
        return "send", "new episode"
    last_post = _dt(state.get("last_post_utc"))
    gap = (now_utc - last_post).total_seconds() / 60 if last_post else 1e9
    escalated = ((a.status == "over" and state.get("last_status") == "approaching")
                 or rank > state.get("last_rank", 0))
    if escalated and gap >= MIN_GAP_MIN:
        return "send", "update: " + ("reached the zone" if a.status == "over" and state.get("last_status") == "approaching"
                                    else "got stronger")
    if escalated:
        return "skip", f"would update but last post was {gap:.0f} min ago (< {MIN_GAP_MIN})"
    return "skip", "already alerted for this episode"


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


def render_image(exp: Export, a: Assessment, path: Path, tile_url: str | None = None) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import BoundaryNorm, ListedColormap

    west, east, south, north = 78.7, 81.3, 11.8, 14.4
    bg, bg_ext, tiles_ok = (None, None, 0)
    if tile_url:
        try:
            bg, bg_ext, tiles_ok = _tile_mosaic(west - 0.2, south - 0.2, east + 0.2, north + 0.2, 8, tile_url)
        except Exception:
            bg = None

    cmap = ListedColormap(["#a8e6a1", "#5fcf6a", "#f4e04d", "#f7a936", "#ee6a2e", "#d62828", "#a4133c", "#7b2cbf"])
    norm = BoundaryNorm([20, 25, 30, 35, 40, 45, 50, 55, 200], cmap.N)

    lead_then = a.lead_min if (a.lead_min and a.lead_min > 0) else 30
    wanted = [("Now", 0), (f"+{lead_then} min", lead_then)]
    names = {n: i for i, n in enumerate(exp.layer_names)}

    def layer_for(lead):
        key = f"plus_{lead}"
        return exp.grid[names[key]] if key in names else exp.grid[names["observed"]]

    fig, axes = plt.subplots(1, 2, figsize=(10.4, 5.6), dpi=110)
    X, Y = _mx(exp.lons), _my(exp.lats)
    ring_t = np.linspace(0, 2 * math.pi, 120)
    ring_lat = CHENNAI[0] + (ZONE_RADIUS_KM / 111.0) * np.sin(ring_t)
    ring_lon = CHENNAI[1] + (ZONE_RADIUS_KM / (111.0 * math.cos(math.radians(CHENNAI[0])))) * np.cos(ring_t)
    for ax, (title, lead) in zip(axes, wanted):
        if bg is not None:
            ax.imshow(bg, extent=bg_ext, origin="upper", interpolation="bilinear", zorder=0)
        else:
            ax.set_facecolor("#e2e8ec")
        g = layer_for(lead).astype(float)
        vals = np.ma.masked_where((g < 20) | (g >= 255), g)
        ax.pcolormesh(X, Y, vals, cmap=cmap, norm=norm, shading="nearest", alpha=0.82, zorder=2)
        ax.plot(_mx(ring_lon), _my(ring_lat), color="#222", lw=1.0, ls="--", zorder=3)
        ax.plot([_mx(CHENNAI[1])], [_my(CHENNAI[0])], marker="o", ms=6, mfc="white", mec="black", zorder=4)
        ax.annotate("Chennai", (_mx(CHENNAI[1]), _my(CHENNAI[0])), xytext=(7, -12), textcoords="offset points",
                    fontsize=9, fontweight="bold", zorder=5)
        ax.set_xlim(_mx(west), _mx(east))
        ax.set_ylim(_my(south), _my(north))
        ax.set_aspect("equal")
        ax.set_xticks([]); ax.set_yticks([])
        when = exp.generated_utc + timedelta(minutes=lead)
        ax.set_title(f"{title}  ({fmt_time(when)} IST)", fontsize=11, fontweight="bold")
    sev, _ = severity_label(a.max_dbz)
    fig.suptitle(f"Chennai radar nowcast: {sev} echoes up to ~{a.max_dbz:.0f} dBZ", fontsize=13, fontweight="bold")
    fig.text(0.5, 0.012,
             "Extrapolation of current storm motion from IMD radar data, not a guaranteed forecast.  "
             "chennairains.com   Map: © OpenStreetMap contributors © CARTO",
             ha="center", fontsize=7.5, color="#444")
    fig.tight_layout(rect=(0, 0.03, 1, 0.95))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return Path(path)


# --- sending ---------------------------------------------------------------------------
def send_telegram(caption: str, image_path: Path | None) -> bool:
    """True only if Telegram accepted it. No credentials -> False (a dry run,
    never an error), so state is not marked as posted."""
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("[social] TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set -- draft only, nothing sent")
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
        if not ok:
            print(f"[social] Telegram refused the draft: {r.status_code} {r.text[:200]}")
        return bool(ok)
    except Exception as e:
        print(f"[social] Telegram send failed: {e!r}")
        return False


# --- orchestration -----------------------------------------------------------------------
def _append_log(path: Path, entry: dict) -> None:
    lines = []
    if path.exists():
        lines = path.read_text().splitlines()
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
        load_err = "export has no grid this cycle"
    except Exception as e:
        exp, load_err = None, f"could not read the export: {e!r}"
    if exp is None:
        a = Assessment("no_data", load_err)
    else:
        a = assess(exp, now_utc)
    action, reason = decide(a, state, now_utc)

    entry = {"t": now_utc.isoformat(timespec="seconds"), "status": a.status, "lead": a.lead_min,
             "max_dbz": round(a.max_dbz, 1), "nodes": a.zone_nodes, "action": action, "reason": reason}

    if action == "send":
        text = build_text(a, exp)
        stamp = now_utc.astimezone(IST).strftime("%Y%m%d_%H%M%S")
        drafts = state_dir / "drafts"
        drafts.mkdir(exist_ok=True)
        png = None
        try:
            png = render_image(exp, a, drafts / f"{stamp}.png", tile_url)
        except Exception as e:
            print(f"[social] image failed, sending text only: {e!r}")
        caption = (f"SHADOW DRAFT (not posted anywhere public)\n\n{text}\n\n"
                   f"Why: {reason}; status={a.status}, first strong echo in zone at +{a.lead_min} min, "
                   f"{a.zone_nodes} nodes >= {ALERT_DBZ:.0f} dBZ within {ZONE_RADIUS_KM:.0f} km.")
        (drafts / f"{stamp}.txt").write_text(text + "\n")
        _prune_drafts(drafts)
        print(f"[social] DRAFT: {text}")
        sent = sender(caption, png)
        entry["sent"] = bool(sent)
        entry["text"] = text
        if sent:
            _, rank = severity_label(a.max_dbz)
            state.update(episode_active=True, last_post_utc=now_utc.isoformat(),
                         last_status=a.status, last_rank=rank)
    else:
        print(f"[social] no post: {reason}")

    state_path.write_text(json.dumps(state, indent=1))
    _append_log(log_path, entry)
    return entry
