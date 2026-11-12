"""Fetch the latest Meteosat IODC IR10.8 and H-SAF h63 (rain rate) pictures for the radar-health check.

13 Nov 2026. Runs in the background during the pipeline (started by the workflow), so a slow EUMETSAT server never
holds up the map. Writes state_social_test/sat/sat_<YYYYmmddTHHMM>.npz (kept with the state; last few only): two bit-packed masks on
a 0.02 deg grid -- "cold" (IR10.8 grey >= COLD_GREY, i.e. a real cloud top) and "rain" (any h63 rain rate). EUMETView usually has a slot about
30-45 min after its time; we try the most recent likely slots, newest first, and stop at the first complete pair.
"""
import concurrent.futures as cf
import pathlib
import sys
import time
from datetime import datetime, timedelta, timezone

import io

import numpy as np
import requests
from PIL import Image

WMS = "https://view.eumetsat.int/geoserver/wms"
BBOX = (7.5, 75.5, 15.0, 82.0)          # lat_min, lon_min, lat_max, lon_max (EPSG:4326 axis order for WMS 1.3.0)
STEP = 0.02
LAYERS = ("msg_iodc:ir108", "msg_iodc:h63")
OUT = pathlib.Path("state_social_test/sat")
COLD_GREY = 100        # replay 4-13 Nov: real Karaikal storms >= 76% supported at this level, the 13 Nov junk <= 18%
KEEP = 4
BUDGET_S = 240


def get(layer: str, slot: datetime, timeout: float) -> bytes | None:
    w = int(round((BBOX[3] - BBOX[1]) / STEP)); h = int(round((BBOX[2] - BBOX[0]) / STEP))
    params = {"service": "WMS", "version": "1.3.0", "request": "GetMap", "layers": layer, "styles": "",
              "crs": "EPSG:4326", "bbox": ",".join(map(str, BBOX)), "width": w, "height": h,
              "format": "image/png", "time": slot.strftime("%Y-%m-%dT%H:%M:00Z")}
    try:
        r = requests.get(WMS, params=params, timeout=timeout)
        if r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n":
            return r.content
    except requests.RequestException:
        pass
    return None


def main() -> int:
    OUT.mkdir(exist_ok=True)
    t0 = time.time()
    now = datetime.now(timezone.utc)
    base = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    slots = [base - timedelta(minutes=m) for m in (30, 45, 60, 75)]
    for slot in slots:
        tag = slot.strftime("%Y%m%dT%H%M")
        if (OUT / f"sat_{tag}.npz").exists():
            print(f"[sat] {tag} already here"); return 0
        for attempt in range(3):
            left = BUDGET_S - (time.time() - t0)
            if left < 20:
                print("[sat] out of time"); return 1
            with cf.ThreadPoolExecutor(2) as ex:
                got = dict(zip(LAYERS, ex.map(lambda l: get(l, slot, min(60.0, left)), LAYERS)))
            if all(got.values()):
                ir = np.array(Image.open(io.BytesIO(got["msg_iodc:ir108"])).convert("L"))
                rain = np.array(Image.open(io.BytesIO(got["msg_iodc:h63"])).convert("RGBA"))[..., 3] > 0
                p = OUT / f"sat_{tag}.npz"
                tmp = OUT / f"sat_{tag}.part.npz"
                np.savez_compressed(tmp, cold=np.packbits(ir >= COLD_GREY), rain=np.packbits(rain),
                                    shape=np.array(ir.shape), bbox=np.array(BBOX), step=STEP, cold_grey=COLD_GREY)
                tmp.rename(p)
                for old in sorted(OUT.glob("sat_*.npz"))[:-KEEP]:
                    old.unlink()
                print(f"[sat] got {tag} in {time.time() - t0:.0f}s"); return 0
            time.sleep(5)
        print(f"[sat] {tag} not available")
    return 1


if __name__ == "__main__":
    sys.exit(main())
