"""Hosted MCP service for COROS FIT parsing.

Uses Streamable HTTP instead of stdio so ChatGPT can call it over a public URL.
Raw records are cached briefly server-side and returned in small chunks; the first
parse response stays compact and includes a dataset_id plus measured metadata.
"""
from __future__ import annotations

import base64
import io
import ipaddress
import json
import os
import secrets
import socket
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from fitparse import FitFile
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

MAX_FIT_BYTES = 80 * 1024 * 1024
MAX_CHUNK_ROWS = 500
CACHE_TTL_SECONDS = 20 * 60
MAX_CACHED_DATASETS = 4
DEFAULT_HOST_SUFFIXES = ("coros.com",)
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
    _DATASETS[dataset_id] = {"created": time.time(), "file_name": Path(file_name).name, "records": rows, "file_bytes": len(raw)}
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
    _purge_cache()
    dataset = _DATASETS.get(dataset_id)
    if dataset is None:
        raise ValueError("找不到该 dataset_id：缓存可能已过期或服务已重启，请重新调用 parse_fit_file_url / parse_fit_file_base64。")
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


async def _healthz(request):
    _purge_cache()
    return JSONResponse({"status": "ok", "service": "coros-fit-analyzer", "cached_datasets": len(_DATASETS)})


# Build a real HTTP ASGI application; an account upload does not run a local stdio process.
app = mcp.streamable_http_app()
app.routes.append(Route("/healthz", _healthz, methods=["GET"]))


if __name__ == "__main__":
    # Local development can run this in stdio mode by setting MCP_LOCAL_STDIO=1.
    if os.environ.get("MCP_LOCAL_STDIO") == "1":
        mcp.run(transport="stdio")
    else:
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "10000")), log_level="info")
