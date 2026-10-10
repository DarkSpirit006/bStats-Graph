#!/usr/bin/env python3
"""Render bStats statistics as SVG cards and embed them in the README.

Each line of ``bstats-plugins.txt`` is a bStats URL, optionally followed by a
range such as ``3h``, ``12d``, ``4w``, ``6m`` or ``1y``. Omit the range for all-time.
Month and year tokens mean 30 and 365 days. Example:
``https://bstats.org/plugin/bukkit/example/123 2w``.
"""

from __future__ import annotations

import argparse
import html
import itertools
import json
import math
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TypeGuard

# Default locations, relative to the repository root. Each can be overridden with a
# command-line option or an environment variable (see ``parse_args``).
DEFAULT_CONFIG = "bstats-plugins.txt"
DEFAULT_README = "README.md"
DEFAULT_OUTPUT = "docs/bstats"

START_MARKER = "<!-- BSTATS-GRAPHS:START -->"
END_MARKER = "<!-- BSTATS-GRAPHS:END -->"

USER_AGENT = "bStats-Graph/1.0"
REQUEST_TIMEOUT = 25
RETRIES = 3

PERIOD_UNIT_MS = {
    "h": 3_600_000,
    "d": 86_400_000,
    "w": 7 * 86_400_000,
    "m": 30 * 86_400_000,  # fixed 30-day month
    "y": 365 * 86_400_000,  # fixed 365-day year
}
PERIOD_RE = re.compile(r"[1-9][0-9]*[hdwmy]", re.IGNORECASE)
MAX_POINTS = 180
TOP_ITEMS = 5

VERSION_CHARTS = (
    "minecraftVersion",
    "bungeecordVersion",
    "velocityVersion",
    "pocketmineVersion",
    "hytaleVersion",
    "spongeVersion",
)
SECONDARY_CHARTS = (
    "pluginVersion",
    "serverSoftware",
    "javaVersion",
    "phpVersion",
    "onlineMode",
    "authMode",
)
LOCATION_CHARTS = ("location",)
PIE_TYPES = frozenset({"simple_pie", "advanced_pie", "drilldown_pie"})

COUNTRY_CODES = {
    "Argentina": "AR", "Australia": "AU", "Austria": "AT", "Bangladesh": "BD",
    "Belarus": "BY", "Belgium": "BE", "Brazil": "BR", "Bulgaria": "BG",
    "Canada": "CA", "Chile": "CL", "China": "CN", "Colombia": "CO",
    "Croatia": "HR", "Czechia": "CZ", "Czech Republic": "CZ", "Denmark": "DK",
    "Egypt": "EG", "Estonia": "EE", "Finland": "FI", "France": "FR",
    "Germany": "DE", "Greece": "GR", "Hong Kong": "HK", "Hungary": "HU",
    "India": "IN", "Indonesia": "ID", "Iran": "IR", "Ireland": "IE",
    "Israel": "IL", "Italy": "IT", "Japan": "JP", "Kazakhstan": "KZ",
    "Latvia": "LV", "Lithuania": "LT", "Luxembourg": "LU", "Malaysia": "MY",
    "Mexico": "MX", "Netherlands": "NL", "The Netherlands": "NL",
    "New Zealand": "NZ", "Nigeria": "NG", "Norway": "NO", "Pakistan": "PK",
    "Peru": "PE", "Philippines": "PH", "Poland": "PL", "Portugal": "PT",
    "Romania": "RO", "Russia": "RU", "Russian Federation": "RU",
    "Saudi Arabia": "SA", "Serbia": "RS", "Singapore": "SG", "Slovakia": "SK",
    "Slovenia": "SI", "South Africa": "ZA", "South Korea": "KR",
    "Korea, Republic of": "KR", "Spain": "ES", "Sri Lanka": "LK",
    "Sweden": "SE", "Switzerland": "CH", "Taiwan": "TW", "Thailand": "TH",
    "Turkey": "TR", "Türkiye": "TR", "Ukraine": "UA",
    "United Arab Emirates": "AE", "United Kingdom": "GB",
    "United States": "US", "Vietnam": "VN", "Viet Nam": "VN",
}  # fmt: skip

PAGE_URL = re.compile(
    r"(?P<origin>(?:https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?)/"
    r"(?:plugin/(?P<plugin_platform>[^/\s()\[\]]+)/[^/\s()\[\]]+/(?P<plugin_id>\d+)"
    r"|global/(?P<global_platform>[A-Za-z0-9_-]+))",
    re.IGNORECASE,
)

# Card layout in pixels. GitHub renders README images at roughly 830-880 px,
# so a 900 px card displays close to its native size.
WIDTH = 900
HEIGHT = 730
# Width of the band around the card that the viewBox crops away. Coordinates below
# are measured from the uncropped canvas, so the card edge sits at (BLEED, BLEED).
BLEED = 12
MARGIN = 40
HEADER_RULE = 124
CHART_LEFT = MARGIN + 20
CHART_TOP = 186
CHART_HEIGHT = 190
CHART_RULE = 422
COLUMN_TOP = 456
COLUMN_WIDTH = 250
COLUMN_X = (MARGIN, MARGIN + 285, MARGIN + 570)
BAR_WIDTH = COLUMN_WIDTH - 26
ROW_HEIGHT = 32
FOOTER_RULE = 684


# Data model


@dataclass(frozen=True)
class Source:
    """One line of the config file."""

    url: str
    api: str
    service_id: int
    platform: str
    period: str | None = None  # None means all-time


@dataclass(frozen=True)
class Service:
    id: int
    name: str
    platform: str
    is_global: bool
    unit: str  # "Servers" or "Proxies"


@dataclass(frozen=True)
class Point:
    timestamp: int
    value: float


@dataclass(frozen=True)
class Share:
    name: str
    value: float
    percent: float


@dataclass(frozen=True)
class Column:
    title: str
    note: str
    items: list[Share]
    kind: str = ""  # "plugin" marks the latest version, "region" adds country codes


@dataclass(frozen=True)
class Stats:
    service: Service
    servers: list[Point]
    players: list[Point]
    columns: tuple[Column, Column, Column]
    period: str | None = None


# JSON helpers


def is_object(value: object) -> TypeGuard[dict[str, Any]]:
    return isinstance(value, dict)


def is_array(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)


def as_dict(value: object) -> dict[str, Any]:
    return value if is_object(value) else {}


def as_list(value: object) -> list[Any]:
    return value if is_array(value) else []


def as_number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


# bStats API


def get_json(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            error = exc
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            error = exc
        if attempt + 1 < RETRIES:
            time.sleep(2**attempt)
    raise RuntimeError(f"Request failed: {url} ({error})")


_service_lists: dict[str, list[Any]] = {}


def find_global_service(api: str, platform: str) -> int:
    """Return the ID of the global statistics service for a platform.

    bStats stores global statistics as services named ``_<platform>_`` owned by
    the ``Admin`` account.
    """
    if api not in _service_lists:
        _service_lists[api] = as_list(get_json(f"{api}/plugins"))

    wanted = f"_{platform.lower()}_"
    for item in _service_lists[api]:
        entry = as_dict(item)
        service_id = entry.get("id")
        if (
            as_text(entry.get("name")).lower() == wanted
            and as_dict(entry.get("owner")).get("name") == "Admin"
            and isinstance(service_id, int)
        ):
            return service_id
    raise RuntimeError(f"No global statistics found for {platform!r} at {api}")


def fetch_service(source: Source) -> tuple[Service, dict[str, Any]]:
    details = as_dict(get_json(f"{source.api}/plugins/{source.service_id}"))
    if not details:
        raise RuntimeError(f"bStats returned no data for service {source.service_id}")

    name = as_text(details.get("name"))
    is_global = bool(details.get("isGlobal")) or (
        re.fullmatch(r"_[\w-]+_", name) is not None
        and as_dict(details.get("owner")).get("name") == "Admin"
    )
    if is_global:
        name = source.platform

    charts = as_dict(details.get("charts")) or as_dict(
        get_json(f"{source.api}/plugins/{source.service_id}/charts")
    )
    servers_title = as_text(as_dict(charts.get("servers")).get("title")).lower()

    service = Service(
        id=source.service_id,
        name=name or f"Plugin {source.service_id}",
        platform=source.platform,
        is_global=is_global,
        unit="Proxies" if "proxy" in servers_title else "Servers",
    )
    return service, charts


def chart_url(source: Source, chart_id: str, query: str = "") -> str:
    chart = urllib.parse.quote(chart_id, safe="")
    return f"{source.api}/plugins/{source.service_id}/charts/{chart}/data{query}"


def fetch_line(source: Source, chart_id: str) -> list[Point]:
    points: list[Point] = []
    for row in as_list(get_json(chart_url(source, chart_id, "?maxElements=5000"))):
        pair = as_list(row)
        if len(pair) < 2:
            continue
        timestamp, value = as_number(pair[0]), as_number(pair[1])
        if timestamp is not None and value is not None:
            points.append(Point(int(timestamp), value))

    if not points:
        raise RuntimeError(f"Chart {chart_id!r} returned no data")
    return sorted(points, key=lambda point: point.timestamp)


def fetch_shares(source: Source, chart_id: str | None) -> list[Share]:
    """Fetch a pie chart. Drilldown pies are reduced to their top level."""
    if chart_id is None:
        return []

    payload = get_json(chart_url(source, chart_id))
    rows = as_list(payload) or as_list(as_dict(payload).get("seriesData"))

    totals: dict[str, float] = {}
    for row in rows:
        entry = as_dict(row)
        name = as_text(entry.get("name", entry.get("label")))
        value = as_number(entry.get("y", entry.get("value", entry.get("count"))))
        if value is not None and value > 0:
            key = name or "Unknown"
            totals[key] = totals.get(key, 0.0) + value

    total = sum(totals.values())
    if total <= 0:
        return []

    shares = [Share(name, value, value / total * 100) for name, value in totals.items()]
    return sorted(shares, key=lambda share: (-share.value, share.name.lower()))


def find_chart(charts: dict[str, Any], candidates: Sequence[str]) -> tuple[str, str] | None:
    """Return ``(chart_id, title)`` of the first candidate that is a pie chart."""
    by_lower = {key.lower(): key for key in charts}
    for candidate in candidates:
        key = by_lower.get(candidate.lower())
        if key is None:
            continue
        chart = as_dict(charts[key])
        if chart.get("type") in PIE_TYPES:
            return key, as_text(chart.get("title")) or key
    return None


def find_line_chart(charts: dict[str, Any], name: str) -> str:
    for key, chart in charts.items():
        if key.lower() == name and as_dict(chart).get("type") == "single_linechart":
            return key
    raise RuntimeError(f"No {name!r} line chart found")


def column_title(title: str) -> str:
    title = title.replace("Bungeecord", "BungeeCord").replace("Pocketmine", "PocketMine")
    return f"{title}s" if title.lower().endswith("version") else title


def collect_stats(source: Source) -> Stats:
    service, charts = fetch_service(source)
    note = f"by {service.unit.lower()}"

    versions = find_chart(charts, VERSION_CHARTS)
    if versions is None:
        excluded = {"pluginversion", "javaversion", "phpversion"}
        others = [
            key for key in charts if key.lower().endswith("version") and key.lower() not in excluded
        ]
        versions = find_chart(charts, others)

    remaining = [c for c in SECONDARY_CHARTS if versions is None or c != versions[0]]
    secondary = find_chart(charts, remaining)
    is_plugin_versions = secondary is not None and secondary[0].lower() == "pluginversion"
    location = find_chart(charts, LOCATION_CHARTS)

    columns = (
        Column(
            title=column_title(versions[1]) if versions else "Versions",
            note=note,
            items=fetch_shares(source, versions[0] if versions else None),
        ),
        Column(
            title=column_title(secondary[1]) if secondary else "Plugin Versions",
            note="adoption" if is_plugin_versions else note,
            items=fetch_shares(source, secondary[0] if secondary else None),
            kind="plugin" if is_plugin_versions else "",
        ),
        Column(
            title="Countries",
            note=note,
            items=fetch_shares(source, location[0] if location else None),
            kind="region",
        ),
    )

    return Stats(
        service=service,
        servers=fetch_line(source, find_line_chart(charts, "servers")),
        players=fetch_line(source, find_line_chart(charts, "players")),
        columns=columns,
        period=source.period,
    )


# Configuration


def display_platform(slug: str) -> str:
    name = urllib.parse.unquote(slug).replace("-", " ").replace("_", " ").title()
    return name.replace("Bungeecord", "BungeeCord").replace("Pocketmine", "PocketMine")


def parse_source(line: str) -> Source | None:
    match = PAGE_URL.search(line)
    if match is None:
        return None

    url = match.group(0).rstrip("/")
    suffix = line[match.end() :].strip().removeprefix("/").strip()
    if suffix:
        if len(suffix.split()) != 1 or PERIOD_RE.fullmatch(suffix) is None:
            raise ValueError(
                f"invalid time range {suffix!r}; append a positive number followed by "
                "h, d, w, m, or y (for example 3h or 2w), or leave it blank for all-time"
            )
        period: str | None = suffix.lower()
    else:
        period = None

    origin = match["origin"]
    if not re.match(r"https?://", url, re.IGNORECASE):
        url, origin = f"https://{url}", f"https://{origin}"
    api = f"{origin}/api/v1"

    if match["plugin_id"]:
        return Source(
            url, api, int(match["plugin_id"]), display_platform(match["plugin_platform"]), period
        )

    platform = match["global_platform"]
    return Source(url, api, find_global_service(api, platform), display_platform(platform), period)


def read_sources(path: Path) -> list[Source]:
    if not path.exists():
        raise RuntimeError(f"{path} not found")

    sources: list[Source] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = re.sub(r"(^|\s)#.*$", "", raw).strip()
        if not line:
            continue
        try:
            source = parse_source(line)
        except ValueError as exc:
            raise RuntimeError(f"{path}:{number}: {exc}") from exc
        if source is None:
            raise RuntimeError(
                f"{path}:{number}: not a bStats page URL "
                "(expected .../plugin/<platform>/<name>/<id> or .../global/<platform>, "
                "optionally followed by a positive number and h, d, w, m, or y)"
            )
        if all(existing.service_id != source.service_id for existing in sources):
            sources.append(source)

    if not sources:
        raise RuntimeError(f"{path} contains no bStats URLs")
    return sources


# Series helpers


def period_milliseconds(period: str) -> int:
    """Convert a token such as ``3h`` or ``2w`` to milliseconds."""
    match = PERIOD_RE.fullmatch(period)
    if match is None:
        raise ValueError(f"invalid time range {period!r}")
    return int(period[:-1]) * PERIOD_UNIT_MS[period[-1].lower()]


def cutoff_timestamp(period: str | None, now_ms: int | None = None) -> int | None:
    """Return the beginning of a configured window; ``None`` means all-time."""
    if period is None:
        return None
    current = int(time.time() * 1000) if now_ms is None else now_ms
    return current - period_milliseconds(period)


def points_in_period(
    points: list[Point], period: str | None, now_ms: int | None = None
) -> list[Point]:
    cutoff = cutoff_timestamp(period, now_ms)
    if cutoff is None:
        return points
    return [point for point in points if point.timestamp >= cutoff]


def value_at_cutoff(points: list[Point], period: str | None) -> float:
    if period is None:
        return points[0].value
    cutoff = cutoff_timestamp(period)
    assert cutoff is not None
    return min(points, key=lambda point: abs(point.timestamp - cutoff)).value


def period_title(period: str | None) -> str:
    return "ALL TIME" if period is None else f"LAST {period.upper()}"


def downsample(points: list[Point]) -> list[Point]:
    """Reduce to MAX_POINTS buckets, keeping each bucket's maximum."""
    if len(points) <= MAX_POINTS:
        return points

    size = len(points) / MAX_POINTS
    result: list[Point] = []
    for index in range(MAX_POINTS):
        bucket = points[int(index * size) : max(int(index * size) + 1, int((index + 1) * size))]
        if bucket:
            peak = max(point.value for point in bucket)
            result.append(Point(bucket[len(bucket) // 2].timestamp, peak))

    if result[-1].timestamp != points[-1].timestamp:
        result.append(points[-1])
    return result


def nice_scale(maximum: float, intervals: int = 4) -> tuple[float, float]:
    """Return ``(axis_max, step)`` using round step sizes."""
    if maximum <= 0:
        return float(intervals), 1.0
    raw_step = maximum / intervals
    magnitude = 10 ** math.floor(math.log10(raw_step))
    step = 10 * magnitude
    for multiplier in (1, 2, 2.5, 3, 4, 5):
        if multiplier * magnitude >= raw_step:
            step = multiplier * magnitude
            break
    if step < 1 and maximum >= intervals:
        step = 1.0
    return step * intervals, step


def smooth_path(points: list[tuple[float, float]]) -> str:
    """Build a monotone cubic path (Fritsch-Carlson) through the points."""
    if not points:
        return ""
    path = f"M{points[0][0]:.1f},{points[0][1]:.1f}"
    if len(points) == 1:
        return path

    slopes: list[float] = []
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        slopes.append((y1 - y0) / (x1 - x0) if x1 != x0 else 0.0)

    tangents: list[float] = [slopes[0]]
    for before, after in itertools.pairwise(slopes):
        tangents.append(0.0 if before * after <= 0 else 2 / (1 / before + 1 / after))
    tangents.append(slopes[-1])

    segments = [path]
    for index, ((x0, y0), (x1, y1)) in enumerate(itertools.pairwise(points)):
        third = (x1 - x0) / 3
        segments.append(
            f"C{x0 + third:.1f},{y0 + tangents[index] * third:.1f} "
            f"{x1 - third:.1f},{y1 - tangents[index + 1] * third:.1f} {x1:.1f},{y1:.1f}"
        )
    return " ".join(segments)


# Formatting


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def format_number(value: float) -> str:
    sign = "-" if value < 0 else ""
    value = abs(value)
    for limit, suffix in ((1_000_000, "M"), (1_000, "k")):
        if value >= limit:
            scaled = value / limit
            text = f"{scaled:.1f}" if scaled < 10 else f"{scaled:.0f}"
            return f"{sign}{text.removesuffix('.0')}{suffix}"
    return f"{sign}{int(value)}" if value.is_integer() else f"{sign}{value:.1f}"


def format_date(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, tz=IST).strftime("%b %d")


def format_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, tz=IST).strftime("%H:%M")


def format_month(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, tz=IST).strftime("%b %y")


def format_change(current: float, previous: float) -> tuple[str, str]:
    change = current - previous
    if abs(change) < 1e-9:
        return "■ 0", "flat"
    arrow, css = ("▲", "up") if change > 0 else ("▼", "down")
    if previous >= 100:
        percent = abs(change) / previous * 100
        return f"{arrow} {percent:.1f}%" if percent < 10 else f"{arrow} {percent:.0f}%", css
    return f"{arrow} {format_number(abs(change))}", css


def text_width(text: str, size: float, bold: bool = False) -> float:
    """Approximate rendered width; SVG text cannot be measured server-side."""
    return len(text) * size * (0.60 if bold else 0.55)


def truncate(text: str, max_width: float, size: float, bold: bool = False) -> str:
    if text_width(text, size, bold) <= max_width:
        return text
    while text and text_width(f"{text}…", size, bold) > max_width:
        text = text[:-1]
    return f"{text.rstrip()}…"


def version_key(name: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", name)) or (-1,)


# SVG rendering

FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,&quot;Segoe UI&quot;,"
    "&quot;Noto Sans&quot;,Helvetica,Arial,sans-serif"
)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

Theme = Literal["auto", "dark", "light"]
THEMES: tuple[Theme, ...] = ("auto", "dark", "light")

DARK_PALETTE = """
  --bg:#0d1117; --panel:#141c28; --border:#203546; --text:#f0f6fc; --muted:#91a0b4;
  --grid:#1e3544; --server:#08d477; --player:#1598ff; --plugin:#bc8cff; --track:#172337;
  --up:#08d477; --down:#f85149; --badge:#1b2636; --plot:#08151e;
  --server-a:#22c55e; --server-b:#059669; --player-a:#38bdf8; --player-b:#2563eb;"""
LIGHT_PALETTE = """
  --bg:#ffffff; --panel:#f6f8fa; --border:#d0d7de; --text:#1f2328; --muted:#656d76;
  --grid:#d5dfe6; --server:#1a7f37; --player:#0969da; --plugin:#8250df; --track:#eaeef2;
  --up:#1a7f37; --down:#cf222e; --badge:#eaeef2; --plot:#f7fafc;
  --server-a:#2da44e; --server-b:#1a7f37; --player-a:#218bff; --player-b:#0969da;"""


def palette(theme: Theme) -> str:
    """Colour variables for a theme. ``auto`` follows the viewer's colour scheme."""
    if theme == "dark":
        return f":root {{{DARK_PALETTE}\n}}"
    if theme == "light":
        return f":root {{{LIGHT_PALETTE}\n}}"
    return (
        f":root {{{DARK_PALETTE}\n}}\n"
        f"@media (prefers-color-scheme: light) {{\n:root {{{LIGHT_PALETTE}\n}}\n}}"
    )


STYLESHEET = f"""
text {{ font-family:{FONT_STACK}; font-variant-numeric:tabular-nums; }}
.frame {{ fill:var(--panel); stroke:var(--border); }}
.divider {{ stroke:var(--border); stroke-width:1; }}
.live {{ fill:var(--up); }}
.kicker {{ fill:var(--muted); font-size:12px; font-weight:600; letter-spacing:.08em; }}
.title {{ fill:var(--text); font-size:30px; font-weight:800; letter-spacing:-.02em; }}
.subtitle {{ fill:var(--muted); font-size:15px; }}
.stat-server {{ fill:url(#stat-server); }}
.stat-player {{ fill:url(#stat-player); }}
.stat-sheen {{ fill:url(#stat-sheen); }}
.stat-label {{ fill:#ffffff; fill-opacity:.82; font-size:11px; font-weight:700;
  letter-spacing:.08em; }}
.stat-value {{ fill:#ffffff; font-size:34px; font-weight:800; letter-spacing:-.03em; }}
.stat-pill {{ fill:#ffffff; fill-opacity:.2; }}
.delta {{ fill:#ffffff; font-size:12.5px; font-weight:700; }}
.stop-server-a {{ stop-color:var(--server-a); }}
.stop-server-b {{ stop-color:var(--server-b); }}
.stop-player-a {{ stop-color:var(--player-a); }}
.stop-player-b {{ stop-color:var(--player-b); }}
.section-title {{ fill:var(--text); font-size:16px; font-weight:700; letter-spacing:-.01em; }}
.note {{ fill:var(--muted); font-size:12.5px; }}
.grid {{ stroke:var(--grid); stroke-width:1; opacity:.38; }}
.grid.base {{ opacity:1; }}
.axis {{ fill:var(--muted); font-size:11.5px; }}
.axis-server {{ fill:var(--server); }}
.axis-player {{ fill:var(--player); }}
.stop-server {{ stop-color:var(--server); }}
.stop-player {{ stop-color:var(--player); }}
.area-server {{ fill:var(--server); fill-opacity:.22; }}
.area-player {{ fill:var(--player); fill-opacity:.20; }}
.line {{ fill:none; stroke-width:2; stroke-linecap:round; stroke-linejoin:round; }}
.line-server {{ stroke:var(--server); }}
.line-player {{ stroke:var(--player); }}
.fill-server {{ fill:var(--server); }}
.fill-player {{ fill:var(--player); }}
.rank {{ fill:var(--muted); font-size:12px; font-weight:700; }}
.rank-name {{ fill:var(--text); font-size:14px; font-weight:500; }}
.rank-value {{ fill:var(--text); font-size:12.5px; font-weight:600; }}
.rank-count {{ fill:var(--muted); font-weight:400; }}
.track {{ fill:var(--track); }}
.bar-0 {{ fill:var(--server); }}
.bar-1 {{ fill:var(--plugin); }}
.bar-2 {{ fill:var(--player); }}
.badge {{ fill:var(--badge); stroke:var(--border); stroke-width:.75; }}
.badge-text {{ fill:var(--muted); font-size:9.5px; font-weight:700; letter-spacing:.04em; }}
.tag {{ fill:none; stroke:var(--plugin); stroke-width:1; }}
.tag-text {{ fill:var(--plugin); font-size:9px; font-weight:700; letter-spacing:.06em; }}
.empty {{ fill:var(--muted); font-size:13px; }}
.plot-bg {{ fill:var(--plot); stroke:var(--border); stroke-width:.8; }}
.chart-panel {{ fill:var(--panel); stroke:var(--border); stroke-width:1; }}
"""


def render_header(stats: Stats) -> str:
    service = stats.service
    box_width, box_height, gap = 172, 78, 12
    box_x = WIDTH - MARGIN - 2 * box_width - gap
    period_tag = "ALL" if stats.period is None else stats.period.upper()

    if service.is_global:
        kicker = f"BSTATS GLOBAL · {period_title(stats.period)}"
        subtitle = f"All {service.name} {service.unit.lower()} reporting to bStats"
    else:
        kicker = f"BSTATS · {period_title(stats.period)}"
        subtitle = "Plugin statistics"

    def stat(x: int, kind: str, label: str, points: list[Point]) -> str:
        current = points[-1].value
        change, _ = format_change(current, value_at_cutoff(points, stats.period))
        pill_width = round(text_width(change, 12.5, bold=True) + 14)
        pill_x = box_width - 14 - pill_width
        return (
            f'<g transform="translate({x},0)">'
            f'<rect width="{box_width}" height="{box_height}" rx="12" class="stat-{kind}"/>'
            f'<rect width="{box_width}" height="{box_height}" rx="12" class="stat-sheen"/>'
            f'<text x="16" y="25" class="stat-label">{esc(label.upper())}</text>'
            f'<text x="{box_width - 16}" y="25" text-anchor="end" class="stat-label">'
            f"{period_tag}</text>"
            f'<text x="16" y="62" class="stat-value">{esc(format_number(current))}</text>'
            f'<rect x="{pill_x}" y="44" width="{pill_width}" height="21" rx="10.5" '
            'class="stat-pill"/>'
            f'<text x="{pill_x + pill_width / 2:.1f}" y="58.5" text-anchor="middle" '
            f'class="delta">{esc(change)}</text>'
            "</g>"
        )

    name = truncate(service.name, box_x - MARGIN - 24, 30, bold=True)
    return f"""
<g transform="translate({MARGIN},46)">
  <circle cx="4" cy="-4.5" r="4" class="live"/>
  <text x="16" y="0" class="kicker">{esc(kicker)}</text>
  <text x="0" y="39" class="title">{esc(name)}</text>
  <text x="0" y="65" class="subtitle">{esc(subtitle)}</text>
</g>
<g transform="translate({box_x},30)">
  {stat(0, "server", service.unit, stats.servers)}
  {stat(box_width + gap, "player", "Players", stats.players)}
</g>
<line x1="{MARGIN}" y1="{HEADER_RULE}" x2="{WIDTH - MARGIN}" y2="{HEADER_RULE}" \
class="divider"/>"""


def render_activity(stats: Stats) -> str:  # noqa: PLR0912
    """Render both series in one full-size panel with independent y scales."""
    width = WIDTH - MARGIN - CHART_LEFT
    baseline = CHART_TOP + CHART_HEIGHT
    now_ms = int(time.time() * 1000)

    servers = points_in_period(stats.servers, stats.period, now_ms)
    players = points_in_period(stats.players, stats.period, now_ms)
    shown_servers, shown_players = downsample(servers), downsample(players)
    all_points = shown_servers + shown_players
    all_history = stats.servers + stats.players
    first_recorded = min(point.timestamp for point in all_history)

    if stats.period is None:
        start = min(point.timestamp for point in all_points)
        end = max(point.timestamp for point in all_points)
    else:
        cutoff = cutoff_timestamp(stats.period, now_ms)
        assert cutoff is not None
        start = max(cutoff, first_recorded)
        end = now_ms
        if not all_points:
            start = cutoff

    if start == end:
        start -= 12 * 60 * 60 * 1000
        end += 12 * 60 * 60 * 1000

    server_value = max((point.value for point in servers), default=0.0)
    player_value = max((point.value for point in players), default=0.0)
    server_max, server_step = nice_scale(server_value)
    player_max, player_step = nice_scale(player_value)

    period_cutoff = cutoff_timestamp(stats.period, now_ms)
    if stats.period is None:
        range_label = f"All time · since {format_date(first_recorded)}"
    elif not all_points:
        range_label = f"No samples in {stats.period}"
    elif period_cutoff is not None and first_recorded > period_cutoff:
        range_label = f"New bStats history · since {format_date(first_recorded)}"
    else:
        range_label = f"Last {stats.period}"

    def x_of(timestamp: int) -> float:
        return CHART_LEFT + (timestamp - start) / (end - start) * width

    def y_of(value: float, maximum: float) -> float:
        return baseline - value / maximum * CHART_HEIGHT

    server_xy = [(x_of(point.timestamp), y_of(point.value, server_max)) for point in shown_servers]
    player_xy = [(x_of(point.timestamp), y_of(point.value, player_max)) for point in shown_players]
    server_path, player_path = smooth_path(server_xy), smooth_path(player_xy)

    def area(path: str, xy: list[tuple[float, float]]) -> str:
        if not path or not xy:
            return ""
        return f"{path} L{xy[-1][0]:.1f},{baseline} L{xy[0][0]:.1f},{baseline} Z"

    grid: list[str] = []
    intervals = max(round(server_max / server_step), round(player_max / player_step))
    for index in range(intervals + 1):
        fraction = index / intervals if intervals else 0
        y = baseline - fraction * CHART_HEIGHT
        server_tick = fraction * server_max
        player_tick = fraction * player_max
        grid.append(
            f'<line x1="{CHART_LEFT}" y1="{y:.1f}" '
            f'x2="{CHART_LEFT + width}" y2="{y:.1f}" '
            f'class="grid{" base" if index == 0 else ""}"/>'
            f'<text x="{CHART_LEFT - 9}" y="{y + 3.5:.1f}" '
            f'text-anchor="end" class="axis axis-server">'
            f"{esc(format_number(server_tick))}</text>"
            f'<text x="{WIDTH - MARGIN + 2}" y="{y + 3.5:.1f}" '
            f'text-anchor="start" class="axis axis-player">{esc(format_number(player_tick))}</text>'
        )

    span_days = (end - start) / 86_400_000
    if (stats.period is not None and stats.period.endswith("h")) or span_days <= 1:
        tick_count, tick_format = 3, format_time
    elif span_days <= 7:
        tick_count, tick_format = 4, format_date
    elif span_days <= 30:
        tick_count, tick_format = 5, format_date
    else:
        tick_count, tick_format = 6, format_month

    dates: list[str] = []
    for index in range(tick_count):
        timestamp = int(start + (end - start) * index / (tick_count - 1))
        tick_x = x_of(timestamp)
        grid.append(
            f'<line x1="{tick_x:.1f}" y1="{CHART_TOP}" '
            f'x2="{tick_x:.1f}" y2="{baseline}" class="grid grid-vertical"/>'
        )
        anchor = "start" if index == 0 else "end" if index == tick_count - 1 else "middle"
        dates.append(
            f'<text x="{tick_x:.1f}" y="{baseline + 22:.1f}" '
            f'text-anchor="{anchor}" class="axis">{esc(tick_format(timestamp))}</text>'
        )

    overlay = ""
    if not all_points:
        overlay = (
            f'<text x="{CHART_LEFT + width / 2:.1f}" '
            f'y="{CHART_TOP + CHART_HEIGHT / 2:.1f}" text-anchor="middle" '
            f'class="empty">No samples in the selected period</text>'
        )

    return f"""
<text x="{MARGIN}" y="156" class="section-title">Activity</text>
<text x="{WIDTH - MARGIN}" y="156" text-anchor="end" class="note">{esc(range_label)}</text>
<rect x="{CHART_LEFT}" y="{CHART_TOP}" width="{width}" height="{CHART_HEIGHT}"
  rx="8" class="plot-bg"/>
{"".join(grid)}
<g clip-path="url(#plot)">
  <path d="{area(player_path, player_xy)}" class="area-player"/>
  <path d="{area(server_path, server_xy)}" class="area-server"/>
  <path d="{player_path}" class="line line-player"/>
  <path d="{server_path}" class="line line-server"/>
</g>
{overlay}
{"".join(dates)}
<line x1="{MARGIN}" y1="{CHART_RULE}" x2="{WIDTH - MARGIN}" y2="{CHART_RULE}"
  class="divider"/>"""


def with_other(items: list[Share]) -> list[Share]:
    if len(items) <= TOP_ITEMS + 1:
        return items
    rest = items[TOP_ITEMS:]
    other = Share(
        name=f"Other ({len(rest)})",
        value=sum(item.value for item in rest),
        percent=sum(item.percent for item in rest),
    )
    return [*items[:TOP_ITEMS], other]


def render_column(index: int, column: Column) -> str:
    x = COLUMN_X[index]
    rows: list[str] = []
    latest = ""
    if column.kind == "plugin" and len(column.items) > 1:
        latest = max((item.name for item in column.items), key=version_key)

    for rank, item in enumerate(with_other(column.items), start=1):
        y = COLUMN_TOP + 18 + (rank - 1) * ROW_HEIGHT
        name_x = x + 26
        extras = ""

        if column.kind == "region":
            code = COUNTRY_CODES.get(item.name, "··" if item.name.startswith("Other") else "")
            if code:
                extras += (
                    f'<rect x="{name_x}" y="{y + 2}" width="26" height="17" rx="3" class="badge"/>'
                    f'<text x="{name_x + 13}" y="{y + 14}" text-anchor="middle" '
                    f'class="badge-text">{code}</text>'
                )
            name_x += 34

        percent = f"{item.percent:.1f}%"
        count = format_number(item.value)
        name_space = x + COLUMN_WIDTH - name_x - text_width(f"{percent} · {count}", 12.5) - 10
        is_latest = item.name == latest
        name = truncate(item.name, name_space - (58 if is_latest else 0), 14)

        if is_latest:
            tag_x = name_x + text_width(name, 14) + 8
            extras += (
                f'<rect x="{tag_x:.0f}" y="{y + 2}" width="46" height="17" rx="8.5" class="tag"/>'
                f'<text x="{tag_x + 23:.0f}" y="{y + 14}" text-anchor="middle" '
                f'class="tag-text">LATEST</text>'
            )

        fill = max(6.0, BAR_WIDTH * item.percent / 100)
        rows.append(
            f'<text x="{x}" y="{y + 15}" class="rank">{rank}</text>'
            f'<text x="{name_x}" y="{y + 15}" class="rank-name">{esc(name)}</text>{extras}'
            f'<text x="{x + COLUMN_WIDTH}" y="{y + 15}" text-anchor="end" class="rank-value">'
            f'{esc(percent)}<tspan class="rank-count"> · {esc(count)}</tspan></text>'
            f'<rect x="{x + 26}" y="{y + 22}" width="{BAR_WIDTH}" height="6" rx="3" class="track"/>'
            f'<rect x="{x + 26}" y="{y + 22}" width="{fill:.1f}" height="6" rx="3" '
            f'class="bar-{index}"/>'
        )

    if not rows:
        rows.append(f'<text x="{x}" y="{COLUMN_TOP + 30}" class="empty">No data reported</text>')

    return (
        f'<text x="{x}" y="{COLUMN_TOP}" class="section-title">{esc(column.title)}</text>'
        f'<text x="{x + COLUMN_WIDTH}" y="{COLUMN_TOP}" text-anchor="end" class="note">'
        f"{esc(column.note)}</text>{''.join(rows)}"
    )


def render_svg(stats: Stats, theme: Theme, updated: str) -> str:
    service = stats.service
    unit = service.unit.lower()
    chart_width = WIDTH - MARGIN - CHART_LEFT
    columns = "\n".join(render_column(i, column) for i, column in enumerate(stats.columns))
    divider_xs = [(a + COLUMN_WIDTH + b) // 2 for a, b in itertools.pairwise(COLUMN_X)]
    dividers = "".join(
        f'<line x1="{x}" y1="{COLUMN_TOP - 16}" x2="{x}" y2="{FOOTER_RULE - 20}" class="divider"/>'
        for x in divider_xs
    )

    shown_width, shown_height = WIDTH - 2 * BLEED, HEIGHT - 2 * BLEED
    description = (
        f"{esc(period_title(stats.period))} statistics: "
        f"{esc(format_number(stats.servers[-1].value))} {esc(unit)} and "
        f"{esc(format_number(stats.players[-1].value))} players."
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{shown_width}" \
height="{shown_height}" viewBox="{BLEED} {BLEED} {shown_width} {shown_height}" role="img" \
aria-labelledby="title desc">
<title id="title">{esc(service.name)} · bStats statistics</title>
<desc id="desc">{description}</desc>
<defs>
  <linearGradient id="stat-server" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" class="stop-server-a"/>
    <stop offset="100%" class="stop-server-b"/>
  </linearGradient>
  <linearGradient id="stat-player" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0%" class="stop-player-a"/>
    <stop offset="100%" class="stop-player-b"/>
  </linearGradient>
  <linearGradient id="stat-sheen" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#ffffff" stop-opacity=".14"/>
    <stop offset="55%" stop-color="#ffffff" stop-opacity="0"/>
  </linearGradient>
  <clipPath id="plot">
    <rect x="{CHART_LEFT - 4}" y="{CHART_TOP - 10}" width="{chart_width + 8}" \
height="{CHART_HEIGHT + 12}"/>
  </clipPath>
</defs>
<style>
{palette(theme)}{STYLESHEET}</style>
<rect x="{BLEED + 0.5}" y="{BLEED + 0.5}" width="{shown_width - 1}" \
height="{shown_height - 1}" rx="16" class="frame"/>
{render_header(stats)}
{render_activity(stats)}
{columns}
{dividers}
<line x1="{MARGIN}" y1="{FOOTER_RULE}" x2="{WIDTH - MARGIN}" y2="{FOOTER_RULE}" class="divider"/>
<text x="{MARGIN}" y="{FOOTER_RULE + 22}" class="note">\
Source: bStats · Anonymous data from opted-in {esc(unit)}</text>
<text x="{WIDTH - MARGIN}" y="{FOOTER_RULE + 22}" text-anchor="end" class="note">\
Updated {updated} IST</text>
</svg>
"""


# README


@dataclass(frozen=True)
class Card:
    service_id: int
    title: str
    url: str


@dataclass(frozen=True)
class Paths:
    root: Path
    config: Path
    readme: Path
    output: Path

    def card(self, service_id: int, theme: Theme) -> Path:
        name = str(service_id) if theme == "auto" else f"{service_id}-{theme}"
        return self.output / f"{name}.svg"

    def from_root(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def from_readme(self, path: Path) -> str:
        return Path(os.path.relpath(path, self.readme.parent)).as_posix()


def git_output(*args: str, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=True, timeout=10, cwd=cwd
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip()


def repository_root() -> Path:
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        return Path(workspace).resolve()
    top = git_output("rev-parse", "--show-toplevel")
    return Path(top).resolve() if top else Path.cwd().resolve()


def parse_args(argv: Sequence[str] | None = None) -> Paths:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    options = (
        ("--config", "BSTATS_CONFIG", DEFAULT_CONFIG, "list of bStats page URLs"),
        ("--readme", "BSTATS_README", DEFAULT_README, "Markdown file to update"),
        ("--output", "BSTATS_OUTPUT", DEFAULT_OUTPUT, "directory for the SVG cards"),
    )
    for flag, env, default, help_text in options:
        parser.add_argument(
            flag,
            default=os.environ.get(env) or default,
            help=f"{help_text} (env {env}, default {default})",
        )
    args = parser.parse_args(argv)
    root = repository_root()

    def resolve(value: object) -> Path:
        path = Path(str(value)).expanduser()
        return (path if path.is_absolute() else root / path).resolve()

    return Paths(root, resolve(args.config), resolve(args.readme), resolve(args.output))


REMOTE_PATTERN = re.compile(
    r"^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?[:/]"
    r"(?P<repo>.+?)(?:\.git)?/?$",
    re.IGNORECASE,
)


def public_base_url(root: Path) -> str | None:
    """URL prefix for raw files on the default branch, used in the copyable snippets.

    ``BSTATS_BASE_URL`` wins when set. Otherwise the host, repository and branch
    come from the GitHub Actions environment, or from the ``origin`` remote and
    the checked-out branch when run locally.
    """
    override = os.environ.get("BSTATS_BASE_URL", "").strip()
    if override:
        return override.rstrip("/")

    server = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "")
    if os.environ.get("GITHUB_REF_TYPE") == "tag":
        branch = ""

    if not server or not repository:
        match = REMOTE_PATTERN.match(git_output("remote", "get-url", "origin", cwd=root))
        if not match:
            return None
        server = server or f"https://{match['host']}"
        repository = repository or match["repo"]
    if not branch:
        branch = git_output("branch", "--show-current", cwd=root)
    if not branch:
        return None

    host = urllib.parse.urlsplit(server).hostname or ""
    if host == "github.com":
        # Serve directly from the raw host instead of following a redirect.
        return f"https://raw.{host.removesuffix('.com')}usercontent.com/{repository}/{branch}"
    return f"{server}/{repository}/raw/{branch}"


def markdown_image(card: Card, src: str) -> str:
    return f"[![{card.title} bStats statistics]({src})]({card.url})"


def picture(card: Card, sources: dict[Theme, str]) -> list[str]:
    """``<picture>`` element that switches with the viewer's theme."""
    alt = esc(f"{card.title} bStats statistics")
    return [
        f'<a href="{esc(card.url)}">',
        "  <picture>",
        f'    <source media="(prefers-color-scheme: dark)" srcset="{esc(sources["dark"])}">',
        f'    <source media="(prefers-color-scheme: light)" srcset="{esc(sources["light"])}">',
        f'    <img src="{esc(sources["auto"])}" alt="{alt}" width="100%">',
        "  </picture>",
        "</a>",
    ]


def render_card_section(card: Card, paths: Paths, base: str | None) -> list[str]:
    files: dict[Theme, Path] = {theme: paths.card(card.service_id, theme) for theme in THEMES}
    local: dict[Theme, str] = {theme: paths.from_readme(path) for theme, path in files.items()}
    public: dict[Theme, str] = {
        theme: f"{base}/{paths.from_root(path)}" if base else local[theme]
        for theme, path in files.items()
    }
    alt = esc(f"{card.title} bStats statistics")

    variants: tuple[tuple[Theme, str], ...] = (("dark", "Dark"), ("light", "Light"))
    lines: list[str] = []
    for theme, label in variants:
        image = f'<img src="{esc(local[theme])}" alt="{alt}" width="100%">'
        lines += [
            f"**{label}**",
            "",
            f'<p align="center"><a href="{esc(card.url)}">{image}</a></p>',
            "",
            "```markdown",
            markdown_image(card, public[theme]),
            "```",
            "",
        ]
    lines += [
        "<details>",
        "<summary><b>Automatic theme</b></summary>",
        "",
        "Follows the viewer's light or dark setting:",
        "",
        "```markdown",
        markdown_image(card, public["auto"]),
        "```",
        "",
        "Switches with the GitHub theme:",
        "",
        "```html",
        *picture(card, public),
        "```",
        "",
        "</details>",
    ]
    return lines


def render_readme_block(cards: list[Card], paths: Paths, base: str | None) -> str:
    lines = [START_MARKER, ""]
    for card in cards:
        if len(cards) > 1:
            lines += [f"### {esc(card.title)}", ""]
        lines += [*render_card_section(card, paths, base), ""]
    lines.append(END_MARKER)
    return "\n".join(lines)


def update_readme(cards: list[Card], paths: Paths) -> None:
    path = paths.readme
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    block = render_readme_block(cards, paths, public_base_url(paths.root))

    start, end = content.find(START_MARKER), content.find(END_MARKER)
    if start != -1 and end > start:
        content = content[:start] + block + content[end + len(END_MARKER) :]
    else:
        prefix = f"{content.rstrip()}\n\n" if content.strip() else ""
        content = f"{prefix}## Statistics\n\n{block}\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def previous_title(path: Path, fallback: str) -> str:
    """Read the title of an existing card, used when a refresh fails."""
    match = re.search(r"<title[^>]*>(.*?) · ", path.read_text(encoding="utf-8"))
    return html.unescape(match.group(1)) if match else fallback


def remove_stale_cards(paths: Paths, active: set[int]) -> None:
    for path in paths.output.glob("*.svg"):
        stem = path.stem.removesuffix("-dark").removesuffix("-light")
        if stem.isdigit() and int(stem) not in active:
            path.unlink()
            print(f"Removed {paths.from_root(path)}")


def main(argv: Sequence[str] | None = None) -> int:
    paths = parse_args(argv)
    try:
        sources = read_sources(paths.config)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    paths.output.mkdir(parents=True, exist_ok=True)
    updated = datetime.now(tz=IST).strftime("%Y-%m-%d %H:%M")
    cards: list[Card] = []
    failed = 0

    for source in sources:
        output = paths.card(source.service_id, "auto")
        try:
            stats = collect_stats(source)
        except RuntimeError as exc:
            failed += 1
            print(f"::warning::{source.url}: {exc}")
            if output.exists():
                cards.append(
                    Card(source.service_id, previous_title(output, source.url), source.url)
                )
            continue

        for theme in THEMES:
            paths.card(source.service_id, theme).write_text(
                render_svg(stats, theme, updated), encoding="utf-8"
            )
        suffix = " (global)" if stats.service.is_global else ""
        cards.append(Card(source.service_id, stats.service.name + suffix, source.url))
        print(f"Updated {paths.from_root(output)} ({stats.service.name}{suffix})")

    remove_stale_cards(paths, {source.service_id for source in sources})
    update_readme(cards, paths)

    if failed == len(sources):
        print("error: every source failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
