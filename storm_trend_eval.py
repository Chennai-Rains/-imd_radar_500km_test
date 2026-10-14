"""Scores storm_trend_log.py calls against what the same storm tracks actually did (run: python3 storm_trend_eval.py [log]).

For every logged blob that carried a trend call (raw class, and the stricter 'persist' subset), the outcome is the ratio
future mass / mass now of the same track ~30 and ~60 min later (nearest logged point within +/-8 min). A track with no
point later although the log carries on, which never touched a coverage edge, counts as 'gone' (dissipated or lost by the
tracker -- reported apart so the two readings can be told apart). Compare each class with the 'steady' row and with the
unconditional 'all' row: a useful trend tool shows intensifying > all > steady > weakening in the share that grew."""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

TOL_MIN = 8.0
GROW, DECAY = 1.2, 0.8


def load(path):
    rows = [json.loads(l) for l in Path(path).read_text().splitlines() if l.strip()]
    for r in rows:
        r["tm"] = datetime.fromisoformat(r["t"]).timestamp() / 60.0
    return rows


def outcomes(rows, lead):
    by_track = defaultdict(list)
    for r in rows:
        for b in r["blobs"]:
            by_track[b["track"]].append((r["tm"], b))
    end = rows[-1]["tm"]
    res = []
    for tid, pts in by_track.items():
        for tm, b in pts:
            if tm + lead > end + TOL_MIN:
                continue                         # outcome not yet observable
            fut = [(abs(t2 - (tm + lead)), b2) for t2, b2 in pts if abs(t2 - (tm + lead)) <= TOL_MIN]
            if fut:
                ratio = min(fut, key=lambda x: x[0])[1]["mass"] / max(b["mass"], 0.1)
                res.append((b, ratio, "obs"))
            elif not b["edge"]:
                res.append((b, 0.0, "gone"))
    return res


def table(res, title):
    groups = defaultdict(list)
    for b, ratio, kind in res:
        groups["all"].append((ratio, kind))
        if b.get("raw"):
            groups[b["raw"]].append((ratio, kind))
            if b.get("persist"):
                groups["persist:" + b["raw"]].append((ratio, kind))
    print(f"\n{title}")
    print(f"{'class':26s}{'n':>5s}{'median x':>10s}{'grew>20%':>10s}{'decay>20%':>11s}{'gone':>7s}")
    for k in ("all", "intensifying", "steady", "weakening", "persist:intensifying", "persist:steady", "persist:weakening"):
        v = groups.get(k, [])
        if not v:
            continue
        r = np.array([x[0] for x in v])
        gone = sum(1 for x in v if x[1] == "gone")
        print(f"{k:26s}{len(v):5d}{np.median(r):10.2f}{100 * np.mean(r > GROW):9.0f}%{100 * np.mean(r < DECAY):10.0f}%{100 * gone / len(v):6.0f}%")


if __name__ == "__main__":
    rows = load(sys.argv[1] if len(sys.argv) > 1 else "state_social_test/storm_trend.jsonl")
    print(f"{len(rows)} runs, {rows[0]['t']} .. {rows[-1]['t']}")
    for lead in (30, 60):
        table(outcomes(rows, lead), f"Outcome {lead} min later (mass ratio of same track)")
