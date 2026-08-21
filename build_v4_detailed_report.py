"""Fetch historical watch-face requests and build a detailed V4 comparison report."""
from __future__ import annotations

import csv
import html
import json
import os
import re
import time
import urllib.parse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests

BASE = "https://api.moyoung.com"
INPUT = "data/api_compare_cases.csv"
OUT = "reports/v4_legacy_detailed_comparison_20260813.html"
MAPPING = {
    "/faces/v3/tag-list": "/faces/v4/tag-list",
    "/faces/v3/list": "/faces/v4/list",
    "/faces/v3/face-detail": "/faces/v4/face-detail",
    "/faces/new/tag-face-list": "/faces/v4/tag-list",
    "/faces/new/face-list": "/faces/v4/list",
    "/faces/new/face-detail": "/faces/v4/face-detail",
    "/faces/sifli/tag-list": "/faces/v4/tag-list",
    "/faces/sifli": "/faces/v4/list",
}


def source_for(path: str) -> str:
    if path.startswith("/faces/new/"):
        return "new"
    if path.startswith("/faces/sifli"):
        return "sifli"
    return "v3"


def make_url(path: str, params: dict[str, str]) -> str:
    return BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")


def load_cases() -> list[dict]:
    seen, cases = set(), []
    with open(INPUT, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("request_method") != "GET":
                continue
            parsed = urllib.parse.urlsplit(row.get("request_uri", ""))
            old_path = parsed.path
            v4_path = MAPPING.get(old_path)
            if not v4_path and old_path.startswith("/faces/sifli/") and old_path.rsplit("/", 1)[-1].isdigit():
                v4_path = "/faces/v4/face-detail"
            if not v4_path:
                continue
            params = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            if old_path.startswith("/faces/sifli/") and old_path.rsplit("/", 1)[-1].isdigit():
                params["id"] = old_path.rsplit("/", 1)[-1]
            key = old_path, v4_path, tuple(sorted(params.items()))
            if key not in seen:
                seen.add(key)
                cases.append({"old_path": old_path, "v4_path": v4_path, "params": params, "expected_source": source_for(old_path)})
    return cases


def get(path: str, params: dict[str, str]) -> dict:
    url, started = make_url(path, params), time.perf_counter()
    try:
        response = requests.get(url, timeout=(8, 35))
        return {
            "url": url, "http": response.status_code,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "body": response.json(), "error": "",
        }
    except Exception as exc:
        return {"url": url, "http": None, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1), "body": None, "error": f"{type(exc).__name__}: {exc}"}


def extract(body: dict | None, old_path: str):
    if not isinstance(body, dict):
        return None
    if old_path == "/faces/v3/list":
        return body.get("data", {}).get("faces", []) if isinstance(body.get("data"), dict) else []
    if old_path == "/faces/sifli":
        return body.get("faces", [])
    return body.get("data")


def scalar(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def field_diffs(old, new, path="$") -> list[dict]:
    """Compare fields supplied by the old API with V4. V4-only fields are intentional additions."""
    diffs = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key, old_value in old.items():
            next_path = f"{path}.{key}"
            if key not in new:
                diffs.append({"path": next_path, "kind": "旧字段缺失", "old": scalar(old_value), "new": "<V4 未返回>"})
            else:
                diffs.extend(field_diffs(old_value, new[key], next_path))
    elif isinstance(old, list) and isinstance(new, list):
        # Lists of face objects are matched by id; scalar lists retain order semantics.
        if all(isinstance(x, dict) and "id" in x for x in old) and all(isinstance(x, dict) and "id" in x for x in new):
            new_by_id = {str(x["id"]): x for x in new}
            for item in old:
                item_id = str(item["id"])
                if item_id not in new_by_id:
                    diffs.append({"path": f"{path}[id={item_id}]", "kind": "表盘缺失", "old": item_id, "new": "<V4 未返回>"})
                else:
                    diffs.extend(field_diffs(item, new_by_id[item_id], f"{path}[id={item_id}]"))
        elif old != new:
            diffs.append({"path": path, "kind": "数组内容不同", "old": scalar(old), "new": scalar(new)})
    elif old != new:
        diffs.append({"path": path, "kind": "值不同", "old": scalar(old), "new": scalar(new)})
    return diffs


def missing_item_diffs(old_items, new_items) -> list[dict]:
    """V4 may add fields/items, but it must not omit old items for a compatible response."""
    if not isinstance(old_items, list) or not isinstance(new_items, list):
        return []
    old_ids = [item.get("id") for item in old_items if isinstance(item, dict) and "id" in item]
    new_ids = {item.get("id") for item in new_items if isinstance(item, dict) and "id" in item}
    missing = [item_id for item_id in old_ids if item_id not in new_ids]
    if not missing:
        return []
    return [{
        "path": "$.data[*].id",
        "kind": "旧接口表盘在 V4 缺失",
        "old": f"旧接口存在 {len(missing)} 个表盘；示例 ID：{', '.join(map(str, missing[:3]))}",
        "new": "V4 未返回这些旧表盘",
    }]


def compare(case: dict) -> dict:
    old, v4 = get(case["old_path"], case["params"]), get(case["v4_path"], case["params"])
    old_body, v4_body = old["body"], v4["body"]
    row = {**case, "old": old, "v4": v4, "old_code": old_body.get("code") if isinstance(old_body, dict) else None, "v4_code": v4_body.get("code") if isinstance(v4_body, dict) else None}
    row["v4_source"] = v4_body.get("meta", {}).get("source") if isinstance(v4_body, dict) and isinstance(v4_body.get("meta"), dict) else ""
    if old["error"] or v4["error"]:
        row.update(result="请求失败", diffs=[
            {"path": "$.transport", "kind": "请求失败", "old": old["error"] or "请求成功", "new": v4["error"] or "请求成功"}
        ])
        return row
    if row["old_code"] != row["v4_code"]:
        row.update(result="业务码不同", diffs=[
            {"path": "$.code", "kind": "业务码不同", "old": row["old_code"], "new": row["v4_code"]},
            {"path": "$.message", "kind": "错误信息不同", "old": old_body.get("message", ""), "new": v4_body.get("message", "")},
        ])
        return row
    if row["v4_source"] and row["v4_source"] != case["expected_source"]:
        old_items = extract(old_body, case["old_path"])
        new_items = extract(v4_body, case["old_path"])
        old_count = len(old_items) if isinstance(old_items, list) else "无法统计"
        new_count = len(new_items) if isinstance(new_items, list) else "无法统计"
        diffs = [
            {"path": "$.meta.source", "kind": "固件数据源路由不同", "old": case["expected_source"], "new": row["v4_source"]},
            {"path": "$.data[*].id", "kind": "旧接口表盘在 V4 缺失", "old": f"旧接口 faces：{old_count} 条", "new": f"V4 data：{new_count} 条"},
        ]
        if old_body.get("total") != v4_body.get("total"):
            diffs.append({"path": "$.total", "kind": "表盘总数不同", "old": old_body.get("total"), "new": v4_body.get("total")})
        row.update(result="固件路由不同", diffs=diffs)
        return row
    if not row["v4_source"] and row["v4_code"] != 0:
        row.update(result="参数校验差异", diffs=[
            {"path": "$.message", "kind": "参数校验信息", "old": old_body.get("message", ""), "new": v4_body.get("message", "")}
        ])
        return row
    if old_body.get("total") != v4_body.get("total") and ("total" in old_body or "total" in v4_body):
        old_total, new_total = old_body.get("total"), v4_body.get("total")
        kind = "V4 表盘总数减少" if isinstance(old_total, int) and isinstance(new_total, int) and new_total < old_total else "表盘总数不同"
        row.update(result="总数不同", diffs=[
            {"path": "$.total", "kind": kind, "old": old_total, "new": new_total}
        ])
        return row
    old_items, new_items = extract(old_body, case["old_path"]), extract(v4_body, case["old_path"])
    diffs = missing_item_diffs(old_items, new_items) + field_diffs(old_items, new_items)
    row.update(diffs=diffs, result="字段/内容不同" if diffs else "一致")
    return row


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def json_block(value) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2) if value is not None else ""
    return f"<pre>{esc(text)}</pre>"


def diff_table(diffs: list[dict]) -> str:
    if not diffs:
        return '<div class="empty">无字段差异，或当前请求在业务码/路由阶段已经不一致。</div>'
    # A detail/list response can contain the same defect on hundreds of face IDs.
    # Show one representative value per normalized JSON path, retaining the count and an example ID.
    grouped = {}
    for item in diffs:
        normalized = re.sub(r"\[id=([^]]+)\]", "[*]", item["path"])
        ids = re.findall(r"\[id=([^]]+)\]", item["path"])
        key = normalized, item["kind"]
        group = grouped.setdefault(key, {"item": item, "count": 0, "ids": []})
        group["count"] += 1
        group["ids"].extend(ids)
    rows = ""
    for (path, kind), group in grouped.items():
        sample = group["item"]
        note = ""
        if group["count"] > 1:
            examples = ", ".join(group["ids"][:3])
            suffix = f"；示例 ID：{examples}" if examples else ""
            note = f" <span class=\"count-note\">（涉及 {group['count']} 处{suffix}）</span>"
        rows += f"<tr><td><code>{esc(path)}</code>{note}</td><td>{esc(kind)}</td><td>{esc(sample['old'])}</td><td>{esc(sample['new'])}</td></tr>"
    return f'<div class="scroll"><table class="diff"><tr><th>JSON 路径</th><th>差异类型</th><th>旧接口实际值</th><th>V4 实际值</th></tr>{rows}</table></div>'


def issue_signature(row: dict) -> tuple:
    """Group the same compatibility defect across different historical inputs."""
    if row["result"] == "字段/内容不同":
        # A record-specific [id=123] must not split the same field defect into many groups.
        fields = tuple(sorted({(re.sub(r"\[id=[^]]+\]", "[*]", item["path"]), item["kind"]) for item in row["diffs"]}))
        return row["expected_source"], row["old_path"], row["v4_path"], row["result"], fields
    return row["expected_source"], row["old_path"], row["v4_path"], row["result"], row["old_code"], row["v4_code"], row["v4_source"]


def group_issues(results: list[dict]) -> list[dict]:
    groups = {}
    for row in results:
        key = issue_signature(row)
        groups.setdefault(key, []).append(row)
    grouped = []
    for rows in groups.values():
        representative = rows[0].copy()
        representative["affected_count"] = len(rows)
        representative["affected_urls"] = [item["old"]["url"] for item in rows]
        grouped.append(representative)
    return grouped


def build_html(results: list[dict], excluded: list[dict]) -> str:
    counts = Counter(x["result"] for x in results)
    groups = group_issues(results)
    issue_groups = [item for item in groups if item["result"] != "一致"]
    pass_groups = [item for item in groups if item["result"] == "一致"]
    platform_counts = {p: Counter(x["result"] for x in results if x["expected_source"] == p) for p in ("v3", "new", "sifli")}
    summary_rows = "".join(f"<tr><td><b>{p.upper()}</b></td><td>{sum(c.values())}</td><td>{c['一致']}</td><td>{sum(v for k,v in c.items() if k != '一致')}</td><td>{esc('；'.join(f'{k} {v}' for k,v in c.items() if k != '一致') or '—')}</td></tr>" for p, c in platform_counts.items())
    issue_rows = []
    display_rows = issue_groups + pass_groups
    for index, row in enumerate(display_rows, 1):
        has_issue = row["result"] != "一致"
        status = "bad" if has_issue else "ok"
        old, v4 = row["old"], row["v4"]
        differences = diff_table(row["diffs"])
        impact = f'影响 {row["affected_count"]} 条请求' if row["affected_count"] > 1 else '影响 1 条请求'
        urls = ''.join(f'<li><a href="{esc(url)}" target="_blank">{esc(url)}</a></li>' for url in row["affected_urls"][1:])
        affected = f'<details class="affected"><summary>{impact}（展开查看其余 {row["affected_count"] - 1} 条同类请求）</summary><ul>{urls or "<li>无</li>"}</ul></details>' if row["affected_count"] > 1 else f'<span class="impact">{impact}</span>'
        issue_rows.append(f'''<article class="case {'issue' if has_issue else 'pass'}" data-result="{esc(row['result'])}" data-platform="{esc(row['expected_source'])}">
<header><span class="num">#{index}</span><div class="issue-title"><span class="badge {status}">{esc(row['result'])}</span><b>{esc(row['expected_source'].upper())}</b><span class="route"><code>{esc(row['old_path'])}</code> → <code>{esc(row['v4_path'])}</code></span></div><span class="meta">旧/V4：{esc(row['old_code'])}/{esc(row['v4_code'])}　耗时：{old['elapsed_ms']} / {v4['elapsed_ms']} ms</span></header>
<div class="request"><div><label>旧接口实际请求</label><a href="{esc(old['url'])}" target="_blank">{esc(old['url'])}</a></div><div><label>V4 实际请求</label><a href="{esc(v4['url'])}" target="_blank">{esc(v4['url'])}</a></div><div><label>V4 自动解析</label><span>期望 <code>{esc(row['expected_source'])}</code>；实际 <code>{esc(row['v4_source'] or '无（参数错误）')}</code></span></div></div>
{affected}<details {'open' if has_issue else ''}><summary>查看差异与两端实际响应</summary><h4>差异明细</h4>{differences}<div class="responses"><div><h4>旧接口响应（实际）</h4>{json_block(old['body'] if not old['error'] else old['error'])}</div><div><h4>V4 响应（实际）</h4>{json_block(v4['body'] if not v4['error'] else v4['error'])}</div></div></details></article>''')
    excluded_rows = []
    for index, row in enumerate(excluded, 1):
        old, v4 = row["old"], row["v4"]
        state = "双方报错" if row["v4_code"] != 0 else "旧接口报错，V4 返回成功"
        excluded_rows.append(f'''<article class="case invalid"><header><span class="num">异常基线 #{index}</span><span class="badge neutral">{esc(state)}</span><b>{esc(row['expected_source'].upper())}</b><span class="route"><code>{esc(row['old_path'])}</code> → <code>{esc(row['v4_path'])}</code></span><span class="meta">旧/V4：{esc(row['old_code'])}/{esc(row['v4_code'])}</span></header><div class="request"><div><label>旧接口实际请求</label><a href="{esc(old['url'])}" target="_blank">{esc(old['url'])}</a></div><div><label>V4 实际请求</label><a href="{esc(v4['url'])}" target="_blank">{esc(v4['url'])}</a></div></div><details><summary>查看双方报错/返回</summary><div class="responses"><div><h4>旧接口响应（实际）</h4>{json_block(old['body'] if not old['error'] else old['error'])}</div><div><h4>V4 响应（实际）</h4>{json_block(v4['body'] if not v4['error'] else v4['error'])}</div></div></details></article>''')
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>表盘 V4 与历史接口详细对照报告</title><style>
:root{{--bg:#f5f7fb;--card:#fff;--ink:#152033;--muted:#667085;--line:#e4e7ec;--red:#b42318;--redbg:#fff1f0;--green:#067647;--greenbg:#ecfdf3;--blue:#175cd3}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}main{{max-width:1440px;margin:auto;padding:30px 24px 64px}}h1{{margin:0 0 6px;font-size:27px}}h2{{font-size:19px;margin:28px 0 10px}}h4{{margin:18px 0 8px}}.muted,.meta{{color:var(--muted)}}.intro{{margin:18px 0;padding:14px 16px;background:#eef4ff;border-left:4px solid var(--blue);border-radius:5px}}.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}.card{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:15px}}.card b{{display:block;font-size:26px;margin:3px 0}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{border-bottom:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}th{{background:#f9fafb}}.scroll{{overflow:auto;border:1px solid var(--line);border-radius:9px}}.toolbar{{position:sticky;top:0;z-index:4;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px;margin:18px 0;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}input,select{{border:1px solid #cfd4dc;border-radius:7px;padding:8px 10px;font:inherit}}.case{{background:#fff;border:1px solid var(--line);border-radius:10px;margin:12px 0;overflow:hidden}}.case.issue{{border-left:5px solid var(--red)}}.case header{{padding:13px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;border-bottom:1px solid var(--line)}}.num{{color:var(--muted)}}.issue-title{{display:flex;align-items:center;gap:10px;flex:1;min-width:460px;font-size:19px;font-weight:800;color:var(--red)}}.issue-title b{{font-size:18px}}.issue-title .route code{{font-size:16px;background:#fff1f0;color:#7a271a}}.badge{{font-size:13px;font-weight:800;border-radius:99px;padding:4px 10px}}.badge.ok{{color:var(--green);background:var(--greenbg)}}.badge.bad{{color:var(--red);background:var(--redbg)}}.badge.neutral{{color:#475467;background:#f2f4f7}}.route{{flex:1;min-width:300px}}.request{{padding:12px 14px;display:grid;gap:9px;background:#fbfcfe}}label{{display:inline-block;width:118px;color:var(--muted);font-weight:650}}a{{color:#175cd3;word-break:break-all}}details{{padding:0 14px 14px}}summary{{padding:12px 0;cursor:pointer;font-weight:650}}code{{font-family:Consolas,monospace;font-size:12px;background:#f2f4f7;padding:1px 4px;border-radius:3px;word-break:break-all}}.diff td{{max-width:340px;white-space:pre-wrap;word-break:break-word}}.count-note{{color:var(--muted);font-size:12px}}.responses{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}.responses>div{{min-width:0}}pre{{margin:0;max-height:500px;overflow:auto;background:#101828;color:#e4e7ec;padding:12px;border-radius:8px;font:12px/1.45 Consolas,monospace;white-space:pre-wrap;word-break:break-word}}.empty{{padding:12px;background:#f9fafb;color:var(--muted)}}.impact{{display:block;padding:8px 14px;color:var(--red);font-weight:650}}.affected{{padding:0 14px;background:#fff8f7}}.affected summary{{color:var(--red);font-size:13px}}.affected ul{{margin:0;padding:0 0 10px 18px;max-height:180px;overflow:auto}}.hidden{{display:none}}@media(max-width:800px){{main{{padding:20px 12px}}.cards{{grid-template-columns:repeat(2,1fr)}}.responses{{grid-template-columns:1fr}}.meta{{width:100%}}.issue-title{{min-width:0;font-size:17px}}.issue-title .route code{{font-size:14px}}label{{display:block;width:auto;margin-bottom:2px}}}}
</style></head><body><main>
<h1>表盘 V4 与历史接口详细对照报告</h1><div class="muted">生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　|　目标环境：<code>{BASE}</code></div>
<div class="intro"><b>说明：</b>以下全部为本次重新发起的真实 GET 请求。主报告只保留“旧接口业务成功（<code>code=0</code>）”的 {len(results)} 条有效基线请求，用来判断 V4 返回是否兼容。相同问题已归类为 {len(issue_groups)} 组，只显示每组 1 条真实样本，并标明影响请求数；不同字段、不同业务码、不同路由仍分别展示。另有 {len(excluded)} 条旧接口本身报错的历史记录，已在底部单独展示：它们<strong>不能算通过</strong>，但由于没有旧接口成功返回内容，不能判为“V4 返回内容不同”。下载上报接口未调用。</div>
<section class="cards"><div class="card"><span>有效请求对照</span><b>{len(results)}</b></div><div class="card"><span>核心一致</span><b style="color:var(--green)">{counts['一致']}</b><span>当前比较范围内无字段差异</span></div><div class="card"><span>差异请求 / 问题组</span><b style="color:var(--red)">{len(results)-counts['一致']} / {len(issue_groups)}</b><span>相同问题仅展示一个样本</span></div><div class="card"><span>测试方式</span><b>只读</b><span>无生产写入</span></div></section>
<h2>按平台汇总</h2><div class="scroll"><table><tr><th>历史数据源</th><th>请求数</th><th>一致</th><th>差异</th><th>差异分类</th></tr>{summary_rows}</table></div>
<h2>逐条明细</h2><div class="toolbar"><label>显示</label><select id="result"><option value="all">全部请求</option><option value="issue" selected>仅不一致</option><option value="pass">仅一致</option></select><label>平台</label><select id="platform"><option value="all">全部</option><option value="v3">V3</option><option value="new">New</option><option value="sifli">Sifli</option></select><input id="search" placeholder="搜索 URL、固件、表盘 ID、字段路径" size="38"><span id="visible" class="muted"></span></div>
<div id="cases">{''.join(issue_rows)}</div>
<h2>双方/旧端报错的异常请求（不计入“返回内容一致”）</h2><div class="muted">这些请求不具有有效的旧接口返回基线。若请求参数在业务上本应有效，则应单独修复；否则属于无效测试数据。当前共 {len(excluded)} 条。</div>{''.join(excluded_rows)}
</main><script>const cases=[...document.querySelectorAll('#cases .case')],result=document.querySelector('#result'),platform=document.querySelector('#platform'),search=document.querySelector('#search'),visible=document.querySelector('#visible');function filter(){{let n=0,q=search.value.toLowerCase();for(const c of cases){{let ok=(result.value==='all'||(result.value==='issue'?c.classList.contains('issue'):c.classList.contains('pass')))&&(platform.value==='all'||c.dataset.platform===platform.value)&&(!q||c.innerText.toLowerCase().includes(q));c.classList.toggle('hidden',!ok);if(ok)n++}}visible.textContent=`显示 ${{n}} / ${{cases.length}} 条`;}}[result,platform,search].forEach(x=>x.addEventListener('input',filter));filter();</script></body></html>'''


def main():
    cases = load_cases()
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(compare, case) for case in cases]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda x: (x["expected_source"], x["old_path"], x["old"]["url"]))
    excluded = [row for row in results if row["old_code"] != 0]
    results = [row for row in results if row["old_code"] == 0]
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(build_html(results, excluded))
    print(OUT)


if __name__ == "__main__":
    main()
