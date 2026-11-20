"""地理计算工具:WGS84 球面距离与点到有向线段的投影。"""
from __future__ import annotations

import math

EARTH_RADIUS_M = 6371008.8
_M_PER_DEG_LAT = 111320.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点球面距离(米)。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def project_to_segment(
    plat: float, plon: float, alat: float, alon: float, blat: float, blon: float
) -> tuple[float, float, float, float]:
    """点 P 到有向线段 A->B 的投影。

    返回 (dist_m, fraction, proj_lat, proj_lon):
      - dist_m:   P 到线段的最短距离(米,局部等距圆柱近似,50m 量级足够精确)
      - fraction: 投影点在线段上的位置比例 [0,1](0=A, 1=B)
      - proj_lat/proj_lon: 投影点坐标
    """
    ky = _M_PER_DEG_LAT
    kx = _M_PER_DEG_LAT * math.cos(math.radians(plat))
    ax, ay = (alon - plon) * kx, (alat - plat) * ky
    bx, by = (blon - plon) * kx, (blat - plat) * ky
    dx, dy = bx - ax, by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 0.0:
        t = 0.0
    else:
        t = -(ax * dx + ay * dy) / seg2
        t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    px, py = ax + t * dx, ay + t * dy
    dist = math.hypot(px, py)
    proj_lat = alat + t * (blat - alat)
    proj_lon = alon + t * (blon - alon)
    return dist, t, proj_lat, proj_lon
