"""Template endpoint comparison based only on the user's supplied link workbook data."""
from __future__ import annotations

import html, json, re, time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit
import requests

INPUT=Path('outputs/user_supplied_test_data_20260814.json')
OUT=Path('reports/v4_template_comparison_user_data_20260814.html')
COMPANY={'V3':'realtek','杰理':'杰理','RTL8773':'rtl8773','中科蓝汛':'中科蓝讯','海思':'海思','杰理nandflash':'jlnand','杰理707':'jl707'}
NEW_PLATFORMS={'杰理','杰理707','杰理nandflash','RTL8773','中科蓝汛','中科蓝讯','海思'}

def expected_source(platform):
    if platform in NEW_PLATFORMS: return 'new'
    if platform=='思澈': return 'sifli'
    return 'v3'

def firmware(fv):
    parts=fv.split('-')
    if len(parts)>=2:
        code=parts[-2]
        return code[:-1] if code and code[-1].isdigit() else code
    return ''

def load_cases():
    raw=json.loads(INPUT.read_text(encoding='utf8'))['rows']; seen=set(); out=[]
    for r in raw:
        params=dict(parse_qsl(urlsplit(r['url']).query,keep_blank_values=True)); fv=params.get('fv','').strip()
        if not fv: continue
        key=(r['platform'],fv)
        if key in seen: continue
        seen.add(key)
        if r['platform']=='思澈': old='https://api.moyoung.com/faces/sifli/face-tpls'; kind='Sifli 独立模板接口'
        elif r['platform'] in COMPANY: old='https://api.moyoung.com/faces/face-tpls?'+urlencode({'company':COMPANY[r['platform']]}); kind='API 公司模板接口'
        else: continue
        fw=firmware(fv); v4='https://api.moyoung.com/faces/v4/face-tpls?'+urlencode({'fv':fv,'firmware':fw})
        out.append({'platform':r['platform'],'fv':fv,'firmware':fw,'old_url':old,'v4_url':v4,'old_kind':kind})
    return out

def get(url):
    t=time.perf_counter()
    try:
        r=requests.get(url,timeout=(8,35));return {'http':r.status_code,'ms':round((time.perf_counter()-t)*1000,1),'body':r.json(),'error':''}
    except Exception as e:return {'http':None,'ms':round((time.perf_counter()-t)*1000,1),'body':None,'error':f'{type(e).__name__}: {e}'}

def brief(value):
    """Show JSON values unambiguously in the report."""
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def json_value(value):
    """Decode JSON held in a string, so JSON key order is not treated as a change."""
    if isinstance(value,str) and value.lstrip()[:1] in ('{','['):
        try:
            decoded=json.loads(value)
            if isinstance(decoded,(dict,list)):
                return decoded
        except (TypeError,ValueError):
            pass
    return value

def type_name(value):
    return {dict:'对象',list:'数组',str:'字符串',int:'整数',float:'小数',bool:'布尔值',type(None):'空值'}.get(type(value),type(value).__name__)

def field_diffs(old,new,path='$'):
    old_value,new_value=json_value(old),json_value(new)
    diffs=[]
    if type(old_value) is not type(new_value):
        diffs.append({'path':path,'kind':f'数据类型不同（{type_name(old_value)} → {type_name(new_value)}）','old':brief(old),'new':brief(new),'old_type':type_name(old_value),'new_type':type_name(new_value)})
    elif isinstance(old_value,dict):
        for key,value in old_value.items():
            child=f'{path}.{key}'
            if key not in new_value: diffs.append({'path':child,'kind':'旧字段在 V4 缺失','old':brief(value),'new':'<V4 未返回>','old_type':type_name(value),'new_type':'缺失'})
            else: diffs.extend(field_diffs(value,new_value[key],child))
    elif isinstance(old_value,list):
        if old_value!=new_value: diffs.append({'path':path,'kind':'数组内容不同','old':brief(old_value),'new':brief(new_value),'old_type':'数组','new_type':'数组'})
    elif old_value!=new_value:
        diffs.append({'path':path,'kind':'值不同','old':brief(old),'new':brief(new),'old_type':type_name(old_value),'new_type':type_name(new_value)})
    return diffs

def compact_diffs(diffs):
    groups={}
    for diff in diffs:
        normalized=re.sub(r'\[id=[^]]+\]','[*]',diff['path'])
        # Different template IDs with the same shape of problem are one issue.
        key=(normalized,diff['kind'],diff.get('old_type',''),diff.get('new_type',''))
        group=groups.setdefault(key,{'sample':diff,'count':0,'ids':[],'examples':[]})
        group['count']+=1
        group['ids']+=re.findall(r'\[id=([^]]+)\]',diff['path'])
        example=(str(diff['old']),str(diff['new']))
        if example not in group['examples'] and len(group['examples'])<3:
            group['examples'].append(example)
    if not groups:return '<div class="empty">模板 ID 和旧字段值均一致</div>'
    rows=''
    for (path,kind,_,_),group in groups.items():
        ids=', '.join(list(dict.fromkeys(group['ids']))[:3])
        affected=f"涉及 {group['count']} 个模板" + (f"；示例 ID：{ids}" if ids else '')
        old_examples='<br>'.join(esc(x[0]) for x in group['examples'])
        new_examples='<br>'.join(esc(x[1]) for x in group['examples'])
        rows+=f"<tr><td><code>{esc(path)}</code></td><td><b>{esc(kind)}</b><small>{affected}</small></td><td>{old_examples}</td><td>{new_examples}</td></tr>"
    return f"<table><tr><th>字段路径</th><th>实际差异</th><th>旧接口示例值</th><th>V4 示例值</th></tr>{rows}</table>"

def run(c):
    old,v4=get(c['old_url']),get(c['v4_url']); ob,vb=old['body'],v4['body']
    oi=[x.get('id') for x in ob.get('tpls',[]) if isinstance(x,dict)] if isinstance(ob,dict) else []
    vi=[x.get('id') for x in vb.get('data',[]) if isinstance(x,dict)] if isinstance(vb,dict) else []
    missing=[x for x in oi if x not in vi]; extra=[x for x in vi if x not in oi]
    old_by_id={str(x.get('id')):x for x in ob.get('tpls',[]) if isinstance(x,dict)} if isinstance(ob,dict) else {}
    v4_by_id={str(x.get('id')):x for x in vb.get('data',[]) if isinstance(x,dict)} if isinstance(vb,dict) else {}
    diffs=[]
    if missing: diffs.append({'path':'$.data[*].id','kind':'旧模板在 V4 缺失','old':f'{len(missing)} 个；示例 ID：{", ".join(map(str,missing[:3]))}','new':'V4 未返回'})
    for template_id in oi:
        key=str(template_id)
        if key in old_by_id and key in v4_by_id: diffs.extend(field_diffs(old_by_id[key],v4_by_id[key],f'$.data[id={template_id}]'))
    source=vb.get('meta',{}).get('source','') if isinstance(vb,dict) else ''
    if old['error'] or v4['error']: status='请求失败'
    elif isinstance(vb,dict) and vb.get('code')==-2:
        status='V4 返回 -2（接口问题）'
        diffs=[{'path':'$.code','kind':'V4 返回 -2（接口问题）','old':brief(ob.get('code') if isinstance(ob,dict) else None),'new':'-2','old_type':'业务码','new_type':'业务码'}]
    elif ob.get('code')!=vb.get('code'): status='业务码不同'
    elif not oi and not vi: status='均无模板'
    elif diffs: status='模板内容不同'
    else: status='模板一致'
    expected=expected_source(c['platform'])
    return {**c,'old':old,'v4':v4,'old_code':ob.get('code') if isinstance(ob,dict) else None,'v4_code':vb.get('code') if isinstance(vb,dict) else None,'expected_source':expected,'v4_source':source,'route_issue':bool(source) and source!=expected,'old_ids':oi,'v4_ids':vi,'missing':missing,'extra':extra,'diffs':diffs,'status':status}

def esc(x):return html.escape(str(x),quote=True)
def pretty(x):return f'<pre>{esc(json.dumps(x,ensure_ascii=False,indent=2) if x is not None else "")}</pre>'

def id_summary(ids):
    return '无' if not ids else f'{len(ids)} 个；示例 ID：{ids[0]}'

def issue_signature(item):
    fields=tuple(sorted((re.sub(r'\[id=[^]]+\]','[*]',d['path']),d['kind']) for d in item['diffs']))
    return item['platform'],item['status'],tuple(item['missing']),tuple(item['extra']),fields

def page(results):
    groups={}
    for item in results:groups.setdefault(issue_signature(item),[]).append(item)
    grouped=[]
    for items in groups.values():
        one=items[0].copy();one['affected']=items;grouped.append(one)
    cards=[]
    for x in grouped:
        old,v4=x['old'],x['v4']; isbad=x['status'] not in {'模板一致','均无模板'} or x['route_issue']; count_affected=len(x['affected'])
        if not isbad:
            continue
        related=''.join(f'<li><code>{esc(item["fv"])}</code>　<a href="{esc(item["v4_url"])}" target="_blank">V4 请求</a></li>' for item in x['affected'][1:])
        overview_rows=''
        if x['route_issue']:
            overview_rows+=f'''<tr><td><b>V4 数据路由不一致</b></td><td>期望 <code>{esc(x['expected_source'])}</code></td><td>实际 <code>{esc(x['v4_source'] or '未返回')}</code></td></tr>'''
        if x['old_code']!=x['v4_code']:
            overview_rows+=f'''<tr><td><b>业务码不一致</b></td><td>{esc(x['old_code'])}</td><td>{esc(x['v4_code'])}</td></tr>'''
        if x['missing']:
            overview_rows+=f'''<tr><td><b>旧模板 ID 在 V4 缺失</b></td><td colspan="2">{esc(id_summary(x['missing']))}</td></tr>'''
        overview=f'''<table><tr><th>实际问题</th><th>旧接口/期望</th><th>V4 实际</th></tr>{overview_rows}</table>''' if overview_rows else ''
        title=x['status']+(' + 路由不同' if x['route_issue'] else '')
        cards.append(f'''<article class="case bad"><header><b>{esc(title)}</b><strong>{esc(x['platform'])}</strong><span>fv：<code>{esc(x['fv'])}</code>　firmware：<code>{esc(x['firmware'])}</code></span><em>影响 {count_affected} 个 fv</em></header><div class="urls"><div><label>旧模板接口</label><a href="{esc(x['old_url'])}" target="_blank">{esc(x['old_url'])}</a></div><div><label>V4 模板接口</label><a href="{esc(x['v4_url'])}" target="_blank">{esc(x['v4_url'])}</a></div></div><details open><summary>查看实际差异</summary>{overview}<h4>字段/值差异</h4>{compact_diffs(x['diffs'])}<details class="raw"><summary>查看完整原始响应</summary><div class="raw"><div><h4>旧接口响应</h4>{pretty(old['body'])}</div><div><h4>V4 响应</h4>{pretty(v4['body'])}</div></div></details>{'<details class="related"><summary>同类 fv '+str(count_affected-1)+' 个</summary><ul>'+related+'</ul></details>' if count_affected>1 else ''}</details></article>''')
    count=Counter(x['status'] for x in results)
    structure='''<div class="note"><b>统一响应结构差异（仅展示一次）：</b>旧接口模板列表字段为 <code>tpls</code>，V4 为 <code>data</code>。比较时已按 <code>tpls → data</code> 对应。</div>'''
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>V4 模板接口对照报告</title><style>body{{margin:0;background:#f5f7fb;color:#172033;font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}}main{{max-width:1400px;margin:auto;padding:28px 24px 60px}}h1{{margin:0}}h4{{margin:18px 0 8px}}.note{{margin:14px 0;padding:13px;background:#fff1f0;border-left:4px solid #b42318}}.cards{{display:flex;gap:12px;flex-wrap:wrap}}.card{{padding:12px;min-width:150px;background:#fff;border:1px solid #e4e7ec;border-radius:9px}}.card b{{display:block;font-size:24px}}.case{{background:#fff;border:1px solid #e4e7ec;border-radius:10px;margin:14px 0;overflow:hidden}}.case.bad{{border-left:5px solid #b42318}}header{{padding:14px;display:flex;gap:14px;align-items:center;flex-wrap:wrap;border-bottom:1px solid #e4e7ec}}header b{{font-size:20px;color:#b42318}}.ok header b{{color:#067647}}header strong{{font-size:18px}}header span{{flex:1}}header em{{color:#667085;font-style:normal}}.urls{{padding:12px 14px;background:#fbfcfe;display:grid;gap:7px}}label{{display:inline-block;width:90px;color:#667085;font-weight:bold}}a{{color:#175cd3;word-break:break-all}}code{{font:12px Consolas,monospace;background:#f2f4f7;padding:2px 4px;border-radius:3px}}details{{padding:0 14px 14px}}summary{{padding:12px 0;font-weight:bold;cursor:pointer}}table{{width:100%;border-collapse:collapse}}th,td{{padding:9px;border-bottom:1px solid #e4e7ec;text-align:left;vertical-align:top;word-break:break-word}}th{{background:#f9fafb}}small{{display:block;color:#667085;margin-top:4px}}.empty{{padding:10px;background:#f9fafb;color:#667085}}.raw{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}pre{{max-height:420px;overflow:auto;background:#101828;color:#e5e7eb;padding:12px;border-radius:7px;font:12px/1.45 Consolas,monospace;white-space:pre-wrap;word-break:break-word}}.related{{background:#fff8f7;margin-top:12px}}.related ul{{max-height:160px;overflow:auto}}@media(max-width:800px){{main{{padding:18px 10px}}.raw{{grid-template-columns:1fr}}}}</style></head><body><main><h1>V4 模板接口差异报告</h1><div>生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　|　数据来源：你提供的链接表</div><div class="note"><b>只展示差异：</b>路由一致、业务码一致、数量一致和 V4 合法新增均不展示。</div>{structure}<div class="cards"><div class="card">测试 fv<b>{len(results)}</b></div><div class="card">已隐藏一致项<b>{count['模板一致']}</b></div><div class="card">差异 fv<b>{count['模板内容不同']}</b></div><div class="card">差异问题组<b>{sum(x['status'] not in {'模板一致','均无模板'} or x['route_issue'] for x in grouped)}</b></div></div>{''.join(cards)}</main></body></html>'''

def main():
    cases=load_cases(); results=[]
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(run,c) for c in cases]): results.append(f.result())
    results.sort(key=lambda x:(x['platform'],x['fv']))
    OUT.write_text(page(results),encoding='utf8')
    print(json.dumps({'report':str(OUT),'cases':len(results),'status':dict(Counter(x['status'] for x in results))},ensure_ascii=False))
if __name__=='__main__':main()
