"""Hosted MCP service for COROS FIT parsing.

Uses Streamable HTTP instead of stdio so ChatGPT can call it over a public URL.
Raw records are cached briefly server-side and returned in small chunks; the first
parse response stays compact and includes a dataset_id plus measured metadata.
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import math
import os
import secrets
import socket
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fitparse import FitFile
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from activity_facts import analyze as build_activity_facts

MAX_FIT_BYTES = 80 * 1024 * 1024
MAX_CHUNK_ROWS = 500
CACHE_TTL_SECONDS = 20 * 60
MAX_CACHED_DATASETS = 4
DEFAULT_HOST_SUFFIXES = ("coros.com",)
ELEVATION_HYSTERESIS_M = 3.0
MIN_CLIMB_SEGMENT_M = 10.0
MAX_EXPORT_POINTS = 100_000
MAX_EXPORT_CHARS = 2_000_000
MAX_WEATHER_BYTES = 2 * 1024 * 1024
GPX_NS = "http://www.topografix.com/GPX/1/1"
GPX_TPX_NS = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
KML_NS = "http://www.opengis.net/kml/2.2"
KML_GX_NS = "http://www.google.com/kml/ext/2.2"
OPEN_METEO_ENDPOINTS = {
    "api.open-meteo.com": "/v1/forecast",
    "archive-api.open-meteo.com": "/v1/archive",
}
CORE_FIELDS = [
    "timestamp", "position_lat", "position_long", "altitude", "enhanced_altitude",
    "heart_rate", "cadence", "fractional_cadence", "speed", "enhanced_speed",
    "distance", "grade", "temperature", "vertical_speed", "stance_time",
    "stance_time_balance", "step_length", "vertical_oscillation", "vertical_ratio",
    "ground_contact_time", "avg_stance_time", "avg_stance_time_balance", "avg_step_length",
    "avg_vertical_oscillation", "avg_vertical_ratio", "avg_ground_contact_time", "power",
    "total_hemoglobin_conc", "saturated_hemoglobin_percent",
]

# Render supplies PORT. DNS-rebinding protection is disabled only because the service
# runs behind Render's reverse proxy; the upstream FIT downloader separately applies
# a strict HTTPS host allowlist and rejects non-public IP destinations.
mcp = FastMCP(
    "coros-fit-analyzer",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "10000")),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
_DATASETS: dict[str, dict[str, Any]] = {}


def _jsonable(value: Any):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _ts(value: Any):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _coord(value: Any):
    if isinstance(value, (int, float)) and abs(value) > 180:
        return value * (180.0 / 2147483648.0)
    return value


def _clean_record(record: dict[str, Any]) -> dict[str, Any]:
    out = {key: record.get(key) for key in CORE_FIELDS if record.get(key) is not None}
    if "position_lat" in out:
        out["latitude"] = _coord(out["position_lat"])
    if "position_long" in out:
        out["longitude"] = _coord(out["position_long"])
    if out.get("enhanced_altitude") is not None:
        out["altitude"] = out["enhanced_altitude"]
    if out.get("enhanced_speed") is not None:
        out["speed"] = out["enhanced_speed"]
    return out


def _suffixes() -> tuple[str, ...]:
    raw = os.environ.get("COROS_ALLOWED_HOST_SUFFIXES", "")
    configured = tuple(x.strip().lower().lstrip(".") for x in raw.split(",") if x.strip())
    return configured or DEFAULT_HOST_SUFFIXES


def _validate_coros_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("仅接受 HTTPS 的 COROS FIT 下载链接。")
    if parsed.username or parsed.password or not parsed.hostname:
        raise ValueError("FIT 下载链接格式无效。")
    if parsed.port not in (None, 443):
        raise ValueError("FIT 下载链接端口不受支持。")
    host = parsed.hostname.lower().rstrip(".")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in _suffixes()):
        raise ValueError(f"下载域名不在 COROS 允许列表中：{host}。可通过 COROS_ALLOWED_HOST_SUFFIXES 配置可信域名。")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"无法解析 FIT 下载域名：{host}") from exc
    if not infos:
        raise ValueError(f"FIT 下载域名没有可用地址：{host}")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise ValueError(f"已拒绝解析到非公网 IP 的下载域名：{host}")
    return url


class _SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urljoin(req.full_url, newurl)
        _validate_coros_url(resolved)
        return super().redirect_request(req, fp, code, msg, headers, resolved)


def _download_fit(url: str) -> bytes:
    _validate_coros_url(url)
    opener = build_opener(_SafeRedirectHandler())
    request = Request(url, headers={"User-Agent": "COROS-FIT-Analyzer/1.0", "Accept": "application/octet-stream"})
    try:
        with opener.open(request, timeout=25) as response:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_FIT_BYTES:
                raise ValueError("FIT 文件超过 80 MiB 限制。")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FIT_BYTES:
                    raise ValueError("FIT 文件超过 80 MiB 限制。")
                chunks.append(chunk)
    except HTTPError as exc:
        raise ValueError(f"COROS FIT 下载失败：HTTP {exc.code}。下载链接可能已过期，请重新获取。") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"COROS FIT 下载失败：{exc}") from exc
    raw = b"".join(chunks)
    if not raw:
        raise ValueError("下载到的 FIT 文件为空。")
    return raw


def _decode_fit(raw: bytes, file_name: str) -> dict[str, Any]:
    if not raw:
        raise ValueError("FIT 文件为空。")
    if len(raw) > MAX_FIT_BYTES:
        raise ValueError("FIT 文件超过 80 MiB 限制。")
    try:
        fit = FitFile(io.BytesIO(raw), check_crc=True)
        fit.parse()
        messages: list[dict[str, Any]] = []
        for message in fit.get_messages():
            row = {"message": message.name}
            for field in message:
                if field.value is not None:
                    row[field.name] = _jsonable(field.value)
            messages.append(row)
    except Exception as exc:
        raise ValueError(f"FIT 解码失败（包含 CRC/结构校验）：{type(exc).__name__}: {exc}") from exc

    counts = Counter(row["message"] for row in messages)
    source_records = [row for row in messages if row.get("message") == "record"]
    rows: list[dict[str, Any]] = []
    for source in source_records:
        row = {key: value for key, value in source.items() if key != "message"}
        row.update(_clean_record(source))
        rows.append(row)

    times = [_ts(row.get("timestamp")) for row in rows]
    valid_times = [value for value in times if value is not None]
    intervals = [(second - first).total_seconds() for first, second in zip(valid_times, valid_times[1:])]
    gaps = [value for value in intervals if value > 1.5]
    duplicate_timestamps = sum(value == 0 for value in intervals)
    backwards_timestamps = sum(value < 0 for value in intervals)
    field_counts = Counter(key for row in source_records for key, value in row.items() if key != "message" and value is not None)
    summary_names = {"file_id", "session", "lap", "sport", "activity", "device_info", "event"}
    summaries = {name: [row for row in messages if row.get("message") == name] for name in summary_names if counts.get(name, 0)}
    dataset_id = secrets.token_urlsafe(12)
    _purge_cache()
    _DATASETS[dataset_id] = {
        "created": time.time(),
        "file_name": Path(file_name).name,
        "records": rows,
        "file_bytes": len(raw),
        "raw_fit": raw,
        "summaries": summaries,
        "weather_cache": {},
    }
    while len(_DATASETS) > MAX_CACHED_DATASETS:
        oldest = min(_DATASETS, key=lambda key: _DATASETS[key]["created"])
        _DATASETS.pop(oldest, None)

    return {
        "parse_status": "success",
        "file_name": Path(file_name).name,
        "file_bytes": len(raw),
        "dataset_id": dataset_id,
        "message_counts": dict(counts),
        "record_count": len(rows),
        "record_fields": sorted(field_counts),
        "record_field_value_counts": dict(field_counts),
        "start_time": min(valid_times).isoformat() if valid_times else None,
        "end_time": max(valid_times).isoformat() if valid_times else None,
        "duration_seconds": round((max(valid_times) - min(valid_times)).total_seconds(), 2) if len(valid_times) > 1 else None,
        "mean_record_interval_seconds": round(sum(intervals) / len(intervals), 3) if intervals else None,
        "gaps_over_1_5_seconds_count": len(gaps),
        "duplicate_timestamps_count": duplicate_timestamps,
        "backwards_timestamps_count": backwards_timestamps,
        "session_lap_and_device_summaries": summaries,
        "first_records_preview": rows[:5],
        "last_records_preview": rows[-3:],
        "records_in_response": 8 if rows else 0,
        "records_truncated_in_response": True if len(rows) > 8 else False,
        "retrieve_remaining_records_with": "get_fit_record_chunk(dataset_id, start_index, count)",
        "available_followups": [
            "export_fit_original",
            "analyze_activity",
            "export_fit_gpx",
            "export_fit_kml",
            "analyze_climb",
            "get_track_location_weather",
        ],
        "dataset_cache_ttl_seconds": CACHE_TTL_SECONDS,
        "warnings": [
            "逐记录数据来自 FIT record 消息，未插值或补造。",
            "完整记录保存在本服务内存中，需在 20 分钟内分块读取；服务重启会清空缓存。",
            "只报告源文件实际解码得到的字段；设备未记录的跑姿指标不会被推测。",
        ],
    }


def _purge_cache() -> None:
    now = time.time()
    for key in [key for key, value in _DATASETS.items() if now - value["created"] > CACHE_TTL_SECONDS]:
        _DATASETS.pop(key, None)


def _get_dataset(dataset_id: str) -> dict[str, Any]:
    _purge_cache()
    dataset = _DATASETS.get(dataset_id)
    if dataset is None:
        raise ValueError("找不到该 dataset_id：缓存可能已过期或服务已重启，请重新调用 parse_fit_file_url / parse_fit_file_base64。")
    return dataset


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _valid_coord(lat: Any, lon: Any) -> bool:
    latitude = _num(lat)
    longitude = _num(lon)
    return latitude is not None and longitude is not None and -90 <= latitude <= 90 and -180 <= longitude <= 180


def _as_utc(value: Any) -> datetime | None:
    moment = value if isinstance(value, datetime) else _ts(value)
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _xml_time(value: Any) -> str | None:
    moment = _as_utc(value)
    if moment is None:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _xml(root: ET.Element, namespaces: dict[str, str]) -> str:
    for prefix, uri in namespaces.items():
        ET.register_namespace(prefix, uri)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _export_name(file_name: str, extension: str) -> str:
    stem = Path(file_name).stem
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in stem)[:80].strip("_")
    return f"{safe or 'activity'}.{extension}"


def _activity_label(dataset: dict[str, Any]) -> str:
    for key in ("sport", "session", "activity"):
        for row in (dataset.get("summaries") or {}).get(key, []):
            for field in ("sport", "sub_sport", "name"):
                value = row.get(field)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return Path(dataset["file_name"]).stem


def _parse_stride(stride: int) -> int:
    try:
        value = int(stride)
    except (TypeError, ValueError) as exc:
        raise ValueError("stride 必须是正整数。") from exc
    if value < 1 or value > 1000:
        raise ValueError("stride 必须在 1 到 1000 之间。")
    return value


def _collect_track_points(records: list[dict[str, Any]], stride: int) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    located = [(index, row) for index, row in enumerate(records) if _valid_coord(row.get("latitude"), row.get("longitude"))]
    skipped = len(records) - len(located)
    if stride == 1 or not located:
        return located, skipped
    sampled = located[::stride]
    if sampled[-1][0] != located[-1][0]:
        sampled.append(located[-1])
    return sampled, skipped


def _export_payload(dataset_id: str, dataset: dict[str, Any], extension: str, content: str, source_points: int, exported_points: int, skipped: int, stride: int) -> dict[str, Any]:
    file_name = _export_name(dataset["file_name"], extension)
    omitted = len(content) > MAX_EXPORT_CHARS
    return {
        "parse_status": "success",
        "format": extension,
        "dataset_id": dataset_id,
        "file_name": file_name,
        "source_point_count": source_points,
        "point_count": exported_points,
        "records_without_coordinates": skipped,
        "stride": stride,
        "download_path": f"/datasets/{dataset_id}/track.{extension}",
        "content_omitted": omitted,
        "content": None if omitted else content,
        "warnings": [
            "轨迹点来自 FIT record 中的原始经纬度，未做纠偏或插值。",
            "没有有效经纬度的记录已跳过。",
            "stride 大于 1 时按轨迹点顺序抽稀，并始终保留最后一个点。",
            "响应超过 2,000,000 字符时不内嵌文件正文，请通过 download_path 下载完整文件。",
        ],
    }


def _build_gpx(dataset: dict[str, Any], points: list[tuple[int, dict[str, Any]]]) -> str:
    label = _activity_label(dataset)
    root = ET.Element(_q(GPX_NS, "gpx"), {"version": "1.1", "creator": "coros-fit-analyzer"})
    metadata = ET.SubElement(root, _q(GPX_NS, "metadata"))
    ET.SubElement(metadata, _q(GPX_NS, "name")).text = label
    times = [_xml_time(row.get("timestamp")) for _, row in points]
    times = [value for value in times if value]
    if times:
        ET.SubElement(metadata, _q(GPX_NS, "time")).text = times[0]
    track = ET.SubElement(root, _q(GPX_NS, "trk"))
    ET.SubElement(track, _q(GPX_NS, "name")).text = label
    segment = ET.SubElement(track, _q(GPX_NS, "trkseg"))
    for _, row in points:
        point = ET.SubElement(segment, _q(GPX_NS, "trkpt"), {
            "lat": f"{_num(row['latitude']):.7f}",
            "lon": f"{_num(row['longitude']):.7f}",
        })
        altitude = _num(row.get("altitude"))
        if altitude is not None:
            ET.SubElement(point, _q(GPX_NS, "ele")).text = f"{altitude:.1f}"
        stamp = _xml_time(row.get("timestamp"))
        if stamp:
            ET.SubElement(point, _q(GPX_NS, "time")).text = stamp
        heart_rate = _num(row.get("heart_rate"))
        cadence = _num(row.get("cadence"))
        temperature = _num(row.get("temperature"))
        if heart_rate is None and cadence is None and temperature is None:
            continue
        extension = ET.SubElement(point, _q(GPX_NS, "extensions"))
        tpx = ET.SubElement(extension, _q(GPX_TPX_NS, "TrackPointExtension"))
        if heart_rate is not None:
            ET.SubElement(tpx, _q(GPX_TPX_NS, "hr")).text = str(int(round(heart_rate)))
        if cadence is not None:
            ET.SubElement(tpx, _q(GPX_TPX_NS, "cad")).text = str(int(round(cadence)))
        if temperature is not None:
            ET.SubElement(tpx, _q(GPX_TPX_NS, "atemp")).text = f"{temperature:.1f}"
    return _xml(root, {"": GPX_NS, "gpxtpx": GPX_TPX_NS})


def _kml_coordinate(row: dict[str, Any]) -> str:
    lon = f"{_num(row['longitude']):.7f}"
    lat = f"{_num(row['latitude']):.7f}"
    altitude = _num(row.get("altitude"))
    if altitude is None:
        return f"{lon},{lat}"
    return f"{lon},{lat},{altitude:.1f}"


def _build_kml(dataset: dict[str, Any], points: list[tuple[int, dict[str, Any]]]) -> str:
    label = _activity_label(dataset)
    root = ET.Element(_q(KML_NS, "kml"))
    document = ET.SubElement(root, _q(KML_NS, "Document"))
    ET.SubElement(document, _q(KML_NS, "name")).text = label
    placemark = ET.SubElement(document, _q(KML_NS, "Placemark"))
    ET.SubElement(placemark, _q(KML_NS, "name")).text = label
    stamps = [_xml_time(row.get("timestamp")) for _, row in points]
    if stamps and all(stamps):
        track = ET.SubElement(placemark, _q(KML_GX_NS, "Track"))
        for stamp in stamps:
            ET.SubElement(track, _q(KML_NS, "when")).text = stamp
        for _, row in points:
            altitude = _num(row.get("altitude"))
            ET.SubElement(track, _q(KML_GX_NS, "coord")).text = " ".join(
                part for part in (
                    f"{_num(row['longitude']):.7f}",
                    f"{_num(row['latitude']):.7f}",
                    None if altitude is None else f"{altitude:.1f}",
                ) if part is not None
            )
    else:
        line = ET.SubElement(placemark, _q(KML_NS, "LineString"))
        ET.SubElement(line, _q(KML_NS, "tessellate")).text = "1"
        if any(_num(row.get("altitude")) is not None for _, row in points):
            ET.SubElement(line, _q(KML_NS, "altitudeMode")).text = "absolute"
        ET.SubElement(line, _q(KML_NS, "coordinates")).text = " ".join(_kml_coordinate(row) for _, row in points)
    return _xml(root, {"": KML_NS, "gx": KML_GX_NS})


def _export_track(dataset_id: str, extension: str, stride: int) -> dict[str, Any]:
    stride = _parse_stride(stride)
    dataset = _get_dataset(dataset_id)
    points, skipped = _collect_track_points(dataset["records"], stride)
    if not points:
        raise ValueError("轨迹中没有有效经纬度，无法导出 GPX 或 KML。")
    if len(points) > MAX_EXPORT_POINTS:
        raise ValueError(f"轨迹点超过 {MAX_EXPORT_POINTS} 个，请增大 stride 后再导出。")
    located = sum(1 for row in dataset["records"] if _valid_coord(row.get("latitude"), row.get("longitude")))
    content = _build_gpx(dataset, points) if extension == "gpx" else _build_kml(dataset, points)
    return _export_payload(dataset_id, dataset, extension, content, located, len(points), skipped, stride)


def _hysteresis_path(altitudes: list[float], threshold: float) -> list[float]:
    path = [altitudes[0]]
    anchor = altitudes[0]
    for altitude in altitudes[1:]:
        if abs(altitude - anchor) >= threshold:
            anchor = altitude
        path.append(anchor)
    return path


def _distance_delta(start: dict[str, Any], end: dict[str, Any]) -> float | None:
    start_distance = start.get("distance_m")
    end_distance = end.get("distance_m")
    if start_distance is None or end_distance is None:
        return None
    delta = end_distance - start_distance
    if delta < 0:
        return None
    return delta


def _segment(samples: list[dict[str, Any]], start: int, end: int, change_m: float, kind: str) -> dict[str, Any]:
    distance = _distance_delta(samples[start], samples[end])
    duration = None
    start_time = _as_utc(samples[start].get("timestamp"))
    end_time = _as_utc(samples[end].get("timestamp"))
    if start_time is not None and end_time is not None and end_time >= start_time:
        duration = round((end_time - start_time).total_seconds(), 1)
    grade = None
    if distance is not None and distance >= 5:
        grade = round(change_m / distance * 100, 2)
    return {
        "kind": kind,
        "start_index": samples[start]["index"],
        "end_index": samples[end]["index"],
        "start_time": samples[start].get("timestamp"),
        "end_time": samples[end].get("timestamp"),
        "start_altitude_m": round(samples[start]["anchor_m"], 1),
        "end_altitude_m": round(samples[end]["anchor_m"], 1),
        "elevation_change_m": round(abs(change_m), 1),
        "distance_m": None if distance is None else round(distance, 1),
        "duration_seconds": duration,
        "average_grade_percent": grade,
    }


def _elevation_segments(samples: list[dict[str, Any]], path: list[float], kind: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    index = 0
    last = len(path) - 1
    while index < last:
        nxt = path[index + 1]
        rising = nxt > path[index]
        if (kind == "climb" and not rising) or (kind == "descent" and nxt >= path[index]):
            index += 1
            continue
        start = index
        while index < last and ((path[index + 1] > path[index]) if kind == "climb" else (path[index + 1] < path[index])):
            index += 1
        change = path[index] - path[start]
        if abs(change) >= MIN_CLIMB_SEGMENT_M:
            segments.append(_segment(samples, start, index, change, kind))
    return segments


def _device_total_ascent(dataset: dict[str, Any]) -> float | None:
    for row in (dataset.get("summaries") or {}).get("session", []):
        for key in ("total_ascent", "enhanced_total_ascent"):
            value = _num(row.get(key))
            if value is not None:
                return round(value, 1)
    return None


def _analyze_climb(dataset_id: str) -> dict[str, Any]:
    dataset = _get_dataset(dataset_id)
    records = dataset["records"]
    samples: list[dict[str, Any]] = []
    for index, row in enumerate(records):
        altitude = _num(row.get("altitude"))
        if altitude is None:
            continue
        samples.append({
            "index": index,
            "altitude_m": altitude,
            "distance_m": _num(row.get("distance")),
            "timestamp": row.get("timestamp"),
        })
    if not samples:
        raise ValueError("记录中没有海拔，无法做爬升分析。")
    altitudes = [sample["altitude_m"] for sample in samples]
    path = _hysteresis_path(altitudes, ELEVATION_HYSTERESIS_M)
    for sample, anchor in zip(samples, path):
        sample["anchor_m"] = anchor
    ascent = 0.0
    descent = 0.0
    for previous, current in zip(path, path[1:]):
        delta = current - previous
        if delta > 0:
            ascent += delta
        elif delta < 0:
            descent -= delta
    climbs = _elevation_segments(samples, path, "climb")
    descents = _elevation_segments(samples, path, "descent")
    graded = [segment for segment in climbs if segment.get("average_grade_percent") is not None and (segment.get("distance_m") or 0) >= 30]
    steepest = max(graded, key=lambda segment: segment["average_grade_percent"]) if graded else None
    largest = max(climbs, key=lambda segment: segment["elevation_change_m"]) if climbs else None
    distance = _distance_delta(samples[0], samples[-1])
    duration = None
    start_time = _as_utc(samples[0].get("timestamp"))
    end_time = _as_utc(samples[-1].get("timestamp"))
    if start_time is not None and end_time is not None and end_time >= start_time:
        duration = round((end_time - start_time).total_seconds(), 1)
    source = "enhanced_altitude" if any(row.get("enhanced_altitude") is not None for row in records) else "altitude"
    return {
        "parse_status": "success",
        "dataset_id": dataset_id,
        "file_name": dataset["file_name"],
        "altitude_source": source,
        "altitude_sample_count": len(samples),
        "records_without_altitude": len(records) - len(samples),
        "start_altitude_m": round(altitudes[0], 1),
        "end_altitude_m": round(altitudes[-1], 1),
        "min_altitude_m": round(min(altitudes), 1),
        "max_altitude_m": round(max(altitudes), 1),
        "elevation_range_m": round(max(altitudes) - min(altitudes), 1),
        "total_ascent_m": round(ascent, 1),
        "total_descent_m": round(descent, 1),
        "ascent_per_km": None if not distance else round(ascent / (distance / 1000), 1),
        "analyzed_distance_m": None if distance is None else round(distance, 1),
        "analyzed_duration_seconds": duration,
        "vertical_meters_per_hour": None if not duration else round(ascent / (duration / 3600), 1),
        "device_reported_total_ascent_m": _device_total_ascent(dataset),
        "climb_segment_count": len(climbs),
        "descent_segment_count": len(descents),
        "largest_climb": largest,
        "steepest_climb": steepest,
        "climb_segments": climbs[:50],
        "descent_segments": descents[:50],
        "segments_truncated": len(climbs) > 50 or len(descents) > 50,
        "method": "按记录海拔顺序累计。相邻变化不足 3 米时保持锚点，不计入爬升或下降，用于抑制海拔抖动。分段起止海拔是回差锚点。爬坡和下坡分段只列出累计变化达到 10 米的区间。缺失海拔不插值。",
    }


def _int_code(value: Any) -> int | None:
    number = _num(value)
    if number is None:
        return None
    return int(number)


def _weather_text(code: Any) -> str | None:
    number = _num(code)
    if number is None:
        return None
    value = int(number)
    labels = {
        0: "晴",
        1: "大部晴朗",
        2: "局部多云",
        3: "阴",
        45: "雾",
        48: "雾凇",
        51: "小毛毛雨",
        53: "毛毛雨",
        55: "强毛毛雨",
        56: "冻毛毛雨",
        57: "强冻毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        66: "冻雨",
        67: "强冻雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        77: "雪粒",
        80: "小阵雨",
        81: "阵雨",
        82: "强阵雨",
        85: "小阵雪",
        86: "阵雪",
        95: "雷暴",
        96: "雷暴伴有冰雹",
        99: "强雷暴伴有冰雹",
    }
    return labels.get(value, f"天气代码 {value}")


def _series_at(values: Any, index: int) -> Any:
    if not isinstance(values, list) or index < 0 or index >= len(values):
        return None
    return values[index]


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("天气服务返回了不受支持的重定向。")


def _fetch_open_meteo(host: str, params: dict[str, str]) -> dict[str, Any]:
    path = OPEN_METEO_ENDPOINTS.get(host)
    if path is None:
        raise ValueError("天气数据源不受支持。")
    request = Request(
        f"https://{host}{path}?{urlencode(params)}",
        headers={"User-Agent": "COROS-FIT-Analyzer/1.0", "Accept": "application/json"},
    )
    try:
        with build_opener(_RejectRedirect()).open(request, timeout=20) as response:
            raw = response.read(MAX_WEATHER_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace").strip()
        raise ValueError(f"天气查询失败：HTTP {exc.code} {detail}".strip()) from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("天气服务"):
            raise
        raise ValueError(f"天气查询失败：{exc}") from exc
    if len(raw) > MAX_WEATHER_BYTES:
        raise ValueError("天气响应超过大小限制。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("天气响应不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise ValueError("天气响应格式无效。")
    if payload.get("error"):
        raise ValueError(f"天气查询失败：{payload.get('reason') or '未知原因'}")
    return payload


def _select_weather(payload: dict[str, Any], moment_utc: datetime, host: str) -> dict[str, Any]:
    offset = int(_num(payload.get("utc_offset_seconds")) or 0)
    local = moment_utc + timedelta(seconds=offset)
    hourly = payload.get("hourly") if isinstance(payload.get("hourly"), dict) else {}
    times = hourly.get("time") if isinstance(hourly.get("time"), list) else []
    if not times:
        raise ValueError("天气响应缺少逐小时数据。")
    local_naive = local.replace(tzinfo=None)
    best_index = None
    best_delta = None
    for index, value in enumerate(times):
        try:
            hour = datetime.fromisoformat(str(value))
        except ValueError:
            continue
        delta = abs((hour - local_naive).total_seconds())
        if best_delta is None or delta < best_delta:
            best_index = index
            best_delta = delta
    if best_index is None:
        raise ValueError("天气响应中的时间无法解析。")
    if best_delta is None or best_delta > 3600:
        raise ValueError("天气小时数据与活动时间相差超过一小时，无法核实。")
    code = _int_code(_series_at(hourly.get("weather_code"), best_index))
    daily = payload.get("daily") if isinstance(payload.get("daily"), dict) else {}
    daily_times = daily.get("time") if isinstance(daily.get("time"), list) else []
    local_date = local.date().isoformat()
    daily_index = daily_times.index(local_date) if local_date in daily_times else None
    daily_code = None if daily_index is None else _int_code(_series_at(daily.get("weather_code"), daily_index))
    return {
        "provider": "Open-Meteo",
        "endpoint_host": host,
        "timezone": payload.get("timezone"),
        "utc_offset_seconds": offset,
        "local_time": local.isoformat(),
        "local_date": local_date,
        "observed_time_local": times[best_index],
        "alignment_error_seconds": best_delta,
        "weather_code": code,
        "weather": _weather_text(code),
        "temperature_c": _num(_series_at(hourly.get("temperature_2m"), best_index)),
        "relative_humidity_percent": _num(_series_at(hourly.get("relative_humidity_2m"), best_index)),
        "precipitation_mm": _num(_series_at(hourly.get("precipitation"), best_index)),
        "wind_speed_m_s": _num(_series_at(hourly.get("wind_speed_10m"), best_index)),
        "daily_weather_code": daily_code,
        "daily_weather": _weather_text(daily_code),
        "daily_temperature_min_c": None if daily_index is None else _num(_series_at(daily.get("temperature_2m_min"), daily_index)),
        "daily_temperature_max_c": None if daily_index is None else _num(_series_at(daily.get("temperature_2m_max"), daily_index)),
        "daily_precipitation_mm": None if daily_index is None else _num(_series_at(daily.get("precipitation_sum"), daily_index)),
    }


def _load_weather(lat: float, lon: float, moment_utc: datetime) -> dict[str, Any]:
    day = moment_utc.date()
    params = {
        "latitude": f"{lat:.6f}",
        "longitude": f"{lon:.6f}",
        "hourly": "temperature_2m,relative_humidity_2m,weather_code,precipitation,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
        "timezone": "auto",
        "start_date": (day - timedelta(days=1)).isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
        "wind_speed_unit": "ms",
    }
    age_days = (datetime.now(timezone.utc).date() - day).days
    hosts = ["api.open-meteo.com", "archive-api.open-meteo.com"] if age_days <= 92 else ["archive-api.open-meteo.com"]
    errors: list[str] = []
    for host in hosts:
        try:
            return _select_weather(_fetch_open_meteo(host, params), moment_utc, host)
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError(errors[-1] if errors else "天气查询失败。")


def _track_point(records: list[dict[str, Any]], record_index: int | None, position: str) -> tuple[int, dict[str, Any], str]:
    if record_index is not None:
        if record_index < 0 or record_index >= len(records):
            raise ValueError(f"record_index 超出范围；当前 record_count={len(records)}。")
        row = records[record_index]
        if not _valid_coord(row.get("latitude"), row.get("longitude")):
            raise ValueError("该记录没有有效经纬度。")
        return record_index, row, "index"
    if position not in ("start", "end"):
        raise ValueError("position 只能是 start 或 end。")
    indexes = [index for index, row in enumerate(records) if _valid_coord(row.get("latitude"), row.get("longitude"))]
    if not indexes:
        raise ValueError("轨迹中没有有效经纬度。")
    chosen = indexes[0] if position == "start" else indexes[-1]
    return chosen, records[chosen], position


def _location_weather(dataset_id: str, record_index: int | None = None, position: str = "end") -> dict[str, Any]:
    dataset = _get_dataset(dataset_id)
    index, row, role = _track_point(dataset["records"], record_index, position)
    latitude = _num(row.get("latitude"))
    longitude = _num(row.get("longitude"))
    moment = _as_utc(row.get("timestamp"))
    result: dict[str, Any] = {
        "parse_status": "success",
        "dataset_id": dataset_id,
        "file_name": dataset["file_name"],
        "position": role,
        "record_index": index,
        "latitude": latitude,
        "longitude": longitude,
        "altitude_m": _num(row.get("altitude")),
        "timestamp": row.get("timestamp"),
        "distance_m": _num(row.get("distance")),
        "weather": None,
        "warnings": [
            "默认位置是轨迹中最后一个有效 GPS 点；position=start 取第一个点，传入 record_index 则使用该记录。",
            "天气按该点坐标及其时间戳换算到当地日期查询，只把经纬度和日期发送给 Open-Meteo。",
            "温度和天气来自格点气象数据，不是手表记录的气温。",
        ],
    }
    if moment is None:
        result["warnings"].append("该轨迹点没有时间戳，无法确定当天日期，因此未查询天气。")
        return result
    cache = dataset.setdefault("weather_cache", {})
    cache_key = f"{latitude:.4f},{longitude:.4f},{moment.strftime('%Y-%m-%dT%H')}"
    if cache_key not in cache:
        cache[cache_key] = _load_weather(latitude, longitude, moment)
    result["weather"] = cache[cache_key]
    return result


@mcp.tool()
def server_health() -> dict:
    """检查托管的 COROS FIT 解析服务是否在线。"""
    _purge_cache()
    return {"status": "ok", "service": "coros-fit-analyzer", "mcp_transport": "streamable-http", "cached_datasets": len(_DATASETS)}


@mcp.tool()
def parse_fit_file_url(file_url: str, file_name: str = "activity.fit") -> dict:
    """直接从 COROS 返回的 HTTPS FIT 下载链接下载并验证原始文件，提取真实 FIT 消息/record 记录。优先使用此工具，避免把几百 KB 的 Base64 原始文件塞进模型上下文。仅允许 COROS 域名（默认 *.coros.com），最多 80 MiB。返回 dataset_id 后，用 get_fit_record_chunk 分页读取记录。"""
    return _decode_fit(_download_fit(file_url), file_name or Path(urlparse(file_url).path).name or "activity.fit")


@mcp.tool()
def parse_fit_file_base64(file_name: str, fit_file_base64: str) -> dict:
    """对实际原始 FIT 文件字节进行 Base64 解码和 CRC 校验。适用于没有可下载 URL 的情况。返回 dataset_id，完整逐记录数据使用 get_fit_record_chunk 分页读取。单文件最多 80 MiB。"""
    try:
        raw = base64.b64decode(fit_file_base64, validate=True)
    except Exception as exc:
        raise ValueError(f"FIT 文件 Base64 解码失败：{exc}") from exc
    return _decode_fit(raw, file_name)


@mcp.tool()
def get_fit_record_chunk(dataset_id: str, start_index: int = 0, count: int = 200) -> dict:
    """读取已解析 FIT 文件的逐记录数据分页。start_index 从 0 开始；每次最多 500 条。用于抽查原始 GPS、海拔、心率、步频、速度、距离及实际存在的跑姿字段，不进行插值。"""
    dataset = _get_dataset(dataset_id)
    if start_index < 0:
        raise ValueError("start_index 必须大于或等于 0。")
    count = max(1, min(int(count), MAX_CHUNK_ROWS))
    records = dataset["records"]
    if start_index > len(records):
        raise ValueError(f"start_index 超出范围；当前 record_count={len(records)}。")
    end_index = min(start_index + count, len(records))
    return {
        "parse_status": "success",
        "dataset_id": dataset_id,
        "file_name": dataset["file_name"],
        "record_count": len(records),
        "start_index": start_index,
        "end_index_exclusive": end_index,
        "records_returned": end_index - start_index,
        "has_more": end_index < len(records),
        "records": records[start_index:end_index],
    }


@mcp.tool()
def export_fit_original(dataset_id: str) -> dict:
    """返回原始 FIT 文件的 JSON 下载描述，供其他 App 导入。文件逐字节保留，不从 record 重建；download_path 相对于本服务，缓存到期后失效。"""
    dataset = _get_dataset(dataset_id)
    raw = dataset.get("raw_fit")
    if raw is None:
        raise ValueError("该数据集未保留原始 FIT 字节，请重新解析原始文件。")
    return {
        "status": "success", "dataset_id": dataset_id, "format": "fit",
        "file_name": _export_name(dataset["file_name"], "fit"),
        "content_type": "application/vnd.ant.fit", "file_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "original_bytes_preserved": True,
        "download_path": f"/datasets/{dataset_id}/original.fit",
        "expires_in_seconds": max(0, int(CACHE_TTL_SECONDS - (time.time()-dataset["created"]))),
        "warnings": ["下载路径相对于当前服务地址，不是完整 URL。", "缓存可能因重启、容量淘汰或过期而提前失效。"],
    }


@mcp.tool()
def export_fit_gpx(dataset_id: str, stride: int = 1) -> dict:
    """把已解析的 FIT 轨迹导出为 GPX 1.1。保留原始经纬度、海拔、时间和实际存在的心率、步频、温度。stride 为抽稀间隔，1 表示不抽稀。"""
    return _export_track(dataset_id, "gpx", stride)


@mcp.tool()
def export_fit_kml(dataset_id: str, stride: int = 1) -> dict:
    """把已解析的 FIT 轨迹导出为 KML。有完整时间戳时使用 gx:Track，否则使用 LineString。stride 为抽稀间隔，1 表示不抽稀。"""
    return _export_track(dataset_id, "kml", stride)


@mcp.tool()
def analyze_activity(
    dataset_id: str,
    hr_zone_boundaries: list[float] | None = None,
    hr_zone_source: str | None = None,
    pain_status: str = "unknown",
    requested_stage: str | None = None,
    nutrition_log: list[dict[str, Any]] | None = None,
    previous_dataset_id: str | None = None,
    include_weather: bool = False,
) -> dict:
    """生成活动事实 JSON：质检、自然坡段、1/5km 分箱、坡度档、心率、跑姿、补给及伤痛门控。

    缺失不补零，不生成因果或伤痛诊断。个人心率边界须明确提供，来源为
    user_max_hr / user_lthr / user_explicit。pain_status 为 unknown/unresolved/resolved。
    nutrition_log 项含 timestamp、carbohydrate_g、water_ml；缺项不推测。
    天气默认关闭，开启后最多查询六个活动时段轨迹点；历史对比只作走廊对齐。
    """
    dataset = _get_dataset(dataset_id)
    previous = _get_dataset(previous_dataset_id)["records"] if previous_dataset_id else None

    def weather(lat, lon, moment):
        cache = dataset.setdefault("weather_cache", {})
        key = f"{lat:.6f},{lon:.6f},{moment.isoformat()}"
        if key not in cache:
            cache[key] = _load_weather(lat, lon, moment)
        return cache[key]

    result = build_activity_facts(
        dataset["records"], dataset.get("summaries"), hr_zone_boundaries,
        hr_zone_source, pain_status, requested_stage, nutrition_log,
        previous, weather if include_weather else None,
    )
    result.update(dataset_id=dataset_id, file_name=dataset["file_name"])
    return result


@mcp.tool()
def analyze_climb(dataset_id: str) -> dict:
    """根据 FIT 记录中的海拔计算累计爬升、下降、海拔范围，以及达到 10 米的爬坡和下坡分段。使用 3 米回差抑制抖动，不对缺失海拔插值。"""
    return _analyze_climb(dataset_id)


@mcp.tool()
def get_track_location_weather(dataset_id: str, record_index: int | None = None, position: str = "end") -> dict:
    """获取轨迹位置，并联网查询该地在该点当天的天气与温度。默认取最后一个有效 GPS 点；position 可设为 start。传入 record_index 时使用该条记录。"""
    return _location_weather(dataset_id, record_index, position)


def _track_download(dataset_id: str, extension: str, stride: str) -> Response:
    try:
        dataset = _get_dataset(dataset_id)
        points, _skipped = _collect_track_points(dataset["records"], _parse_stride(stride))
        if not points:
            raise ValueError("轨迹中没有有效经纬度，无法导出 GPX 或 KML。")
        if len(points) > MAX_EXPORT_POINTS:
            raise ValueError(f"轨迹点超过 {MAX_EXPORT_POINTS} 个，请增大 stride 后再导出。")
        content = _build_gpx(dataset, points) if extension == "gpx" else _build_kml(dataset, points)
        filename = _export_name(dataset["file_name"], extension)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    media = "application/gpx+xml" if extension == "gpx" else "application/vnd.google-earth.kml+xml"
    return Response(content, media_type=f"{media}; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _track_gpx(request):
    return _track_download(request.path_params["dataset_id"], "gpx", request.query_params.get("stride", "1"))


async def _track_kml(request):
    return _track_download(request.path_params["dataset_id"], "kml", request.query_params.get("stride", "1"))


async def _healthz(request):
    _purge_cache()
    return JSONResponse({"status": "ok", "service": "coros-fit-analyzer", "cached_datasets": len(_DATASETS)})


async def _original_fit(request):
    try:
        dataset_id = request.path_params["dataset_id"]
        descriptor = export_fit_original(dataset_id)
        raw = _get_dataset(dataset_id)["raw_fit"]
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return Response(raw, media_type=descriptor["content_type"], headers={
        "Content-Disposition": f"attachment; filename=activity.fit; filename*=UTF-8''{quote(descriptor['file_name'], safe='')}",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
    })


async def _analyze_fit_upload(request):
    """App-facing binary upload, JSON response. No multipart or Base64 wrapper."""
    media = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media not in ("application/octet-stream", "application/vnd.ant.fit"):
        return JSONResponse({"error": "请直接上传 FIT 二进制，Content-Type 为 application/octet-stream 或 application/vnd.ant.fit。"}, status_code=415)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > MAX_FIT_BYTES:
            return JSONResponse({"error": "FIT 文件超过 80 MiB 限制。"}, status_code=413)
        raw.extend(chunk)
    try:
        parsed = _decode_fit(bytes(raw), request.query_params.get("file_name", "activity.fit"))
        dataset_id = parsed["dataset_id"]
        result = {"schema_version": "1.0", "parse": parsed,
                  "facts": analyze_activity(dataset_id),
                  "original_fit": export_fit_original(dataset_id)}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


# Build a real HTTP ASGI application; an account upload does not run a local stdio process.
app = mcp.streamable_http_app()
app.routes.append(Route("/healthz", _healthz, methods=["GET"]))
app.routes.append(Route("/analyze/fit", _analyze_fit_upload, methods=["POST"]))
app.routes.append(Route("/datasets/{dataset_id}/original.fit", _original_fit, methods=["GET"]))
app.routes.append(Route("/datasets/{dataset_id}/track.gpx", _track_gpx, methods=["GET"]))
app.routes.append(Route("/datasets/{dataset_id}/track.kml", _track_kml, methods=["GET"]))


if __name__ == "__main__":
    # Local development can run this in stdio mode by setting MCP_LOCAL_STDIO=1.
    if os.environ.get("MCP_LOCAL_STDIO") == "1":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), log_level="info")
