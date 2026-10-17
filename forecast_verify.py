"""Nowcast verification log (18 Oct 2026). Test-repo only; never touches the map or the alerts.

Every run it stores where each moved-forward layer (plus_30 / plus_60 / plus_90) and the observed picture put strong
echo (>=35 dBZ; >=45 dBZ kept separately). Later, when the observed picture's scan time reaches the time such a forecast
was valid for, it scores that forecast against the observed echo, and also scores plain persistence (the observed picture
of the issuing run, no motion) against the same truth. One JSON line per verified (run, lead) goes to
state_social_test/forecast_verify.jsonl:

  POD  share of observed strong nodes with a forecast node within the neighbourhood radius
  FAR  share of forecast strong nodes with no observed node within the radius
  CSI  hits / (hits + misses + false alarms), computed on nodes with radar coverage in the observed picture
  radius = 3 / 5 / 7 nodes (~7 / 11 / 15 km) for 30 / 60 / 90 min, the uncertainty the export documents
Fields prefixed 'p_' are the persistence baseline. A forecast with skill shows higher CSI than persistence.
Truth is the 'observed' layer; its valid time is generated time minus the median age of the radars in use."""
import json
import statistics
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from scipy import ndimage

BUF = Path("state_social_test/forecast_buffer.npz")
LOG = Path("state_social_test/forecast_verify.jsonl")
LEADS = (30, 60, 90)
RADIUS = {30: 3, 60: 5, 90: 7}
THRS = (35, 45)
KEEP_MIN = 125.0
MATCH_TOL_MIN = 3.5


def _valid_obs(ex):
    ages = [r["age_min"] for r in ex.doc.get("radars", []) if r.get("used") and r.get("age_min") is not None]
    age = statistics.median(ages) if ages else 0.0
    return ex.generated_utc - timedelta(minutes=age), age


def _score(fc, truth, covered, r):
    st = np.ones((2 * r + 1, 2 * r + 1), bool)
    t_near = ndimage.binary_dilation(truth, structure=st)
    f_near = ndimage.binary_dilation(fc, structure=st)
    nt, nf = int(truth.sum()), int(fc.sum())
    hit_t = int((truth & f_near).sum())          # observed nodes the forecast covered
    hit_f = int((fc & t_near & covered).sum())   # forecast nodes confirmed
    fa = int((fc & ~t_near & covered).sum())
    miss = nt - hit_t
    return {"n_obs": nt, "n_fc": int((fc & covered).sum()),
            "POD": round(hit_t / nt, 3) if nt else None,
            "FAR": round(fa / max(int((fc & covered).sum()), 1), 3) if (fc & covered).sum() else None,
            "CSI": round(hit_f / (hit_f + miss + fa), 3) if (hit_f + miss + fa) else None}


def verify(json_path, grid_path):
    import social_alerts
    ex = social_alerts.load_export(Path(json_path), Path(grid_path))
    if ex is None or "observed" not in ex.layer_names:
        return None
    obs_valid, age = _valid_obs(ex)
    names = ex.layer_names
    obs = ex.grid[names.index("observed")]
    covered = obs != 255
    # ---- load the buffer ----
    buf = []
    if BUF.exists():
        z = np.load(BUF, allow_pickle=True)
        buf = list(z["entries"])
    out = []
    for e in buf:
        t0 = datetime.fromisoformat(e["t0"])
        for lead in LEADS:
            if abs((t0 + timedelta(minutes=lead) - obs_valid).total_seconds()) / 60.0 > MATCH_TOL_MIN:
                continue
            if any(json.loads(l).get("t0") == e["t0"] and json.loads(l).get("lead") == lead
                   for l in (LOG.read_text().splitlines()[-40:] if LOG.exists() else [])):
                continue
            for thr in THRS:
                truth = (obs >= thr) & covered
                fc = np.zeros(obs.shape, bool); fc.flat[e[f"plus_{lead}_{thr}"]] = True
                pe = np.zeros(obs.shape, bool); pe.flat[e[f"obs_{thr}"]] = True
                f = _score(fc, truth, covered, RADIUS[lead]); p = _score(pe, truth, covered, RADIUS[lead])
                out.append({"t": ex.generated_utc.isoformat(timespec="seconds"), "t0": e["t0"], "lead": lead,
                            "thr": thr, "obs_age_min": round(age, 0), **f, **{"p_" + k: v for k, v in p.items()}})
    if out:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as fh:
            for o in out:
                fh.write(json.dumps(o, separators=(",", ":")) + "\n")
    # ---- store this run's layers ----
    entry = {"t0": ex.generated_utc.isoformat(timespec="seconds")}
    for thr in THRS:
        entry[f"obs_{thr}"] = np.flatnonzero((obs >= thr) & (obs != 255)).astype(np.uint32)
        for lead in LEADS:
            nm = f"plus_{lead}"
            if nm in names:
                g = ex.grid[names.index(nm)]
                entry[f"plus_{lead}_{thr}"] = np.flatnonzero((g >= thr) & (g != 255)).astype(np.uint32)
            else:
                entry[f"plus_{lead}_{thr}"] = np.zeros(0, np.uint32)
    now = ex.generated_utc
    keep = [e for e in buf if (now - datetime.fromisoformat(e["t0"])).total_seconds() / 60.0 <= KEEP_MIN
            and e["t0"] != entry["t0"]] + [entry]
    BUF.parent.mkdir(parents=True, exist_ok=True)
    arr = np.empty(len(keep), dtype=object)
    for i, e in enumerate(keep):
        arr[i] = e
    np.savez_compressed(BUF, entries=arr)
    return out
