#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 CLB / 访问日志聚合文件生成 P0/P1/P2 请求清单，并自动抽取 POST body 样本。

输入文件至少包含：
- request_uri
- request_method
- cnt

支持 .csv 和 .csv.gz。

针对你的场景：
日志里 POST /faces/*/face-download 没有 body，但 GET /faces/*/face-download?id=xxx&fv=yyy
可以作为 body 样本来源，自动生成：
{"id": xxx, "fv": "yyy"}
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import parse_qsl, urlsplit


def open_text(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def norm_key_map(row: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in row.items():
        kk = str(k).lstrip("\ufeff").strip().strip('"').strip("'")
        out[kk] = "" if v is None else str(v)
    return out


def parse_int(v: Any) -> int:
    try:
        return int(float(str(v).strip()))
    except Exception:
        return 0


def maybe_int(v: str) -> Any:
    v = str(v)
    if v.isdigit():
        try:
            return int(v)
        except Exception:
            return v
    return v


def normalize_path(path: str) -> str:
    """把 /faces/new/123 这类动态数字路径归一成 /faces/new/{id}，避免 P0/P1 被 ID 膨胀。"""
    return re.sub(r"/\d+(?=/|$)", "/{id}", path)


def uri_parts(uri: str) -> Tuple[str, str, str, str]:
    p = urlsplit(uri)
    raw_path = p.path or uri.split("?", 1)[0] or "/"
    path = normalize_path(raw_path)
    qkeys = "&".join(sorted({k for k, _ in parse_qsl(p.query, keep_blank_values=True)}))
    return path, qkeys, p.query, raw_path


def route_template(method: str, path: str, qkeys: str) -> str:
    return f"{method.upper()} {path}?{qkeys}"


def is_get_face_download(row: Dict[str, str]) -> bool:
    return row.get("request_method", "").upper() == "GET" and urlsplit(row.get("request_uri", "")).path.endswith("/face-download")


def build_url(base: str, uri: str) -> str:
    if not uri.startswith("/"):
        uri = "/" + uri
    return base.rstrip("/") + uri


def read_rows(path: Path) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with open_text(path) as f:
        reader = csv.DictReader(f)
        for raw in reader:
            raw = norm_key_map(raw)
            uri = (raw.get("request_uri") or raw.get("uri") or raw.get("path") or "").strip()
            method = (raw.get("request_method") or raw.get("method") or "GET").strip().upper() or "GET"
            cnt = parse_int(raw.get("cnt") or raw.get("count") or raw.get("req_count") or "1")
            if not uri:
                continue
            path_part, qkeys, _, raw_path = uri_parts(uri)
            rows.append({
                "request_method": method,
                "request_uri": uri,
                "cnt": str(cnt),
                "route_pattern": path_part,
                "raw_path": raw_path,
                "qkeys": qkeys,
                "route_template": route_template(method, path_part, qkeys),
            })
    return rows


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def write_request_csv(path: Path, rows: List[Dict[str, str]], old_base: str, new_base: str) -> None:
    fields = ["request_method", "request_uri", "cnt", "route_pattern", "qkeys", "route_template", "old_url", "new_url", "select_reason"]
    out_rows: List[Dict[str, str]] = []
    for r in rows:
        out = dict(r)
        out["old_url"] = build_url(old_base, out["request_uri"])
        out["new_url"] = build_url(new_base, out["request_uri"])
        out_rows.append(out)
    write_csv(path, out_rows, fields)


def write_summary_csv(path: Path, rows: List[Dict[str, str]], group_keys: List[str]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[str, ...], Dict[str, Any]] = {}
    for r in rows:
        key = tuple(r[k] for k in group_keys)
        cnt = int(r["cnt"])
        if key not in grouped:
            grouped[key] = {
                **{k: r[k] for k in group_keys},
                "total_requests": 0,
                "unique_exact_uri": 0,
                "sample_uri": r["request_uri"],
                "max_single_uri_cnt": 0,
            }
        g = grouped[key]
        g["total_requests"] += cnt
        g["unique_exact_uri"] += 1
        if cnt >= g["max_single_uri_cnt"]:
            g["max_single_uri_cnt"] = cnt
            g["sample_uri"] = r["request_uri"]
    total = sum(int(r["cnt"]) for r in rows) or 1
    out = list(grouped.values())
    out.sort(key=lambda x: int(x["total_requests"]), reverse=True)
    for r in out:
        r["traffic_pct"] = round(int(r["total_requests"]) / total, 8)
    fields = group_keys + ["total_requests", "unique_exact_uri", "sample_uri", "max_single_uri_cnt", "traffic_pct"]
    write_csv(path, out, fields)
    return out


def dedupe(rows: Iterable[Dict[str, str]]) -> List[Dict[str, str]]:
    best: Dict[Tuple[str, str], Dict[str, str]] = {}
    for r in rows:
        key = (r["request_method"], r["request_uri"])
        if key not in best:
            best[key] = dict(r)
            continue
        old = best[key]
        if int(r["cnt"]) > int(old["cnt"]):
            reason = old.get("select_reason", "")
            new = dict(r)
            add = new.get("select_reason", "")
            if reason and reason not in add:
                new["select_reason"] = add + "+" + reason if add else reason
            best[key] = new
        else:
            reason = old.get("select_reason", "")
            add = r.get("select_reason", "")
            if add and add not in reason:
                old["select_reason"] = reason + "+" + add if reason else add
    out = list(best.values())
    out.sort(key=lambda r: int(r["cnt"]), reverse=True)
    return out


def top_n_by_group(rows: List[Dict[str, str]], keys: List[str], n: int, reason: str) -> List[Dict[str, str]]:
    grouped: Dict[Tuple[str, ...], List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        grouped[tuple(r[k] for k in keys)].append(r)
    selected: List[Dict[str, str]] = []
    for items in grouped.values():
        for r in sorted(items, key=lambda x: int(x["cnt"]), reverse=True)[:n]:
            x = dict(r)
            x["select_reason"] = reason
            selected.append(x)
    return selected


def generate_post_body_cases(rows: List[Dict[str, str]], samples_per_route: int) -> List[Dict[str, str]]:
    """从 GET /faces/*/face-download?id=&fv= 自动生成 POST body 样本。"""
    # 先找日志中实际存在的 POST 下载路由，避免生成无用路径。
    post_download_paths = {
        urlsplit(r["request_uri"]).path
        for r in rows
        if r["request_method"] == "POST" and "face-download" in urlsplit(r["request_uri"]).path
    }
    candidates: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    seen: set[Tuple[str, str, str]] = set()

    for r in sorted(rows, key=lambda x: int(x["cnt"]), reverse=True):
        if r["request_method"] != "GET":
            continue
        p = urlsplit(r["request_uri"])
        path = p.path
        if path not in post_download_paths or "face-download" not in path:
            continue
        q = dict(parse_qsl(p.query, keep_blank_values=True))
        id_v = q.get("id", "").strip()
        fv = q.get("fv", "").strip()
        if not id_v or not fv:
            continue
        key = (path, id_v, fv)
        if key in seen:
            continue
        seen.add(key)
        body = {"id": maybe_int(id_v), "fv": fv}
        candidates[path].append({
            "request_method": "POST",
            "request_path": path,
            "body_case_no": str(len(candidates[path]) + 1),
            "body_type": "json",
            "body_json": json.dumps(body, ensure_ascii=False, separators=(",", ":")),
            "headers_json": json.dumps({"Content-Type": "application/json"}, ensure_ascii=False),
            "source_uri": r["request_uri"],
            "source_cnt": r["cnt"],
            "remark": "auto_from_get_face_download_query_id_fv",
        })
        if samples_per_route > 0 and len(candidates[path]) >= samples_per_route:
            continue

    out: List[Dict[str, str]] = []
    for path, items in sorted(candidates.items()):
        out.extend(items[:samples_per_route] if samples_per_route > 0 else items)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Reduce log CSV to P0/P1/P2 replay lists and POST body cases.")
    parser.add_argument("--input", required=True, help="原始或聚合日志 CSV / CSV.GZ")
    parser.add_argument("--out-dir", default="data", help="输出目录")
    parser.add_argument("--old-base", default="https://api.moyoung.com")
    parser.add_argument("--new-base", default="https://watchface-api-sz.moyoung.com")
    parser.add_argument("--p2-min-cnt", type=int, default=20)
    parser.add_argument("--post-body-samples-per-route", type=int, default=20, help="每个 POST 下载路由抽取多少组 {id,fv} body 样本")
    parser.add_argument("--include-get-face-download", action="store_true", help="默认不把 GET /face-download 放入 P0/P1/P2；加此参数才保留，用于检查错误方法兼容性")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    ensure_dir(out_dir)
    rows = read_rows(Path(args.input))
    rows.sort(key=lambda r: int(r["cnt"]), reverse=True)
    # 注意：GET /faces/*/face-download?id=&fv= 只用于抽取 POST body 样本，
    # 不进入主对比清单，避免把下载接口主流程误测成 GET。
    main_rows = rows if args.include_get_face_download else [r for r in rows if not is_get_face_download(r)]

    total_req = sum(int(r["cnt"]) for r in rows)
    get_cnt = sum(int(r["cnt"]) for r in rows if r["request_method"] == "GET")
    post_cnt = sum(int(r["cnt"]) for r in rows if r["request_method"] == "POST")
    print(f"读取 URI 行数: {len(rows)}；总请求次数: {total_req}；GET={get_cnt}；POST={post_cnt}")

    route_pattern_summary = write_summary_csv(out_dir / "01_route_pattern_summary.csv", main_rows, ["request_method", "route_pattern"])
    route_template_summary = write_summary_csv(out_dir / "02_route_template_summary.csv", main_rows, ["request_method", "route_pattern", "qkeys", "route_template"])

    p0: List[Dict[str, str]] = []
    p0.extend(top_n_by_group(main_rows, ["request_method", "route_pattern", "qkeys"], 1, "P0_template_top1"))
    for r in main_rows:
        if r["request_method"] == "POST":
            x = dict(r); x["select_reason"] = "P0_all_post"; p0.append(x)
    p0 = dedupe(p0)

    p1: List[Dict[str, str]] = []
    for r in main_rows[:200]:
        x = dict(r); x["select_reason"] = "exact_top200"; p1.append(x)
    p1.extend(top_n_by_group(main_rows, ["request_method", "route_pattern"], 10, "route_top10"))
    p1.extend(top_n_by_group(main_rows, ["request_method", "route_pattern", "qkeys"], 3, "template_top3"))
    for r in main_rows:
        if r["request_method"] == "POST":
            x = dict(r); x["select_reason"] = "all_post"; p1.append(x)
    p1 = dedupe(p1)

    p2: List[Dict[str, str]] = []
    for r in main_rows:
        if int(r["cnt"]) >= args.p2_min_cnt:
            x = dict(r); x["select_reason"] = f"cnt>={args.p2_min_cnt}"; p2.append(x)
    p2.extend(p0)
    p2 = dedupe(p2)

    write_request_csv(out_dir / "03_p0_smoke_59_templates.csv", p0, args.old_base, args.new_base)
    write_request_csv(out_dir / "04_p1_compat_selected.csv", p1, args.old_base, args.new_base)
    write_request_csv(out_dir / "05_p2_high_frequency_cnt_ge_20_plus_p0.csv", p2, args.old_base, args.new_base)

    post_body_cases = generate_post_body_cases(rows, args.post_body_samples_per_route)
    post_fields = ["request_method", "request_path", "body_case_no", "body_type", "body_json", "headers_json", "source_uri", "source_cnt", "remark"]
    write_csv(out_dir / "post_body_cases.csv", post_body_cases, post_fields)

    readme = f"""# 精简结果说明

原始 URI 行数：{len(rows)}
总请求次数：{total_req}
GET 请求次数：{get_cnt}
POST 请求次数：{post_cnt}
GET /face-download 行数：{sum(1 for r in rows if is_get_face_download(r))}（默认仅用于抽取 POST body，不进入主对比清单）

输出文件：

- 01_route_pattern_summary.csv：按 method + path 汇总
- 02_route_template_summary.csv：按 method + path + 参数 key 集合汇总
- 03_p0_smoke_59_templates.csv：每个接口形态取 top1，并保留所有 POST
- 04_p1_compat_selected.csv：top200 URI + 每路由 top10 + 每模板 top3 + 所有 POST
- 05_p2_high_frequency_cnt_ge_20_plus_p0.csv：cnt >= {args.p2_min_cnt} 的高频 URI + P0
- post_body_cases.csv：从 GET 下载接口自动抽取的 POST body 样本，格式为 {{"id": xxx, "fv": "xxx"}}；下载接口主流程仍按 POST 测试

建议执行顺序：先 P0，再 P1，最后 P2。
"""
    (out_dir / "00_readme_summary.md").write_text(readme, encoding="utf-8")

    print(f"route_pattern_summary: {len(route_pattern_summary)} 类")
    print(f"route_template_summary: {len(route_template_summary)} 类")
    print(f"P0: {len(p0)} 条")
    print(f"P1: {len(p1)} 条")
    print(f"P2: {len(p2)} 条")
    print(f"POST body 样本: {len(post_body_cases)} 条")
    print(f"输出目录: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
