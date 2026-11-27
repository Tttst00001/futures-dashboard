"""把本次构建的可观测信息写进站点，便于不带凭据直接从线上核对。"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
snapshot_path = ROOT / "data" / "market" / "snapshot.json"
out_path = ROOT / "build" / "build-info.json"

snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
products = snapshot.get("products", [])
status = snapshot.get("status") or {}
errors = status.get("errors") or []

latest_daily = max((p.get("latest_daily") or "" for p in products), default="")
latest_hourly = max((p.get("latest_hourly") or "" for p in products), default="")
min30_codes = [p["code"] for p in products if (p.get("signal") or {}).get("chart_min30")]

info = {
    "generated_at": snapshot.get("generated_at"),
    "built_at": os.environ.get("BUILD_TIME") or "",
    "mode": os.environ.get("UPDATE_MODE") or "",
    "products": len(products),
    "selected_total": status.get("selected_count"),
    "up5": (snapshot.get("overview") or {}).get("up5"),
    "down5": (snapshot.get("overview") or {}).get("down5"),
    "latest_daily": latest_daily,
    "latest_hourly": latest_hourly,
    "min30_products": len(min30_codes),
    "fetch_errors": len(errors),
    "fetch_error_sample": errors[:5],
    "contract_errors": len(status.get("contract_errors") or []),
    "run_id": os.environ.get("GITHUB_RUN_ID") or "",
    "run_number": os.environ.get("GITHUB_RUN_NUMBER") or "",
    "event": os.environ.get("GITHUB_EVENT_NAME") or "",
    "trigger": {"schedule": "定时任务", "push": "手动推送", "workflow_dispatch": "手动触发"}.get(
        os.environ.get("GITHUB_EVENT_NAME") or "", os.environ.get("GITHUB_EVENT_NAME") or ""
    ),
    "schedule": {
        "timezone": "Asia/Shanghai",
        "weekdays": "周一至周五",
        "intraday": ["10:05", "11:20", "14:20", "15:05", "22:05", "23:05"],
        "daily": "17:05",
        "driver": "ticker.yml 自续调度链（GITHUB_TOKEN 触发 workflow_dispatch）",
    },
}

out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

lines = [
    "### 本次构建",
    "",
    f"- 数据快照：`{info['generated_at']}`",
    f"- 抓取完成：`{info['built_at']}`",
    f"- 触发方式：{info['trigger']}（event = `{info['event']}`）",
    f"- 定时计划：{info['schedule']['weekdays']} {info['schedule']['timezone']} "
    + " / ".join(info['schedule']['intraday'])
    + f"（{info['schedule']['daily']} 更新日线）",
    f"- 品种覆盖：{info['products']} / {info['selected_total']}",
    f"- 日线最新：`{info['latest_daily']}`；60 分钟最新：`{info['latest_hourly']}`",
    f"- 30 分钟数据覆盖：{info['min30_products']} 个品种",
    f"- 抓取异常：{info['fetch_errors']} 条；主力合约识别失败：{info['contract_errors']} 个",
]
if errors:
    lines += ["", "```"] + [json.dumps(e, ensure_ascii=False) for e in errors[:5]] + ["```"]
print("\n".join(lines))
print("\n" + json.dumps(info, ensure_ascii=False, indent=2))
