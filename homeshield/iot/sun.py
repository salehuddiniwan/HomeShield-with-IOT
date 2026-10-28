"""Sunrise and sunset for a place, for schedules like "30 min after sunset".

The sunrise equation in its simplified form: within a minute or two near the
equator, which is plenty for switching a porch light.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Optional

# Malaysian state capitals and big towns: (latitude, longitude).
CITIES: dict[str, tuple[float, float]] = {
    "Kuala Lumpur": (3.1390, 101.6869), "Putrajaya": (2.9264, 101.6964),
    "Shah Alam": (3.0733, 101.5185), "Seremban": (2.7259, 101.9424),
    "Melaka": (2.1896, 102.2501), "Johor Bahru": (1.4927, 103.7414),
    "Ipoh": (4.5975, 101.0901), "George Town": (5.4141, 100.3288),
    "Alor Setar": (6.1248, 100.3678), "Kangar": (6.4414, 100.1986),
    "Kota Bharu": (6.1254, 102.2381), "Kuala Terengganu": (5.3296, 103.1370),
    "Kuantan": (3.8077, 103.3260), "Kota Kinabalu": (5.9804, 116.0735),
    "Kuching": (1.5535, 110.3593),
}
DEFAULT_CITY = "Kuala Lumpur"


def sun_times(day: dt.date, lat: float, lon: float) -> tuple[Optional[float], Optional[float]]:
    """(sunrise, sunset) on `day` as Unix timestamps; None where the sun
    doesn't rise or set that day (polar regions)."""
    # The day count wants the date at 0h UT: from noon it lands on the next day.
    midnight = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
    jd = midnight.timestamp() / 86400 + 2440587.5
    n = math.ceil(jd - 2451545.0 + 0.0008)
    j_star = n - lon / 360.0
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)
    c = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)
    transit = 2451545.0 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    decl = math.asin(math.sin(lam) * math.sin(math.radians(23.4397)))
    phi = math.radians(lat)
    cos_w = ((math.sin(math.radians(-0.833)) - math.sin(phi) * math.sin(decl))
             / (math.cos(phi) * math.cos(decl)))
    if not -1.0 <= cos_w <= 1.0:
        return None, None
    w = math.degrees(math.acos(cos_w)) / 360.0

    def unix(j: float) -> float:
        return (j - 2440587.5) * 86400

    return unix(transit - w), unix(transit + w)
