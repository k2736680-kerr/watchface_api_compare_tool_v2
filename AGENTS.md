# 表盘 / API 对比工具 v2 · Agent 持久记忆

> GitHub：`k2736680-kerr/watchface_api_compare_tool_v2`（`~/AutoTestTools/Projects/watchface_api_compare_tool_v2`）

## 是什么

用同一份用例清单请求 `old_base` / `new_base`，Diff 后出 CSV / JSONL / HTML。另有 v4 表盘路由模板、Node 辅助脚本与 Python 报告构建器。

## 怎么跑

```bash
cd ~/AutoTestTools/Projects/watchface_api_compare_tool_v2
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 编辑 config/config.json 的 old_base / new_base（勿提交生产域名含密钥 query）
python compare_apis.py
# Windows 便捷: .\run_compare.ps1 -OldBase ... -NewBase ...
```

用例默认：`data/new_api_compare_cases.csv`；POST 多样本：`config/post_body_cases.csv`。

## 关键目录

| 路径 | 作用 |
|------|------|
| `compare_apis.py` | 主对比 |
| `build_v4_*.py` / `*.mjs` | 表盘路由与报告管线 |
| `config/` | `config.json`、post cases |
| `outputs/`、`logs/` | 本地产物，通常 gitignore |
| `reduce_requests_from_log.py` | 从日志抽用例 |

## 勿动

- **不要提交 `.venv/`**（历史上曾误跟踪，应保持 gitignore）。
- 对比前确认 `old_base`/`new_base` 指向授权环境。

## Mac 注意

- 纯 Python + 可选 Node（`*.mjs`）；`run_compare.ps1` 在 Mac 用 `python compare_apis.py` 等价参数。
- 路径用 `/` 即可。

## 先读

- `README.md`