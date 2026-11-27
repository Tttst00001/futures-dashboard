import datetime as dt
import math
import sys

import numpy as np

from MyTT import KDJ, RSI

KDJ_N = 21
KDJ_M1 = 21
KDJ_M2 = 21
RSI_N = 10
MIN_WEEKS = 25


def _bar(date, open_, high, low, close, volume, hold=None):
    return {"date": date, "open": open_, "high": high, "low": low, "close": close, "volume": volume, "hold": hold}


def _closes_to_rows(closes, spread=0.0):
    rows = []
    for i, close in enumerate(closes):
        date = (dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat()
        rows.append(_bar(date, close, close + spread, close - spread, close, 1.0))
    return rows


def kdj(rows, n=KDJ_N, m1=KDJ_M1, m2=KDJ_M2):
    """KDJ_TDX 口径：RSV 走 SMA(RSV,m1,1)、K 走 SMA(K,m2,1)。返回 [{i,date,K,D,J}]，跳过 RSV 未成熟的根。"""
    if len(rows) < n:
        return []
    close = np.array([row["close"] for row in rows], dtype=float)
    high = np.array([row["high"] for row in rows], dtype=float)
    low = np.array([row["low"] for row in rows], dtype=float)
    k, d, j = KDJ(close, high, low, n, m1, m2)
    out = []
    for i, row in enumerate(rows):
        if not (math.isfinite(k[i]) and math.isfinite(d[i]) and math.isfinite(j[i])):
            continue
        out.append({"i": i, "date": row["date"], "K": float(k[i]), "D": float(d[i]), "J": float(j[i])})
    return out


def rsi(rows, n=RSI_N):
    if len(rows) < n + 1:
        return []
    close = np.array([row["close"] for row in rows], dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        values = RSI(close, n)
    out = []
    for i, row in enumerate(rows):
        value = values[i]
        if not math.isfinite(value):
            continue
        out.append({"i": i, "date": row["date"], "RSI": float(value)})
    return out


def directions(series):
    """每根 K 与 D 的强弱：K>D 为 1（多），否则 0（空）。"""
    return [1 if point["K"] > point["D"] else 0 for point in series]


def last_cross(series):
    """最近一次 K/D 穿越发生在倒数第几根，0 表示最新这根刚穿越；从未穿越返回 None。"""
    flags = directions(series)
    for i in range(len(flags) - 1, 0, -1):
        if flags[i] != flags[i - 1]:
            return len(flags) - 1 - i
    return None


def cross_bar(rows, series):
    """最近一次交叉那根对应的原始 K 线下标，没有交叉返回 None。"""
    bars_ago = last_cross(series)
    if bars_ago is None:
        return None
    return series[len(series) - 1 - bars_ago]["i"]


def weekly_rows(daily_rows):
    """日线按 ISO 自然周合成：周一开、周内高低、周五收、成交量累加、持仓取周末值。"""
    groups = {}
    for row in daily_rows:
        day = dt.date.fromisoformat(row["date"][:10])
        groups.setdefault(day.isocalendar()[:2], []).append(row)
    out = []
    for key in sorted(groups):
        days = groups[key]
        out.append(_bar(
            days[-1]["date"],
            days[0]["open"],
            max(day["high"] for day in days),
            min(day["low"] for day in days),
            days[-1]["close"],
            sum(day["volume"] for day in days),
            days[-1].get("hold"),
        ))
    return out


CHART_BARS = 80


def chart_bars(rows, series, rsi_series, count=CHART_BARS):
    """弹窗 K 线用：每根 [时间,开,高,低,收,量,K,D,J,RSI]，只输出 KDJ 已成熟的根。数值保留 2 位以压缩页面体积。"""
    rsi_by_index = {point["i"]: point["RSI"] for point in rsi_series}
    out = []
    for point in series[-count:]:
        row = rows[point["i"]]
        rsi_value = rsi_by_index.get(point["i"])
        out.append([
            row["date"][:16], round(row["open"], 2), round(row["high"], 2), round(row["low"], 2),
            round(row["close"], 2), round(row["volume"], 2),
            round(point["K"], 2), round(point["D"], 2), round(point["J"], 2),
            None if rsi_value is None else round(rsi_value, 2),
        ])
    return out


def dots(rows, count=5):
    """最近 count根的多空点阵（1=K>D），KDJ 成熟根数不够时返回已有根数；不足 2 根返回 None。"""
    series = kdj(rows)
    if len(series) < 2:
        return None
    return directions(series)[-count:]
