"""Compare V4 only against real user-provided URLs extracted from the workbook."""
from __future__ import annotations

import html
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

INPUT = Path("outputs/user_supplied_test_data_20260814.json")
OUT = Path("reports/v4_user_supplied_links_comparison_20260814.html")
BASE = "https://api.moyoung.com"


# The published routing rule has one important exception: 杰理 nandflash still
# uses the New data source.  Keep all rules explicit; unknown platforms follow
# the documented fallback (V3), rather than silently being assumed to be New.
NEW_PLATFORMS = {"杰理", "杰理707", "杰理nandflash", "RTL8773", "中科蓝汛", "中科蓝讯", "海思"}
SIFLI_PLATFORMS = {"思澈"}

def expected_source(platform: str) -> str:
    if platform in NEW_PLATFORMS: return "new"
    if platform in SIFLI_PLATFORMS: return "sifli"
    return "v3"


def mapped_v4_path(old_path: str) -> str | None:
    mapping = {
        "/faces/v3/tag-list": "/faces/v4/tag-list", "/faces/v3/list": "/faces/v4/list",
        "/faces/v3/face-detail": "/faces/v4/face-detail",
        "/faces/new/tag-face-list": "/faces/v4/tag-list", "/faces/new/face-list": "/faces/v4/list",
        "/faces/new/face-detail": "/faces/v4/face-detail", "/faces/sifli/tag-list": "/faces/v4/tag-list",
        "/faces/sifli": "/faces/v4/list",
    }
    if old_path in mapping: return mapping[old_path]
    if re.fullmatch(r"/faces/sifli/\d+", old_path): return "/faces/v4/face-detail"
    if old_path in {"/faces/v3/face-download", "/faces/new/face-download", "/faces/sifli/face-download"}: return "/faces/v4/face-download"
    return None


def prepared_get_rows():
    data = json.loads(INPUT.read_text(encoding="utf-8"))
    inherited_fv: dict[str, str] = {}
    rows, skipped = [], []
    for source in data["rows"]:
        parsed = urlsplit(source["url"])
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if params.get("fv"):
            inherited_fv[source["platform"]] = params["fv"].strip()
        v4_path = mapped_v4_path(parsed.path)
        if not v4_path:
            if parsed.path == "/v2/faces":
                reason = "V4 没有对应接口；不能与 /faces/v4/list 比较"
            elif re.fullmatch(r"/faces/new/\d+", parsed.path):
                reason = "旧接口仅获取文件和预览，V4 face-detail 是完整详情；业务不同，不能比较"
            else:
                reason = "V4 没有对应接口"
            skipped.append({**source, "reason": reason})
            continue
        transformed = False
        if re.fullmatch(r"/faces/sifli/\d+", parsed.path):
            params["id"] = parsed.path.rsplit("/", 1)[-1]
            if not params.get("fv") and inherited_fv.get(source["platform"]):
                params["fv"] = inherited_fv[source["platform"]]
                transformed = True
        v4_url = urlunsplit(("https", "api.moyoung.com", v4_path, urlencode(params), ""))
        rows.append({**source, "old_path": parsed.path, "v4_path": v4_path, "v4_url": v4_url, "params": params, "expected_source": expected_source(source["platform"]), "inherited_fv": transformed})
    return rows, skipped


def parse_post_body(text: str):
    value = text.strip()
    if not value: return None
    try:
        parsed = json.loads(value)
        return json.loads(parsed) if isinstance(parsed, str) else parsed
    except Exception:
        return {"_raw_body": value}


def fetch(url: str, method="GET", body=None):
    started = time.perf_counter()
    try:
        response = requests.request(method, url, json=body, timeout=(8, 35))
        body = response.json()
        return {"url": url, "http": response.status_code, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "body": body, "error": ""}
    except Exception as exc:
        return {"url": url, "http": None, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "body": None, "error": f"{type(exc).__name__}: {exc}"}


def extract(body, old_path, is_v4=False):
    if not isinstance(body, dict): return None
    # Every V4 business endpoint returns its payload directly in `data`.
    # Legacy endpoints use several different envelopes and must be decoded
    # with their own structure before comparing records by ID.
    if is_v4:
        return body.get("data")
    if old_path == "/faces/v3/list":
        return body.get("data", {}).get("faces", []) if isinstance(body.get("data"), dict) else []
    if old_path == "/faces/sifli": return body.get("faces", [])
    return body.get("data")


def envelope_diff(old_path):
    mapping={
        "/faces/v3/list": ("$.data.faces", "$.data"),
        "/faces/sifli": ("$.faces", "$.data"),
    }
    if old_path not in mapping:
        return None
    old_location,new_location=mapping[old_path]
    return {"path":"$","kind":"JSON 结构不同（列表位置变化）","old":old_location,"new":new_location}


def brief(value):
    if isinstance(value, (dict, list)): return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def field_diffs(old, new, path="$"):
    diffs = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key, old_value in old.items():
            child = f"{path}.{key}"
            if key not in new: diffs.append({"path": child, "kind": "旧字段在 V4 缺失", "old": brief(old_value), "new": "<V4 未返回>"})
            else: diffs.extend(field_diffs(old_value, new[key], child))
    elif isinstance(old, list) and isinstance(new, list):
        if all(isinstance(x, dict) and "id" in x for x in old) and all(isinstance(x, dict) and "id" in x for x in new):
            new_by_id = {str(x["id"]): x for x in new}
            missing = [str(x["id"]) for x in old if str(x["id"]) not in new_by_id]
            if missing: diffs.append({"path": f"{path}[*].id", "kind": "旧接口表盘在 V4 缺失", "old": f"{len(missing)} 个；示例 ID：{', '.join(missing[:3])}", "new": "V4 未返回"})
            for item in old:
                if str(item["id"]) in new_by_id: diffs.extend(field_diffs(item, new_by_id[str(item["id"])], f"{path}[id={item['id']}]"))
        elif old != new: diffs.append({"path": path, "kind": "数组内容不同", "old": brief(old), "new": brief(new)})
    elif old != new: diffs.append({"path": path, "kind": "值不同", "old": brief(old), "new": brief(new)})
    return diffs


def compare(row):
    payload = parse_post_body(row["body"]) if row["method"] == "POST" else None
    old, v4 = fetch(row["url"], row["method"], payload), fetch(row["v4_url"], row["method"], payload)
    old_body, v4_body = old["body"], v4["body"]
    result = {**row, "old": old, "v4": v4, "old_code": old_body.get("code") if isinstance(old_body, dict) else None, "v4_code": v4_body.get("code") if isinstance(v4_body, dict) else None}
    result["v4_source"] = v4_body.get("meta", {}).get("source") if isinstance(v4_body, dict) and isinstance(v4_body.get("meta"), dict) else ""
    if old["error"] or v4["error"]:
        result.update(status="请求失败", diffs=[{"path":"$.transport","kind":"请求失败","old":old["error"] or "成功","new":v4["error"] or "成功"}]); return result
    if result["v4_code"] == -2:
        result.update(status="V4 返回 -2（接口问题）", diffs=[
            {"path":"$.code","kind":"V4 返回 -2（接口问题）","old":result["old_code"],"new":-2},
            {"path":"$.message","kind":"V4 的 -2 错误信息","old":old_body.get("message","") if isinstance(old_body,dict) else "","new":v4_body.get("message","") if isinstance(v4_body,dict) else ""},
        ]); return result
    if result["old_code"] != result["v4_code"]:
        result.update(status="业务码不同", diffs=[{"path":"$.code","kind":"业务码不同","old":result["old_code"],"new":result["v4_code"]},{"path":"$.message","kind":"错误信息不同","old":old_body.get("message",""),"new":v4_body.get("message","")}]); return result
    if result["old_code"] != 0:
        result.update(status="双方报错", diffs=[{"path":"$.message","kind":"双方未得到有效业务返回","old":old_body.get("message",""),"new":v4_body.get("message","")}]); return result
    if row["method"] == "POST":
        diffs = field_diffs(old_body, v4_body)
        result.update(status="差异" if diffs else "一致", diffs=diffs, request_body=payload)
        return result
    old_data, v4_data = extract(old_body, row["old_path"], False), extract(v4_body, row["old_path"], True)
    diffs = []
    structure=envelope_diff(row["old_path"])
    if structure:
        diffs.append(structure)
    if result["v4_source"] != row["expected_source"]:
        diffs.append({"path":"$.meta.source","kind":"固件路由不同","old":row["expected_source"],"new":result["v4_source"] or "未返回"})
    if old_body.get("total") != v4_body.get("total") and ("total" in old_body or "total" in v4_body):
        diffs.append({"path":"$.total","kind":"表盘总数不同","old":old_body.get("total"),"new":v4_body.get("total")})
    diffs.extend(field_diffs(old_data, v4_data))
    result.update(status="差异" if diffs else "一致", diffs=diffs)
    return result


def esc(value): return html.escape(str(value), quote=True)
def raw(value): return f"<pre>{esc(json.dumps(value, ensure_ascii=False, indent=2) if value is not None else '')}</pre>"

def compact_diffs(diffs):
    buckets = {}
    for diff in diffs:
        path = re.sub(r"\[id=[^]]+\]", "[*]", diff["path"])
        bucket = buckets.setdefault((path, diff["kind"]), {"sample": diff, "count": 0})
        bucket["count"] += 1
    rows = "".join(f"<tr><td><code>{esc(path)}</code>{' <small>×'+str(item['count'])+'</small>' if item['count']>1 else ''}</td><td>{esc(kind)}</td><td>{esc(item['sample']['old'])}</td><td>{esc(item['sample']['new'])}</td></tr>" for (path,kind),item in buckets.items())
    return f"<table class='diff'><tr><th>字段路径</th><th>问题</th><th>旧接口实际值</th><th>V4 实际值</th></tr>{rows}</table>" if rows else "<div class='empty'>无字段差异</div>"

def signature(row):
    fields = tuple(sorted((re.sub(r"\[id=[^]]+\]", "[*]", d["path"]), d["kind"]) for d in row["diffs"]))
    if row.get("v4_code") == -2:
        # Every V4 -2 is the same top-level interface failure. Keep one sample
        # and retain all affected real requests under that sample.
        return "V4_MINUS_2", row["status"], fields
    return row["platform"], row["old_path"], row["v4_path"], row["status"], fields

def grouped(rows):
    groups = {}
    for row in rows: groups.setdefault(signature(row), []).append(row)
    result=[]
    for items in groups.values():
        one=items[0].copy(); one["affected"]=items; result.append(one)
    return result

def report(results, skipped):
    grouped_rows = grouped(results)
    # Across the whole report, show an identical field problem only once.
    # Different old/new values remain separate problems.
    seen_diffs=set(); issues=[]
    for row in (r for r in grouped_rows if r["status"] != "一致"):
        unique=[]
        for diff in row['diffs']:
            path=re.sub(r"\[id=[^]]+\]","[*]",diff['path'])
            key=(path,diff['kind'],str(diff['old']),str(diff['new']))
            if key in seen_diffs:
                continue
            seen_diffs.add(key); unique.append(diff)
        if unique:
            item=row.copy(); item['diffs']=unique; issues.append(item)
    passed=[r for r in grouped_rows if r["status"] == "一致"]
    def card(r, i):
        old,v4=r["old"],r["v4"]
        count=len(r["affected"])
        related="".join(f"<li>{esc(x['url'])}</li>" for x in r["affected"][1:])
        return f'''<article class="case {'bad' if r['status']!='一致' else 'ok'}" data-status="{'issue' if r['status']!='一致' else 'pass'}" data-platform="{esc(r['platform'])}">
<header><b>{esc(r['status'])}</b><strong>{esc(r['platform'])}</strong><span><code>{esc(r['old_path'])}</code> → <code>{esc(r['v4_path'])}</code></span><em>影响 {count} 条请求</em></header>
<div class="urls"><div><label>旧接口</label><a href="{esc(old['url'])}" target="_blank">{esc(old['url'])}</a></div><div><label>V4</label><a href="{esc(v4['url'])}" target="_blank">{esc(v4['url'])}</a></div></div>
<details open><summary>查看已确认的差异</summary>{compact_diffs(r['diffs'])}<details class="raw"><summary>查看完整原始响应</summary><div class="raw"><div><h4>旧接口响应</h4>{raw(old['body'])}</div><div><h4>V4 响应</h4>{raw(v4['body'])}</div></div></details>{'<details class="related"><summary>同类请求 '+str(count-1)+' 条</summary><ul>'+related+'</ul></details>' if count>1 else ''}</details></article>'''
    skip_rows="".join(f"<tr><td>{esc(x['platform'])}</td><td>{esc(x['method'])}</td><td>{esc(x['reason'])}</td><td><code>{esc(x['url'])}</code></td></tr>" for x in skipped)
    skipped_html=f'''<h2>V4 没有对应接口（不进行错误对比）</h2><div class="skip"><table><tr><th>平台</th><th>方法</th><th>说明</th><th>旧接口真实请求</th></tr>{skip_rows}</table></div>''' if skipped else ''
    all_cards="".join(card(r,i+1) for i,r in enumerate(issues)) or "<div class='empty'>本次未发现差异</div>"
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V4 表盘业务接口问题报告</title><style>
body{{margin:0;background:#f5f7fb;color:#172033;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}main{{max-width:1440px;margin:auto;padding:28px 24px 60px}}h1{{margin:0}}h2{{margin-top:28px}}.note{{margin:14px 0;padding:13px;border-left:4px solid #175cd3;background:#eef4ff}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{background:#fff;border:1px solid #e4e7ec;border-radius:9px;padding:12px;min-width:145px}}.card b{{font-size:24px;display:block}}.case{{background:#fff;border:1px solid #e4e7ec;border-radius:10px;margin:12px 0;overflow:hidden}}.case.bad{{border-left:5px solid #b42318}}.case header{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:14px;border-bottom:1px solid #e4e7ec}}.case.bad header b{{color:#b42318;font-size:20px}}.case.ok header b{{color:#067647;font-size:18px}}.case header strong{{font-size:18px}}.case header span{{font-size:16px;flex:1}}.case header em{{color:#667085;font-style:normal}}.urls{{padding:12px 14px;background:#fbfcfe;display:grid;gap:7px}}label{{display:inline-block;width:55px;color:#667085;font-weight:bold}}a{{color:#175cd3;word-break:break-all}}code{{font:12px Consolas,monospace;background:#f2f4f7;padding:2px 4px;border-radius:3px}}details{{padding:0 14px 14px}}summary{{padding:12px 0;font-weight:bold;cursor:pointer}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:9px;border-bottom:1px solid #e4e7ec;text-align:left;vertical-align:top;word-break:break-word}}th{{background:#f9fafb}}.diff{{border:1px solid #e4e7ec;border-radius:7px;overflow:hidden}}.diff small{{color:#667085}}.raw{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}pre{{max-height:450px;overflow:auto;margin:0;background:#101828;color:#e5e7eb;padding:12px;border-radius:7px;font:12px/1.45 Consolas,monospace;white-space:pre-wrap;word-break:break-word}}.empty{{padding:10px;color:#667085;background:#f9fafb}}.related{{background:#fff8f7;margin-top:10px}}.related ul{{max-height:160px;overflow:auto}}.skip{{overflow:auto;border:1px solid #e4e7ec;border-radius:8px}}@media(max-width:800px){{main{{padding:18px 10px}}.raw{{grid-template-columns:1fr}}.case header span{{min-width:100%}}}}
</style></head><body><main><h1>V4 表盘业务接口问题报告</h1><div>生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　|　测试数据：<code>{esc(str(INPUT))}</code></div><div class="note"><b>范围：</b>已删除整个模板接口大类；保留列表、标签、详情和下载接口。只展示实际差异，返回一致的请求隐藏。V4 返回 <code>-2</code> 一律标记为接口问题。没有明确 V4 对应接口的旧请求不强行比较，单独提示。V4 可新增字段，但不能缺少旧字段或改变旧字段值。同类问题只保留一个真实样本。</div><div class="cards"><div class="card">GET 对照<b>{sum(x['method']=='GET' for x in results)}</b></div><div class="card">POST 对照<b>{sum(x['method']=='POST' for x in results)}</b></div><div class="card">V4 无对应接口<b>{len(skipped)}</b></div><div class="card">已隐藏一致<b>{sum(x['status']=='一致' for x in results)}</b></div><div class="card">差异请求<b>{sum(x['status']!='一致' for x in results)}</b></div><div class="card">问题组<b>{len(issues)}</b></div></div><h2>业务接口问题（已归类；每类只保留一个样本）</h2>{all_cards}{skipped_html}</main></body></html>'''

def main():
    rows, skipped = prepared_get_rows()
    results=[]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures=[pool.submit(compare,row) for row in rows]
        for future in as_completed(futures): results.append(future.result())
    results.sort(key=lambda x:(x['platform'],x['sheet_row']))
    OUT.write_text(report(results,skipped),encoding="utf-8")
    print(json.dumps({"report":str(OUT),"get_tested":sum(x['method']=='GET' for x in results),"post_tested":sum(x['method']=='POST' for x in results),"skipped":len(skipped),"status":dict(Counter(x['status'] for x in results))},ensure_ascii=False))

if __name__ == "__main__": main()
