"""One-off: build assets/tn_places.json (towns and cities in Tamil Nadu) from OpenStreetMap.

Run by .github/workflows/fetch_osm_places.yml (this workspace cannot reach OSM).
Output: a thinned list of places, each {name, lat, lon, kind, district}.
Data (c) OpenStreetMap contributors, ODbL.
"""
import difflib, json, math, sys, time
import urllib.parse, urllib.request
from pathlib import Path
from matplotlib.path import Path as MPath

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "tn_places.json"
ENDPOINTS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
QUERY = """[out:json][timeout:180];
(
  node["place"~"^(city|town)$"](8.0,76.2,13.7,80.5);
);
out tags center;"""
THIN_KM = 9.0          # drop a place if a more important one is within this distance
CHENNAI = (13.0827, 80.2707)


def fetch():
    last = None
    for url in ENDPOINTS:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": QUERY}).encode(),
                                             headers={"User-Agent": "ChennaiRains-nowcast/1.0 (places gazetteer)"})
                with urllib.request.urlopen(req, timeout=240) as r:
                    return json.load(r)
            except Exception as e:  # noqa
                last = e
                print("fetch failed", url, attempt, e, file=sys.stderr)
                time.sleep(15)
    raise SystemExit(f"Overpass unreachable: {last}")


def km(a, b):
    dlat = (a[0] - b[0]) * 111.0
    dlon = (a[1] - b[1]) * 111.0 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dlat, dlon)


def main():
    els = fetch()["elements"]
    gj = json.loads((ROOT / "assets" / "tn_districts.geojson").read_text())
    dists = []
    for f in gj["features"]:
        g = f["geometry"]
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        dists.append((f["properties"]["district"], [MPath(p[0]) for p in polys]))

    def district_of(lon, lat):
        for name, paths in dists:
            if any(p.contains_point((lon, lat)) for p in paths):
                return name
        return None

    cand = []
    for e in els:
        t = e.get("tags", {})
        name = (t.get("name:en") or t.get("name") or "").strip()
        if not name or not name.isascii():
            continue
        lat, lon = e.get("lat"), e.get("lon")
        if lat is None:
            continue
        kind = t["place"]
        d = district_of(lon, lat)
        if d is None:
            continue
        try:
            pop = int(str(t.get("population", "0")).replace(",", ""))
        except ValueError:
            pop = 0
        rank = {"city": 1, "town": 2}[kind]
        # district headquarters first, so they are never thinned away by a neighbour
        if any(difflib.SequenceMatcher(None, name.lower(), dn.lower()).ratio() >= 0.82 for dn, _ in dists):
            rank = 0
        cand.append({"name": name, "lat": round(lat, 4), "lon": round(lon, 4), "kind": kind,
                     "district": d, "pop": pop, "rank": rank})
    cand.sort(key=lambda p: (p["rank"], -p["pop"], p["name"]))
    kept = []
    for p in cand:
        thin = THIN_KM
        if any(km((p["lat"], p["lon"]), (k["lat"], k["lon"])) < thin for k in kept):
            continue
        kept.append(p)
    seen, out = set(), []
    for p in kept:
        if p["name"] in seen:
            continue
        seen.add(p["name"])
        out.append({k: p[k] for k in ("name", "lat", "lon", "kind", "district")})
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print(f"{len(els)} OSM places -> {len(cand)} in Tamil Nadu -> {len(out)} after thinning")
    from collections import Counter
    print(Counter(p["kind"] for p in out))


if __name__ == "__main__":
    main()
