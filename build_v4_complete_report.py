"""Publish the latest non-template V4 business endpoint report as one HTML file."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

ENDPOINTS = Path("reports/v4_user_supplied_links_comparison_20260814.html")
OUT = Path("reports/v4_complete_comparison_user_data_20260814.html")


def main_content(page: str) -> str:
    match = re.search(r"<main>(.*)</main>", page, re.S)
    if not match:
        raise ValueError("报告正文不存在")
    content = match.group(1)
    # The combined report supplies its own clear title; retain each source's data.
    return re.sub(r"<h1>.*?</h1>", "", content, count=1, flags=re.S)


def main() -> None:
    if not ENDPOINTS.exists():
        raise FileNotFoundError("请先运行业务接口对照测试，再生成报告")
    endpoints = main_content(ENDPOINTS.read_text(encoding="utf-8"))
    OUT.write_text(f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>V4 表盘业务接口问题报告</title>
<style>
body{{margin:0;background:#f5f7fb;color:#172033;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1440px;margin:auto;padding:28px 24px 60px}} h1{{margin:0}} h2{{margin-top:30px}} h3{{margin:0 0 12px;font-size:21px}}
.lead{{margin:14px 0 22px;color:#475467}} .section{{background:#fff;border:1px solid #d0d5dd;border-radius:12px;margin:22px 0;padding:20px;overflow:hidden}}
.section > .section-note{{margin:0 0 18px;padding:10px 12px;background:#eef4ff;border-left:4px solid #175cd3;color:#344054}}
.section .note{{margin:14px 0;padding:13px;background:#fff1f0;border-left:4px solid #b42318}} .cards{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{padding:12px;min-width:145px;background:#fff;border:1px solid #e4e7ec;border-radius:9px}}.card b{{display:block;font-size:24px}}
.case{{background:#fff;border:1px solid #e4e7ec;border-radius:10px;margin:14px 0;overflow:hidden}}.case.bad{{border-left:5px solid #b42318}}
header{{padding:14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #e4e7ec}}header b{{font-size:20px;color:#b42318}}.ok header b{{color:#067647}}header strong{{font-size:18px}}header span{{flex:1}}header em{{color:#667085;font-style:normal}}
.urls{{padding:12px 14px;background:#fbfcfe;display:grid;gap:7px}}label{{display:inline-block;width:90px;color:#667085;font-weight:bold}}a{{color:#175cd3;word-break:break-all}}code{{font:12px Consolas,monospace;background:#f2f4f7;padding:2px 4px;border-radius:3px}}
details{{padding:0 14px 14px}}summary{{padding:12px 0;font-weight:bold;cursor:pointer}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:9px;border-bottom:1px solid #e4e7ec;text-align:left;vertical-align:top;word-break:break-word}}th{{background:#f9fafb}}small{{display:block;color:#667085;margin-top:4px}}
.raw{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}pre{{max-height:420px;overflow:auto;background:#101828;color:#e5e7eb;padding:12px;border-radius:7px;font:12px/1.45 Consolas,monospace;white-space:pre-wrap;word-break:break-word}}.related{{background:#fff8f7;margin-top:12px}}.related ul{{max-height:160px;overflow:auto}}.empty{{padding:10px;background:#f9fafb;color:#667085}}
@media(max-width:800px){{main{{padding:18px 10px}}.section{{padding:14px 10px}}.raw{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>V4 表盘业务接口问题报告</h1>
<div class="lead">生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。模板接口大类已删除；保留列表、标签、详情和下载。只展示差异，重复问题仅保留一个样例。</div>
<section class="section"><h3>表盘业务接口：列表、标签、详情、下载</h3><div class="section-note">包含真实 GET 和下载上报 POST；一致项已隐藏，无法对应 V4 的旧接口单独提示。</div>{endpoints}</section>
</main></body></html>''', encoding="utf-8")
    print(OUT)


if __name__ == "__main__":
    main()
