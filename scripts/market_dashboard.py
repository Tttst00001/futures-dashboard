import argparse
import functools
import http.server
import json
import math
import socketserver
from pathlib import Path

from market_config import MARKET_DATA_DIR, PRODUCTS, STEEL_CODES, WEB_DIR
from market_data import build_raw_snapshot, update_market
from market_indicators import MIN_WEEKS, chart_bars, cross_bar, directions, kdj, last_cross, rsi, weekly_rows


def clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def mean(values):
    values = [x for x in values if x is not None]
    return sum(values) / len(values) if values else 0.0


def change(rows, bars):
    if len(rows) <= bars:
        return 0.0
    base = rows[-bars - 1]["close"]
    return (rows[-1]["close"] / base - 1) * 100 if base else 0.0


def ema(values, period):
    if not values:
        return None
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def atr14(rows):
    if len(rows) < 2:
        return 0.0
    trs = []
    for i, row in enumerate(rows[1:], 1):
        pc = rows[i - 1]["close"]
        trs.append(max(row["high"] - row["low"], abs(row["high"] - pc), abs(row["low"] - pc)))
    period = min(14, len(trs))
    value = mean(trs[:period])
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def adx14(rows, period=14):
    if len(rows) < period * 2 + 1:
        return {"adx": 0.0, "plus_di": 0.0, "minus_di": 0.0}

    plus_dm, minus_dm, tr = [], [], []
    for i, row in enumerate(rows[1:], 1):
        previous = rows[i - 1]
        up = row["high"] - previous["high"]
        down = previous["low"] - row["low"]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(row["high"] - row["low"], abs(row["high"] - previous["close"]), abs(row["low"] - previous["close"])))

    smoothed_plus = sum(plus_dm[:period])
    smoothed_minus = sum(minus_dm[:period])
    smoothed_tr = sum(tr[:period])
    dx_values = []

    def directional_index(smoothed_movement):
        return 100.0 * smoothed_movement / smoothed_tr if smoothed_tr else 0.0

    for i in range(period, len(tr)):
        if i > period:
            smoothed_plus = smoothed_plus - smoothed_plus / period + plus_dm[i - 1]
            smoothed_minus = smoothed_minus - smoothed_minus / period + minus_dm[i - 1]
            smoothed_tr = smoothed_tr - smoothed_tr / period + tr[i - 1]

        plus_di = directional_index(smoothed_plus)
        minus_di = directional_index(smoothed_minus)
        total = plus_di + minus_di
        dx_values.append(100.0 * abs(plus_di - minus_di) / total if total else 0.0)

    adx = mean(dx_values[:period])
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period

    return {
        "adx": adx,
        "plus_di": directional_index(smoothed_plus),
        "minus_di": directional_index(smoothed_minus),
    }


FACTOR_NAMES = ["mom21", "mom63", "mom12_1", "atr_mom21", "donchian55", "oi_confirm", "direction_score"]

# 量价分析 SOP：阈值固定，改规则必须同时升级版本号并更新 docs/量价分析SOP.md
VP_SOP_VERSION = "VP-1.0"
VP_VOLUME_WINDOW = 20   # 量比基准：前 20 日均量（不含当日）
VP_BAND_DRY = 0.8       # <0.8 缩量
VP_BAND_WARM = 1.2      # 0.8–1.2 常量，1.2–1.8 温和放量
VP_BAND_HEAVY = 1.8     # >1.8 巨量
VP_OI_DAY = 1.0         # 当日持仓变化阈值 ±1%
VP_OI_5D = 3.0          # 5 日持仓变化阈值 ±3%
VP_RATIO_HOT = 2.0      # 量仓比过热线


def volume_grade(ratio):
    if ratio is None:
        return "无数据"
    if ratio < VP_BAND_DRY:
        return "缩量"
    if ratio < VP_BAND_WARM:
        return "常量"
    if ratio < VP_BAND_HEAVY:
        return "温和放量"
    return "巨量"


def vp_analysis(daily):
    """量价分析（SOP VP-1.0）：量比 × 持仓变化 × 价格方向 → 状态与动作。

    状态判定优先级固定：缩量 > 换手平仓 > 价涨仓减 > 趋势启动 > 中性。
    """
    if not daily or len(daily) < VP_VOLUME_WINDOW + 2 or not daily[-1].get("volume"):
        return None
    last = daily[-1]
    prev = daily[-2]
    base = [row["volume"] for row in daily[-VP_VOLUME_WINDOW - 1:-1] if row.get("volume")]
    if not base:
        return None
    avg_volume = mean(base)
    ratio = last["volume"] / avg_volume if avg_volume else None
    grade = volume_grade(ratio)

    hold = last.get("hold")
    hold_prev = prev.get("hold")
    oi_day = (hold / hold_prev - 1) * 100 if hold and hold_prev else None
    hold_5 = daily[-6].get("hold") if len(daily) > 6 else None
    oi_5d = (hold / hold_5 - 1) * 100 if hold and hold_5 else None
    price_day = (last["close"] / prev["close"] - 1) * 100 if prev["close"] else 0.0
    price_5d = (last["close"] / daily[-6]["close"] - 1) * 100 if len(daily) > 6 and daily[-6]["close"] else 0.0
    volume_oi = last["volume"] / hold if hold else None
    same_way = (price_day > 0) == (price_5d > 0)

    if ratio is not None and ratio < VP_BAND_DRY:
        state, state_text, action, level = "C", "缩量观望", "不做，等量能恢复", "flat"
    elif ratio is not None and ratio >= VP_BAND_HEAVY and oi_day is not None and oi_day <= -VP_OI_DAY:
        state, state_text, action, level = "B", "换手 / 平仓", "不追，等回踩确认", "warn"
    elif oi_5d is not None and oi_5d <= -VP_OI_5D and price_5d > 0:
        state, state_text, action, level = "D", "价涨仓减（背离）", "减仓，警惕获利了结", "warn"
    elif ratio is not None and ratio >= VP_BAND_WARM and oi_day is not None and oi_day >= VP_OI_DAY and same_way:
        state, state_text, action, level = "A", "新钱进场", "可按砖图方向进场", "up"
    else:
        state, state_text, action, level = "E", "中性", "按砖图执行，量能无加分", "flat"

    checks = [
        {"label": "量能档位",
         "value": f"{ratio:.2f}" if ratio is not None else "—",
         "rule": "1.2–1.8 温和放量为准入；<0.8 否决；>1.8 禁追",
         "pass": ratio is not None and VP_BAND_WARM <= ratio < VP_BAND_HEAVY},
        {"label": "持仓·当日",
         "value": f"{oi_day:+.1f}%" if oi_day is not None else "—",
         "rule": "≥ +1% 增仓（新钱）；≤ −1% 减仓",
         "pass": oi_day is not None and oi_day >= VP_OI_DAY},
        {"label": "持仓·5日",
         "value": f"{oi_5d:+.1f}%" if oi_5d is not None else "—",
         "rule": "≤ −3% 视为持续减仓（获利了结）",
         "pass": oi_5d is not None and oi_5d > -VP_OI_5D},
        {"label": "量仓比",
         "value": f"{volume_oi:.2f}" if volume_oi is not None else "—",
         "rule": "< 2.0 换手正常；≥ 2.0 过热",
         "pass": volume_oi is not None and volume_oi < VP_RATIO_HOT},
        {"label": "价仓同向",
         "value": "是" if same_way else "否",
         "rule": "当日与 5 日价格同向，才允许判定趋势",
         "pass": same_way},
    ]

    series = []
    for i in range(max(VP_VOLUME_WINDOW, len(daily) - 10), len(daily)):
        row = daily[i]
        window = [x["volume"] for x in daily[i - VP_VOLUME_WINDOW:i] if x.get("volume")]
        window_mean = mean(window) if window else 0
        row_ratio = row["volume"] / window_mean if window_mean else None
        row_hold = row.get("hold")
        prev_hold = daily[i - 1].get("hold") if i else None
        row_oi = (row_hold / prev_hold - 1) * 100 if row_hold and prev_hold else None
        series.append([row["date"], row["volume"], row_ratio, row_hold, row_oi])

    return {
        "version": VP_SOP_VERSION,
        "asof": last["date"],
        "volume": {"value": last["volume"], "avg": avg_volume, "ratio": ratio, "grade": grade},
        "oi": {"value": hold, "day": oi_day, "five": oi_5d},
        "price": {"day": price_day, "five": price_5d},
        "volume_oi": volume_oi,
        "state": state,
        "state_text": state_text,
        "action": action,
        "level": level,
        "checks": checks,
        "series": series,
        "thresholds": {
            "window": VP_VOLUME_WINDOW, "dry": VP_BAND_DRY, "warm": VP_BAND_WARM,
            "heavy": VP_BAND_HEAVY, "oi_day": VP_OI_DAY, "oi_5d": VP_OI_5D, "ratio_hot": VP_RATIO_HOT,
        },
    }


def factor_metrics(rows):
    mom21 = change(rows, 21)
    mom63 = change(rows, 63)
    mom12_1 = 0.0
    if len(rows) > 252 and rows[-253]["close"]:
        mom12_1 = (rows[-22]["close"] / rows[-253]["close"] - 1) * 100

    window = rows[-55:]
    high = max(row["high"] for row in window)
    low = min(row["low"] for row in window)
    donchian55 = (rows[-1]["close"] - low) / (high - low) * 100 if high != low else 50.0

    oi_confirm = 0.0
    if len(rows) > 21 and rows[-21].get("hold") and rows[-1].get("hold"):
        direction = 1 if mom21 > 0 else -1 if mom21 < 0 else 0
        oi_confirm = direction * math.log(rows[-1]["hold"] / rows[-21]["hold"])

    atr_pct = atr14(rows) / rows[-1]["close"] * 100 if rows[-1]["close"] else 0.0
    dmi = adx14(rows)
    return {
        "mom21": mom21,
        "mom63": mom63,
        "mom12_1": mom12_1,
        "atr_mom21": mom21 / atr_pct if atr_pct else 0.0,
        "donchian55": donchian55,
        "oi_confirm": oi_confirm,
        "direction_score": dmi["plus_di"] - dmi["minus_di"],
        "adx": dmi["adx"],
        "plus_di": dmi["plus_di"],
        "minus_di": dmi["minus_di"],
    }


def volume_price_metrics(rows):
    if len(rows) < 41:
        return {"obv20": 0.0, "mfi14": 50.0, "amount_change": 0.0, "volume_price_divergence": 0.0}

    obv = 0.0
    obv_series = [0.0]
    for i, row in enumerate(rows[1:], 1):
        if row["close"] > rows[i - 1]["close"]:
            obv += row["volume"]
        elif row["close"] < rows[i - 1]["close"]:
            obv -= row["volume"]
        obv_series.append(obv)

    average_volume = mean([row["volume"] for row in rows[-20:]])
    obv20 = (obv_series[-1] - obv_series[-21]) / average_volume if average_volume else 0.0

    positive_flow = 0.0
    negative_flow = 0.0
    for i in range(len(rows) - 14, len(rows)):
        current = rows[i]
        previous = rows[i - 1]
        current_tp = (current["high"] + current["low"] + current["close"]) / 3
        previous_tp = (previous["high"] + previous["low"] + previous["close"]) / 3
        money_flow = current_tp * current["volume"]
        if current_tp > previous_tp:
            positive_flow += money_flow
        elif current_tp < previous_tp:
            negative_flow += money_flow

    if positive_flow and negative_flow:
        mfi14 = 100.0 - 100.0 / (1.0 + positive_flow / negative_flow)
    elif positive_flow:
        mfi14 = 100.0
    else:
        mfi14 = 0.0

    amounts = [row["close"] * row["volume"] for row in rows]
    recent_amount = mean(amounts[-20:])
    previous_amount = mean(amounts[-40:-20])
    amount_change = math.log(recent_amount / previous_amount) if recent_amount > 0 and previous_amount > 0 else 0.0

    recent_volume = mean([row["volume"] for row in rows[-20:]])
    previous_volume = mean([row["volume"] for row in rows[-40:-20]])
    volume_direction = 1 if change(rows, 20) > 0 else -1 if change(rows, 20) < 0 else 0
    volume_price_divergence = volume_direction * math.log(recent_volume / previous_volume) if recent_volume > 0 and previous_volume > 0 else 0.0

    return {
        "obv20": obv20,
        "mfi14": mfi14,
        "amount_change": amount_change,
        "volume_price_divergence": volume_price_divergence,
    }


def add_factor_ranks(products):
    for product in products:
        product.setdefault("ranks", {})
    for name in FACTOR_NAMES:
        ordered = sorted(products, key=lambda x: x["factors"][name])
        total = max(len(ordered) - 1, 1)
        for rank, product in enumerate(ordered):
            product["ranks"][name] = rank / total * 100
    for product in products:
        product["factor_score"] = mean(product["ranks"][name] for name in FACTOR_NAMES)


VOLUME_PRICE_FACTORS = ["obv20", "mfi14", "amount_change", "volume_price_divergence"]


def add_volume_price_ranks(products):
    for product in products:
        product.setdefault("vp_ranks", {})
    for name in VOLUME_PRICE_FACTORS:
        ordered = sorted(products, key=lambda x: x["vp_factors"][name])
        total = max(len(ordered) - 1, 1)
        for rank, product in enumerate(ordered):
            product["vp_ranks"][name] = rank / total * 100
    for product in products:
        product["vp_score"] = mean(product["vp_ranks"][name] for name in VOLUME_PRICE_FACTORS)


def tail_flags(series, count=5):
    flags = directions(series)
    return flags[-count:] if flags else None


def tail_volumes(rows, series, count=5):
    return [rows[point["i"]]["volume"] for point in series[-count:]]


def signal_metrics(item):
    daily = item["main_daily"]
    hourly = item["main_hourly"]
    kdj_daily = kdj(daily)
    kdj_hourly = kdj(hourly)
    kdj_min30 = kdj(item["main_min30"])
    weekly = weekly_rows(daily)
    kdj_weekly = kdj(weekly) if len(weekly) >= MIN_WEEKS else []
    cross_index = cross_bar(daily, kdj_daily)
    signal_close = daily[cross_index]["close"] if cross_index is not None else None
    latest_hourly = hourly[-1]["close"] if hourly else None
    gain = None
    if cross_index is not None and cross_index + 1 < len(daily) and daily[cross_index + 1]["close"]:
        gain = (daily[-1]["close"] / daily[cross_index + 1]["close"] - 1) * 100
    rsi_hourly = rsi(hourly)
    rsi_min30 = rsi(item["main_min30"])
    daily_flags = directions(kdj_daily)
    hourly_flags = tail_flags(kdj_hourly) or []
    last_daily = daily[-1] if daily else None
    avg_volume21 = mean([row["volume"] for row in daily[-21:]]) if daily else None
    volume_oi_ratio = None
    if last_daily and last_daily["volume"] and last_daily.get("hold"):
        volume_oi_ratio = last_daily["volume"] / last_daily["hold"]
    return {
        "contract": item["main_contract"],
        "bars_daily": len(daily),
        "bars_weekly": len(weekly),
        "avg_volume21": avg_volume21,
        "volume_oi_ratio": volume_oi_ratio,
        "dots_daily": tail_flags(kdj_daily),
        "dots_hourly": tail_flags(kdj_hourly),
        "dots_min30": tail_flags(kdj_min30),
        "dots_weekly": tail_flags(kdj_weekly),
        "vol_daily": tail_volumes(daily, kdj_daily),
        "vol_hourly": tail_volumes(hourly, kdj_hourly),
        "vol_min30": tail_volumes(item["main_min30"], kdj_min30),
        "vol_weekly": tail_volumes(weekly, kdj_weekly),
        "chart_daily": chart_bars(daily, kdj_daily, rsi(daily)),
        "chart_hourly": chart_bars(hourly, kdj_hourly, rsi_hourly),
        "chart_min30": chart_bars(item["main_min30"], kdj_min30, rsi_min30),
        "direction": daily_flags[-1] if daily_flags else None,
        "kdj": kdj_daily[-1] if kdj_daily else None,
        "cross_days": last_cross(kdj_daily),
        "cross_bars_hourly": last_cross(kdj_hourly),
        "hourly_flipped": len(set(hourly_flags[-4:])) > 1,
        "rsi_hourly": rsi_hourly[-1]["RSI"] if rsi_hourly else None,
        "signal_close": signal_close,
        "latest_hourly": latest_hourly,
        "spread": None if signal_close is None or latest_hourly is None else latest_hourly - signal_close,
        "gain_after_cross": gain,
        "ema55": ema([row["close"] for row in daily], 55),
        "vp": vp_analysis(daily),
    }


def analyze_product(item):
    daily = item["daily"]
    hourly = item["hourly"]
    last = daily[-1]
    series = kdj(daily)
    kdj_now = series[-1]
    ema55 = ema([x["close"] for x in daily], 55) or last["close"]
    volume_base = [x["volume"] for x in daily[-21:-1] if x.get("volume") is not None]
    volume_ratio = last["volume"] / mean(volume_base) if mean(volume_base) else 1.0
    oi_change_5d = 0.0
    if len(daily) > 6 and last.get("hold") and daily[-6].get("hold"):
        oi_change_5d = (last["hold"] / daily[-6]["hold"] - 1) * 100
    daily_changes = {n: change(daily, n) for n in (1, 5, 20, 60)}
    hourly_changes = {n: change(hourly, n) for n in (1, 5, 20)}
    atr_pct = atr14(daily) / last["close"] * 100 if last["close"] else 0.0
    trend_score = (
        0.45 * clamp(daily_changes[20] / 10.0)
        + 0.25 * clamp(hourly_changes[20] / 5.0)
        + 0.15 * clamp((kdj_now["K"] - kdj_now["D"]) / 30.0)
        + 0.15 * clamp((last["close"] / ema55 - 1) / 5.0)
    )
    raw_heat = (
        0.28 * min(abs(daily_changes[1]) / 3.0, 1)
        + 0.32 * min(abs(daily_changes[5]) / 8.0, 1)
        + 0.18 * min(max(volume_ratio - 1, 0) / 2.0, 1)
        + 0.12 * min(atr_pct / 4.0, 1)
        + 0.10 * abs(trend_score)
    ) * 100
    return {
        **{k: item[k] for k in ("code", "name", "symbol", "family", "main_contract")},
        "close": last["close"],
        "latest_daily": last["date"],
        "latest_hourly": hourly[-1]["date"] if hourly else None,
        "daily_change": daily_changes,
        "hourly_change": hourly_changes,
        "volume_ratio": volume_ratio,
        "oi_change_5d": oi_change_5d,
        "atr_pct": atr_pct,
        "signal": signal_metrics(item),
        "ema55": ema55,
        "ema_state": "多头" if last["close"] >= ema55 else "空头",
        "trend_score": trend_score,
        "factors": factor_metrics(daily),
        "ranks": {},
        "factor_score": 0.0,
        "vp_factors": volume_price_metrics(daily),
        "vp_ranks": {},
        "vp_score": 0.0,
        "raw_heat": raw_heat,
        "heat": 0,
        "daily_series": [[x["date"], x["close"]] for x in daily],
        "hourly_series": [[x["date"], x["close"]] for x in hourly],
    }


def percentile_ranks(products):
    ordered = sorted(products, key=lambda x: x["raw_heat"])
    n = len(ordered)
    for rank, product in enumerate(ordered):
        product["heat"] = rank / (n - 1) * 100 if n > 1 else 50


def family_summaries(products):
    summaries = []
    for family in dict.fromkeys(x["family"] for x in products):
        members = [x for x in products if x["family"] == family]
        up = sum(1 for x in members if x["daily_change"][5] > 0)
        down = sum(1 for x in members if x["daily_change"][5] < 0)
        factor_rank = mean(x["factor_score"] for x in members)
        summaries.append({
            "family": family,
            "count": len(members),
            "members": len(members),
            "up5": up,
            "down5": down,
            "bias": mean(x["trend_score"] for x in members),
            "factor_rank": factor_rank,
            "vp_rank": mean(x["vp_score"] for x in members),
            "components": {name: mean(x["ranks"][name] for x in members) for name in FACTOR_NAMES},
            "avg_heat": mean(x["heat"] for x in members),
            "best": max(members, key=lambda x: x["factor_score"])["name"],
            "worst": min(members, key=lambda x: x["factor_score"])["name"],
        })
    return sorted(summaries, key=lambda x: x["factor_rank"], reverse=True)


def pearson(left, right):
    pairs = [(a, b) for a, b in zip(left, right) if a is not None and b is not None]
    if len(pairs) < 3:
        return 0.0
    xs, ys = zip(*pairs)
    mx, my = mean(xs), mean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in pairs)
    denominator = math.sqrt(sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    return numerator / denominator if denominator else 0.0


def steel_analysis(products):
    by_code = {x["code"]: x for x in products}
    codes = [x for x in STEEL_CODES if x in by_code]
    maps = {code: {date: close for date, close in by_code[code]["daily_series"]} for code in codes}
    returns = {}
    for code in codes:
        dates = list(maps[code])
        returns[code] = {
            date: maps[code][date] / maps[code][previous] - 1
            for previous, date in zip(dates, dates[1:])
            if maps[code][previous]
        }
    common_dates = sorted(set.intersection(*(set(maps[code]) for code in codes))) if codes else []
    common_dates = common_dates[-60:]
    correlation = []
    for a in codes:
        row = []
        for b in codes:
            dates = sorted(set(returns[a]) & set(returns[b]))[-90:]
            row.append(pearson([returns[a][d] for d in dates], [returns[b][d] for d in dates]))
        correlation.append(row)
    normalized = {code: [] for code in codes}
    for date in common_dates:
        base = maps[code][common_dates[0]]
        for code in codes:
            normalized[code].append([date, maps[code][date] / base * 100])
    return {
        "codes": codes,
        "correlation": correlation,
        "dates": common_dates,
        "normalized": normalized,
    }


def sanitize_status(status):
    errors = [{k: v for k, v in item.items() if k != "path"} for item in status.get("errors", [])]
    entries = {}
    for symbol, periods in status.get("entries", {}).items():
        entries[symbol] = {
            period: {k: v for k, v in info.items() if k != "path"}
            for period, info in periods.items()
        }
    return {
        "source": status.get("source", "新浪财经"),
        "generated_at": status.get("generated_at"),
        "selected_count": status.get("selected_count"),
        "contract_errors": status.get("contract_errors", []),
        "errors": errors,
        "entries": entries,
    }


def build_analysis(raw):
    products = [analyze_product(item) for item in raw["products"]]
    percentile_ranks(products)
    add_factor_ranks(products)
    add_volume_price_ranks(products)
    up5 = sum(1 for x in products if x["daily_change"][5] > 0)
    down5 = sum(1 for x in products if x["daily_change"][5] < 0)
    return {
        "generated_at": raw["generated_at"],
        "source": raw["source"],
        "note": "KDJ/RSI 信号按主力日期合约计算，截面因子与相关性按连续合约计算；换月会造成连续价格跳空。",
        "overview": {
            "count": len(products),
            "total": len(PRODUCTS),
            "up5": up5,
            "down5": down5,
            "flat5": len(products) - up5 - down5,
            "avg_heat": mean(x["heat"] for x in products),
            "avg_bias": mean(x["trend_score"] for x in products),
        },
        "products": products,
        "families": family_summaries(products),
        "steel": steel_analysis(products),
        "missing": raw["missing"],
        "status": sanitize_status(raw["status"]),
    }


def render_dashboard(analysis, output_path):
    template_path = Path(__file__).resolve().parent.parent / "web" / "dashboard_template.html"
    template = template_path.read_text(encoding="utf-8")
    payload = json.dumps(analysis, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("__DATA_JSON__", payload).replace("__GENERATED_AT__", analysis["generated_at"])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def serve(directory, port):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    class ReusableServer(socketserver.TCPServer):
        allow_reuse_address = True
    with ReusableServer(("0.0.0.0", port), handler) as httpd:
        print(f"看板已启动: http://127.0.0.1:{port}")
        httpd.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="生成期货产业链热度 HTML 看板")
    parser.add_argument("--symbols", help="只生成指定品种，逗号分隔")
    parser.add_argument("--force", action="store_true", help="强制重新抓取")
    parser.add_argument("--no-fetch", action="store_true", help="只用本地缓存")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", default=str(WEB_DIR / "index.html"))
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()] if args.symbols else None
    selected = [p for p in PRODUCTS if symbols is None or p["symbol"] in symbols or p["code"] in symbols]
    if not args.no_fetch:
        manifest, _ = update_market(symbols, args.force, args.workers)
        identified = [p for p in selected if manifest.get("main_contracts", {}).get(p["code"])]
        print(f"主力合约识别 {len(identified)}/{len(selected)}"
              + (f"，失败 {len(manifest['contract_errors'])} 个" if manifest.get("contract_errors") else ""))
    raw = build_raw_snapshot(selected)
    if not raw["products"]:
        raise SystemExit("没有可用本地数据，请先运行 py scripts/market_dashboard.py --force")
    analysis = build_analysis(raw)
    output = render_dashboard(analysis, args.output)
    MARKET_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (MARKET_DATA_DIR / "snapshot.json").write_text(json.dumps(analysis, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"看板已生成: {output}")
    print(f"品种 {analysis['overview']['count']} 个；5日上涨 {analysis['overview']['up5']}，下跌 {analysis['overview']['down5']}")
    if args.serve:
        serve(Path(args.output).parent, args.port)


if __name__ == "__main__":
    main()
