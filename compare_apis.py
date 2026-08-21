#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API 兼容性对比工具

核心目标：
1. 旧接口作为基准；
2. 新接口只替换 host，path/query/body 尽量保持一致；
3. 自动对比 HTTP 状态码、JSON 结构、字段类型、字段值；
4. POST 接口支持使用多组 body 样本回放；
5. 输出 CSV / JSONL / HTML 报告。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import re
import sys
import time
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests


DEFAULT_CONFIG_PATH = Path("config/config.json")
DEFAULT_POST_BODY_CASES = Path("config/post_body_cases.csv")
DEFAULT_POST_CASES_JSON = Path("config/post_cases.json")

_REQUEST_THROTTLE_LOCK = threading.Lock()
_REQUEST_THROTTLE_NEXT_TS = 0.0


DIFF_TYPE_CN = {
    "HTTP_STATUS_MISMATCH": "HTTP 状态码不一致",
    "JSON_PARSE_MISMATCH": "JSON 解析结果不一致",
    "MISSING_IN_NEW": "新接口缺少旧接口字段",
    "EXTRA_IN_NEW": "新接口多出字段",
    "TYPE_MISMATCH": "字段类型不一致",
    "VALUE_MISMATCH": "字段值不一致",
    "ARRAY_ID_SET_MISMATCH": "列表 ID 集合不一致",
    "ARRAY_LENGTH_MISMATCH": "数组长度不一致",
    "ARRAY_ORDER_MISMATCH": "数组顺序不一致",
    "NON_JSON_BODY_MISMATCH": "非 JSON 响应内容不一致",
}

CATEGORY_CN = dict(DIFF_TYPE_CN)
CATEGORY_CN.update({
    "DRY_RUN": "仅预览未执行",
    "REQUEST_ERROR": "请求异常",
    "NETWORK_ERROR": "网络错误",
})

RESULT_CN = {
    "PASS": "通过",
    "FAIL": "失败",
    "ERROR": "错误",
    "DRY_RUN": "仅预览",
    "SKIP": "跳过",
}

TYPE_NAME_CN = {
    "dict": "对象",
    "list": "数组",
    "str": "字符串",
    "int": "整数",
    "float": "小数",
    "bool": "布尔",
    "null": "null",
}


def zh_diff_type(t: Any) -> str:
    return DIFF_TYPE_CN.get(str(t), str(t))


def zh_category(t: Any) -> str:
    return CATEGORY_CN.get(str(t), str(t))


def zh_result(t: Any) -> str:
    return RESULT_CN.get(str(t), str(t))


def zh_type_name(t: Any) -> str:
    return TYPE_NAME_CN.get(str(t), str(t))


@dataclass
class HttpResult:
    ok: bool
    status_code: Optional[int]
    elapsed_ms: int
    headers: Dict[str, str]
    body_bytes: bytes
    truncated: bool
    error: str = ""

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body_bytes).hexdigest() if self.body_bytes else ""

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type") or self.headers.get("content-type") or ""

    @property
    def text_preview(self) -> str:
        if not self.body_bytes:
            return ""
        text = self.body_bytes[:700].decode("utf-8", errors="replace")
        return text.replace("\r", " ").replace("\n", " ")


@dataclass
class BodyCase:
    body_case_no: str
    request_method: str
    request_path: str
    body_type: str
    body: Any
    headers: Dict[str, str]
    source_uri: str = ""
    source_cnt: str = ""
    remark: str = ""

    @property
    def preview(self) -> str:
        try:
            s = json.dumps(self.body, ensure_ascii=False, sort_keys=True)
        except Exception:
            s = str(self.body)
        return s[:500]


def norm_key_map(row: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in row.items():
        kk = str(k).lstrip("\ufeff").strip().strip('"').strip("'")
        out[kk] = "" if v is None else str(v)
    return out


def load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def emit(message: str, log_path: str = "", quiet_console: bool = False) -> None:
    if not quiet_console:
        print(message, flush=True)
    if log_path:
        with Path(log_path).open("a", encoding="utf-8", newline="") as f:
            f.write(str(message) + "\n")


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def print_progress(
    done: int,
    total: int,
    result_counts: Counter,
    start_ts: float,
    last_result: Optional[Dict[str, Any]] = None,
    log_path: str = "",
    quiet_console: bool = False,
) -> None:
    display_total = max(total, 0)
    calc_total = max(total, 1)
    percent = min(100.0, done * 100.0 / calc_total)
    elapsed = max(0.001, time.time() - start_ts)
    rate = done / elapsed if done else 0.0
    remaining = (calc_total - done) / rate if rate > 0 else 0.0
    width = 24
    filled = min(width, int(width * done / calc_total))
    bar = "#" * filled + "-" * (width - filled)
    last = ""
    if last_result:
        route = last_result.get("request_path") or request_path(str(last_result.get("request_uri") or ""))
        last = f" | 最近: {last_result.get('request_method')} {route} => {last_result.get('result')}/{last_result.get('category')}"
    emit(
        "[进度] "
        f"[{bar}] {done}/{display_total} {percent:5.1f}% "
        f"| PASS={result_counts.get('PASS', 0)} FAIL={result_counts.get('FAIL', 0)} ERROR={result_counts.get('ERROR', 0)} "
        f"| {rate:.2f}条/s | 已用 {format_duration(elapsed)} | 预计剩余 {format_duration(remaining)}"
        f"{last}",
        log_path,
        quiet_console,
    )


def build_url(base: str, request_uri: str) -> str:
    """用 base 替换 host，保留 request_uri 的 path/query。"""
    if request_uri.startswith("http://") or request_uri.startswith("https://"):
        parsed = urlsplit(request_uri)
        uri = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    else:
        uri = request_uri
    if not uri.startswith("/"):
        uri = "/" + uri
    return base.rstrip("/") + uri


def request_path(request_uri: str) -> str:
    if request_uri.startswith("http://") or request_uri.startswith("https://"):
        return urlsplit(request_uri).path or "/"
    return urlsplit(request_uri).path or request_uri.split("?", 1)[0] or "/"


def method_path(method: str, request_uri: str) -> str:
    return f"{method.upper()} {request_path(request_uri)}"


NORMAL_PLATFORM_ROUTES = {
    "/faces",
    "/face-detail",
    "/v2/faces",
    "/faces/pointer/list",
    "/faces/pointer/detail",
    "/faces/v3/tag-list",
    "/faces/v3/list",
    "/faces/v3/face-detail",
    "/faces/v3/face-download",
}

JIELI_PLATFORM_ROUTES = {
    "/faces/new/tag-face-list",
    "/faces/new/face-list",
    "/faces/new/face-detail",
    "/faces/new/face-download",
    "/faces/new/{id}",
}

SIFLI_PLATFORM_ROUTES = {
    "/faces/sifli",
    "/faces/sifli/tag-list",
    "/faces/sifli/face-detail",
    "/faces/sifli/face-download",
}


def normalize_platform_path(path: str) -> str:
    if re.fullmatch(r"/faces/new/\d+", path or ""):
        return "/faces/new/{id}"
    return path or "/"


def detect_platform_from_path(path: str) -> str:
    route = normalize_platform_path(path)
    if route in JIELI_PLATFORM_ROUTES or route.startswith("/faces/new/"):
        return "jieli"
    if route in SIFLI_PLATFORM_ROUTES or route.startswith("/faces/sifli"):
        return "sifli"
    if route in NORMAL_PLATFORM_ROUTES or route.startswith("/faces/v3/"):
        return "normal"
    return "unknown"


def route_template_from_uri(method: str, uri: str) -> Tuple[str, str, str]:
    path = normalize_platform_path(request_path(uri))
    qkeys = "&".join(sorted({k for k, _ in parse_qsl(urlsplit(uri).query, keep_blank_values=True)}))
    return path, qkeys, f"{method.upper()} {path}?{qkeys}"


def enrich_case_metadata(row: Dict[str, str]) -> Dict[str, str]:
    method = (row.get("request_method") or "GET").upper().strip() or "GET"
    uri = (row.get("request_uri") or "").strip()
    route, qkeys, template = route_template_from_uri(method, uri)
    row["request_method"] = method
    row["request_uri"] = uri
    row["route_pattern"] = row.get("route_pattern") or route
    row["qkeys"] = row.get("qkeys") or qkeys
    row["route_template"] = row.get("route_template") or template
    detected_platform = detect_platform_from_path(row["route_pattern"])
    current_platform = (row.get("platform") or "").strip()
    row["platform"] = current_platform if current_platform and current_platform != "unknown" else detected_platform
    row["case_group"] = row.get("case_group") or ""
    row["compare_profile"] = row.get("compare_profile") or ""
    return row


def parse_json_string(s: str, default: Any) -> Any:
    s = (s or "").strip()
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def load_cases(
    csv_path: Path,
    limit: int = 0,
    method_filter: str = "",
    route_regex: str = "",
    include_get_face_download: bool = False,
    priority_filter: str = "",
) -> List[Dict[str, str]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        raw_rows = [norm_key_map(r) for r in csv.DictReader(f)]

    out: List[Dict[str, str]] = []
    method_filter = method_filter.upper().strip()
    route_re = re.compile(route_regex) if route_regex else None
    priority_set = {x.strip().lower() for x in str(priority_filter or "").split(",") if x.strip()}

    for i, row in enumerate(raw_rows, start=1):
        if priority_set and str(row.get("case_priority") or "").strip().lower() not in priority_set:
            continue
        method = (row.get("request_method") or row.get("method") or "GET").upper().strip() or "GET"
        if method_filter and method != method_filter:
            continue
        uri = (row.get("request_uri") or row.get("uri") or row.get("path") or "").strip()
        if not uri:
            continue
        if route_re and not route_re.search(request_path(uri)):
            continue
        # 通用接口对比不默认跳过任何 GET 路由。include_get_face_download 保留为兼容旧命令行。
        row["case_no"] = str(i)
        row["request_method"] = method
        row["request_uri"] = uri
        row["case_priority"] = row.get("case_priority") or ""
        row.setdefault("cnt", "")
        enrich_case_metadata(row)
        out.append(row)
        if limit and len(out) >= limit:
            break
    return out


def with_query_params(request_uri: str, updates: Dict[str, str]) -> str:
    parsed = urlsplit(request_uri)
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    seen = set()
    new_items: List[Tuple[str, str]] = []
    for k, v in query_items:
        if k in updates:
            new_items.append((k, updates[k]))
            seen.add(k)
        else:
            new_items.append((k, v))
    for k, v in updates.items():
        if k not in seen:
            new_items.append((k, v))
    new_query = urlencode(new_items, doseq=True)
    if request_uri.startswith("http://") or request_uri.startswith("https://"):
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, parsed.fragment))
    return urlunsplit(("", "", parsed.path or request_path(request_uri), new_query, parsed.fragment))


def expand_pagination_boundary_cases(rows: List[Dict[str, str]], boundary_page: int, boundary_per_page: int) -> List[Dict[str, str]]:
    if boundary_page <= 0 or boundary_per_page <= 0:
        return rows

    out = list(rows)
    existing = {(str(r.get("request_method", "GET")).upper(), str(r.get("request_uri", ""))) for r in rows}
    for row in rows:
        method = str(row.get("request_method") or "GET").upper()
        if method != "GET":
            continue
        uri = str(row.get("request_uri") or "")
        parsed = urlsplit(uri)
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if "p" not in params or "per_page" not in params:
            continue
        boundary_uri = with_query_params(uri, {"p": str(boundary_page), "per_page": str(boundary_per_page)})
        key = (method, boundary_uri)
        if key in existing:
            continue
        new_row = dict(row)
        new_row["request_uri"] = boundary_uri
        new_row["cnt"] = "pagination_boundary"
        reason = str(row.get("select_reason") or "")
        suffix = f"pagination_boundary_p{boundary_page}_per{boundary_per_page}"
        new_row["select_reason"] = f"{reason}+{suffix}" if reason else suffix
        new_row["old_url"] = ""
        new_row["new_url"] = ""
        enrich_case_metadata(new_row)
        out.append(new_row)
        existing.add(key)
    return out


def dedupe_case_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for row in rows:
        method = str(row.get("request_method") or "GET").upper()
        uri = str(row.get("request_uri") or "")
        body = str(row.get("post_body_json") or "")
        key = (method, uri, body)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def load_post_body_cases(csv_path: Path, limit_per_route: int = 0) -> Dict[Tuple[str, str], List[BodyCase]]:
    """读取多组 POST body 样本。CSV 字段：request_method, request_path, body_type, body_json, headers_json。"""
    by_route: Dict[Tuple[str, str], List[BodyCase]] = defaultdict(list)
    if not csv_path.exists():
        return by_route

    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for idx, raw in enumerate(reader, start=1):
            row = norm_key_map(raw)
            method = (row.get("request_method") or "POST").upper().strip()
            path = (row.get("request_path") or row.get("path") or "").strip()
            if not path:
                continue
            body_type = (row.get("body_type") or "json").lower().strip()
            body = parse_json_string(row.get("body_json") or row.get("body") or "", {})
            headers = parse_json_string(row.get("headers_json") or row.get("headers") or "", {})
            if not isinstance(headers, dict):
                headers = {}
            if body_type == "json" and "Content-Type" not in headers and "content-type" not in {k.lower() for k in headers}:
                headers["Content-Type"] = "application/json"
            case = BodyCase(
                body_case_no=row.get("body_case_no") or str(idx),
                request_method=method,
                request_path=path,
                body_type=body_type,
                body=body,
                headers={str(k): str(v) for k, v in headers.items()},
                source_uri=row.get("source_uri", ""),
                source_cnt=row.get("source_cnt", ""),
                remark=row.get("remark", ""),
            )
            key = (method, path)
            if limit_per_route <= 0 or len(by_route[key]) < limit_per_route:
                by_route[key].append(case)
    return by_route


def load_static_post_cases(json_path: Path) -> Dict[str, BodyCase]:
    """读取静态 POST 配置，兼容旧版 post_cases.json。"""
    data = load_json(json_path)
    out: Dict[str, BodyCase] = {}
    for key, conf in data.items():
        if key.upper() in {"README", "EXAMPLES"} or not isinstance(conf, dict):
            continue
        parts = key.split(" ", 1)
        method = parts[0].upper() if parts else "POST"
        path = parts[1].strip() if len(parts) > 1 else key
        if "?" in path:
            path = path.split("?", 1)[0]
        body_type = str(conf.get("body_type", "json")).lower().strip()
        headers = conf.get("headers") if isinstance(conf.get("headers"), dict) else {}
        out[f"{method} {path}"] = BodyCase(
            body_case_no="static",
            request_method=method,
            request_path=path,
            body_type=body_type,
            body=conf.get("body") if conf.get("body") is not None else {},
            headers={str(k): str(v) for k, v in headers.items()},
            remark="from post_cases.json",
        )
    return out


def expand_rows_with_post_bodies(
    rows: List[Dict[str, str]],
    body_cases: Dict[Tuple[str, str], List[BodyCase]],
    static_cases: Dict[str, BodyCase],
    post_mode: str,
) -> Tuple[List[Tuple[Dict[str, str], Optional[BodyCase]]], List[Dict[str, Any]]]:
    """POST 路由展开为多组 body；GET 原样保留。"""
    expanded: List[Tuple[Dict[str, str], Optional[BodyCase]]] = []
    skipped: List[Dict[str, Any]] = []
    for row in rows:
        method = row.get("request_method", "GET").upper()
        uri = row.get("request_uri", "")
        path = request_path(uri)
        if method != "POST":
            expanded.append((row, None))
            continue

        if post_mode == "skip":
            skipped.append({**row, "result": "SKIP", "category": "SKIP_POST_BY_MODE"})
            continue

        inline_body = parse_json_string(row.get("post_body_json") or "", None)
        if inline_body is not None:
            headers = parse_json_string(row.get("post_headers_json") or "", {})
            if not isinstance(headers, dict):
                headers = {}
            headers.setdefault("Content-Type", "application/json")
            expanded.append((
                row,
                BodyCase(
                    row.get("body_case_no") or "inline",
                    method,
                    path,
                    "json",
                    inline_body,
                    {str(k): str(v) for k, v in headers.items()},
                    source_uri=row.get("id_source_uri", row.get("request_uri", "")),
                    remark=row.get("case_group", "inline_post_body"),
                ),
            ))
            continue

        samples = body_cases.get((method, path), [])
        if samples:
            for sample in samples:
                expanded.append((row, sample))
            continue

        static = static_cases.get(f"{method} {path}")
        if static:
            expanded.append((row, static))
            continue

        if post_mode == "empty":
            expanded.append((row, BodyCase("empty", method, path, "json", {}, {"Content-Type": "application/json"}, remark="empty body")))
        else:
            skipped.append({**row, "result": "SKIP", "category": "SKIP_POST_NO_BODY_CASE"})
    return expanded, skipped


def read_limited_response(resp: requests.Response, max_bytes: int) -> Tuple[bytes, bool]:
    chunks: List[bytes] = []
    total = 0
    truncated = False
    for chunk in resp.iter_content(chunk_size=65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            keep = len(chunk) - (total - max_bytes)
            if keep > 0:
                chunks.append(chunk[:keep])
            truncated = True
            break
        chunks.append(chunk)
    return b"".join(chunks), truncated


def throttle_request(min_interval_seconds: float) -> None:
    global _REQUEST_THROTTLE_NEXT_TS
    if min_interval_seconds <= 0:
        return
    with _REQUEST_THROTTLE_LOCK:
        now = time.monotonic()
        wait_seconds = _REQUEST_THROTTLE_NEXT_TS - now
        if wait_seconds > 0:
            time.sleep(wait_seconds)
            now = time.monotonic()
        _REQUEST_THROTTLE_NEXT_TS = now + min_interval_seconds


def body_kwargs(body_case: Optional[BodyCase], config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if body_case is None:
        return {}
    forced_type = str((config or {}).get("force_post_body_type") or "").strip().lower()
    body_type = forced_type or body_case.body_type
    kwargs: Dict[str, Any] = {"headers": dict(body_case.headers)}
    if forced_type:
        # 覆盖 Content-Type，便于快速验证新 Go 接口到底期望 JSON 还是表单。
        if forced_type == "json":
            kwargs["headers"]["Content-Type"] = "application/json"
        elif forced_type == "form":
            kwargs["headers"]["Content-Type"] = "application/x-www-form-urlencoded"
    if body_type == "json":
        kwargs["json"] = body_case.body
    elif body_type == "form":
        kwargs["data"] = body_case.body
    elif body_type == "raw":
        if isinstance(body_case.body, str):
            kwargs["data"] = body_case.body.encode("utf-8")
        else:
            kwargs["data"] = json.dumps(body_case.body, ensure_ascii=False).encode("utf-8")
    else:
        raise ValueError(f"不支持的 body_type: {body_type}")
    return kwargs


def fetch_once(
    method: str,
    url: str,
    base_headers: Dict[str, str],
    extra_kwargs: Dict[str, Any],
    timeout: float,
    max_response_bytes: int,
    allow_redirects: bool,
    request_delay: float,
) -> HttpResult:
    headers = dict(base_headers)
    extra_headers = extra_kwargs.pop("headers", None)
    if isinstance(extra_headers, dict):
        headers.update({str(k): str(v) for k, v in extra_headers.items()})

    throttle_request(request_delay)
    start = time.time()
    try:
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=allow_redirects,
            **extra_kwargs,
        )
        body, truncated = read_limited_response(resp, max_response_bytes)
        elapsed_ms = int((time.time() - start) * 1000)
        return HttpResult(True, resp.status_code, elapsed_ms, dict(resp.headers), body, truncated)
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return HttpResult(False, None, elapsed_ms, {}, b"", False, f"{type(e).__name__}: {e}")


def fetch_with_retries(
    method: str,
    url: str,
    base_headers: Dict[str, str],
    kwargs: Dict[str, Any],
    timeout: float,
    max_response_bytes: int,
    retries: int,
    retry_sleep: float,
    allow_redirects: bool,
    request_delay: float,
) -> HttpResult:
    last: Optional[HttpResult] = None
    for attempt in range(retries + 1):
        # 复制一层，避免 headers 被 pop 后影响另一个请求
        copied = dict(kwargs)
        last = fetch_once(method, url, base_headers, copied, timeout, max_response_bytes, allow_redirects, request_delay)
        if last.ok:
            return last
        if attempt < retries:
            time.sleep(retry_sleep)
    assert last is not None
    return last


def parse_json_safely(body: bytes) -> Tuple[bool, Any, str]:
    if not body:
        return False, None, "EMPTY_BODY"
    try:
        return True, json.loads(body.decode("utf-8")), ""
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def is_pagination_boundary_case(row: Dict[str, Any]) -> bool:
    return str(row.get("cnt") or "") == "pagination_boundary" or "pagination_boundary" in str(row.get("select_reason") or "")


def extract_faces_array(value: Any) -> Optional[List[Any]]:
    if not isinstance(value, dict):
        return None
    faces = value.get("faces")
    if isinstance(faces, list):
        return faces
    data = value.get("data")
    if isinstance(data, dict) and isinstance(data.get("faces"), list):
        return data.get("faces")
    return None


def is_empty_face_page(value: Any) -> bool:
    faces = extract_faces_array(value)
    return isinstance(faces, list) and len(faces) == 0


def is_empty_face_error(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    message = str(value.get("message") or value.get("msg") or "").strip().lower()
    try:
        code = int(value.get("code"))
    except Exception:
        return False
    return code == -4 and message in {"empty face", "empty faces"}


def pagination_empty_response_equivalent(row: Dict[str, Any], old_json: Any, new_json: Any, config: Dict[str, Any]) -> bool:
    if not config.get("treat_empty_face_error_as_equivalent", config.get("treat_pagination_empty_face_as_equivalent", True)):
        return False
    return (is_empty_face_page(old_json) and is_empty_face_error(new_json)) or (is_empty_face_error(old_json) and is_empty_face_page(new_json))


def query_params(uri: str) -> Dict[str, str]:
    return {str(k): str(v) for k, v in parse_qsl(urlsplit(uri).query, keep_blank_values=True)}


def build_uri(path: str, params: Dict[str, Any]) -> str:
    items = [(str(k), str(v)) for k, v in params.items() if v is not None and str(v) != ""]
    return f"{path}?{urlencode(items)}" if items else path


def id_value_for_body(value: str) -> Any:
    text = str(value)
    return int(text) if re.fullmatch(r"\d+", text) else text


def make_manual_case(
    method: str,
    uri: str,
    *,
    platform: str,
    case_group: str,
    select_reason: str,
    compare_profile: str = "strict",
    post_body: Optional[Dict[str, Any]] = None,
    id_source_uri: str = "",
) -> Dict[str, str]:
    row: Dict[str, str] = {
        "request_method": method.upper(),
        "request_uri": uri,
        "cnt": "manual_derived" if case_group.startswith("manual_derived") else "manual",
        "select_reason": select_reason,
        "compare_profile": compare_profile,
        "platform": platform,
        "case_group": case_group,
        "id_source_uri": id_source_uri,
        "old_url": "",
        "new_url": "",
    }
    if post_body is not None:
        row["post_body_json"] = json.dumps(post_body, ensure_ascii=False, separators=(",", ":"))
    enrich_case_metadata(row)
    return row


def is_manual_strict_seed(row: Dict[str, str]) -> bool:
    profile = str(row.get("compare_profile") or "").strip().lower()
    group = str(row.get("case_group") or "")
    cnt = str(row.get("cnt") or "")
    reason = str(row.get("select_reason") or "")
    return profile == "strict" and (
        cnt == "manual"
        or group.startswith("manual_seed")
        or reason.startswith("manual_user_report")
    )


def normal_static_cases(seed: Dict[str, str]) -> List[Dict[str, str]]:
    p = query_params(seed.get("request_uri", ""))
    base = {
        "tpls": p.get("tpls", "60"),
        "tested": p.get("tested", "yes"),
        "fv": p.get("fv", "MOY-LOJ3-2.0.2"),
        "per_page": p.get("per_page", "20"),
        "max_size": p.get("max_size", "54655652"),
        "ver": p.get("ver", "1.0"),
        "p": p.get("p", "1"),
    }
    lang = p.get("lang", "en")
    rows = [
        make_manual_case(
            "GET",
            build_uri("/faces/v3/tag-list", {**base, "lang": lang}),
            platform="normal",
            case_group="manual_derived_normal",
            select_reason="manual_derived_normal_tag_list",
        ),
        make_manual_case(
            "GET",
            build_uri("/faces/v3/list", {**base, "lang": lang, "tag_id": p.get("tag_id", "1")}),
            platform="normal",
            case_group="manual_derived_normal",
            select_reason="manual_derived_normal_v3_list_tag1",
        ),
    ]
    # 旧普通平台还有 /faces 和 /face-detail；/face-detail 需要 id，ID 派生阶段再补。
    if base.get("fv") and base.get("tpls"):
        rows.append(make_manual_case(
            "GET",
            build_uri("/faces", {"tpls": base["tpls"], "tested": base["tested"], "fv": base["fv"], "per_page": base["per_page"], "max_size": base["max_size"], "ver": base["ver"], "p": base["p"]}),
            platform="normal",
            case_group="manual_derived_normal",
            select_reason="manual_derived_normal_faces",
        ))
    return rows


def jieli_static_cases(seed: Dict[str, str]) -> List[Dict[str, str]]:
    p = query_params(seed.get("request_uri", ""))
    base = {
        "lang": p.get("lang", "zh-CN"),
        "tpls": p.get("tpls", "72"),
        "fv": p.get("fv", "MOY-VHI2-2.0.5"),
        "p": p.get("p", "1"),
        "per_page": p.get("per_page", "30"),
        "tested": p.get("tested", "yes"),
        "api_ver": p.get("api_ver", "53"),
        "max_size": p.get("max_size", "1048576"),
        "func_ver": p.get("func_ver", "3"),
    }
    return [
        make_manual_case(
            "GET",
            build_uri("/faces/new/face-list", {**base, "tag_id": p.get("tag_id", "1")}),
            platform="jieli",
            case_group="manual_derived_jieli",
            select_reason="manual_derived_jieli_face_list_tag1",
        )
    ]


def append_ids(value: Any, out: List[str]) -> None:
    if isinstance(value, dict):
        if "id" in value and isinstance(value.get("id"), (int, str)):
            text = str(value.get("id"))
            if text:
                out.append(text)
        for child in value.values():
            append_ids(child, out)
    elif isinstance(value, list):
        for child in value:
            append_ids(child, out)


def extract_ids(value: Any) -> List[str]:
    raw: List[str] = []
    append_ids(value, raw)
    out: List[str] = []
    seen = set()
    for item in raw:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def fetch_json_for_manual_ids(row: Dict[str, str], config: Dict[str, Any], args: argparse.Namespace) -> Tuple[List[str], List[str], str]:
    method = str(row.get("request_method") or "GET").upper()
    if method != "GET":
        return [], [], "只从 GET 列表接口提取 ID"
    headers = config.get("default_headers") if isinstance(config.get("default_headers"), dict) else {}
    timeout = float(args.timeout or config.get("timeout_seconds", 15))
    max_bytes = int(args.max_response_bytes or config.get("max_response_bytes", 2097152))
    allow_redirects = bool(args.allow_redirects or config.get("allow_redirects", False))
    request_delay = float(getattr(args, "request_delay", 0.0) or 0.0)
    old_res = fetch_with_retries(method, build_url(config["old_base"], row.get("request_uri", "")), headers, {}, timeout, max_bytes, args.retries, args.retry_sleep, allow_redirects, request_delay)
    new_res = fetch_with_retries(method, build_url(config["new_base"], row.get("request_uri", "")), headers, {}, timeout, max_bytes, args.retries, args.retry_sleep, allow_redirects, request_delay)
    if not old_res.ok or not new_res.ok:
        return [], [], f"提取 ID 请求失败: old={old_res.error}; new={new_res.error}"
    old_is_json, old_json, old_err = parse_json_safely(old_res.body_bytes)
    new_is_json, new_json, new_err = parse_json_safely(new_res.body_bytes)
    if not old_is_json or not new_is_json:
        return [], [], f"提取 ID 响应不是 JSON: old={old_err}; new={new_err}"
    return extract_ids(old_json), extract_ids(new_json), ""


def normal_id_cases(seed: Dict[str, str], ids: List[str], source_uri: str) -> List[Dict[str, str]]:
    p = query_params(seed.get("request_uri", ""))
    tpls = p.get("tpls", "60")
    fv = p.get("fv", "MOY-LOJ3-2.0.2")
    base_detail = {
        "fv": fv,
        "tpls": tpls,
        "tested": p.get("tested", "yes"),
        "max_size": p.get("max_size", "54655652"),
        "ver": p.get("ver", "1.0"),
        "lang": p.get("lang", "en"),
    }
    rows: List[Dict[str, str]] = []
    for face_id in ids:
        rows.extend([
            make_manual_case(
                "GET",
                build_uri("/faces/v3/face-detail", {**base_detail, "id": face_id}),
                platform="normal",
                case_group="manual_derived_normal_id",
                select_reason="manual_derived_normal_v3_face_detail_common_id",
                id_source_uri=source_uri,
            ),
            make_manual_case(
                "GET",
                build_uri("/face-detail", {"id": face_id, "fv": fv, "tpls": tpls, "tested": p.get("tested", "yes"), "lang": p.get("lang", "en")}),
                platform="normal",
                case_group="manual_derived_normal_id",
                select_reason="manual_derived_normal_face_detail_common_id",
                id_source_uri=source_uri,
            ),
            make_manual_case(
                "POST",
                "/faces/v3/face-download",
                platform="normal",
                case_group="manual_derived_normal_id",
                select_reason="manual_derived_normal_v3_face_download_common_id",
                post_body={"fv": fv, "id": id_value_for_body(face_id)},
                id_source_uri=source_uri,
            ),
        ])
    return rows


def jieli_id_cases(seed: Dict[str, str], ids: List[str], source_uri: str) -> List[Dict[str, str]]:
    p = query_params(seed.get("request_uri", ""))
    base = {
        "lang": p.get("lang", "zh-CN"),
        "tpls": p.get("tpls", "72"),
        "fv": p.get("fv", "MOY-VHI2-2.0.5"),
        "tested": p.get("tested", "yes"),
        "api_ver": p.get("api_ver", "53"),
        "max_size": p.get("max_size", "1048576"),
        "func_ver": p.get("func_ver", "3"),
    }
    rows: List[Dict[str, str]] = []
    for face_id in ids:
        rows.extend([
            make_manual_case(
                "GET",
                build_uri("/faces/new/face-detail", {**base, "id": face_id}),
                platform="jieli",
                case_group="manual_derived_jieli_id",
                select_reason="manual_derived_jieli_face_detail_common_id",
                id_source_uri=source_uri,
            ),
            make_manual_case(
                "GET",
                build_uri(f"/faces/new/{face_id}", base),
                platform="jieli",
                case_group="manual_derived_jieli_id",
                select_reason="manual_derived_jieli_path_id_common_id",
                id_source_uri=source_uri,
            ),
            make_manual_case(
                "POST",
                "/faces/new/face-download",
                platform="jieli",
                case_group="manual_derived_jieli_id",
                select_reason="manual_derived_jieli_face_download_common_id",
                post_body={"fv": base["fv"], "id": id_value_for_body(face_id)},
                id_source_uri=source_uri,
            ),
        ])
    return rows


def derive_manual_cases(rows: List[Dict[str, str]], config: Dict[str, Any], args: argparse.Namespace) -> List[Dict[str, str]]:
    mode = str(getattr(args, "manual_derived_mode", "off") or "off").lower()
    if mode == "off":
        return []

    seeds = [r for r in rows if is_manual_strict_seed(r)]
    derived: List[Dict[str, str]] = []
    id_source_rows: List[Tuple[Dict[str, str], Dict[str, str]]] = []

    for seed in seeds:
        platform = str(seed.get("platform") or detect_platform_from_path(request_path(seed.get("request_uri", ""))))
        path = request_path(seed.get("request_uri", ""))
        if platform == "normal":
            seed["platform"] = "normal"
            seed["case_group"] = seed.get("case_group") or "manual_seed_normal"
            static_rows = normal_static_cases(seed) if path in {"/v2/faces", "/faces", "/faces/v3/tag-list", "/faces/v3/list"} else []
            derived.extend(static_rows)
            if path in {"/v2/faces", "/faces/v3/list"}:
                id_source_rows.append((seed, seed))
            for candidate in static_rows:
                if request_path(candidate.get("request_uri", "")) == "/faces/v3/list":
                    id_source_rows.append((seed, candidate))
        elif platform == "jieli":
            seed["platform"] = "jieli"
            seed["case_group"] = seed.get("case_group") or "manual_seed_jieli"
            static_rows = jieli_static_cases(seed) if path in {"/faces/new/tag-face-list", "/faces/new/face-list"} else []
            derived.extend(static_rows)
            if path in {"/faces/new/tag-face-list", "/faces/new/face-list"}:
                id_source_rows.append((seed, seed))
            for candidate in static_rows:
                if request_path(candidate.get("request_uri", "")) == "/faces/new/face-list":
                    id_source_rows.append((seed, candidate))

    if args.dry_run or mode == "static":
        return dedupe_case_rows(derived)

    id_limit = max(0, int(getattr(args, "manual_derived_id_limit", 3) or 0))
    if id_limit <= 0:
        return dedupe_case_rows(derived)

    seen_sources = set()
    for seed, source in id_source_rows:
        source_key = (source.get("request_method", "GET"), source.get("request_uri", ""))
        if source_key in seen_sources:
            continue
        seen_sources.add(source_key)
        old_ids, new_ids, err = fetch_json_for_manual_ids(source, config, args)
        if err:
            emit(f"手工派生 ID 跳过: {source.get('request_uri')}；{err}", args.console_log, args.quiet_console)
            continue
        common = [x for x in old_ids if x in set(new_ids)][:id_limit]
        if not common:
            emit(
                f"手工派生 ID 跳过: {source.get('request_uri')}；旧 ID 数={len(old_ids)}，新 ID 数={len(new_ids)}，无共同 ID。列表差异会由 strict 用例记录。",
                args.console_log,
                args.quiet_console,
            )
            continue
        platform = str(seed.get("platform") or "")
        if platform == "normal":
            derived.extend(normal_id_cases(seed, common, source.get("request_uri", "")))
        elif platform == "jieli":
            derived.extend(jieli_id_cases(seed, common, source.get("request_uri", "")))
        emit(f"手工派生 ID: {platform} 从 {source.get('request_uri')} 取共同 ID {common}", args.console_log, args.quiet_console)

    return dedupe_case_rows(derived)


def should_ignore_path(path: str, key: Optional[str], config: Dict[str, Any]) -> bool:
    if config.get("ignore_response_id_values", True) and key == "id":
        if path == "$.id" or path == "$.data.id" or "[id=" in path:
            return True
    ignore_keys = set(config.get("ignore_keys_anywhere") or [])
    if key and key in ignore_keys:
        return True
    ignore_paths = [str(p) for p in (config.get("ignore_json_paths") or []) if str(p).strip()]
    if path in ignore_paths:
        return True
    if key and f"$..{key}" in ignore_paths:
        return True
    norm_path = normalize_diff_path_for_issue(path)
    for pattern in ignore_paths:
        if not pattern.startswith("$"):
            continue
        if pattern == path or normalize_diff_path_for_issue(pattern) == norm_path:
            return True
    return False


def normalize_value(value: Any, path: str, key: Optional[str], config: Dict[str, Any]) -> Any:
    if should_ignore_path(path, key, config):
        return "__IGNORED__"

    old_base = str(config.get("old_base", "")).rstrip("/")
    new_base = str(config.get("new_base", "")).rstrip("/")
    normalize_domain = bool(config.get("normalize_domains_in_string_values", True))

    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            child_path = f"{path}.{k}" if path else f"$.{k}"
            if should_ignore_path(child_path, str(k), config):
                continue
            out[k] = normalize_value(v, child_path, str(k), config)
        return out

    if isinstance(value, list):
        return [normalize_value(v, f"{path}[{i}]", None, config) for i, v in enumerate(value)]

    if isinstance(value, str) and normalize_domain:
        if old_base:
            value = value.replace(old_base, "__BASE__")
        if new_base:
            value = value.replace(new_base, "__BASE__")
    return value


def type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int) and not isinstance(v, bool):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def maybe_numeric_equal(a: Any, b: Any, config: Dict[str, Any]) -> bool:
    if not config.get("numeric_string_equal", False):
        return False
    try:
        return float(a) == float(b)
    except Exception:
        return False


def short_value(v: Any, max_len: int = 300) -> Any:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    else:
        s = str(v)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return v if isinstance(v, (int, float, bool)) or v is None else s



def path_key(path: str) -> str:
    """从 $.data[0].created_at 取出最后一个字段名 created_at。"""
    m = re.search(r"\.([A-Za-z0-9_]+)(?:\[[^\]]+\])?$", path)
    return m.group(1) if m else ""


def parse_datetime_like(value: str) -> Optional[str]:
    """把常见时间字符串规整到 YYYY-MM-DD HH:MM:SS，用于比较 PHP 与 Go 的时间格式差异。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    # 2024-12-17 14:11:56
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", text)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return None


def file_download_url_compatible(old: Any, new: Any) -> bool:
    """兼容旧项目中的 file URL 前缀差异规则；通用配置默认关闭。"""
    if not isinstance(old, str) or not isinstance(new, str):
        return False
    old_text = old.strip()
    new_text = new.strip()
    if not old_text or not new_text:
        return old_text == new_text
    if old_text == new_text:
        return True
    if new_text.startswith(old_text):
        return True
    old_base = old_text.rstrip("/") + "/"
    if new_text.startswith(old_base):
        return True
    try:
        old_parts = urlsplit(old_text)
        new_parts = urlsplit(new_text)
        if old_parts.scheme != new_parts.scheme or old_parts.netloc != new_parts.netloc:
            return False
        old_path = old_parts.path or "/"
        new_path = new_parts.path or "/"
        if old_path in {"/", ""}:
            return True
        return new_path.startswith(old_path.rstrip("/") + "/")
    except Exception:
        return False


def primitive_equivalent(old: Any, new: Any, path: str, config: Dict[str, Any]) -> bool:
    """宽松等价：只在配置允许时使用，避免把无业务影响的格式差异当成失败。"""
    key = path_key(path)

    if old == new:
        return True

    if config.get("file_url_prefix_compatible", True) and (key == "file" or path.endswith(".file")):
        if file_download_url_compatible(old, new):
            return True

    if config.get("numeric_string_equal", False):
        if isinstance(old, (str, int, float)) and isinstance(new, (str, int, float)):
            try:
                if str(old).strip() != "" and str(new).strip() != "" and float(old) == float(new):
                    return True
            except Exception:
                pass

    null_empty_keys = set(config.get("null_empty_string_equal_keys") or [])
    if key in null_empty_keys and {old, new} == {None, ""}:
        return True

    if config.get("datetime_string_equal", True):
        datetime_keys = set(config.get("datetime_keys") or [])
        if (key in datetime_keys) or key.endswith("_at") or key.endswith("Time") or key.endswith("time"):
            od = parse_datetime_like(old) if isinstance(old, str) else None
            nd = parse_datetime_like(new) if isinstance(new, str) else None
            if od and nd and od == nd:
                return True

    return False


def should_match_array_by_id(old: Any, new: Any, config: Dict[str, Any]) -> bool:
    if not config.get("match_arrays_by_id", False):
        return False
    if not isinstance(old, list) or not isinstance(new, list):
        return False
    if not old or not new:
        return False
    old_dicts = [x for x in old if isinstance(x, dict) and "id" in x]
    new_dicts = [x for x in new if isinstance(x, dict) and "id" in x]
    return len(old_dicts) >= max(1, len(old) // 2) and len(new_dicts) >= max(1, len(new) // 2)


def build_id_map(items: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict) and "id" in item:
            out[str(item.get("id"))] = item
    return out


def is_scalar_array(items: List[Any]) -> bool:
    return all(not isinstance(x, (dict, list)) for x in items)


def scalar_sort_key(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def schema_sample(values: List[Any]) -> Any:
    """Build a representative shape for schema-only array comparison."""
    values = [v for v in values if v is not None]
    if not values:
        return None

    dicts = [v for v in values if isinstance(v, dict)]
    if dicts:
        keys = sorted({k for item in dicts for k in item.keys()})
        return {k: schema_sample([item[k] for item in dicts if k in item]) for k in keys}

    lists = [v for v in values if isinstance(v, list)]
    if lists:
        child_values: List[Any] = []
        for item in lists:
            child_values.extend(item)
        return [schema_sample(child_values)] if child_values else []

    return values[0]


def compare_json(old: Any, new: Any, config: Dict[str, Any], path: str = "$", diffs: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    if diffs is None:
        diffs = []
    max_diffs = int(config.get("max_diffs_per_case", 200))
    if len(diffs) >= max_diffs:
        return diffs

    old = normalize_value(old, path, None, config)
    new = normalize_value(new, path, None, config)

    # 类型不同：允许通过配置把少量“格式差异”视为等价，例如 1 与 "1"、null 与 ""、时间格式。
    if type_name(old) != type_name(new):
        if primitive_equivalent(old, new, path, config):
            return diffs
        # int/float 之间视为同类数字
        if not (isinstance(old, (int, float)) and isinstance(new, (int, float))):
            diffs.append({"type": "TYPE_MISMATCH", "path": path, "old_type": type_name(old), "new_type": type_name(new), "old_value": short_value(old), "new_value": short_value(new)})
            return diffs

    if isinstance(old, dict) and isinstance(new, dict):
        old_keys = set(old.keys())
        new_keys = set(new.keys())
        for k in sorted(old_keys - new_keys):
            if len(diffs) >= max_diffs:
                return diffs
            child = f"{path}.{k}" if path != "$" else f"$.{k}"
            if should_ignore_path(child, k, config):
                continue
            diffs.append({"type": "MISSING_IN_NEW", "path": child, "old_value": short_value(old.get(k))})
        if config.get("compare_extra_fields_in_new", True):
            for k in sorted(new_keys - old_keys):
                if len(diffs) >= max_diffs:
                    return diffs
                child = f"{path}.{k}" if path != "$" else f"$.{k}"
                if should_ignore_path(child, k, config):
                    continue
                diffs.append({"type": "EXTRA_IN_NEW", "path": child, "new_value": short_value(new.get(k))})
        for k in sorted(old_keys & new_keys):
            if len(diffs) >= max_diffs:
                return diffs
            child = f"{path}.{k}" if path != "$" else f"$.{k}"
            if should_ignore_path(child, k, config):
                continue
            compare_json(old[k], new[k], config, child, diffs)
        return diffs

    if isinstance(old, list) and isinstance(new, list):
        if config.get("compare_array_items_by_schema", False):
            if old and new:
                compare_json(schema_sample(old), schema_sample(new), config, f"{path}[*]", diffs)
            return diffs
        # v4 关键修正：对象数组默认按 id 对齐。
        # 例如 faces 列表里同样是 id=10、id=4，只是返回顺序不同，不再按 faces[0] 误报 VALUE_MISMATCH。
        # 只有 id 集合不同，或同一个 id 下字段/类型/值不同，才记录差异。
        if should_match_array_by_id(old, new, config):
            old_map = build_id_map(old)
            new_map = build_id_map(new)
            old_ids = set(old_map.keys())
            new_ids = set(new_map.keys())
            missing = sorted(old_ids - new_ids, key=lambda x: (len(x), x))
            extra = sorted(new_ids - old_ids, key=lambda x: (len(x), x))
            if config.get("compare_array_id_sets", True) and (missing or extra):
                diffs.append({
                    "type": "ARRAY_ID_SET_MISMATCH",
                    "path": path,
                    "missing_ids_in_new": missing[:80],
                    "extra_ids_in_new": extra[:80],
                    "old_id_count": len(old_ids),
                    "new_id_count": len(new_ids),
                })
            for item_id in sorted(old_ids & new_ids, key=lambda x: (len(x), x)):
                if len(diffs) >= max_diffs:
                    return diffs
                compare_json(old_map[item_id], new_map[item_id], config, f"{path}[id={item_id}]", diffs)
            if config.get("compare_array_order", False):
                old_order = [str(x.get("id")) for x in old if isinstance(x, dict) and "id" in x]
                new_order = [str(x.get("id")) for x in new if isinstance(x, dict) and "id" in x]
                if old_order != new_order:
                    diffs.append({"type": "ARRAY_ORDER_MISMATCH", "path": path, "old_first_ids": old_order[:20], "new_first_ids": new_order[:20]})
            return diffs

        # 标量数组也按集合/多重集合比较，避免 [1,2] 和 [2,1] 这种顺序差异误报。
        if config.get("treat_scalar_arrays_as_unordered", True) and is_scalar_array(old) and is_scalar_array(new):
            old_sorted = sorted(old, key=scalar_sort_key)
            new_sorted = sorted(new, key=scalar_sort_key)
            if config.get("compare_array_lengths", True) and len(old_sorted) != len(new_sorted):
                diffs.append({"type": "ARRAY_LENGTH_MISMATCH", "path": path, "old_length": len(old_sorted), "new_length": len(new_sorted)})
            for i, (a, b) in enumerate(zip(old_sorted, new_sorted)):
                if len(diffs) >= max_diffs:
                    return diffs
                compare_json(a, b, config, f"{path}[unordered#{i}]", diffs)
            return diffs

        if config.get("compare_array_lengths", True) and len(old) != len(new):
            diffs.append({"type": "ARRAY_LENGTH_MISMATCH", "path": path, "old_length": len(old), "new_length": len(new)})
        for i, (a, b) in enumerate(zip(old, new)):
            if len(diffs) >= max_diffs:
                return diffs
            compare_json(a, b, config, f"{path}[{i}]", diffs)
        return diffs

    always_compare_paths = set(config.get("compare_value_paths_always") or [])
    should_compare_value = config.get("compare_values", True) or path in always_compare_paths
    if should_compare_value and not primitive_equivalent(old, new, path, config):
        diffs.append({"type": "VALUE_MISMATCH", "path": path, "old_value": short_value(old), "new_value": short_value(new)})
    return diffs


def diff_category(diffs: List[Dict[str, Any]]) -> str:
    if not diffs:
        return ""
    order = [
        "HTTP_STATUS_MISMATCH",
        "JSON_PARSE_MISMATCH",
        "MISSING_IN_NEW",
        "TYPE_MISMATCH",
        "ARRAY_ID_SET_MISMATCH",
        "ARRAY_LENGTH_MISMATCH",
        "VALUE_MISMATCH",
        "EXTRA_IN_NEW",
        "NON_JSON_BODY_MISMATCH",
    ]
    types = {str(d.get("type")) for d in diffs}
    for t in order:
        if t in types:
            return t
    return str(diffs[0].get("type", "JSON_DIFF"))




def classify_severity(result: Dict[str, Any], diffs: List[Dict[str, Any]]) -> Tuple[str, str]:
    """给失败结果增加优先级，方便先看真正影响客户端兼容的问题。"""
    if not diffs:
        return "", ""
    path = str(result.get("request_path", ""))
    method = str(result.get("request_method", ""))
    types = {str(d.get("type")) for d in diffs}

    if method == "POST" and path.endswith("/face-download"):
        # 旧 code=0 新 code=-1 这一类属于下载/更新业务失败，优先级最高。
        return "P0", "下载接口业务结果变化：重点检查 Go 是否正确解析 body，以及下载/更新逻辑、id/fv 查询条件是否和 PHP 一致"
    if "MISSING_IN_NEW" in types:
        return "P0", "旧接口存在字段，但新接口缺失；可能导致老客户端解析失败"
    if "HTTP_STATUS_MISMATCH" in types:
        return "P0", "HTTP 状态码变化；路由、请求方法或网关配置可能不兼容"
    if "TYPE_MISMATCH" in types:
        # 根结构变化如 data dict -> list 通常会直接导致客户端解析失败。
        root_shape = any(str(d.get("path")) in {"$", "$.data"} for d in diffs if d.get("type") == "TYPE_MISMATCH")
        if root_shape:
            return "P0", "响应根结构或 data 结构变化；老客户端大概率不兼容"
        return "P1", "字段类型变化；需要确认移动端模型是否能兼容"
    if "ARRAY_ID_SET_MISMATCH" in types:
        return "P1", "列表内容变化：已忽略排序，只比较 ID 集合；请检查 SQL 过滤条件或数据源"
    if "ARRAY_LENGTH_MISMATCH" in types:
        return "P1", "数组长度变化；可能是过滤条件、分页或数据源不一致"
    if types == {"EXTRA_IN_NEW"}:
        return "P3", "新接口仅多字段；通常不影响老客户端，但仍建议确认"
    if "VALUE_MISMATCH" in types:
        return "P1", "业务字段值变化；如果不是动态字段，需要后端核对逻辑"
    return "P2", "其他差异；建议结合示例请求进一步确认"

def make_result_base(row: Dict[str, str], body_case: Optional[BodyCase], config: Dict[str, Any]) -> Dict[str, Any]:
    method = row.get("request_method", "GET").upper()
    uri = row.get("request_uri", "")
    return {
        "case_no": row.get("case_no", ""),
        "body_case_no": body_case.body_case_no if body_case else "",
        "request_method": method,
        "request_uri": uri,
        "request_path": request_path(uri),
        "cnt": row.get("cnt", ""),
        "case_priority": row.get("case_priority", ""),
        "route_pattern": row.get("route_pattern", ""),
        "route_template": row.get("route_template", ""),
        "platform": row.get("platform", ""),
        "case_group": row.get("case_group", ""),
        "select_reason": row.get("select_reason", ""),
        "compare_profile": row.get("compare_profile", ""),
        "id_source_uri": row.get("id_source_uri", ""),
        "post_body": body_case.preview if body_case else "",
        "post_body_source_uri": body_case.source_uri if body_case else "",
        "old_url": build_url(config["old_base"], uri),
        "new_url": build_url(config["new_base"], uri),
        "result": "",
        "category": "",
        "diff_count": 0,
        "diff_preview": "",
        "old_http_status": "",
        "new_http_status": "",
        "old_elapsed_ms": "",
        "new_elapsed_ms": "",
        "old_content_type": "",
        "new_content_type": "",
        "old_body_sha256": "",
        "new_body_sha256": "",
        "old_truncated": False,
        "new_truncated": False,
        "severity": "",
        "likely_cause": "",
        "error": "",
    }


def case_compare_config(row: Dict[str, str], base_config: Dict[str, Any]) -> Dict[str, Any]:
    """Allow specific regression cases to tune comparison without overriding current compatibility policy."""
    profile = str(row.get("compare_profile") or "").strip().lower()
    if not profile:
        return base_config

    config = dict(base_config)
    if profile in {"strict", "full", "value", "values"}:
        config["compare_extra_fields_in_new"] = False
        config["match_arrays_by_id"] = False
        config["compare_array_id_sets"] = False
        config["compare_array_lengths"] = False
        config["compare_array_items_by_schema"] = True
        config["compare_values"] = False
        config["compare_value_paths_always"] = []
        config["compare_array_order"] = False
    elif profile in {"schema"}:
        config["compare_extra_fields_in_new"] = False
        config["match_arrays_by_id"] = False
        config["compare_array_id_sets"] = False
        config["compare_array_lengths"] = False
        config["compare_array_items_by_schema"] = True
        config["compare_values"] = False
        config["compare_value_paths_always"] = []
    return config


def compare_case(row: Dict[str, str], body_case: Optional[BodyCase], config: Dict[str, Any], args: argparse.Namespace) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    method = row.get("request_method", "GET").upper()
    result = make_result_base(row, body_case, config)

    if args.dry_run:
        result.update({"result": "DRY_RUN", "category": "DRY_RUN"})
        return result, None

    headers = config.get("default_headers") if isinstance(config.get("default_headers"), dict) else {}
    timeout = float(args.timeout or config.get("timeout_seconds", 15))
    max_bytes = int(args.max_response_bytes or config.get("max_response_bytes", 2097152))
    allow_redirects = bool(args.allow_redirects or config.get("allow_redirects", False))
    kwargs = body_kwargs(body_case, config)
    compare_config = case_compare_config(row, config)

    request_delay = float(getattr(args, "request_delay", 0.0) or 0.0)
    old_res = fetch_with_retries(method, result["old_url"], headers, kwargs, timeout, max_bytes, args.retries, args.retry_sleep, allow_redirects, request_delay)
    new_res = fetch_with_retries(method, result["new_url"], headers, kwargs, timeout, max_bytes, args.retries, args.retry_sleep, allow_redirects, request_delay)

    result.update({
        "old_http_status": old_res.status_code if old_res.status_code is not None else "",
        "new_http_status": new_res.status_code if new_res.status_code is not None else "",
        "old_elapsed_ms": old_res.elapsed_ms,
        "new_elapsed_ms": new_res.elapsed_ms,
        "old_content_type": old_res.content_type,
        "new_content_type": new_res.content_type,
        "old_body_sha256": old_res.body_sha256,
        "new_body_sha256": new_res.body_sha256,
        "old_truncated": old_res.truncated,
        "new_truncated": new_res.truncated,
    })

    if not old_res.ok or not new_res.ok:
        diffs = [{
            "type": "NETWORK_ERROR",
            "path": "$",
            "old_status": old_res.status_code if old_res.status_code is not None else "",
            "new_status": new_res.status_code if new_res.status_code is not None else "",
            "old_error": old_res.error,
            "new_error": new_res.error,
            "old_preview": old_res.text_preview,
            "new_preview": new_res.text_preview,
        }]
        result.update({
            "result": "ERROR",
            "category": "NETWORK_ERROR",
            "diff_count": 1,
            "diff_preview": json.dumps(diffs, ensure_ascii=False),
            "severity": "P2",
            "likely_cause": "网络请求失败或超时；这不是字段差异，建议单独复跑确认是否偶发",
            "error": f"old={old_res.error}; new={new_res.error}",
        })
        return result, {"case": result, "old_preview": old_res.text_preview, "new_preview": new_res.text_preview, "diffs": diffs}

    if old_res.status_code != new_res.status_code:
        diffs = [{"type": "HTTP_STATUS_MISMATCH", "old": old_res.status_code, "new": new_res.status_code}]
        severity, likely_cause = classify_severity(result, diffs)
        result.update({"result": "FAIL", "category": "HTTP_STATUS_MISMATCH", "diff_count": 1, "diff_preview": f"old={old_res.status_code}, new={new_res.status_code}", "severity": severity, "likely_cause": likely_cause})
        return result, {"case": result, "old_preview": old_res.text_preview, "new_preview": new_res.text_preview, "diffs": diffs}

    old_is_json, old_json, old_json_err = parse_json_safely(old_res.body_bytes)
    new_is_json, new_json, new_json_err = parse_json_safely(new_res.body_bytes)

    if old_is_json != new_is_json:
        diffs = [{"type": "JSON_PARSE_MISMATCH", "old_json_error": old_json_err, "new_json_error": new_json_err}]
        severity, likely_cause = classify_severity(result, diffs)
        result.update({"result": "FAIL", "category": "JSON_PARSE_MISMATCH", "diff_count": 1, "diff_preview": f"old_json={old_is_json}, new_json={new_is_json}", "severity": severity, "likely_cause": likely_cause})
        return result, {"case": result, "old_preview": old_res.text_preview, "new_preview": new_res.text_preview, "diffs": diffs}

    if old_is_json and new_is_json:
        if pagination_empty_response_equivalent(result, old_json, new_json, compare_config):
            result.update({"result": "PASS", "category": ""})
            return result, None
        diffs = compare_json(old_json, new_json, compare_config)
        if diffs:
            severity, likely_cause = classify_severity(result, diffs)
            result.update({"result": "FAIL", "category": diff_category(diffs), "diff_count": len(diffs), "diff_preview": json.dumps(diffs[:3], ensure_ascii=False), "severity": severity, "likely_cause": likely_cause})
            return result, {"case": result, "old_preview": old_res.text_preview, "new_preview": new_res.text_preview, "diffs": diffs[: int(config.get("max_diffs_per_case", 200))]}
        result.update({"result": "PASS", "category": ""})
        return result, None

    # 非 JSON：状态码一致后，比较 body 哈希。下载类接口如果返回二进制，这里能发现内容是否不同。
    if old_res.body_sha256 != new_res.body_sha256:
        diffs = [{"type": "NON_JSON_BODY_MISMATCH", "old_sha256": old_res.body_sha256, "new_sha256": new_res.body_sha256, "old_preview": old_res.text_preview, "new_preview": new_res.text_preview}]
        severity, likely_cause = classify_severity(result, diffs)
        result.update({"result": "FAIL", "category": "NON_JSON_BODY_MISMATCH", "diff_count": 1, "diff_preview": f"old_sha={old_res.body_sha256[:12]}, new_sha={new_res.body_sha256[:12]}", "severity": severity, "likely_cause": likely_cause})
        return result, {"case": result, "old_preview": old_res.text_preview, "new_preview": new_res.text_preview, "diffs": diffs}

    result.update({"result": "PASS", "category": ""})
    return result, None


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        return
    ensure_dir(path.parent)
    fields: List[str] = []
    for r in rows:
        for k in r.keys():
            if k not in fields:
                fields.append(k)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")



def make_diff_display_cn(diffs: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    """把差异 JSON 展示成中文说明，保留原始字段便于后端定位。"""
    out: List[Dict[str, Any]] = []
    for d in (diffs or [])[:limit]:
        item = dict(d)
        item["类型说明"] = zh_diff_type(d.get("type"))
        if "old_type" in item:
            item["旧类型说明"] = zh_type_name(item.get("old_type"))
        if "new_type" in item:
            item["新类型说明"] = zh_type_name(item.get("new_type"))
        out.append(item)
    return out


def summarize_diffs_cn(diffs: List[Dict[str, Any]], limit: int = 6) -> str:
    """生成适合聚合表展示的中文差异摘要。"""
    if not diffs:
        return ""
    parts: List[str] = []
    seen = set()
    for d in diffs:
        t = str(d.get("type", ""))
        path = normalize_diff_path_for_issue(str(d.get("path", "")))
        if t == "TYPE_MISMATCH":
            text = f"{path} 类型不一致：旧={zh_type_name(d.get('old_type'))}，新={zh_type_name(d.get('new_type'))}"
        elif t == "VALUE_MISMATCH":
            text = f"{path} 值不一致"
            if path in {"$.code", "$.msg", "$.message", "$.status", "$.cod"}:
                text += f"：旧={d.get('old_value')}，新={d.get('new_value')}"
        elif t == "ARRAY_ID_SET_MISMATCH":
            text = f"{path} 的 ID 集合不一致：旧 {d.get('old_id_count')} 个，新 {d.get('new_id_count')} 个"
            missing = d.get("missing_ids_in_new") or []
            extra = d.get("extra_ids_in_new") or []
            if missing:
                text += f"；新接口缺少 {missing[:8]}"
            if extra:
                text += f"；新接口多出 {extra[:8]}"
        elif t == "ARRAY_LENGTH_MISMATCH":
            text = f"{path} 数组长度不一致：旧={d.get('old_length')}，新={d.get('new_length')}"
        elif t == "MISSING_IN_NEW":
            text = f"{path} 新接口缺少字段"
        elif t == "EXTRA_IN_NEW":
            text = f"{path} 新接口多出字段"
        elif t == "HTTP_STATUS_MISMATCH":
            text = f"HTTP 状态码不一致：旧={d.get('old')}，新={d.get('new')}"
        elif t == "JSON_PARSE_MISMATCH":
            text = "JSON 解析结果不一致"
        elif t == "NON_JSON_BODY_MISMATCH":
            text = "非 JSON 响应内容哈希不一致"
        elif t == "NETWORK_ERROR":
            old_err = d.get("old_error") or "请求成功"
            new_err = d.get("new_error") or "请求成功"
            text = f"网络请求失败：旧接口={old_err}，新接口={new_err}"
        else:
            text = f"{zh_diff_type(t)}：{path}"
        if text not in seen:
            seen.add(text)
            parts.append(text)
        if len(parts) >= limit:
            break
    return "；".join(parts)


def normalize_diff_path_for_issue(path: str) -> str:
    """把路径里的具体数组下标 / id 归一，便于把同类问题合并。"""
    if not path:
        return ""
    path = re.sub(r"\[id=[^\]]+\]", "[id=*]", path)
    path = re.sub(r"\[unordered#\d+\]", "[*]", path)
    path = re.sub(r"\[\d+\]", "[*]", path)
    return path


def compact_diff_for_signature(d: Dict[str, Any]) -> str:
    """生成差异签名：同类问题应归到同一组；具体请求值放到 example_diff 里。"""
    t = str(d.get("type", ""))
    path = normalize_diff_path_for_issue(str(d.get("path", "")))
    if t == "TYPE_MISMATCH":
        return f"{t}:{path}:{d.get('old_type')}->{d.get('new_type')}"
    if t == "HTTP_STATUS_MISMATCH":
        return f"{t}:{d.get('old')}->{d.get('new')}"
    if t == "JSON_PARSE_MISMATCH":
        return t
    if t == "NETWORK_ERROR":
        return f"{t}:{path}"
    if t in {"MISSING_IN_NEW", "EXTRA_IN_NEW", "ARRAY_ID_SET_MISMATCH", "ARRAY_LENGTH_MISMATCH", "NON_JSON_BODY_MISMATCH"}:
        return f"{t}:{path}"
    if t == "VALUE_MISMATCH":
        # 业务 code/msg 差异要合并成一类；普通字段值差异只按路径归并，避免几百个不同值各成一组。
        old_v = d.get("old_value")
        new_v = d.get("new_value")
        if path in {"$.code", "$.msg", "$.message", "$.status", "$.cod"}:
            return f"{t}:{path}:{old_v}->{new_v}"
        return f"{t}:{path}"
    return f"{t}:{path}"


def issue_signature_from_diffs(diffs: List[Dict[str, Any]], limit: int = 8) -> str:
    parts = []
    seen = set()
    for d in diffs:
        sig = compact_diff_for_signature(d)
        if sig not in seen:
            seen.add(sig)
            parts.append(sig)
        if len(parts) >= limit:
            break
    return " | ".join(parts) if parts else "NO_DIFF"


def readable_issue_signature(signature: str, max_len: int = 500) -> str:
    if len(signature) <= max_len:
        return signature
    return signature[:max_len] + "..."


def describe_diff_path_cn(path: str) -> str:
    path = normalize_diff_path_for_issue(path)
    known = {
        "$.current_page": "分页当前页字段 current_page",
        "$.per_page": "分页每页数量字段 per_page",
        "$.page": "分页当前页字段 page",
        "$.data": "响应里的 data 字段",
        "$.total": "总数 total 字段",
        "$.code": "业务状态码 code",
        "$.msg": "业务消息 msg",
        "$.message": "业务消息 message",
    }
    if path in known:
        return known[path]
    if "[*]" in path:
        return "列表元素里的字段：" + path
    return "响应字段：" + path


def build_issue_groups(results: List[Dict[str, Any]], details: List[Dict[str, Any]], include_network_errors: bool = False) -> List[Dict[str, Any]]:
    """把原始失败用例聚合成问题清单：接口 + 优先级 + 分类 + 差异签名。"""
    detail_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for d in details:
        c = d.get("case", {})
        detail_by_key[(str(c.get("case_no")), str(c.get("body_case_no")))] = d

    groups: Dict[Tuple[str, str, str, str, str, str], Dict[str, Any]] = {}
    for r in results:
        if r.get("result") not in {"FAIL", "ERROR"}:
            continue
        if r.get("category") == "NETWORK_ERROR" and not include_network_errors:
            continue
        key2 = (str(r.get("case_no")), str(r.get("body_case_no")))
        d = detail_by_key.get(key2, {})
        diffs = d.get("diffs", []) or []
        signature = issue_signature_from_diffs(diffs)
        route = f"{r.get('request_method')} {r.get('request_path')}"
        platform = str(r.get("platform") or "").strip()
        if not platform or platform == "unknown":
            platform = detect_platform_from_path(str(r.get("request_path") or ""))
        gkey = (platform, str(r.get("severity")), route, str(r.get("category")), str(r.get("likely_cause")), signature)
        if gkey not in groups:
            groups[gkey] = {
                "severity": r.get("severity", ""),
                "route": route,
                "platform": platform,
                "case_priority": r.get("case_priority", ""),
                "case_group": r.get("case_group", ""),
                "category": r.get("category", ""),
                "category_cn": zh_category(r.get("category", "")),
                "likely_cause": r.get("likely_cause", ""),
                "issue_summary_cn": summarize_diffs_cn(diffs),
                "issue_signature": signature,
                "issue_signature_short": readable_issue_signature(signature),
                "affected_cases": 0,
                "example_method": r.get("request_method", ""),
                "example_uri": r.get("request_uri", ""),
                "example_post_body": r.get("post_body", ""),
                "example_compare_profile": r.get("compare_profile", ""),
                "example_id_source_uri": r.get("id_source_uri", ""),
                "example_status_old_new": f"{r.get('old_http_status')}/{r.get('new_http_status')}",
                "example_diff_count": r.get("diff_count", 0),
                "example_diffs": json.dumps(diffs[:5], ensure_ascii=False),
                "case_nos": [],
                "body_case_nos": [],
            }
        g = groups[gkey]
        g["affected_cases"] += 1
        if len(g["case_nos"]) < 30:
            g["case_nos"].append(str(r.get("case_no", "")))
            if r.get("body_case_no"):
                g["body_case_nos"].append(str(r.get("body_case_no", "")))

    severity_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "": 9}
    out = list(groups.values())
    out.sort(key=lambda g: (severity_rank.get(str(g.get("severity")), 8), str(g.get("route")), str(g.get("category")), -int(g.get("affected_cases") or 0)))
    for i, g in enumerate(out, start=1):
        g["issue_no"] = i
        g["case_nos"] = ",".join(g.get("case_nos") or [])
        g["body_case_nos"] = ",".join(g.get("body_case_nos") or [])
    return out


def build_summary(results: List[Dict[str, Any]], skipped: List[Dict[str, Any]], started_at: str, args: argparse.Namespace) -> Dict[str, Any]:
    total_executed = len(results)
    by_result = Counter(str(r.get("result", "")) for r in results)
    by_category = Counter(str(r.get("category", "")) for r in results if r.get("category"))
    by_case_priority = Counter(str(r.get("case_priority") or "unmarked") for r in results)
    fail_by_case_priority = Counter(
        str(r.get("case_priority") or "unmarked") for r in results if r.get("result") in {"FAIL", "ERROR"}
    )
    by_platform = Counter(str(r.get("platform") or "unknown") for r in results)
    fail_by_platform = Counter(str(r.get("platform") or "unknown") for r in results if r.get("result") in {"FAIL", "ERROR"})
    failed = [r for r in results if r.get("result") in {"FAIL", "ERROR"}]
    fail_route = Counter(f"{r.get('request_method')} {r.get('request_path')}" for r in failed)
    avg_latency_old = int(sum(int(r.get("old_elapsed_ms") or 0) for r in results) / total_executed) if total_executed else 0
    avg_latency_new = int(sum(int(r.get("new_elapsed_ms") or 0) for r in results) / total_executed) if total_executed else 0
    return {
        "started_at": started_at,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "input": args.input,
        "executed_cases": total_executed,
        "skipped_cases": len(skipped),
        "pass": by_result.get("PASS", 0),
        "fail": by_result.get("FAIL", 0),
        "error": by_result.get("ERROR", 0),
        "dry_run": by_result.get("DRY_RUN", 0),
        "by_result": dict(by_result),
        "by_category": dict(by_category),
        "by_case_priority": dict(by_case_priority),
        "fail_by_case_priority": dict(fail_by_case_priority),
        "by_platform": dict(by_platform),
        "fail_by_platform": dict(fail_by_platform),
        "top_failed_routes": fail_route.most_common(20),
        "avg_old_elapsed_ms": avg_latency_old,
        "avg_new_elapsed_ms": avg_latency_new,
    }


def write_html_report(path: Path, summary: Dict[str, Any], results: List[Dict[str, Any]], details: List[Dict[str, Any]], skipped: List[Dict[str, Any]], issue_groups: Optional[List[Dict[str, Any]]] = None) -> None:
    """生成更接近测试平台的单文件 HTML 报告。只改展示层，不改变 CSV/JSON 输出。"""
    issue_groups = issue_groups or []
    failed = [r for r in results if r.get("result") in {"FAIL", "ERROR"}]

    detail_by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for d in details:
        c = d.get("case", {})
        detail_by_key[(str(c.get("case_no")), str(c.get("body_case_no")))] = d

    dedup_fail_rows: Dict[Tuple[str, str, str, str, str], Dict[str, Any]] = {}
    for r in failed:
        d = detail_by_key.get((str(r.get("case_no")), str(r.get("body_case_no"))), {})
        diffs = d.get("diffs", []) or []
        signature = issue_signature_from_diffs(diffs) if diffs else str(r.get("error") or r.get("category") or r.get("result"))
        route = f"{r.get('request_method')} {r.get('request_path')}"
        key = (str(r.get("platform") or "unknown"), str(r.get("severity")), route, str(r.get("category")), signature)
        if key not in dedup_fail_rows:
            item = dict(r)
            item["_dedup_count"] = 0
            item["_dedup_signature"] = signature
            dedup_fail_rows[key] = item
        dedup_fail_rows[key]["_dedup_count"] = int(dedup_fail_rows[key].get("_dedup_count") or 0) + 1
    fail_rows = list(dedup_fail_rows.values())[:120]

    def esc(x: Any) -> str:
        return html.escape("" if x is None else str(x))

    def fmt_value(v: Any, max_len: int = 260) -> str:
        if v is None:
            text = "null"
        elif isinstance(v, str):
            text = v
        else:
            text = json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)
        return text[:max_len] + "..." if len(text) > max_len else text

    def pct(num: Any, den: Any) -> float:
        try:
            n = float(num or 0)
            d = float(den or 0)
            return round(n * 100.0 / d, 2) if d else 0.0
        except Exception:
            return 0.0

    def side_by_side_diffs(diffs: List[Dict[str, Any]], limit: int = 5) -> str:
        rows: List[str] = []
        for d in (diffs or [])[:limit]:
            t = str(d.get("type", ""))
            p = normalize_diff_path_for_issue(str(d.get("path", "")))
            if t == "MISSING_IN_NEW":
                old_text, new_text, note = "存在: " + fmt_value(d.get("old_value")), "缺失", "新接口少字段"
            elif t == "EXTRA_IN_NEW":
                old_text, new_text, note = "无", "新增: " + fmt_value(d.get("new_value")), "新接口多字段"
            elif t == "TYPE_MISMATCH":
                old_text = f"{zh_type_name(d.get('old_type'))}: {fmt_value(d.get('old_value'))}"
                new_text = f"{zh_type_name(d.get('new_type'))}: {fmt_value(d.get('new_value'))}"
                note = "字段类型不同"
            elif t == "VALUE_MISMATCH":
                old_text, new_text, note = fmt_value(d.get("old_value")), fmt_value(d.get("new_value")), "字段值不同"
            elif t == "ARRAY_LENGTH_MISMATCH":
                old_text, new_text, note = str(d.get("old_length")), str(d.get("new_length")), "数组长度不同"
            elif t == "ARRAY_ID_SET_MISMATCH":
                old_text = f"ID 数 {d.get('old_id_count')}；新接口缺少: {fmt_value(d.get('missing_ids_in_new'))}"
                new_text = f"ID 数 {d.get('new_id_count')}；新接口多出: {fmt_value(d.get('extra_ids_in_new'))}"
                note = "列表内容不同"
            elif t == "HTTP_STATUS_MISMATCH":
                old_text, new_text, note = fmt_value(d.get("old")), fmt_value(d.get("new")), "HTTP 状态码不同"
            elif t == "NETWORK_ERROR":
                old_status = d.get("old_status") if d.get("old_status") != "" else "无状态码"
                new_status = d.get("new_status") if d.get("new_status") != "" else "无状态码"
                old_error = d.get("old_error") or "请求成功"
                new_error = d.get("new_error") or "请求成功"
                old_text = f"状态码: {old_status}\n错误: {old_error}"
                new_text = f"状态码: {new_status}\n错误: {new_error}"
                note = "请求失败或超时"
            else:
                old_text = fmt_value(d.get("old_value", d.get("old", "")))
                new_text = fmt_value(d.get("new_value", d.get("new", "")))
                note = zh_diff_type(t)
            rows.append(f"""
            <tr>
              <td><code>{esc(p)}</code><span class="path-note">{esc(describe_diff_path_cn(p))}</span><span class="muted">{esc(zh_diff_type(t))}</span></td>
              <td class="old"><pre>{esc(old_text)}</pre></td>
              <td class="new"><pre>{esc(new_text)}</pre></td>
              <td><span class="judge">{esc(note)}</span></td>
            </tr>
            """)
        if not rows:
            return "<div class='empty'>无差异明细</div>"
        return f"""
        <table class="diff-table">
          <tr><th>字段路径</th><th>旧接口</th><th>新接口</th><th>判断</th></tr>
          {''.join(rows)}
        </table>
        """

    old_base = str(summary.get("old_base") or "")
    new_base = str(summary.get("new_base") or "")
    profile = str(summary.get("profile") or "")
    profile_note = str(summary.get("profile_note") or "")
    if not profile_note:
        profile_note = "结构/字段/类型校验；忽略普通值、列表 ID、列表长度和新接口多字段" if profile == "schema" else "兼容校验"
    tool_name = str(summary.get("tool_name") or "API Compare")
    report_title = str(summary.get("report_title") or "API 兼容性对比报告")

    executed = int(summary.get("executed_cases") or 0)
    passed = int(summary.get("pass") or 0)
    fail = int(summary.get("fail") or 0)
    error = int(summary.get("error") or 0)
    skipped_count = int(summary.get("skipped_cases") or 0)
    issue_count = int(summary.get("issue_group_count") or len(issue_groups))
    pass_rate = pct(passed, executed)
    old_ms = int(summary.get("avg_old_elapsed_ms") or 0)
    new_ms = int(summary.get("avg_new_elapsed_ms") or 0)
    latency_delta = old_ms - new_ms
    latency_text = f"快 {pct(latency_delta, old_ms)}%" if old_ms and latency_delta > 0 else (f"慢 {pct(abs(latency_delta), old_ms)}%" if old_ms and latency_delta < 0 else "持平")

    severity_counter = Counter(str(g.get("severity") or "未分级") for g in issue_groups)
    category_counter = Counter(str(r.get("category") or "") for r in failed if r.get("category"))
    platform_counter = Counter(str(r.get("platform") or "unknown") for r in results)
    platform_fail_counter = Counter(str(r.get("platform") or "unknown") for r in failed)
    severity_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "未分级": 8, "": 9}
    report_status = "有 P0 阻塞问题" if severity_counter.get("P0") else ("存在兼容差异" if fail or error else "全部通过")
    status_class = "danger" if severity_counter.get("P0") else ("warn" if fail or error else "ok")

    cards = "".join(f"""
      <div class="kpi {esc(cls)}">
        <div class="kpi-label">{esc(label)}</div>
        <div class="kpi-value">{esc(value)}</div>
        <div class="kpi-sub">{esc(sub)}</div>
      </div>
    """ for label, value, sub, cls in [
        ("通过率", f"{pass_rate}%", f"{passed}/{executed} 通过", "ok" if pass_rate == 100 else "warn"),
        ("聚合问题", issue_count, "去重后的真实问题数", "danger" if issue_count else "ok"),
        ("失败 / 错误", f"{fail} / {error}", "失败用例与请求异常", "danger" if fail or error else "ok"),
        ("平均耗时", f"旧 {old_ms}ms / 新 {new_ms}ms", latency_text, ""),
        ("跳过", skipped_count, "未执行请求", ""),
    ])

    severity_levels = [k for k in ["P0", "P1", "P2", "P3"] if severity_counter.get(k, 0) > 0]
    if not severity_levels and issue_groups:
        severity_levels = [k for k in ["P0", "P1", "P2", "P3"] if k in severity_counter]
    sev_cards = "".join(
        f"<div class='sev-card'><span class='sev {esc(k)}'>{esc(k)}</span><b>{esc(severity_counter.get(k, 0))}</b></div>"
        for k in severity_levels
    ) or "<div class='empty'>没有问题聚合项。</div>"
    sev_filter_buttons = "".join(
        f"<button class='btn' data-sev='{esc(k)}'>{esc(k)}</button>" for k in severity_levels
    )
    priority_order = ["p0", "p1", "p2", "manual", "customer", "unmarked"]
    by_priority = summary.get("by_case_priority") or {}
    fail_by_priority = summary.get("fail_by_case_priority") or {}
    priority_keys = [k for k in priority_order if k in by_priority] + sorted(
        k for k in by_priority.keys() if k not in priority_order
    )
    priority_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td>{esc(by_priority.get(k, 0))}</td>"
        f"<td class='ok'>{esc(int(by_priority.get(k, 0)) - int(fail_by_priority.get(k, 0)))}</td>"
        f"<td class='bad'>{esc(fail_by_priority.get(k, 0))}</td></tr>"
        for k in priority_keys
    ) or "<tr><td colspan='4' class='empty'>没有用例优先级统计。</td></tr>"
    category_rows = "".join(f"<tr><td>{esc(zh_category(k))}<br><code>{esc(k)}</code></td><td>{esc(v)}</td></tr>" for k, v in category_counter.most_common())
    route_rows = "".join(f"<tr><td><code>{esc(k)}</code></td><td>{esc(v)}</td></tr>" for k, v in summary.get("top_failed_routes", []))
    platform_rows = "".join(
        f"<tr><td><code>{esc(k)}</code></td><td>{esc(v)}</td><td>{esc(platform_fail_counter.get(k, 0))}</td></tr>"
        for k, v in platform_counter.most_common()
    )

    # 接口维度聚合
    route_issue_map: Dict[str, Dict[str, Any]] = {}
    for g in issue_groups:
        route = str(g.get("route") or "")
        if route not in route_issue_map:
            route_issue_map[route] = {"route": route, "severity": str(g.get("severity") or ""), "issue_count": 0, "affected_cases": 0, "summaries": []}
        item = route_issue_map[route]
        item["issue_count"] = int(item.get("issue_count") or 0) + 1
        item["affected_cases"] = int(item.get("affected_cases") or 0) + int(g.get("affected_cases") or 0)
        if severity_rank.get(str(g.get("severity") or ""), 9) < severity_rank.get(str(item.get("severity") or ""), 9):
            item["severity"] = str(g.get("severity") or "")
        summary_text = str(g.get("issue_summary_cn") or g.get("issue_signature_short") or "")
        if summary_text and len(item["summaries"]) < 3:
            item["summaries"].append(summary_text)

    route_issue_rows = "".join(f"""
      <tr class="filter-row" data-severity="{esc(item.get('severity'))}" data-search="{esc((item.get('route') or '') + ' ' + ' '.join(item.get('summaries') or []))}">
        <td><span class="sev {esc(item.get('severity'))}">{esc(item.get('severity'))}</span></td>
        <td><b>{esc(item.get('route'))}</b></td>
        <td>{esc(item.get('issue_count'))}</td>
        <td>{esc(item.get('affected_cases'))}</td>
        <td>{'<br>'.join(esc(x) for x in item.get('summaries') or [])}</td>
      </tr>
    """ for item in sorted(route_issue_map.values(), key=lambda x: (severity_rank.get(str(x.get("severity") or ""), 9), str(x.get("route") or ""))))

    issue_rows_parts: List[str] = []
    for g in issue_groups[:180]:
        diffs_raw = parse_json_string(str(g.get("example_diffs") or "[]"), [])
        uri = str(g.get("example_uri") or "")
        old_url = build_url(old_base, uri) if old_base and uri else ""
        new_url = build_url(new_base, uri) if new_base and uri else ""
        search_text = " ".join([str(g.get("case_priority") or ""), str(g.get("platform") or ""), str(g.get("case_group") or ""), str(g.get("route") or ""), str(g.get("issue_summary_cn") or ""), str(g.get("category_cn") or ""), uri, str(g.get("example_post_body") or "")])
        issue_rows_parts.append(f"""
        <section class="issue-card filter-row" data-severity="{esc(g.get('severity'))}" data-search="{esc(search_text)}">
          <div class="issue-head">
            <div><span class="issue-no">#{esc(g.get('issue_no'))}</span> <span class="sev {esc(g.get('severity'))}">{esc(g.get('severity'))}</span></div>
            <div class="issue-title"><b>{esc(g.get('route'))}</b><span>优先级 {esc(g.get('case_priority') or '未标注')} · 平台 {esc(g.get('platform') or 'unknown')} · 影响 {esc(g.get('affected_cases'))} 条样本 · {esc(g.get('category_cn') or zh_category(g.get('category')))}</span></div>
          </div>
          <div class="issue-summary">{esc(g.get('issue_summary_cn') or g.get('issue_signature_short'))}</div>
          <details class="issue-detail">
            <summary>展开示例请求与差异对照</summary>
            <div class="meta-grid">
              <div><b>请求 URI</b><code>{esc(uri)}</code></div>
              <div><b>POST body</b><code>{esc(g.get('example_post_body'))}</code></div>
              <div><b>优先级 / 平台 / 用例组</b><code>{esc(g.get('case_priority') or '未标注')} / {esc(g.get('platform') or 'unknown')} / {esc(g.get('case_group') or '')}</code></div>
              <div><b>单用例校验模式</b><code>{esc(g.get('example_compare_profile') or profile)}</code></div>
              <div><b>状态码</b><code>旧/新: {esc(g.get('example_status_old_new'))}</code></div>
              <div><b>ID 来源列表</b><code>{esc(g.get('example_id_source_uri') or '')}</code></div>
              <div><b>原始签名</b><code>{esc(g.get('issue_signature_short'))}</code></div>
            </div>
            <div class="urlpair">
              <div class="old"><b>旧接口完整 URL</b><code>{esc(old_url)}</code></div>
              <div class="new"><b>新接口完整 URL</b><code>{esc(new_url)}</code></div>
            </div>
            {side_by_side_diffs(diffs_raw, limit=6)}
          </details>
        </section>
        """)
    issue_rows = "".join(issue_rows_parts) or "<div class='empty'>没有聚合问题。</div>"

    fail_html_parts: List[str] = []
    for r in fail_rows:
        key = (str(r.get("case_no")), str(r.get("body_case_no")))
        d = detail_by_key.get(key, {})
        search_text = " ".join([str(r.get("case_priority") or ""), str(r.get("platform") or ""), str(r.get("case_group") or ""), str(r.get("request_uri") or ""), str(r.get("category_cn") or ""), str(r.get("severity") or "")])
        fail_html_parts.append(f"""
        <tr class="filter-row" data-severity="{esc(r.get('severity'))}" data-search="{esc(search_text)}">
          <td><span class="sev {esc(r.get('severity'))}">{esc(r.get('severity'))}</span></td>
          <td>{esc(r.get('result_cn') or zh_result(r.get('result')))}<br><span class="muted">{esc(r.get('category_cn') or zh_category(r.get('category')))}</span><br><span class="muted">优先级: {esc(r.get('case_priority') or '未标注')}</span><br><span class="muted">平台: {esc(r.get('platform') or 'unknown')}</span><br><span class="muted">模式: {esc(r.get('compare_profile') or profile)}</span></td>
          <td>{esc(r.get('_dedup_count', 1))}</td>
          <td class="fail-uri"><code>{esc(r.get('request_uri'))}</code></td>
          <td>{esc(r.get('old_http_status'))}/{esc(r.get('new_http_status'))}</td>
          <td>{side_by_side_diffs(d.get("diffs", []), limit=4)}</td>
        </tr>
        """)
    fail_table = "".join(fail_html_parts) or "<tr><td colspan='6' class='empty'>没有失败明细。</td></tr>"
    skipped_rows = "".join(f"<tr><td>{esc(r.get('request_method'))}</td><td><code>{esc(r.get('request_uri'))}</code></td><td>{esc(r.get('category'))}</td></tr>" for r in skipped[:100]) or "<tr><td colspan='3' class='empty'>没有跳过请求。</td></tr>"

    page = f"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(report_title)}</title>
<style>
:root {{ --bg:#f3f6fb; --panel:#ffffff; --panel2:#f8fafc; --text:#111827; --muted:#64748b; --line:#e2e8f0; --old:#fff7ed; --new:#eff6ff; --ok:#16a34a; --danger:#dc2626; --warn:#ea580c; --blue:#2563eb; --shadow:0 12px 30px rgba(15,23,42,.08); }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Microsoft YaHei,Arial,sans-serif; color:var(--text); background:var(--bg); }}
a {{ color:inherit; }}
.shell {{ display:grid; grid-template-columns:240px minmax(0,1fr); min-height:100vh; }}
.sidebar {{ position:sticky; top:0; height:100vh; padding:22px 16px; background:#0f172a; color:#dbeafe; }}
.brand {{ font-weight:800; font-size:18px; line-height:1.25; color:#fff; margin-bottom:18px; }}
.nav a {{ display:block; padding:10px 12px; margin:6px 0; border-radius:10px; color:#cbd5e1; text-decoration:none; }}
.nav a:hover {{ background:#1e293b; color:#fff; }}
.side-foot {{ position:absolute; bottom:18px; left:16px; right:16px; color:#94a3b8; font-size:12px; line-height:1.6; }}
.main {{ min-width:0; }}
.hero {{ padding:28px 32px 18px; background:linear-gradient(135deg,#0f172a,#1e3a8a); color:#fff; }}
.hero h1 {{ margin:0 0 8px; font-size:26px; }}
.hero-meta {{ color:#bfdbfe; font-size:13px; }}
.status {{ display:inline-flex; align-items:center; gap:8px; margin-top:14px; padding:8px 12px; border-radius:999px; font-weight:700; background:rgba(255,255,255,.12); }}
.status.ok {{ color:#bbf7d0; }} .status.warn {{ color:#fed7aa; }} .status.danger {{ color:#fecaca; }}
.content {{ padding:24px 32px 40px; }}
.section {{ margin-bottom:26px; }}
.section h2 {{ margin:0 0 12px; font-size:20px; }}
.kpis {{ display:grid; grid-template-columns:repeat(5,minmax(150px,1fr)); gap:14px; margin-top:-42px; margin-bottom:20px; }}
.kpi {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; padding:16px; box-shadow:var(--shadow); }}
.kpi-label {{ color:var(--muted); font-size:13px; }} .kpi-value {{ font-size:25px; font-weight:800; margin:8px 0 4px; }} .kpi-sub {{ color:var(--muted); font-size:12px; }}
.kpi.ok .kpi-value {{ color:var(--ok); }} .kpi.warn .kpi-value {{ color:var(--warn); }} .kpi.danger .kpi-value {{ color:var(--danger); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); overflow:hidden; }}
.panel-pad {{ padding:16px; }}
.endpoint-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
.endpoint {{ border:1px solid var(--line); border-radius:14px; padding:12px; }} .endpoint.old {{ background:var(--old); }} .endpoint.new {{ background:var(--new); }} .endpoint b {{ display:block; margin-bottom:6px; }}
.toolbar {{ position:sticky; top:0; z-index:5; display:flex; gap:10px; align-items:center; padding:12px; background:rgba(243,246,251,.92); backdrop-filter:blur(12px); border:1px solid var(--line); border-radius:16px; margin-bottom:16px; }}
.search {{ flex:1; border:1px solid var(--line); border-radius:12px; padding:11px 12px; font-size:14px; }}
.btn {{ border:1px solid var(--line); background:#fff; border-radius:12px; padding:10px 12px; cursor:pointer; }} .btn.active {{ background:#0f172a; color:#fff; border-color:#0f172a; }}
.sev-cards {{ display:flex; gap:10px; flex-wrap:wrap; }} .sev-card {{ display:flex; align-items:center; gap:8px; background:#fff; border:1px solid var(--line); border-radius:12px; padding:8px 10px; }} .sev-card b {{ font-size:18px; }}
.sev {{ display:inline-block; min-width:34px; text-align:center; padding:4px 7px; border-radius:999px; color:#fff; font-weight:800; font-size:12px; }}
.sev.P0 {{ background:#dc2626; }} .sev.P1 {{ background:#ea580c; }} .sev.P2 {{ background:#ca8a04; }} .sev.P3 {{ background:#2563eb; }} .sev.未分级, .sev.none {{ background:#64748b; }}
table {{ width:100%; border-collapse:collapse; background:#fff; }} th,td {{ border-bottom:1px solid var(--line); padding:10px; vertical-align:top; font-size:13px; }} th {{ text-align:left; background:#f8fafc; color:#334155; }}
code {{ word-break:break-all; white-space:pre-wrap; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }}
pre {{ margin:0; max-height:180px; overflow:auto; white-space:pre-wrap; word-break:break-word; font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:12px; }}
.muted {{ color:var(--muted); font-size:12px; }} .path-note {{ display:block; margin:4px 0; color:#334155; font-weight:700; }}
.issue-list {{ display:grid; gap:12px; }}
.issue-card {{ background:#fff; border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); padding:16px; }}
.issue-head {{ display:grid; grid-template-columns:110px 1fr; gap:12px; align-items:start; }} .issue-no {{ color:var(--muted); font-weight:700; margin-right:6px; }}
.issue-title b {{ display:block; font-size:16px; }} .issue-title span {{ color:var(--muted); font-size:12px; }}
.issue-summary {{ margin:12px 0; padding:12px; border-radius:12px; background:#f8fafc; line-height:1.6; font-weight:650; }}
.issue-detail summary {{ cursor:pointer; color:#1d4ed8; font-weight:700; margin:8px 0; }}
.meta-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin:10px 0; }} .meta-grid div {{ border:1px solid var(--line); border-radius:12px; padding:10px; background:#fff; }} .meta-grid b {{ display:block; margin-bottom:6px; }}
.urlpair {{ display:grid; grid-template-columns:1fr 1fr; gap:10px; margin:10px 0; }} .urlpair>div {{ border:1px solid var(--line); border-radius:12px; padding:10px; }} .urlpair b {{ display:block; margin-bottom:6px; }}
.old {{ background:var(--old); }} .new {{ background:var(--new); }} .judge {{ font-weight:700; }}
.diff-table {{ table-layout:fixed; margin-top:10px; }} .diff-table th:nth-child(1),.diff-table td:nth-child(1){{width:26%;}} .diff-table th:nth-child(2),.diff-table td:nth-child(2){{width:30%;}} .diff-table th:nth-child(3),.diff-table td:nth-child(3){{width:30%;}} .diff-table th:nth-child(4),.diff-table td:nth-child(4){{width:14%;}}
.table-scroll {{ width:100%; overflow:auto; }}
.fail-table {{ table-layout:fixed; min-width:1120px; }}
.fail-table th,.fail-table td {{ padding:10px 8px; }}
.fail-table th:nth-child(1),.fail-table td:nth-child(1){{width:70px;}}
.fail-table th:nth-child(2),.fail-table td:nth-child(2){{width:92px;}}
.fail-table th:nth-child(3),.fail-table td:nth-child(3){{width:72px; text-align:center;}}
.fail-table th:nth-child(4),.fail-table td:nth-child(4){{width:28%;}}
.fail-table th:nth-child(5),.fail-table td:nth-child(5){{width:88px; white-space:nowrap;}}
.fail-table th:nth-child(6),.fail-table td:nth-child(6){{width:auto;}}
.fail-table .fail-uri code {{ display:block; max-height:72px; overflow:auto; line-height:1.45; word-break:break-all; }}
.fail-table .diff-table {{ margin-top:0; }}
.fail-table .diff-table th,.fail-table .diff-table td {{ padding:8px; }}
.fail-table .diff-table th:nth-child(1),.fail-table .diff-table td:nth-child(1){{width:25%;}}
.fail-table .diff-table th:nth-child(2),.fail-table .diff-table td:nth-child(2){{width:30%;}}
.fail-table .diff-table th:nth-child(3),.fail-table .diff-table td:nth-child(3){{width:30%;}}
.fail-table .diff-table th:nth-child(4),.fail-table .diff-table td:nth-child(4){{width:15%;}}
.empty {{ padding:18px; text-align:center; color:var(--muted); }}
.hidden {{ display:none !important; }}
@media (max-width:1100px) {{ .shell{{grid-template-columns:1fr;}} .sidebar{{position:static;height:auto;}} .side-foot{{position:static;margin-top:12px;}} .kpis{{grid-template-columns:repeat(2,minmax(0,1fr)); margin-top:16px;}} .endpoint-grid,.meta-grid,.urlpair{{grid-template-columns:1fr;}} .content{{padding:16px;}} }}
</style>
</head>
<body>
<div class="shell">
  <aside class="sidebar">
    <div class="brand">{esc(tool_name)}<br>兼容性报告</div>
    <nav class="nav">
      <a href="#overview">概览</a>
      <a href="#issues">问题聚合</a>
      <a href="#routes">接口总览</a>
      <a href="#failed">失败明细</a>
      <a href="#skipped">跳过请求</a>
    </nav>
    <div class="side-foot">模式：{esc(profile)}<br>{esc(profile_note)}</div>
  </aside>
  <div class="main">
    <header class="hero" id="overview">
      <h1>{esc(report_title)}</h1>
      <div class="hero-meta">开始：{esc(summary.get('started_at'))}　结束：{esc(summary.get('finished_at'))}　输入：<code>{esc(summary.get('input'))}</code></div>
      <div class="status {esc(status_class)}">● {esc(report_status)}</div>
    </header>
    <main class="content">
      <div class="kpis">{cards}</div>
      <section class="section panel">
        <div class="panel-pad endpoint-grid">
          <div class="endpoint old"><b>旧接口基准</b><code>{esc(old_base)}</code></div>
          <div class="endpoint new"><b>新接口待测</b><code>{esc(new_base)}</code></div>
        </div>
      </section>

      <div class="toolbar">
        <input id="q" class="search" placeholder="搜索接口 / URI / 差异摘要 / POST body，例如 /api/user、$.data、P0">
        <button class="btn active" data-sev="">全部</button>
        {sev_filter_buttons}
      </div>

      <section class="section panel">
        <div class="panel-pad">
          <h2>用例套件执行统计</h2>
          <div class="muted">对应 CSV 字段 <code>case_priority</code>（p0/p1/p2/manual 等）。跑 <code>-Suite all</code> 时会在同一份报告里汇总各套件样本数。</div>
        </div>
        <table><tr><th>套件</th><th>执行数</th><th>通过</th><th>失败/错误</th></tr>{priority_rows}</table>
      </section>

      <section class="section panel">
        <div class="panel-pad">
          <h2>问题严重级别</h2>
          <div class="muted">P0/P1/P2/P3 表示<strong>问题严重程度</strong>，不是用例套件 p0/p1/p2；无 P3 级问题时下方不显示 P3。</div>
          <div class="sev-cards">{sev_cards}</div>
        </div>
      </section>

      <section class="section panel">
        <div class="panel-pad"><h2>接口分组汇总</h2><div class="muted">对应 CSV 字段 <code>platform</code>，可用于按业务线、服务或模块分组。</div></div>
        <table><tr><th>平台</th><th>执行数</th><th>失败/错误数</th></tr>{platform_rows or '<tr><td colspan="3" class="empty">没有平台数据。</td></tr>'}</table>
      </section>

      <section class="section" id="issues">
        <h2>问题聚合</h2>
        <div class="issue-list">{issue_rows}</div>
      </section>

      <section class="section panel" id="routes">
        <div class="panel-pad"><h2>接口问题总览</h2></div>
        <table><tr><th>最高优先级</th><th>接口</th><th>问题数</th><th>影响样本数</th><th>主要问题</th></tr>{route_issue_rows}</table>
      </section>

      <section class="section panel">
        <div class="panel-pad"><h2>失败分类</h2></div>
        <table><tr><th>分类</th><th>数量</th></tr>{category_rows or '<tr><td colspan="2" class="empty">没有失败分类。</td></tr>'}</table>
      </section>

      <section class="section panel">
        <div class="panel-pad"><h2>失败路由 Top 20</h2></div>
        <table><tr><th>路由</th><th>失败数</th></tr>{route_rows or '<tr><td colspan="2" class="empty">没有失败路由。</td></tr>'}</table>
      </section>

      <section class="section panel" id="failed">
        <div class="panel-pad"><h2>代表性失败明细</h2><div class="muted">已按接口和差异签名去重，完整明细看 failed_requests.csv。</div></div>
        <div class="table-scroll"><table class="fail-table"><tr><th>优先级</th><th>结果/分类</th><th>代表样本数</th><th>URI</th><th>状态码 旧/新</th><th>差异对照</th></tr>{fail_table}</table></div>
      </section>

      <section class="section panel" id="skipped">
        <div class="panel-pad"><h2>跳过请求</h2><div class="muted">最多展示 100 条。</div></div>
        <table><tr><th>方法</th><th>URI</th><th>原因</th></tr>{skipped_rows}</table>
      </section>
    </main>
  </div>
</div>
<script>
(function() {{
  const q = document.getElementById('q');
  const buttons = Array.from(document.querySelectorAll('[data-sev]'));
  let currentSev = '';
  function applyFilter() {{
    const keyword = (q.value || '').trim().toLowerCase();
    document.querySelectorAll('.filter-row').forEach(row => {{
      const text = (row.getAttribute('data-search') || row.innerText || '').toLowerCase();
      const sev = row.getAttribute('data-severity') || '';
      const okKeyword = !keyword || text.includes(keyword);
      const okSev = !currentSev || sev === currentSev;
      row.classList.toggle('hidden', !(okKeyword && okSev));
    }});
  }}
  q.addEventListener('input', applyFilter);
  buttons.forEach(btn => btn.addEventListener('click', () => {{
    buttons.forEach(x => x.classList.remove('active'));
    btn.classList.add('active');
    currentSev = btn.getAttribute('data-sev') || '';
    applyFilter();
  }}));
}})();
</script>
</body></html>
"""
    ensure_dir(path.parent)
    path.write_text(page, encoding="utf-8")

def main() -> int:
    parser = argparse.ArgumentParser(description="Compare old/new APIs with same path/query/body.")
    parser.add_argument("--input", required=True, help="请求清单 CSV，例如 data/new_api_compare_cases.csv")
    parser.add_argument("--extra-input", action="append", default=[], help="额外请求清单 CSV，可重复传入；用于手工回归用例")
    parser.add_argument("--pagination-boundary", action="store_true", help="为带 p/per_page 的 GET 请求额外生成分页越界用例")
    parser.add_argument("--boundary-page", type=int, default=5, help="分页越界用例使用的 p 值")
    parser.add_argument("--boundary-per-page", type=int, default=100, help="分页越界用例使用的 per_page 值")
    parser.add_argument("--out", required=True, help="报告输出目录")
    parser.add_argument("--console-log", default="", help="把控制台运行日志同时写入指定 UTF-8 文件")
    parser.add_argument("--quiet-console", action="store_true", help="只写 console-log，不直接打印到 stdout；由入口脚本负责实时转发日志")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置 JSON")
    parser.add_argument("--old-base", default="", help="覆盖 config.json 里的 old_base")
    parser.add_argument("--new-base", default="", help="覆盖 config.json 里的 new_base")
    parser.add_argument("--post-body-cases", default=str(DEFAULT_POST_BODY_CASES), help="POST body 样本 CSV")
    parser.add_argument("--post-cases-json", default=str(DEFAULT_POST_CASES_JSON), help="静态 POST body 配置 JSON")
    parser.add_argument("--post-body-limit-per-route", type=int, default=10, help="每个 POST 路由最多使用多少组 body 样本，0 表示不限制")
    parser.add_argument("--post-mode", choices=["config-only", "empty", "skip"], default="config-only", help="POST 无 body 样本时的处理方式")
    parser.add_argument("--workers", type=int, default=4, help="并发请求数")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条请求，调试用")
    parser.add_argument("--method", default="", help="只跑指定方法，如 GET/POST")
    parser.add_argument("--route-regex", default="", help="只跑匹配 path 的正则，例如 /api/user")
    parser.add_argument("--case-priority", default="", help="只跑指定 case_priority，多个用逗号分隔，例如 p0,p1")
    parser.add_argument("--include-get-face-download", action="store_true", help="兼容旧命令行参数；通用模式下 GET 路由不会被默认跳过")
    parser.add_argument("--dry-run", action="store_true", help="只生成 URL/body，不真正请求")
    parser.add_argument("--timeout", type=float, default=0, help="请求超时秒数，默认读取 config")
    parser.add_argument("--max-response-bytes", type=int, default=0, help="最大读取响应字节数，默认读取 config")
    parser.add_argument("--retries", type=int, default=0, help="网络错误重试次数，默认 0，避免掩盖问题")
    parser.add_argument("--retry-sleep", type=float, default=0.5, help="重试间隔秒")
    parser.add_argument("--request-delay", type=float, default=0.0, help="全局请求最小间隔秒数；用于降低服务端偶发断开连接")
    parser.add_argument("--progress-interval", type=float, default=5.0, help="进度刷新间隔秒，0 表示只在每 50 条和结束时输出")
    parser.add_argument("--include-network-errors-in-issues", action="store_true", help="把网络异常也放进问题聚合；默认只在错误明细保留")
    parser.add_argument("--allow-redirects", action="store_true", help="允许跟随 301/302；默认不跟随，便于发现差异")
    parser.add_argument("--profile", choices=["schema", "strict", "compat", "business"], default="strict", help="compare profile: schema checks structure/fields, strict checks exact compatibility")
    parser.add_argument("--force-post-body-type", choices=["", "json", "form", "raw"], default="", help="强制把 POST body 按 json/form/raw 发送，用于排查 Go 是否未正确解析 JSON")
    parser.add_argument("--manual-derived-mode", choices=["off", "static", "id"], default="off", help="手工 strict 用例派生模式：off 只跑原始客户/手工请求；static 派生同平台列表；id 再从列表提共同 ID 派生详情/下载")
    parser.add_argument("--manual-derived-id-limit", type=int, default=3, help="手工 strict 用例从列表接口提取共同 ID 后，每个平台最多派生多少个 ID 的 detail/download 用例")
    args = parser.parse_args()

    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out_dir = Path(args.out)
    ensure_dir(out_dir)

    config = load_json(Path(args.config))
    if args.old_base:
        config["old_base"] = args.old_base
    if args.new_base:
        config["new_base"] = args.new_base
    if not config.get("old_base") or not config.get("new_base"):
        print("config.json 必须包含 old_base 和 new_base", file=sys.stderr, flush=True)
        return 2

    # v4 默认所有模式都按 id 对齐对象数组；顺序不同不算失败。
    # 如果确实要检查顺序，可以在 config.json 里把 compare_array_order 改成 true。
    config.setdefault("match_arrays_by_id", True)
    config.setdefault("compare_array_order", False)
    config.setdefault("treat_scalar_arrays_as_unordered", True)
    config.setdefault("compare_array_id_sets", False)
    config.setdefault("compare_array_lengths", False)
    config.setdefault("compare_array_items_by_schema", False)
    config.setdefault("compare_values", False)
    config.setdefault("compare_value_paths_always", [])

    # profile 会覆盖少量配置；默认 strict 仍然按“返回必须一致”来跑，但不会把数组排序差异当失败。
    if args.profile == "schema":
        config["compare_extra_fields_in_new"] = False
        config["datetime_string_equal"] = True
        config["match_arrays_by_id"] = False
        config["compare_array_id_sets"] = False
        config["compare_array_lengths"] = False
        config["compare_array_items_by_schema"] = True
        config["compare_values"] = False
        config["compare_value_paths_always"] = []
    elif args.profile == "compat":
        config["compare_extra_fields_in_new"] = False
        config["datetime_string_equal"] = True
        config["match_arrays_by_id"] = True
    elif args.profile == "business":
        config["compare_extra_fields_in_new"] = False
        config["datetime_string_equal"] = True
        config["numeric_string_equal"] = True
        config["match_arrays_by_id"] = True
        config["null_empty_string_equal_keys"] = sorted(set(config.get("null_empty_string_equal_keys") or []) | {"uploader"})

    if args.force_post_body_type:
        config["force_post_body_type"] = args.force_post_body_type

    rows = load_cases(Path(args.input), args.limit, args.method, args.route_regex, args.include_get_face_download, args.case_priority)
    for extra_input in args.extra_input:
        extra_path = Path(extra_input)
        if extra_path.exists():
            rows.extend(load_cases(extra_path, 0, args.method, args.route_regex, args.include_get_face_download, args.case_priority))
    before_manual_derived = len(rows)
    rows.extend(derive_manual_cases(rows, config, args))
    rows = dedupe_case_rows(rows)
    manual_derived_count = len(rows) - before_manual_derived
    if manual_derived_count:
        emit(f"手工严格用例派生: 新增 {manual_derived_count} 条平台内用例", args.console_log, args.quiet_console)
    if args.pagination_boundary:
        before_boundary = len(rows)
        rows = expand_pagination_boundary_cases(rows, args.boundary_page, args.boundary_per_page)
        rows = dedupe_case_rows(rows)
        emit(f"分页边界用例: 新增 {len(rows) - before_boundary} 条，p={args.boundary_page}, per_page={args.boundary_per_page}", args.console_log, args.quiet_console)
    for idx, row in enumerate(rows, start=1):
        row["case_no"] = str(idx)
    post_body_cases = load_post_body_cases(Path(args.post_body_cases), args.post_body_limit_per_route)
    static_post_cases = load_static_post_cases(Path(args.post_cases_json))
    expanded, skipped = expand_rows_with_post_bodies(rows, post_body_cases, static_post_cases, args.post_mode)

    emit(f"读取请求行: {len(rows)}；展开后执行用例: {len(expanded)}；跳过: {len(skipped)}", args.console_log, args.quiet_console)
    if post_body_cases:
        route_counts = {f"{m} {p}": len(v) for (m, p), v in post_body_cases.items()}
        emit(f"POST body 样本: {route_counts}", args.console_log, args.quiet_console)

    results: List[Dict[str, Any]] = []
    details: List[Dict[str, Any]] = []
    result_counts: Counter = Counter()
    progress_start_ts = time.time()
    last_progress_ts = 0.0
    progress_interval = max(0.0, float(args.progress_interval))
    total_expanded = len(expanded)
    print_progress(0, total_expanded, result_counts, progress_start_ts, log_path=args.console_log, quiet_console=args.quiet_console)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        future_map = {pool.submit(compare_case, row, body_case, config, args): (row, body_case) for row, body_case in expanded}
        done = 0
        for fut in as_completed(future_map):
            res, detail = fut.result()
            results.append(res)
            result_counts[str(res.get("result") or "")] += 1
            if detail:
                details.append(detail)
            done += 1
            now = time.time()
            should_print = done == total_expanded or done % 50 == 0
            if progress_interval > 0:
                should_print = should_print or (now - last_progress_ts >= progress_interval)
            if should_print:
                print_progress(done, total_expanded, result_counts, progress_start_ts, res, args.console_log, args.quiet_console)
                last_progress_ts = now

    # 保持报告顺序稳定
    def sort_key(r: Dict[str, Any]) -> Tuple[int, int]:
        try:
            c = int(r.get("case_no") or 0)
        except Exception:
            c = 0
        try:
            b = int(str(r.get("body_case_no") or "0").split("-", 1)[0])
        except Exception:
            b = 0
        return c, b

    results.sort(key=sort_key)
    details.sort(key=lambda d: sort_key(d.get("case", {})))

    # 给 CSV/HTML 增加中文展示字段。原始英文分类保留，方便自动化过滤；中文字段方便人工阅读。
    for r in results:
        r["result_cn"] = zh_result(r.get("result", ""))
        r["category_cn"] = zh_category(r.get("category", ""))

    summary = build_summary(results, skipped, started, args)
    summary["old_base"] = config.get("old_base", "")
    summary["new_base"] = config.get("new_base", "")
    summary["profile"] = args.profile
    summary["tool_name"] = config.get("tool_name", "API Compare")
    summary["report_title"] = config.get("report_title", "API 兼容性对比报告")
    summary["profile_note"] = config.get("profile_note", "")
    issue_groups = build_issue_groups(results, details, args.include_network_errors_in_issues)
    summary["issue_group_count"] = len(issue_groups)
    # 长跑过程中报告目录可能被外部清理；落盘前再次确保目录存在。
    ensure_dir(out_dir)
    write_csv(out_dir / "results.csv", results)
    write_csv(out_dir / "failed_requests.csv", [r for r in results if r.get("result") in {"FAIL", "ERROR"}])
    write_csv(out_dir / "issue_summary.csv", issue_groups)
    write_csv(out_dir / "skipped_requests.csv", skipped)
    write_jsonl(out_dir / "diff_details.jsonl", details)
    for json_path, payload in (
        (out_dir / "issue_summary.json", issue_groups),
        (out_dir / "summary.json", summary),
    ):
        ensure_dir(json_path.parent)
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_html_report(out_dir / "report.html", summary, results, details, skipped, issue_groups)

    emit(json.dumps(summary, ensure_ascii=False, indent=2), args.console_log, args.quiet_console)
    emit(f"报告目录: {out_dir}", args.console_log, args.quiet_console)
    if summary.get("fail") or summary.get("error"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
