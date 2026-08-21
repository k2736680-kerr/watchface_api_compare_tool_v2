# API Compare Tool

用于对比旧接口和新接口返回是否兼容。工具会用同一份请求清单分别请求 `old_base` 和 `new_base`，并输出 CSV / JSONL / HTML 报告。

## 配置域名

默认配置在 `config\config.json`：

```json
{
  "old_base": "https://old.example.com",
  "new_base": "https://new.example.com"
}
```

也可以运行时覆盖，不用改配置文件：

```powershell
.\run_compare.ps1 -OldBase "https://old.example.com" -NewBase "https://new.example.com"
```

## 准备接口清单

默认读取：

```text
data\new_api_compare_cases.csv
```

最少需要这些字段：

```csv
case_priority,request_method,request_uri,cnt,route_pattern,qkeys,route_template,old_url,new_url,select_reason,platform,case_group,compare_profile,post_body_json
p0,GET,/api/example?id=1,,,,,,,,core,,,
p0,POST,/api/example,,,,,,,,core,,, "{""id"":1}"
```

常用字段说明：

- `case_priority`：套件分组，例如 `p0`、`p1`、`p2`、`manual`、`customer`
- `request_method`：`GET`、`POST` 等
- `request_uri`：路径和 query，例如 `/api/user?id=1`；也可以填完整 URL，工具只取 path/query
- `platform`：可选，用于报告里的接口分组，例如 `user`、`order`、`payment`
- `compare_profile`：可选，填 `strict` 时该用例强制严格比较
- `post_body_json`：可选，单条 POST body；需要多组 body 时用 `config\post_body_cases.csv`

## POST body 样本

多组 POST body 放在：

```text
config\post_body_cases.csv
```

格式：

```csv
request_method,request_path,body_case_no,body_type,body_json,headers_json,source_uri,source_cnt,remark
POST,/api/example,1,json,"{""id"":1}","{""Content-Type"":""application/json""}",,,example
```

静态兜底配置在 `config\post_cases.json`，匹配键格式为 `POST /api/example`。

## 运行

默认跑全部用例：

```powershell
.\run_compare.ps1
```

只跑某个套件：

```powershell
.\run_compare.ps1 -Suite p0
.\run_compare.ps1 -Suite p1
.\run_compare.ps1 -Suite post
```

临时指定输入文件：

```powershell
.\run_compare.ps1 -CasesInput "data\my_api_cases.csv"
```

只预览请求，不发 HTTP：

```powershell
.\run_compare.ps1 -DryRun
```

## 报告

每次运行生成：

```text
reports\<suite>_yyyyMMdd_HHmmss\
```

重点看：

- `report.html`：人工查看
- `summary.json`：整体统计
- `issue_summary.csv`：按问题聚合
- `failed_requests.csv`：失败请求明细
- `diff_details.jsonl`：字段级差异

默认 `schema` 模式重点检查结构、字段名和字段类型，忽略普通字段值、数组 ID 集合、数组长度和新接口多字段。需要严格比较时运行：

```powershell
.\run_compare.ps1 -Profile strict
```

## 文件说明

```text
compare_apis.py                  主对比程序
run_compare.ps1                  日常运行入口
config\config.json               域名、headers、忽略字段、比较规则
config\post_body_cases.csv       多组 POST body 样本
config\post_cases.json           静态 POST body 兜底配置
data\new_api_compare_cases.csv   当前默认接口清单
data\api_compare_cases.csv       旧表盘接口清单，保留但不再默认运行
config\watchface_post_body_cases.csv 旧表盘 POST body 样本归档
reports\                         运行报告
logs\                            运行日志
```
