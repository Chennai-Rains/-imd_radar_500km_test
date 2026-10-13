"""Paired Chennai radar log (14 Oct 2026): one JSON line per new pair of Chennai DWR PPI (cni_ppi, 0.2 deg) and Chennai
DWR MAXZ (cni_maxz) pictures, so the MAXZ -> PPI intensity relation can be fitted from real data (the NEM onset
spell is the plan). Test-repo only, never touches the map or the alerts.

Each line: observation times of the PPI, MAXZ and NIOT pictures, and for four distance bands from the Chennai DWR site
(0-45, 45-90, 90-135 km where the PPI reaches; 135-250 km where only MAXZ does) the number of 0.01 deg grid nodes
(~1.2 km2 each) at or above 20/30/35/40/45/48/52 dBZ in each product, the joint histogram of PPI value against MAXZ
value (4 dBZ bins from 20, bin -1 = no echo / not covered; keys "ppi_bin,maxz_bin"), and the strongest value in each."""
import json
from pathlib import Path

import numpy as np

import nowcast_bot as nb

LOG = Path("state_social_test/radar_pairs.jsonl")
THRESH = (20, 30, 35, 40, 45, 48, 52)
BANDS = ((0, 45), (45, 90), (90, 135), (135, 250))
MAX_DT_MIN = 8.0


def _sample(product, lat, lon):
    d = nb._last_dbz.get(product)
    if d is None:
        return None
    px, py = nb.latlon_to_pixel(lat, lon, product)
    ox, oy = nb.PRODUCTS[product]["plot_bbox"][:2]
    x = np.round(px - ox).astype(int)
    y = np.round(py - oy).astype(int)
    ok = (x >= 0) & (x < d.shape[1]) & (y >= 0) & (y < d.shape[0])
    out = np.full(lat.shape, np.nan)
    out[ok] = d[y[ok], x[ok]]
    return out


def _bin(v):
    v = np.nan_to_num(v, nan=-1.0)
    return np.where(v >= 20, np.minimum(((v - 20) // 4).astype(int), 10), -1)


def log_pair(now_utc):
    obs = nb._last_obs_time_seen
    t_p, t_m, t_n = obs.get("cni_ppi"), obs.get("cni_maxz"), obs.get("maxz")
    if t_p is None or t_m is None or abs((t_p - t_m).total_seconds()) / 60.0 > MAX_DT_MIN:
        return None
    key = [t_p.isoformat(), t_m.isoformat()]
    if LOG.exists():
        last = LOG.read_text().strip().splitlines()[-1:]
        if last and json.loads(last[0]).get("key") == key:
            return None          # this pair is already logged
    site = nb.site_for("cni_ppi")
    la, lo = np.meshgrid(np.arange(site["site_lat"] - 2.3, site["site_lat"] + 2.3, 0.01),
                         np.arange(site["site_lon"] - 2.3, site["site_lon"] + 2.3, 0.01), indexing="ij")
    r = np.hypot((la - site["site_lat"]) * 111.0, (lo - site["site_lon"]) * 111.0 * np.cos(np.radians(site["site_lat"])))
    P, M, N = _sample("cni_ppi", la, lo), _sample("cni_maxz", la, lo), _sample("maxz", la, lo)
    if P is None or M is None:
        return None
    ns = nb.site_for("maxz")
    rn = np.hypot((la - ns["site_lat"]) * 111.0, (lo - ns["site_lon"]) * 111.0 * np.cos(np.radians(ns["site_lat"])))
    rec = {"t": now_utc.isoformat(), "key": key, "dt_min": round((t_p - t_m).total_seconds() / 60.0, 1),
           "niot_obs": t_n.isoformat() if t_n else None, "node_km2": 1.2, "bands": {}}
    for lo_km, hi_km in BANDS:
        m = (r >= lo_km) & (r < hi_km)
        if not m.any():
            continue
        b = {"max_maxz": None if not np.isfinite(M[m]).any() else round(float(np.nanmax(M[m])), 1),
             "n_maxz": {str(t): int((M[m] >= t).sum()) for t in THRESH}}
        if hi_km <= 135:
            b["max_ppi"] = None if not np.isfinite(P[m]).any() else round(float(np.nanmax(P[m])), 1)
            b["n_ppi"] = {str(t): int((P[m] >= t).sum()) for t in THRESH}
            either = m & ((np.nan_to_num(P, nan=-1) >= 20) | (np.nan_to_num(M, nan=-1) >= 20))
            pb, mb = _bin(P[either]), _bin(M[either])
            joint = {}
            for a, c in zip(pb.tolist(), mb.tolist()):
                k = f"{a},{c}"
                joint[k] = joint.get(k, 0) + 1
            b["joint_ppi_maxz"] = joint
            if N is not None:
                mn = m & (rn <= 80)
                b["n_niot_in_80km"] = {str(t): int((N[mn] >= t).sum()) for t in (20, 35, 45)}
                b["max_niot_in_80km"] = None if not np.isfinite(N[mn]).any() else round(float(np.nanmax(N[mn])), 1)
        rec["bands"][f"{lo_km}-{hi_km}"] = b
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec
