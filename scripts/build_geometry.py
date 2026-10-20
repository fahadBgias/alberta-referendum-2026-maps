#!/usr/bin/env python3
"""Rebuild data/geometry.json from the Government of Alberta's electoral-division map service.

The boundaries are the 2019 Provincial Electoral Divisions (87 divisions), which are the ones
the 2026 referendum was counted in. They only change after a redistribution, so this script
is run by hand, not on the schedule.

What it does:
  1. Asks the map service for every division, simplified on the server:
     about 30 m for the 46 Edmonton and Calgary divisions, about 1 km for the rest.
  2. Converts each outline into a compact SVG path in Alberta 10TM metres:
     a 2 km grid for the province view, and a 100 m grid for the Edmonton and Calgary insets.
  3. Writes data/geometry.json, which the page draws directly (no mapping library needed).

Standard library only. Usage: python scripts/build_geometry.py
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = ("https://geospatial.alberta.ca/titan/rest/services/boundary/"
           "goa_administrative_area_10tm_nad83_aep/MapServer/3/query")
USER_AGENT = "alberta-referendum-maps/1.0 (boundary build)"

LAST_CALGARY = 26        # divisions 1-26 are Calgary
LAST_CITY = 46           # divisions 27-46 are Edmonton
CITY_SIMPLIFY_M = 30
RURAL_SIMPLIFY_M = 1000
PROVINCE_GRID_M = 2000
INSET_GRID_M = 100

OUT = Path(__file__).resolve().parent.parent / "data" / "geometry.json"


def query(simplify_m: int) -> dict[int, list]:
    """Return {division number: rings} at the given server-side simplification."""
    params = {
        "where": "1=1",
        "outFields": "EDNUMBER",
        "returnGeometry": "true",
        "maxAllowableOffset": simplify_m,
        "geometryPrecision": 0,
        "f": "json",
    }
    req = urllib.request.Request(SERVICE + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return {int(f["attributes"]["EDNUMBER"]): f["geometry"]["rings"] for f in data["features"]}


def extent(ring_sets) -> list[float]:
    xs, ys = [], []
    for rings in ring_sets:
        for ring in rings:
            for x, y in ring:
                xs.append(x)
                ys.append(y)
    return [min(xs), min(ys), max(xs), max(ys)]


def js_round(v: float) -> int:
    """Round half up, like JavaScript's Math.round (Python's round() rounds half to even)."""
    return math.floor(v + 0.5)


def to_path(rings, x0: float, y1: float, grid: float) -> str:
    """Encode rings as an SVG path: an absolute move, then relative line steps on the grid.

    North is up: y is measured down from the top edge (y1). Steps that round to zero are dropped.
    """
    out = []
    for ring in rings:
        steps, px, py, start = [], None, None, None
        for x, y in ring:
            gx, gy = js_round((x - x0) / grid), js_round((y1 - y) / grid)
            if start is None:
                start = (gx, gy)
            else:
                dx, dy = gx - px, gy - py
                if dx == 0 and dy == 0:
                    continue
                steps.append(("" if dx < 0 else " ") + str(dx) + ("" if dy < 0 else " ") + str(dy))
            px, py = gx, gy
        out.append(f"M{start[0]} {start[1]}l" + "".join(steps) + "z")
    return "".join(out).replace("l ", "l")


def build(city: dict[int, list], rural: dict[int, list]) -> dict:
    shapes = {n: city[n] for n in city if n <= LAST_CITY}
    shapes.update({n: rural[n] for n in rural if n > LAST_CITY})
    if sorted(shapes) != list(range(1, 88)):
        raise SystemExit(f"Expected divisions 1-87, got {sorted(shapes)}")

    prov = extent(shapes.values())
    cal = extent(shapes[n] for n in range(1, LAST_CALGARY + 1))
    edm = extent(shapes[n] for n in range(LAST_CALGARY + 1, LAST_CITY + 1))

    def size(e, grid):
        return [math.ceil((e[2] - e[0]) / grid), math.ceil((e[3] - e[1]) / grid)]

    features = {}
    for n in range(1, 88):
        paths = [to_path(shapes[n], prov[0], prov[3], PROVINCE_GRID_M)]
        if n <= LAST_CALGARY:
            paths.append(to_path(shapes[n], cal[0], cal[3], INSET_GRID_M))
        elif n <= LAST_CITY:
            paths.append(to_path(shapes[n], edm[0], edm[3], INSET_GRID_M))
        features[str(n)] = paths

    return {"pv": size(prov, PROVINCE_GRID_M), "cal": size(cal, INSET_GRID_M),
            "edm": size(edm, INSET_GRID_M), "f": features}


def main() -> None:
    geometry = build(query(CITY_SIMPLIFY_M), query(RURAL_SIMPLIFY_M))
    OUT.write_text(json.dumps(geometry, separators=(",", ":")))
    print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
