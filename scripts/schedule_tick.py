"""自续调度：睡到下一个更新时刻，用 workflow_dispatch 触发一次构建，再把下一棒交给自己。

GitHub 的 schedule 事件只承诺“尽力而为”，高负载会延迟甚至直接丢弃（本仓库实测 0 次生效），
这里改成由仓库自带 GITHUB_TOKEN 主动 dispatch（GitHub 明确允许 token 触发 workflow_dispatch）。
每个 job 最多睡 5 小时，剩下的时间用“交棒”串起来，避免 6 小时上限把链条掐断。
"""
import datetime as dt
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

# 更新时间（北京时间，周一至周五）：盘中 30/60 分钟线 6 次 + 收盘后日线 1 次
SLOTS = ["10:05", "11:20", "14:20", "15:05", "17:05", "22:05", "23:05"]

MAX_CHUNK = 5 * 60 * 60   # 单次最长睡眠 5 小时，job 上限 350 分钟
MIN_LEAD = 40             # 距离目标时刻不足 40 秒就顺延到下一档，避免醒来刚好错过
BUILD_WORKFLOW = "pages.yml"
TICK_WORKFLOW = "ticker.yml"


def log(msg):
    print(f"[{dt.datetime.now(TZ):%Y-%m-%d %H:%M:%S} CST] {msg}", flush=True)


def next_slot(now):
    """返回下一个更新时刻；周末不排，最多向后找 9 天。"""
    for d in range(9):
        day = (now + dt.timedelta(days=d)).date()
        if day.weekday() >= 5:
            continue
        for slot in SLOTS:
            hh, mm = (int(x) for x in slot.split(":"))
            at = dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=TZ)
            if at > now + dt.timedelta(seconds=MIN_LEAD):
                return at
    return None


def dispatch(workflow):
    repo = os.environ.get("GITHUB_REPOSITORY") or "Tttst00001/futures-dashboard"
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    env = {**os.environ, "GH_TOKEN": token}
    for attempt in range(1, 4):
        try:
            subprocess.run(
                ["gh", "workflow", "run", workflow, "--ref", "main", "--repo", repo],
                check=True, env=env, timeout=120,
            )
            log(f"已触发 {workflow}（第 {attempt} 次尝试）")
            return
        except Exception as exc:  # noqa: BLE001
            log(f"触发 {workflow} 失败（第 {attempt} 次）：{exc}")
            time.sleep(20 * attempt)
    raise SystemExit(f"无法触发 {workflow}，调度链中断")


def main():
    now = dt.datetime.now(TZ)
    target = next_slot(now)
    if target is None:
        log("没有可用的更新时刻，退出")
        return 1
    wait = (target - now).total_seconds()
    log(f"下一档更新：{target:%Y-%m-%d %H:%M}（等待 {wait / 3600:.2f} 小时）")

    if wait <= MAX_CHUNK:
        time.sleep(wait)
        log("到点，触发构建")
        dispatch(BUILD_WORKFLOW)
        log("交棒给下一轮调度")
        dispatch(TICK_WORKFLOW)
    else:
        log(f"离开下一档还早，先睡 {MAX_CHUNK / 3600:.0f} 小时后交棒")
        time.sleep(MAX_CHUNK)
        dispatch(TICK_WORKFLOW)
    return 0


if __name__ == "__main__":
    sys.exit(main())
