"""Independent numerical oracle for S5's fixed metric inputs (stdlib only)."""
import json
import math
import sys


def validate(process, outputs):
    if process == "geometry.area":
        result = outputs["outputScalar"]["value"]
        if result["measure"] != "area" or result["inputSrid"] != 3857:
            raise ValueError("area metadata does not identify the metric input")
        if type(result["value"]) not in (int, float) or not math.isclose(result["value"], 1.0, abs_tol=1e-9):
            raise ValueError("unit square must have area 1")
    elif process == "geometry.buffer":
        feature = outputs["outputFeatureLayer"]["value"]
        if feature["type"] == "FeatureCollection":
            if len(feature["features"]) != 1:
                raise ValueError("expected exactly one buffered feature")
            feature = feature["features"][0]
        geometry = feature["geometry"]
        if feature["type"] != "Feature" or geometry["type"] != "Polygon":
            raise ValueError("expected a polygon feature")
        rings = geometry["coordinates"]
        if len(rings) != 1 or len(rings[0]) < 4 or rings[0][0] != rings[0][-1]:
            raise ValueError("expected a closed exterior ring without holes")
        ring = rings[0]
        xs, ys = zip(*[(point[0], point[1]) for point in ring])
        if not all(math.isfinite(value) for value in (*xs, *ys)):
            raise ValueError("non-finite buffer coordinate")
        if any(not math.isclose(math.hypot(x, y), 10, abs_tol=1e-6) for x, y in zip(xs, ys)):
            raise ValueError("buffer vertices must lie on the radius-10 circle")
        # A straight segment between correct vertices can still cut far inside
        # the circle. Bound its midpoint's radial error to 1% of the radius;
        # the server's 32-segment approximation stays within this tolerance.
        if any(math.hypot((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) < 9.9
               for a, b in zip(ring, ring[1:])):
            raise ValueError("buffer boundary cuts too far inside the radius-10 circle")
        bounds = [min(xs), min(ys), max(xs), max(ys)]
        if any(not math.isclose(a, b, abs_tol=1e-6) for a, b in zip(bounds, [-10, -10, 10, 10])):
            raise ValueError(f"incorrect buffer bounds: {bounds}")
        area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))) / 2
        if not math.isclose(area, math.pi * 100, rel_tol=0.01):
            raise ValueError(f"incorrect buffer area: {area}")
    else:
        raise ValueError(f"no oracle for {process}")


if __name__ == "__main__":
    try:
        validate(sys.argv[1], json.load(sys.stdin))
    except (ValueError, KeyError, TypeError, IndexError) as error:
        print(f"GP result rejected: {error}", file=sys.stderr)
        sys.exit(1)
