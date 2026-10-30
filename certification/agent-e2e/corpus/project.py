"""Stdlib forward and inverse projections for the GA generalization datasets.

These are the published parameter sets, not web-Mercator metres near the equator.
HONUA:TM-180 is a WGS 84 transverse Mercator centred on the antimeridian. It is
not an EPSG code: UTM zone 1 and zone 60 each stop at that meridian, so neither
can hold one unsplit feature that crosses it.
"""
from __future__ import annotations

import math


WGS84 = {"a": 6378137.0, "inv_f": 298.257223563}
GRS80 = {"a": 6378137.0, "inv_f": 298.257222101}

CRS = {
    "EPSG:32633": {
        "method": "transverse-mercator",
        "ellipsoid": WGS84,
        "latitudeOfOrigin": 0.0,
        "centralMeridian": 15.0,
        "scaleFactor": 0.9996,
        "falseEasting": 500000.0,
        "falseNorthing": 0.0,
    },
    "HONUA:TM-180": {
        "method": "transverse-mercator",
        "ellipsoid": WGS84,
        "latitudeOfOrigin": 0.0,
        "centralMeridian": 180.0,
        "scaleFactor": 0.9996,
        "falseEasting": 500000.0,
        "falseNorthing": 0.0,
    },
    "EPSG:26943": {
        "method": "lambert-conformal-conic-2sp",
        "ellipsoid": GRS80,
        "latitudeOfOrigin": 36.5,
        "centralMeridian": -120.5,
        "standardParallel1": 38.4333333333333,
        "standardParallel2": 37.0666666666667,
        "falseEasting": 2000000.0,
        "falseNorthing": 500000.0,
    },
}


def _ellipsoid(crs):
    spec = CRS[crs]["ellipsoid"]
    a = spec["a"]
    f = 1.0 / spec["inv_f"]
    e2 = f * (2.0 - f)
    return a, e2


def _delta_longitude(lon_deg, lon0_deg):
    delta = lon_deg - lon0_deg
    while delta <= -180.0:
        delta += 360.0
    while delta > 180.0:
        delta -= 360.0
    return math.radians(delta)


def _wrap_longitude(deg):
    while deg > 180.0:
        deg -= 360.0
    while deg <= -180.0:
        deg += 360.0
    return deg


def _tm_forward(lat_deg, lon_deg, crs):
    p = CRS[crs]
    a, e2 = _ellipsoid(crs)
    ep2 = e2 / (1.0 - e2)
    lat = math.radians(lat_deg)
    dlon = _delta_longitude(lon_deg, p["centralMeridian"])
    s = math.sin(lat)
    c = math.cos(lat)
    n = a / math.sqrt(1.0 - e2 * s * s)
    t = math.tan(lat) ** 2
    cc = ep2 * c * c
    aa = c * dlon
    m = a * (
        (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * lat
        - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * lat)
        + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * lat)
        - (35 * e2 ** 3 / 3072) * math.sin(6 * lat)
    )
    k0 = p["scaleFactor"]
    easting = p["falseEasting"] + k0 * n * (
        aa
        + (1 - t + cc) * aa ** 3 / 6
        + (5 - 18 * t + t * t + 72 * cc - 58 * ep2) * aa ** 5 / 120
    )
    northing = p["falseNorthing"] + k0 * (
        m
        + n * math.tan(lat) * (
            aa ** 2 / 2
            + (5 - t + 9 * cc + 4 * cc * cc) * aa ** 4 / 24
            + (61 - 58 * t + t * t + 600 * cc - 330 * ep2) * aa ** 6 / 720
        )
    )
    return easting, northing


def _tm_inverse(easting, northing, crs):
    p = CRS[crs]
    a, e2 = _ellipsoid(crs)
    ep2 = e2 / (1.0 - e2)
    k0 = p["scaleFactor"]
    m = (northing - p["falseNorthing"]) / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi = (
        mu
        + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
        + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
        + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
        + (1097 * e1 ** 4 / 512) * math.sin(8 * mu)
    )
    s = math.sin(phi)
    c = math.cos(phi)
    n = a / math.sqrt(1 - e2 * s * s)
    r = a * (1 - e2) / (1 - e2 * s * s) ** 1.5
    t = math.tan(phi) ** 2
    cc = ep2 * c * c
    d = (easting - p["falseEasting"]) / (n * k0)
    lat = phi - (n * math.tan(phi) / r) * (
        d ** 2 / 2
        - (5 + 3 * t + 10 * cc - 4 * cc * cc - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t + 298 * cc + 45 * t * t - 252 * ep2 - 3 * cc * cc) * d ** 6 / 720
    )
    lon = math.radians(p["centralMeridian"]) + (
        d
        - (1 + 2 * t + cc) * d ** 3 / 6
        + (5 - 2 * cc + 28 * t - 3 * cc * cc + 8 * ep2 + 24 * t * t) * d ** 5 / 120
    ) / c
    return math.degrees(lat), _wrap_longitude(math.degrees(lon))


def _lcc_constants(crs):
    p = CRS[crs]
    a, e2 = _ellipsoid(crs)
    e = math.sqrt(e2)

    def m_of(phi):
        return math.cos(phi) / math.sqrt(1 - e2 * math.sin(phi) ** 2)

    def t_of(phi):
        s = math.sin(phi)
        return math.tan(math.pi / 4 - phi / 2) / ((1 - e * s) / (1 + e * s)) ** (e / 2)

    phi1 = math.radians(p["standardParallel1"])
    phi2 = math.radians(p["standardParallel2"])
    phi0 = math.radians(p["latitudeOfOrigin"])
    m1, m2 = m_of(phi1), m_of(phi2)
    t0, t1, t2 = t_of(phi0), t_of(phi1), t_of(phi2)
    n = math.log(m1 / m2) / math.log(t1 / t2)
    f = m1 / (n * t1 ** n)
    rho0 = a * f * t0 ** n
    return a, e, n, f, rho0


def _lcc_forward(lat_deg, lon_deg, crs):
    p = CRS[crs]
    a, e, n, f, rho0 = _lcc_constants(crs)
    phi = math.radians(lat_deg)
    s = math.sin(phi)
    t = math.tan(math.pi / 4 - phi / 2) / ((1 - e * s) / (1 + e * s)) ** (e / 2)
    rho = a * f * t ** n
    theta = n * _delta_longitude(lon_deg, p["centralMeridian"])
    easting = p["falseEasting"] + rho * math.sin(theta)
    northing = p["falseNorthing"] + rho0 - rho * math.cos(theta)
    return easting, northing


def _lcc_inverse(easting, northing, crs):
    p = CRS[crs]
    a, e, n, f, rho0 = _lcc_constants(crs)
    dx = easting - p["falseEasting"]
    dy = rho0 - (northing - p["falseNorthing"])
    rho = math.copysign(math.hypot(dx, dy), n)
    theta = math.atan2(dx, dy)
    t = (rho / (a * f)) ** (1.0 / n)
    phi = math.pi / 2 - 2 * math.atan(t)
    for _ in range(12):
        s = math.sin(phi)
        phi = math.pi / 2 - 2 * math.atan(t * ((1 - e * s) / (1 + e * s)) ** (e / 2))
    lon = math.radians(p["centralMeridian"]) + theta / n
    return math.degrees(phi), _wrap_longitude(math.degrees(lon))


def forward(crs, lat_deg, lon_deg):
    method = CRS[crs]["method"]
    if method == "transverse-mercator":
        return _tm_forward(lat_deg, lon_deg, crs)
    if method == "lambert-conformal-conic-2sp":
        return _lcc_forward(lat_deg, lon_deg, crs)
    raise KeyError(crs)


def inverse(crs, easting, northing):
    method = CRS[crs]["method"]
    if method == "transverse-mercator":
        return _tm_inverse(easting, northing, crs)
    if method == "lambert-conformal-conic-2sp":
        return _lcc_inverse(easting, northing, crs)
    raise KeyError(crs)
