import argparse
import concurrent.futures
import datetime as dt
import json
import os
import re
import tempfile
import time
import urllib.request
from pathlib import Path

from market_config import DAILY_DIR, HOURLY_DIR, MAIN_MONTH_WINDOW, MARKET_DATA_DIR, MIN30_DIR, PRODUCTS

SINA_DAILY = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_data_=/InnerFuturesNewService.getDailyKLine?symbol={symbol}"
SINA_MIN60 = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_data_=/InnerFuturesNewService.getFewMinLine?symbol={symbol}&type=60"
SINA_MIN30 = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_data_=/InnerFuturesNewService.getFewMinLine?symbol={symbol}&type=30"

TTL_BY_PERIOD = {"daily": 6 * 3600, "60": 30 * 60, "30": 30 * 60}


def daily_is_fresh(path):
    """日线一天一次：今天的 bar 已入库，或现在还没收盘（昨天的就是最新），或今天已经抓过。"""
    rows = load_json(path, [])
    if not rows:
        return False
    if rows[-1]["date"] == dt.date.today().isoformat():
        return True
    if dt.datetime.now().hour < 16:
        return True
    return path.stat().st_mtime >= time.mktime(dt.date.today().timetuple())


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fetch_sina(symbol, period, retries=2):
    if period == "daily":
        api = SINA_DAILY
    elif period == "30":
        api = SINA_MIN30
    else:
        api = SINA_MIN60
    url = api.format(symbol=symbol)
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                text = resp.read().decode("utf-8", "ignore")
            match = re.search(r"\((\[.*\])\)", text, re.S)
            if not match:
                raise RuntimeError("新浪响应格式异常")
            rows = json.loads(match.group(1))
            return normalize_rows(rows, period)
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(str(last_error))


def normalize_rows(rows, period):
    out = []
    for row in rows:
        try:
            date = str(row["d"])
            if period == "daily":
                date = date[:10]
            out.append({
                "date": date,
                "open": float(row["o"]),
                "high": float(row["h"]),
                "low": float(row["l"]),
                "close": float(row["c"]),
                "volume": float(row["v"]),
                "hold": float(row["p"]) if row.get("p") not in (None, "") else None,
                "settle": float(row["s"]) if row.get("s") not in (None, "") else None,
            })
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda x: x["date"])
    return out


def candidate_symbols(code, ref_date, window=MAIN_MONTH_WINDOW):
    out = []
    for offset in range(window):
        total = ref_date.month - 1 + offset
        out.append(f"{code}{(ref_date.year + total // 12) % 100:02d}{total % 12 + 1:02d}")
    return out


def pick_by_volume(rows_by_symbol):
    """主力 = 候选里最新一日成交量最大的合约。不用多日累计：旧合约在换月期靠清仓刷量，累计窗口会把它顶成主力。"""
    best = None
    best_volume = -1.0
    for symbol, rows in rows_by_symbol.items():
        if not rows:
            continue
        volume = rows[-1]["volume"]
        if volume > best_volume:
            best, best_volume = symbol, volume
    return best


def discover_main_contract(product, ref_date, window=MAIN_MONTH_WINDOW):
    rows_by_symbol = {}
    failures = []
    for symbol in candidate_symbols(product["code"], ref_date, window):
        try:
            rows_by_symbol[symbol] = fetch_sina(symbol, "daily")
        except Exception as exc:
            failures.append(f"{symbol}:{exc}")
    best = pick_by_volume(rows_by_symbol)
    if best is None:
        raise RuntimeError(" ".join(failures) or "没有可用候选合约")
    return best


def update_main_contracts(products=None, force=False, max_workers=4, ref_date=None):
    path = MARKET_DATA_DIR / "main_contract.json"
    cache = load_json(path, {}) or {}
    contracts = dict(cache.get("contracts") or {})
    errors = []
    if force or not contracts or not is_fresh(path, 6 * 3600):
        ref_date = ref_date or dt.date.today()
        selected = PRODUCTS if products is None else products
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(discover_main_contract, p, ref_date): p for p in selected}
            for future in concurrent.futures.as_completed(futures):
                product = futures[future]
                try:
                    contracts[product["code"]] = future.result()
                except Exception as exc:
                    errors.append({"code": product["code"], "error": str(exc)})
        atomic_json(path, {"generated_at": now_iso(), "ref_date": str(ref_date), "contracts": contracts})
    return contracts, errors


def cache_path(symbol, period):
    directory = {"daily": DAILY_DIR, "60": HOURLY_DIR, "30": MIN30_DIR}[period]
    return directory / f"{symbol}.json"


def merge_rows(existing, incoming):
    merged = {}
    for row in existing or []:
        merged[row["date"]] = row
    for row in incoming:
        merged[row["date"]] = row
    return [merged[key] for key in sorted(merged)]


def is_fresh(path, ttl_seconds):
    return path.exists() and time.time() - path.stat().st_mtime < ttl_seconds


def update_one(symbol, period, force=False):
    path = cache_path(symbol, period)
    fresh = daily_is_fresh(path) if period == "daily" else is_fresh(path, TTL_BY_PERIOD[period])
    if not force and fresh:
        rows = load_json(path, [])
        return {"symbol": symbol, "period": period, "status": "fresh", "rows": len(rows), "path": str(path)}
    try:
        incoming = fetch_sina(symbol, period)
        existing = load_json(path, [])
        rows = merge_rows(existing, incoming)
        atomic_json(path, rows)
        return {
            "symbol": symbol, "period": period, "status": "updated", "rows": len(rows),
            "latest": rows[-1]["date"] if rows else None, "path": str(path),
        }
    except Exception as exc:
        rows = load_json(path, [])
        return {
            "symbol": symbol, "period": period, "status": "stale" if rows else "error",
            "rows": len(rows), "error": str(exc), "path": str(path),
        }


def update_market(symbols=None, force=False, max_workers=4):
    selected = [p for p in PRODUCTS if symbols is None or p["symbol"] in symbols or p["code"] in symbols]
    missing = set(symbols or []) - {p["symbol"] for p in selected} - {p["code"] for p in selected}
    if missing:
        raise ValueError("未知品种: " + ", ".join(sorted(missing)))
    MARKET_DATA_DIR.mkdir(parents=True, exist_ok=True)
    contracts, contract_errors = update_main_contracts(selected, force, max_workers)
    jobs = []
    for product in selected:
        jobs.append((product["symbol"], "daily"))
        jobs.append((product["symbol"], "60"))
        main = contracts.get(product["code"])
        if main:
            jobs.extend([(main, "daily"), (main, "60"), (main, "30")])
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(update_one, symbol, period, force) for symbol, period in jobs]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    manifest = load_json(MARKET_DATA_DIR / "manifest.json", {"entries": {}, "errors": []})
    entries = manifest.setdefault("entries", {})
    errors = []
    for result in results:
        entry = entries.setdefault(result["symbol"], {})
        period = {"60": "hourly", "30": "min30"}.get(result["period"], result["period"])
        entry[period] = {k: v for k, v in result.items() if k not in ("symbol", "period")}
        if result["status"] in ("stale", "error"):
            errors.append(result)
    manifest.update({
        "source": "新浪财经",
        "generated_at": now_iso(),
        "selected_count": len(selected),
        "main_contracts": contracts,
        "contract_errors": contract_errors,
        "errors": errors,
    })
    atomic_json(MARKET_DATA_DIR / "manifest.json", manifest)
    return manifest, selected


def tail(symbol, period, bars):
    if not symbol:
        return []
    return load_json(cache_path(symbol, period), [])[-bars:]


def build_raw_snapshot(products, daily_bars=300, hourly_bars=180, main_daily_bars=196, main_intraday_bars=1023):
    manifest = load_json(MARKET_DATA_DIR / "manifest.json", {})
    contracts = manifest.get("main_contracts") or (load_json(MARKET_DATA_DIR / "main_contract.json", {}) or {}).get("contracts", {})
    items = []
    missing = []
    for product in products:
        daily = load_json(cache_path(product["symbol"], "daily"), [])
        if not daily:
            missing.append(product["symbol"])
            continue
        main = contracts.get(product["code"])
        items.append({
            **product,
            "main_contract": main,
            "daily": daily[-daily_bars:],
            "hourly": load_json(cache_path(product["symbol"], "60"), [])[-hourly_bars:],
            "main_daily": tail(main, "daily", main_daily_bars),
            "main_hourly": tail(main, "60", main_intraday_bars),
            "main_min30": tail(main, "30", main_intraday_bars),
        })
    return {
        "generated_at": now_iso(),
        "source": "新浪财经",
        "continuous_contract": True,
        "products": items,
        "missing": missing,
        "status": manifest,
    }


def main():
    parser = argparse.ArgumentParser(description="抓取期货主力合约与连续合约的日线、60分钟、30分钟数据")
    parser.add_argument("--symbols", help="逗号分隔的品种代码或新浪代码，如 RB,I0,IF0")
    parser.add_argument("--force", action="store_true", help="忽略缓存强制更新")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()] if args.symbols else None
    manifest, selected = update_market(symbols, args.force, args.workers)
    errors = manifest.get("errors", [])
    identified = [p for p in selected if manifest.get("main_contracts", {}).get(p["code"])]
    print(f"更新完成: {len(selected)} 个品种, 主力合约 {len(identified)} 个, 数据任务失败 {len(errors)} 个")
    if manifest.get("contract_errors"):
        print("主力合约识别失败（沿用上一次结果）:")
        for error in manifest["contract_errors"]:
            print(f"  {error['code']}: {error['error']}")
    if errors:
        print("以下数据保留了旧缓存或抓取失败:")
        for error in errors:
            print(f"  {error['symbol']} {error['period']}: {error.get('error', error['status'])}")


if __name__ == "__main__":
    main()
