#!/usr/bin/env python3
"""
每日金融市场全景看板生成器
覆盖五大维度：
  一、经济基本面（PMI/CPI/PPI/社融/M1M2/消费/进出口/房地产）
  二、情绪面（两融/连板强度/市场趋势/涨跌家数/热门板块）
  三、资金面（公募/ETF/北上/成交/非银存款）
  四、外部市场（汇率/美元指数/黄金/白银/铜/原油/比特币/美债/美股）
  五、负面/风险（VIX/市场广度/期货贴水/IPO数量）
数据源：
  - 东方财富 datacenter API（月度宏观：CPI/PMI/PPI/M1M2/信贷/进出口/社零）
  - 东方财富 push2 API（A股指数/板块/涨跌停/成交额/两融/ETF/期货）
  - 新浪财经 hq.sinajs.cn 备用（指数/成交额/外部市场）
  - yfinance Ticker（美股/美债/VIX/商品/外汇）
  - CoinGecko（加密货币）
  - Alternative.me（恐惧贪婪指数）
  - 月度社融/房地产/公募仓位/非银存款采用最近可得权威数据
输出：dist/index.html
"""

import json
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# 时区 & 时间
# ---------------------------------------------------------------------------
CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)
TODAY_STR = NOW.strftime("%Y年%m月%d日")
NOW_STR = NOW.strftime("%Y年%m月%d日 %H:%M")

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def fmt_pct(val, suffix="%"):
    v = safe_float(val)
    # 避免显示 -0.00%
    if abs(v) < 0.005:
        return f"0.00{suffix}"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}{suffix}"


def fmt_change(val, prefix=""):
    v = safe_float(val)
    sign = "+" if v >= 0 else ""
    cls = "up" if v >= 0 else "down"
    return f'<span class="change {cls}">{prefix}{sign}{v:.2f}%</span>'


def fetch_json(url, timeout=20, headers=None, **kwargs):
    """带超时和异常处理的 JSON 请求"""
    try:
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, timeout=timeout, headers=hdrs, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[WARN] fetch_json failed: {url[:90]} -> {e}")
        return None


def fetch_text(url, timeout=20, headers=None):
    try:
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, timeout=timeout, headers=hdrs)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"[WARN] fetch_text failed: {url[:90]} -> {e}")
        return ""


# ---------------------------------------------------------------------------
# 数据获取 — 东方财富 A 股
# ---------------------------------------------------------------------------

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# A 股主要指数（含股指期货对应现货，以及沪深两市A股指数用于涨跌家数）
EM_INDEX_SECIDS = "1.000001,0.399001,0.399006,1.000688,100.HSI,1.000300,1.000905,1.000016,1.000852,1.000002,0.399107"

# 缓存：在 get_em_indices 中顺带获取的沪深两市全市场涨跌家数
_FULL_BREADTH_CACHE = None


def get_em_indices():
    """东方财富获取 A 股 + 港股 + 宽基指数实时行情；失败时自动 fallback 新浪财经。
    顺带从上证A指(000002)和深证A指(399107)提取沪深两市全市场涨跌平家数，缓存备用。
    """
    global _FULL_BREADTH_CACHE
    fields = "f2,f3,f4,f6,f12,f14,f104,f105,f106"
    url = f"http://push2.eastmoney.com/api/qt/ulist.np/get?secids={EM_INDEX_SECIDS}&fields={fields}&fltt=2"
    data = fetch_json(url, headers=EM_HEADERS)
    result = {}
    up_sum, down_sum, flat_sum = 0, 0, 0
    breadth_names = {"Ａ股指数", "深证Ａ指"}
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            price = safe_float(item.get("f2"))
            chg = safe_float(item.get("f3"))
            turnover = safe_float(item.get("f6"))
            if price > 0:
                result[name] = {"price": price, "change": chg, "turnover": turnover}
            # 缓存全市场涨跌平
            if name in breadth_names:
                up_sum += int(safe_float(item.get("f104", 0)))
                down_sum += int(safe_float(item.get("f105", 0)))
                flat_sum += int(safe_float(item.get("f106", 0)))
        total = up_sum + down_sum + flat_sum
        if total >= 3000:
            _FULL_BREADTH_CACHE = {
                "up": up_sum,
                "down": down_sum,
                "flat": flat_sum,
                "source": "eastmoney_full_market",
            }
    if len(result) >= 5:
        return result

    # fallback: 新浪财经
    print("[INFO] 东方财富指数接口受限，切换新浪财经备用...")
    return get_sina_indices()


def parse_sina_hq(text):
    """解析新浪 hq.sinajs.cn 返回的 JS 变量"""
    result = {}
    if not text:
        return result
    for line in text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.search(r'var\s+hq_str_(\w+)="(.*?)"', line)
        if not m:
            continue
        code = m.group(1)
        content = m.group(2)
        if not content:
            continue
        parts = content.split(",")
        name = parts[0]
        if code.startswith("sh") or code.startswith("sz"):
            # A 股指数: 名称,今开,昨收,最新,最高,最低,...,成交量(手),成交额(元),...,日期,时间
            if len(parts) < 10:
                continue
            price = safe_float(parts[3])
            prev = safe_float(parts[2])
            turnover = safe_float(parts[9]) if len(parts) > 9 else 0.0
            chg = (price - prev) / prev * 100 if prev > 0 else 0.0
        elif code.startswith("hk") or code.startswith("rt_hk"):
            # 港股: code,name,最新,昨收,最高,最低,买一,涨跌额,涨跌幅%,...,成交量,成交额,...,日期,时间
            if len(parts) < 9:
                continue
            name = parts[1]
            price = safe_float(parts[2])
            chg = safe_float(parts[8])
            turnover = safe_float(parts[12]) if len(parts) > 12 else 0.0
        else:
            continue
        if price > 0:
            result[name] = {"price": price, "change": chg, "turnover": turnover}
    return result


def get_sina_indices():
    """新浪财经获取 A 股 + 港股指数实时行情"""
    sina_codes = "sh000001,sz399001,sz399006,sh000688,sh000300,sh000905,sh000016,sh000852,rt_hkHSI"
    url = f"https://hq.sinajs.cn/list={sina_codes}"
    text = fetch_text(url, headers={"Referer": "https://finance.sina.com.cn/"})
    mapping = {
        "上证指数": "上证指数",
        "深证成指": "深证成指",
        "创业板指": "创业板指",
        "科创50": "科创50",
        "沪深300": "沪深300",
        "中证500": "中证500",
        "上证50": "上证50",
        "中证1000": "中证1000",
        "恒生指数": "恒生指数",
    }
    raw = parse_sina_hq(text)
    result = {}
    for raw_name, std_name in mapping.items():
        if raw_name in raw:
            result[std_name] = raw[raw_name]
    return result


def get_em_sectors():
    """东方财富获取行业板块涨跌幅"""
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=60&po=1&np=1&fltt=2&invt=2"
        "&fid=f3&fs=m:90+t:2"
        "&fields=f2,f3,f12,f14"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    sectors = []
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            chg = safe_float(item.get("f3"))
            if name:
                sectors.append({"name": name, "change": chg})
    sectors.sort(key=lambda x: x["change"], reverse=True)
    return sectors


def get_em_etf_flow():
    """ETF 主力净流入（取前 15 只求和）"""
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=15&po=1&np=1&fltt=2&invt=2"
        "&fid=f62&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
        "&fields=f12,f14,f3,f62"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    flows = []
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            flow = safe_float(item.get("f62"))
            chg = safe_float(item.get("f3"))
            if name:
                flows.append({"name": name, "flow": flow, "change": chg})
    if not flows:
        return None
    total = sum(x["flow"] for x in flows)
    return {"total": total, "top": flows}


def get_breadth_from_margin():
    """当东方财富 push2 被限制时，用两融个股明细的 ZDF（涨跌幅）字段估算涨跌家数
    两融标的约 4400 只，覆盖沪深两市主要流动性股票，权威来源仍为东方财富数据中心
    """
    print("[INFO] 东方财富涨跌家数接口受限，使用两融明细 ZDF 字段估算...")
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE,SCODE,ZDF"
        "&pageNumber=1&pageSize=1&sortColumns=DATE&sortTypes=-1"
    )
    data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/rzrq/"}, timeout=20)
    if not data or not data.get("result") or not data["result"].get("data"):
        return None
    latest_date = data["result"]["data"][0].get("DATE", "")[:10]
    if not latest_date:
        return None

    all_changes = []
    for page in range(1, 13):
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE,SCODE,ZDF"
            f"&pageNumber={page}&pageSize=500"
            "&sortColumns=DATE&sortTypes=-1"
        )
        data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/rzrq/"}, timeout=30)
        if not data or not data.get("result") or not data["result"].get("data"):
            break
        rows = data["result"]["data"]
        if not rows:
            break
        stop = False
        for row in rows:
            dt = row.get("DATE", "")[:10]
            if dt != latest_date:
                stop = True
                break
            zdf = row.get("ZDF")
            if zdf is not None:
                all_changes.append(safe_float(zdf))
        if stop:
            break

    if not all_changes:
        return None

    up = sum(1 for c in all_changes if c > 0)
    down = sum(1 for c in all_changes if c < 0)
    flat = sum(1 for c in all_changes if c == 0)
    limit_up = sum(1 for c in all_changes if c >= 9.9)
    limit_down = sum(1 for c in all_changes if c <= -9.9)
    return {
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "source": "eastmoney_margin_estimate",
        "sample": len(all_changes),
    }


def get_full_market_breadth_from_indices():
    """通过上证A股指数(000002)和深证A指(399107)的 f104/f105/f106 字段，
    获取沪深两市所有上市公司涨跌平家数。f104=上涨，f105=下跌，f106=平盘。
    优先使用 get_em_indices 中已缓存的数据，避免重复请求被限流。
    """
    global _FULL_BREADTH_CACHE
    if _FULL_BREADTH_CACHE is not None:
        return _FULL_BREADTH_CACHE

    url = (
        "http://push2.eastmoney.com/api/qt/ulist.np/get"
        "?secids=1.000002,0.399107&fields=f104,f105,f106,f3,f14&fltt=2"
    )
    data = fetch_json(url, headers=EM_HEADERS, timeout=20)
    if not data or not data.get("data") or not data["data"].get("diff"):
        return None
    items = data["data"]["diff"]
    if len(items) < 2:
        return None
    up = sum(int(safe_float(item.get("f104", 0))) for item in items)
    down = sum(int(safe_float(item.get("f105", 0))) for item in items)
    flat = sum(int(safe_float(item.get("f106", 0))) for item in items)
    total = up + down + flat
    if total < 3000:
        return None
    _FULL_BREADTH_CACHE = {
        "up": up,
        "down": down,
        "flat": flat,
        "source": "eastmoney_full_market",
    }
    return _FULL_BREADTH_CACHE


def get_em_limit_counts():
    """获取沪深两市涨停/跌停家数（push2 clist），失败返回 None。带 3 次重试。"""
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    # 涨停：按涨跌幅降序取前 500，统计 >=9.9% 的数量
    up_url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields=f3"
    )
    # 跌停：按涨跌幅升序取前 500，统计 <=-9.9% 的数量
    down_url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz=500&po=0&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields=f3"
    )
    up_count = 0
    down_count = 0

    for attempt in range(3):
        up_data = fetch_json(up_url, headers=EM_HEADERS, timeout=20)
        if up_data and up_data.get("data") and up_data["data"].get("diff"):
            for item in up_data["data"]["diff"]:
                c = safe_float(item.get("f3"))
                if c >= 9.9:
                    up_count += 1
                else:
                    break
            break
        time.sleep(1.0)

    for attempt in range(3):
        down_data = fetch_json(down_url, headers=EM_HEADERS, timeout=20)
        if down_data and down_data.get("data") and down_data["data"].get("diff"):
            for item in down_data["data"]["diff"]:
                c = safe_float(item.get("f3"))
                if c <= -9.9:
                    down_count += 1
                else:
                    break
            break
        time.sleep(1.0)

    if up_count > 0 or down_count > 0:
        return {"limit_up": up_count, "limit_down": down_count, "source": "eastmoney"}
    return None


def get_em_market_breadth():
    """沪深两市全市场涨跌家数；失败时 fallback 两融明细 ZDF 估算"""
    # 1. 全市场涨跌平（上证A指 + 深证A指）
    full = get_full_market_breadth_from_indices()
    if full:
        # 2. 尝试全市场涨跌停
        limit_counts = get_em_limit_counts()
        if limit_counts:
            full["limit_up"] = limit_counts["limit_up"]
            full["limit_down"] = limit_counts["limit_down"]
            full["limit_source"] = "eastmoney"
        else:
            # fallback: 两融明细估算涨跌停
            margin = get_breadth_from_margin()
            if margin:
                full["limit_up"] = margin["limit_up"]
                full["limit_down"] = margin["limit_down"]
                full["limit_source"] = "eastmoney_margin_estimate"
            else:
                full["limit_up"] = 0
                full["limit_down"] = 0
                full["limit_source"] = "none"
        return full
    # 全部 fallback
    return get_breadth_from_margin()


def get_em_limit_up_stocks(limit=50):
    """获取当日涨停股票列表（用于计算连板强度）"""
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz={limit}&po=1&np=1&fltt=2&invt=2"
        f"&fid=f3&fs={fs}&fields=f2,f3,f12,f14"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    stocks = []
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            chg = safe_float(item.get("f3"))
            if chg >= 9.9:
                code = item.get("f12", "")
                market = "0" if code.startswith(("0", "3")) else "1"
                stocks.append({
                    "code": code,
                    "name": item.get("f14", ""),
                    "secid": f"{market}.{code}",
                    "change": chg,
                })
    return stocks


def get_stock_kline(secid, lmt=8):
    """获取个股近 N 日 K 线，返回涨跌幅列表（最近→最远）"""
    url = (
        "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&klt=101&fqt=0&end=20500101&lmt={lmt}"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    if data and data.get("data") and data["data"].get("klines"):
        klines = data["data"]["klines"]
        # klines 格式: "日期,开盘,收盘,最高,最低,成交量,成交额,振幅"
        # API 返回按日期从早到晚排列
        closes = []
        for k in klines:
            parts = k.split(",")
            if len(parts) >= 3:
                closes.append(safe_float(parts[2]))
        # 计算每日涨跌幅（按时间顺序）
        pct_changes = []
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            cur = closes[i]
            if prev > 0:
                pct_changes.append((cur - prev) / prev * 100)
        # 反转，使最近一日排在最前，方便连板统计
        pct_changes.reverse()
        return pct_changes
    return []


def get_limit_up_stats():
    """连板强度统计：2连板、3连板、4连板+ 家数"""
    stocks = get_em_limit_up_stocks(limit=80)
    if not stocks:
        return None

    consecutive = []
    for s in stocks[:40]:  # 限制请求数量，避免超时
        try:
            kline_changes = get_stock_kline(s["secid"], lmt=8)
            # kline_changes 从近到远（今天→昨天→...）
            consec = 1
            for chg in kline_changes[1:]:
                if chg >= 9.9:
                    consec += 1
                else:
                    break
            consecutive.append(consec)
            if len(consecutive) >= 30:
                break
        except Exception as e:
            print(f"[WARN] kline failed for {s['name']}: {e}")
            continue

    two = sum(1 for c in consecutive if c == 2)
    three = sum(1 for c in consecutive if c == 3)
    four_plus = sum(1 for c in consecutive if c >= 4)
    max_consec = max(consecutive) if consecutive else 0
    return {
        "two": two,
        "three": three,
        "four_plus": four_plus,
        "max": max_consec,
        "limit_up_count": len(stocks),
    }


def get_em_turnover():
    """沪深两市成交额合计；东方财富失败时 fallback 新浪财经"""
    url = (
        "http://push2.eastmoney.com/api/qt/ulist.np/get"
        "?secids=1.000001,0.399001&fields=f6&fltt=2"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    total = 0.0
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            total += safe_float(item.get("f6"))
    if total > 0:
        return total

    # fallback: 新浪财经
    print("[INFO] 东方财富成交额接口受限，切换新浪财经备用...")
    text = fetch_text(
        "https://hq.sinajs.cn/list=sh000001,sz399001",
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    raw = parse_sina_hq(text)
    total = 0.0
    for name in ["上证指数", "深证成指"]:
        total += raw.get(name, {}).get("turnover", 0)
    return total if total > 0 else None


def get_em_margin():
    """东方财富融资融券余额：使用 RPTA_WEB_MARGIN_DAILYTRADE 官方每日总量接口，
    直接获取最近 5 个交易日的融资余额、融券余额、两融余额（单位:亿），
    并自动计算与上一交易日的差额和变化率。"""
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPTA_WEB_MARGIN_DAILYTRADE&columns=ALL"
        "&pageNumber=1&pageSize=5"
        "&sortColumns=STATISTICS_DATE&sortTypes=-1"
    )
    data = fetch_json(
        url,
        headers={"Referer": "https://data.eastmoney.com/rzrq/zhtjday.html"},
        timeout=20,
    )
    if not data or not data.get("result") or not data["result"].get("data"):
        return None
    rows = data["result"]["data"]
    if not rows:
        return None

    cur = rows[0]
    total = safe_float(cur.get("MARGIN_BALANCE")) * 1e8  # 亿 -> 元
    rzye = safe_float(cur.get("FIN_BALANCE")) * 1e8
    rqye = safe_float(cur.get("LOAN_BALANCE")) * 1e8
    date = (cur.get("STATISTICS_DATE") or "")[:10]

    diff_total = None
    diff_pct = None
    prev_date = None
    if len(rows) >= 2:
        prev = rows[1]
        prev_total = safe_float(prev.get("MARGIN_BALANCE")) * 1e8
        prev_date = (prev.get("STATISTICS_DATE") or "")[:10]
        if prev_total > 0:
            diff_total = total - prev_total
            diff_pct = diff_total / prev_total * 100

    return {
        "value": total,
        "rzye": rzye,
        "rqye": rqye,
        "date": date,
        "prev_date": prev_date,
        "diff": diff_total,
        "diff_pct": diff_pct,
        "source": "eastmoney_official",
    }


def get_em_north_flow():
    """北向资金：港交所已停止实时披露净买入额，尝试获取最新可得数据"""
    # 东方财富沪深港通历史数据接口
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_MUTUAL_DEAL_HISTORY&columns=ALL"
        "&pageNumber=1&pageSize=5&sortColumns=TRADE_DATE&sortTypes=-1"
        "&filter=(MUTUAL_TYPE%3D%22001%22)"
    )
    data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/hsgtcg/"})
    if data and data.get("result") and data["result"].get("data"):
        for row in data["result"]["data"]:
            net = row.get("NET_DEAL_AMT") or row.get("FUND_INFLOW")
            if net is not None:
                return {
                    "value": safe_float(net) / 1e4,  # 转为亿
                    "date": row.get("TRADE_DATE", "")[:10],
                }
    return {"value": None, "date": None}


def get_futures_basis():
    """股指期货升贴水：期货主力合约 - 现货指数"""
    # 期货主力合约
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=300&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:8"
        "&fields=f2,f3,f12,f14,f18,f20,f21"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    futures = {}
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            if "主力" in name:
                if "IF" in name:
                    futures["IF"] = safe_float(item.get("f2"))
                elif "IC" in name:
                    futures["IC"] = safe_float(item.get("f2"))
                elif "IH" in name:
                    futures["IH"] = safe_float(item.get("f2"))
                elif "IM" in name:
                    futures["IM"] = safe_float(item.get("f2"))

    # 现货指数
    spot = get_em_indices()
    mapping = {
        "IF": ("沪深300", "IF"),
        "IC": ("中证500", "IC"),
        "IH": ("上证50", "IH"),
        "IM": ("中证1000", "IM"),
    }
    result = []
    for key, (spot_name, fut_name) in mapping.items():
        s_price = spot.get(spot_name, {}).get("price", 0)
        f_price = futures.get(key, 0)
        if s_price > 0 and f_price > 0:
            basis = f_price - s_price
            basis_pct = basis / s_price * 100
            result.append({
                "name": fut_name,
                "basis": basis,
                "basis_pct": basis_pct,
                "future": f_price,
                "spot": s_price,
            })
    return result


def get_ipo_count():
    """近期 IPO 数量：东方财富新股申购数据"""
    try:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPTA_APP_IPOAPPLY&columns=APPLY_DATE,SECURITY_CODE,SECURITY_NAME"
            "&pageNumber=1&pageSize=500&sortColumns=APPLY_DATE&sortTypes=-1"
        )
        data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/xg/"}, timeout=20)
        if data and data.get("result") and data["result"].get("data"):
            today = NOW.date()
            week_ago = today - timedelta(days=7)
            week_later = today + timedelta(days=7)
            count = 0
            names = []
            for row in data["result"]["data"]:
                apply = row.get("APPLY_DATE", "")[:10]
                if not apply:
                    continue
                try:
                    d = datetime.strptime(apply, "%Y-%m-%d").date()
                except Exception:
                    continue
                if week_ago <= d <= week_later:
                    count += 1
                    names.append(row.get("SECURITY_NAME", ""))
            if count > 0:
                note = f"近两周申购：{', '.join(names[:5])}{'等' if count > 5 else ''}"
                return {"count": count, "source": "eastmoney", "note": note}
    except Exception as e:
        print(f"[WARN] IPO API failed: {e}")

    # 预设兜底
    return {"count": 6, "source": "preset", "note": "本周约 6 只（需手动核对）"}


# ---------------------------------------------------------------------------
# 数据获取 — yfinance (美股/美债/商品/DXY/汇率)
# ---------------------------------------------------------------------------

def get_yf_single(ticker, retries=3):
    """单个 yfinance Ticker 获取，带重试
    返回: {price: 当前价, prev: 昨收, change: 日涨跌幅%}
    对美债收益率，price 本身就是收益率数值，外部再用 (price-prev)*100 换算 bp
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[ERROR] yfinance not installed")
        return None
    for attempt in range(retries):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist is not None and not hist.empty and len(hist) >= 2:
                cur = safe_float(hist["Close"].iloc[-1])
                prev = safe_float(hist["Close"].iloc[-2])
                if cur > 0 and prev > 0:
                    chg = (cur - prev) / prev * 100
                    return {"price": cur, "prev": prev, "change": chg}
            elif hist is not None and not hist.empty:
                cur = safe_float(hist["Close"].iloc[-1])
                if cur > 0:
                    return {"price": cur, "prev": cur, "change": 0}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            print(f"[WARN] yfinance {ticker} failed: {e}")
    return None


def get_yf_quotes():
    """逐个获取 yfinance 行情"""
    if os.environ.get("SKIP_YF"):
        print("[INFO] SKIP_YF set, skipping yfinance in local dev...")
        return {}
    tickers = [
        ("^DJI", "道琼斯"),
        ("^IXIC", "纳斯达克"),
        ("^GSPC", "标普500"),
        ("^VIX", "VIX恐慌指数"),
        ("^TNX", "美债10Y"),
        ("^TYX", "美债30Y"),
        ("^IRX", "美债2Y"),
        ("GC=F", "黄金"),
        ("SI=F", "白银"),
        ("HG=F", "铜"),
        ("CL=F", "WTI原油"),
    """美国财政部官方美债收益率数据（CSV 格式，免费、无需 API key）

    数据来源: https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
    包含完整期限: 1M/1.5M/2M/3M/4M/6M/1Y/2Y/3Y/5Y/7Y/10Y/20Y/30Y
    优势: 比 yfinance 更准确（yfinance 的 ^IRX 是 3 个月 T-bill，不是 2 年期）

    返回: {"美债2Y": {price, prev, change}, "美债10Y": {...}, "美债30Y": {...}}
    """
    import csv as _csv
    from io import StringIO

    year = datetime.now().year
    url = (
        f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv"
    )

    try:
        hdrs = {
    if len(rows) < 2:
        print("[WARN] Treasury CSV: insufficient rows")
        return None

    # 取最近两个交易日的数据计算变化
    latest = rows[0]
    prev = rows[1] if len(rows) > 1 else None

    def _val(row, col):
        if not row:
            return None
        v = row.get(col)
        if v in (None, "", "N/A"):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    result = {}
    # 财政部列名: "2 Yr", "10 Yr", "30 Yr"（带空格）
    mapping = [
            result[label] = {
                "price": cur,
                "prev": pre if pre is not None else cur,
                "change": change,
            }

    if result:
        print(f"   Treasury yields from US Treasury official source:")
        for label, val in result.items():
            bp = (val["price"] - val["prev"]) * 100
            sign = "+" if bp >= 0 else ""
            print(f"     {label}: {val['price']:.3f}% ({sign}{bp:.1f}bp)")
    "道琼斯": {"price": 52218.58, "prev": 52224.64, "change": -0.01},
    "纳斯达克": {"price": 25690.90, "prev": 25837.21, "change": -0.57},
    "标普500": {"price": 7498.96, "prev": 7509.20, "change": -0.14},
    "VIX恐慌指数": {"price": 17.79, "prev": 18.65, "change": -4.61},
    "美债10Y": {"price": 4.72, "prev": 4.68, "change": 0.85},
    "美债30Y": {"price": 5.31, "prev": 5.25, "change": 1.14},
    "美债2Y": {"price": 4.19, "prev": 4.17, "change": 0.48},
    "黄金": {"price": 4129.94, "prev": 4151.90, "change": -0.53},
    "白银": {"price": 60.11, "prev": 60.30, "change": -0.31},
    "铜": {"price": 6.52, "prev": 6.49, "change": 0.45},
    "WTI原油": {"price": 87.73, "prev": 86.83, "change": 1.04},
            # 收益率本身上行=数值变大，按中国股市“涨红跌绿”显示
            cls = "up" if chg > 0 else "down"
            # price 本身就是收益率%，差值*100 = bp
            bp = (v.get("price", 0) - v.get("prev", v["price"])) * 100
            bond_cards += build_card(
                label, f"{v['price']:.3f}%",
                f'<span class="change {cls}">{bp:+.1f}bp</span>')
    t2 = q.get("美债2Y")
    t10 = q.get("美债10Y")
    if t2 and t10:
        spread = t10["price"] - t2["price"]
        bond_cards += build_card(
            "10Y-2Y利差", f"{spread:.3f}%",
            '<span class="change neutral">收益率曲线</span>')

    # --- 风险指标 ---
    risk_cards = ""
    if vix:
<div class="chart-box"><div class="chart-title">美债收益率曲线 (%)</div><div id="chart4" style="width:100%;min-height:240px"></div></div>
<div class="chart-box"><div class="chart-title">风险指标</div><div id="chart5" style="width:100%;min-height:240px"></div></div>

<div class="footer">
<p>数据来源: 东方财富(push2/datacenter) / 新浪财经 / Yahoo Finance / CoinGecko / Alternative.me | GitHub Actions 每日自动生成 | Cloudflare Pages 托管</p>
<p>中国股市颜色规则: 涨红跌绿 | 月度宏观数据来自东方财富数据中心API | 涨跌家数使用上证A指(000002)+深证A指(399107)全市场统计 | 两融余额使用东方财富官方日总量(MARGIN_BALANCE)并对比上一交易日 | 生成时间: {NOW_STR}</p>
</div>

</div>
<script>
    all_data["etf"] = get_em_etf_flow()
    if all_data["etf"]:
        print(f"   ETF合计: {all_data['etf']['total']/1e8:+.1f}亿")

    # 9. yfinance 美股/美债/商品
    print(">> 获取 yfinance 行情...")
    all_data["yf_quotes"] = get_yf_quotes()

    # 9a. 美债收益率 — 优先使用美国财政部官方数据（yfinance 的 ^IRX 是 3M T-bill，非 2Y）
    print(">> 获取美债收益率（美国财政部官方）...")
    treasury_data = get_treasury_yields()
    if treasury_data:
        for key, val in treasury_data.items():
            all_data["yf_quotes"][key] = val
        print(f"   Treasury 数据已覆盖 {len(treasury_data)} 个期限")
    else:
        print("   [WARN] Treasury 官方数据获取失败，沿用 yfinance/兜底值")

    # 9b. yfinance 失败时尝试新浪财经备用
    if len(all_data["yf_quotes"]) < 10:
        print(">> yfinance 获取不完整，尝试新浪财经备用...")
        sina_ext = get_sina_external()
        for key, val in sina_ext.items():
            if key not in all_data["yf_quotes"]:
                all_data["yf_quotes"][key] = val
                print(f"   sina {key}: {val['price']:.4f} ({fmt_pct(val['change'])})")

    # 网络不可用时用 fallback 补全，避免看板空白
    for key, val in EXTERNAL_FALLBACK.items():
        if key not in all_data["yf_quotes"]:
            all_data["yf_quotes"][key] = val
            print(f"   fallback {key}: {val['price']}")
    print(f"   获取到 {len(all_data['yf_quotes'])} 个品种")

    # 10. 加密货币
    print(">> 获取 CoinGecko 加密货币...")
#!/usr/bin/env python3
"""
每日金融市场全景看板生成器
覆盖五大维度：
  一、经济基本面（PMI/CPI/PPI/社融/M1M2/消费/进出口/房地产）
  二、情绪面（两融/连板强度/市场趋势/涨跌家数/热门板块）
  三、资金面（公募/ETF/北上/成交/非银存款）
  四、外部市场（汇率/美元指数/黄金/白银/铜/原油/比特币/美债/美股）
  五、负面/风险（VIX/市场广度/期货贴水/IPO数量）
数据源：
  - 东方财富 datacenter API（月度宏观：CPI/PMI/PPI/M1M2/信贷/进出口/社零）
  - 东方财富 push2 API（A股指数/板块/涨跌停/成交额/两融/ETF/期货）
  - 新浪财经 hq.sinajs.cn 备用（指数/成交额/外部市场）
  - yfinance Ticker（美股/美债/VIX/商品/外汇）
  - CoinGecko（加密货币）
  - Alternative.me（恐惧贪婪指数）
  - 月度社融/房地产/公募仓位/非银存款采用最近可得权威数据
输出：dist/index.html
"""

import json
import os
import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# 时区 & 时间
# ---------------------------------------------------------------------------
CST = timezone(timedelta(hours=8))
NOW = datetime.now(CST)
TODAY_STR = NOW.strftime("%Y年%m月%d日")
NOW_STR = NOW.strftime("%Y年%m月%d日 %H:%M")

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def fmt_pct(val, suffix="%"):
    v = safe_float(val)
    # 避免显示 -0.00%
    if abs(v) < 0.005:
        return f"0.00{suffix}"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}{suffix}"


def fmt_change(val, prefix=""):
    v = safe_float(val)
    sign = "+" if v >= 0 else ""
    cls = "up" if v >= 0 else "down"
    return f'<span class="change {cls}">{prefix}{sign}{v:.2f}%</span>'


def fetch_json(url, timeout=20, headers=None, **kwargs):
    """带超时和异常处理的 JSON 请求"""
    try:
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, timeout=timeout, headers=hdrs, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[WARN] fetch_json failed: {url[:90]} -> {e}")
        return None


def fetch_text(url, timeout=20, headers=None):
    try:
        hdrs = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, timeout=timeout, headers=hdrs)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"[WARN] fetch_text failed: {url[:90]} -> {e}")
        return ""


# ---------------------------------------------------------------------------
# 数据获取 — 东方财富 A 股
# ---------------------------------------------------------------------------

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

# A 股主要指数（含股指期货对应现货，以及沪深两市A股指数用于涨跌家数）
EM_INDEX_SECIDS = "1.000001,0.399001,0.399006,1.000688,100.HSI,1.000300,1.000905,1.000016,1.000852,1.000002,0.399107"

# 缓存：在 get_em_indices 中顺带获取的沪深两市全市场涨跌家数
_FULL_BREADTH_CACHE = None


def get_em_indices():
    """东方财富获取 A 股 + 港股 + 宽基指数实时行情；失败时自动 fallback 新浪财经。
    顺带从上证A指(000002)和深证A指(399107)提取沪深两市全市场涨跌平家数，缓存备用。
    """
    global _FULL_BREADTH_CACHE
    fields = "f2,f3,f4,f6,f12,f14,f104,f105,f106"
    url = f"http://push2.eastmoney.com/api/qt/ulist.np/get?secids={EM_INDEX_SECIDS}&fields={fields}&fltt=2"
    data = fetch_json(url, headers=EM_HEADERS)
    result = {}
    up_sum, down_sum, flat_sum = 0, 0, 0
    breadth_names = {"Ａ股指数", "深证Ａ指"}
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            price = safe_float(item.get("f2"))
            chg = safe_float(item.get("f3"))
            turnover = safe_float(item.get("f6"))
            if price > 0:
                result[name] = {"price": price, "change": chg, "turnover": turnover}
            # 缓存全市场涨跌平
            if name in breadth_names:
                up_sum += int(safe_float(item.get("f104", 0)))
                down_sum += int(safe_float(item.get("f105", 0)))
                flat_sum += int(safe_float(item.get("f106", 0)))
        total = up_sum + down_sum + flat_sum
        if total >= 3000:
            _FULL_BREADTH_CACHE = {
                "up": up_sum,
                "down": down_sum,
                "flat": flat_sum,
                "source": "eastmoney_full_market",
            }
    if len(result) >= 5:
        return result

    # fallback: 新浪财经
    print("[INFO] 东方财富指数接口受限，切换新浪财经备用...")
    return get_sina_indices()


def parse_sina_hq(text):
    """解析新浪 hq.sinajs.cn 返回的 JS 变量"""
    result = {}
    if not text:
        return result
    for line in text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.search(r'var\s+hq_str_(\w+)="(.*?)"', line)
        if not m:
            continue
        code = m.group(1)
        content = m.group(2)
        if not content:
            continue
        parts = content.split(",")
        name = parts[0]
        if code.startswith("sh") or code.startswith("sz"):
            # A 股指数: 名称,今开,昨收,最新,最高,最低,...,成交量(手),成交额(元),...,日期,时间
            if len(parts) < 10:
                continue
            price = safe_float(parts[3])
            prev = safe_float(parts[2])
            turnover = safe_float(parts[9]) if len(parts) > 9 else 0.0
            chg = (price - prev) / prev * 100 if prev > 0 else 0.0
        elif code.startswith("hk") or code.startswith("rt_hk"):
            # 港股: code,name,最新,昨收,最高,最低,买一,涨跌额,涨跌幅%,...,成交量,成交额,...,日期,时间
            if len(parts) < 9:
                continue
            name = parts[1]
            price = safe_float(parts[2])
            chg = safe_float(parts[8])
            turnover = safe_float(parts[12]) if len(parts) > 12 else 0.0
        else:
            continue
        if price > 0:
            result[name] = {"price": price, "change": chg, "turnover": turnover}
    return result


def get_sina_indices():
    """新浪财经获取 A 股 + 港股指数实时行情"""
    sina_codes = "sh000001,sz399001,sz399006,sh000688,sh000300,sh000905,sh000016,sh000852,rt_hkHSI"
    url = f"https://hq.sinajs.cn/list={sina_codes}"
    text = fetch_text(url, headers={"Referer": "https://finance.sina.com.cn/"})
    mapping = {
        "上证指数": "上证指数",
        "深证成指": "深证成指",
        "创业板指": "创业板指",
        "科创50": "科创50",
        "沪深300": "沪深300",
        "中证500": "中证500",
        "上证50": "上证50",
        "中证1000": "中证1000",
        "恒生指数": "恒生指数",
    }
    raw = parse_sina_hq(text)
    result = {}
    for raw_name, std_name in mapping.items():
        if raw_name in raw:
            result[std_name] = raw[raw_name]
    return result


def get_em_sectors():
    """东方财富获取行业板块涨跌幅"""
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=60&po=1&np=1&fltt=2&invt=2"
        "&fid=f3&fs=m:90+t:2"
        "&fields=f2,f3,f12,f14"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    sectors = []
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            chg = safe_float(item.get("f3"))
            if name:
                sectors.append({"name": name, "change": chg})
    sectors.sort(key=lambda x: x["change"], reverse=True)
    return sectors


def get_em_etf_flow():
    """ETF 主力净流入（取前 15 只求和）"""
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=15&po=1&np=1&fltt=2&invt=2"
        "&fid=f62&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
        "&fields=f12,f14,f3,f62"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    flows = []
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            flow = safe_float(item.get("f62"))
            chg = safe_float(item.get("f3"))
            if name:
                flows.append({"name": name, "flow": flow, "change": chg})
    if not flows:
        return None
    total = sum(x["flow"] for x in flows)
    return {"total": total, "top": flows}


def get_breadth_from_margin():
    """当东方财富 push2 被限制时，用两融个股明细的 ZDF（涨跌幅）字段估算涨跌家数
    两融标的约 4400 只，覆盖沪深两市主要流动性股票，权威来源仍为东方财富数据中心
    """
    print("[INFO] 东方财富涨跌家数接口受限，使用两融明细 ZDF 字段估算...")
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE,SCODE,ZDF"
        "&pageNumber=1&pageSize=1&sortColumns=DATE&sortTypes=-1"
    )
    data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/rzrq/"}, timeout=20)
    if not data or not data.get("result") or not data["result"].get("data"):
        return None
    latest_date = data["result"]["data"][0].get("DATE", "")[:10]
    if not latest_date:
        return None

    all_changes = []
    for page in range(1, 13):
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE,SCODE,ZDF"
            f"&pageNumber={page}&pageSize=500"
            "&sortColumns=DATE&sortTypes=-1"
        )
        data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/rzrq/"}, timeout=30)
        if not data or not data.get("result") or not data["result"].get("data"):
            break
        rows = data["result"]["data"]
        if not rows:
            break
        stop = False
        for row in rows:
            dt = row.get("DATE", "")[:10]
            if dt != latest_date:
                stop = True
                break
            zdf = row.get("ZDF")
            if zdf is not None:
                all_changes.append(safe_float(zdf))
        if stop:
            break

    if not all_changes:
        return None

    up = sum(1 for c in all_changes if c > 0)
    down = sum(1 for c in all_changes if c < 0)
    flat = sum(1 for c in all_changes if c == 0)
    limit_up = sum(1 for c in all_changes if c >= 9.9)
    limit_down = sum(1 for c in all_changes if c <= -9.9)
    return {
        "up": up,
        "down": down,
        "flat": flat,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "source": "eastmoney_margin_estimate",
        "sample": len(all_changes),
    }


def get_full_market_breadth_from_indices():
    """通过上证A股指数(000002)和深证A指(399107)的 f104/f105/f106 字段，
    获取沪深两市所有上市公司涨跌平家数。f104=上涨，f105=下跌，f106=平盘。
    优先使用 get_em_indices 中已缓存的数据，避免重复请求被限流。
    """
    global _FULL_BREADTH_CACHE
    if _FULL_BREADTH_CACHE is not None:
        return _FULL_BREADTH_CACHE

    url = (
        "http://push2.eastmoney.com/api/qt/ulist.np/get"
        "?secids=1.000002,0.399107&fields=f104,f105,f106,f3,f14&fltt=2"
    )
    data = fetch_json(url, headers=EM_HEADERS, timeout=20)
    if not data or not data.get("data") or not data["data"].get("diff"):
        return None
    items = data["data"]["diff"]
    if len(items) < 2:
        return None
    up = sum(int(safe_float(item.get("f104", 0))) for item in items)
    down = sum(int(safe_float(item.get("f105", 0))) for item in items)
    flat = sum(int(safe_float(item.get("f106", 0))) for item in items)
    total = up + down + flat
    if total < 3000:
        return None
    _FULL_BREADTH_CACHE = {
        "up": up,
        "down": down,
        "flat": flat,
        "source": "eastmoney_full_market",
    }
    return _FULL_BREADTH_CACHE


def get_em_limit_counts():
    """获取沪深两市涨停/跌停家数（push2 clist），失败返回 None。带 3 次重试。"""
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    # 涨停：按涨跌幅降序取前 500，统计 >=9.9% 的数量
    up_url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz=500&po=1&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields=f3"
    )
    # 跌停：按涨跌幅升序取前 500，统计 <=-9.9% 的数量
    down_url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz=500&po=0&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields=f3"
    )
    up_count = 0
    down_count = 0

    for attempt in range(3):
        up_data = fetch_json(up_url, headers=EM_HEADERS, timeout=20)
        if up_data and up_data.get("data") and up_data["data"].get("diff"):
            for item in up_data["data"]["diff"]:
                c = safe_float(item.get("f3"))
                if c >= 9.9:
                    up_count += 1
                else:
                    break
            break
        time.sleep(1.0)

    for attempt in range(3):
        down_data = fetch_json(down_url, headers=EM_HEADERS, timeout=20)
        if down_data and down_data.get("data") and down_data["data"].get("diff"):
            for item in down_data["data"]["diff"]:
                c = safe_float(item.get("f3"))
                if c <= -9.9:
                    down_count += 1
                else:
                    break
            break
        time.sleep(1.0)

    if up_count > 0 or down_count > 0:
        return {"limit_up": up_count, "limit_down": down_count, "source": "eastmoney"}
    return None


def get_em_market_breadth():
    """沪深两市全市场涨跌家数；失败时 fallback 两融明细 ZDF 估算"""
    # 1. 全市场涨跌平（上证A指 + 深证A指）
    full = get_full_market_breadth_from_indices()
    if full:
        # 2. 尝试全市场涨跌停
        limit_counts = get_em_limit_counts()
        if limit_counts:
            full["limit_up"] = limit_counts["limit_up"]
            full["limit_down"] = limit_counts["limit_down"]
            full["limit_source"] = "eastmoney"
        else:
            # fallback: 两融明细估算涨跌停
            margin = get_breadth_from_margin()
            if margin:
                full["limit_up"] = margin["limit_up"]
                full["limit_down"] = margin["limit_down"]
                full["limit_source"] = "eastmoney_margin_estimate"
            else:
                full["limit_up"] = 0
                full["limit_down"] = 0
                full["limit_source"] = "none"
        return full
    # 全部 fallback
    return get_breadth_from_margin()


def get_em_limit_up_stocks(limit=50):
    """获取当日涨停股票列表（用于计算连板强度）"""
    fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz={limit}&po=1&np=1&fltt=2&invt=2"
        f"&fid=f3&fs={fs}&fields=f2,f3,f12,f14"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    stocks = []
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            chg = safe_float(item.get("f3"))
            if chg >= 9.9:
                code = item.get("f12", "")
                market = "0" if code.startswith(("0", "3")) else "1"
                stocks.append({
                    "code": code,
                    "name": item.get("f14", ""),
                    "secid": f"{market}.{code}",
                    "change": chg,
                })
    return stocks


def get_stock_kline(secid, lmt=8):
    """获取个股近 N 日 K 线，返回涨跌幅列表（最近→最远）"""
    url = (
        "http://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&klt=101&fqt=0&end=20500101&lmt={lmt}"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    if data and data.get("data") and data["data"].get("klines"):
        klines = data["data"]["klines"]
        # klines 格式: "日期,开盘,收盘,最高,最低,成交量,成交额,振幅"
        # API 返回按日期从早到晚排列
        closes = []
        for k in klines:
            parts = k.split(",")
            if len(parts) >= 3:
                closes.append(safe_float(parts[2]))
        # 计算每日涨跌幅（按时间顺序）
        pct_changes = []
        for i in range(1, len(closes)):
            prev = closes[i - 1]
            cur = closes[i]
            if prev > 0:
                pct_changes.append((cur - prev) / prev * 100)
        # 反转，使最近一日排在最前，方便连板统计
        pct_changes.reverse()
        return pct_changes
    return []


def get_limit_up_stats():
    """连板强度统计：2连板、3连板、4连板+ 家数"""
    stocks = get_em_limit_up_stocks(limit=80)
    if not stocks:
        return None

    consecutive = []
    for s in stocks[:40]:  # 限制请求数量，避免超时
        try:
            kline_changes = get_stock_kline(s["secid"], lmt=8)
            # kline_changes 从近到远（今天→昨天→...）
            consec = 1
            for chg in kline_changes[1:]:
                if chg >= 9.9:
                    consec += 1
                else:
                    break
            consecutive.append(consec)
            if len(consecutive) >= 30:
                break
        except Exception as e:
            print(f"[WARN] kline failed for {s['name']}: {e}")
            continue

    two = sum(1 for c in consecutive if c == 2)
    three = sum(1 for c in consecutive if c == 3)
    four_plus = sum(1 for c in consecutive if c >= 4)
    max_consec = max(consecutive) if consecutive else 0
    return {
        "two": two,
        "three": three,
        "four_plus": four_plus,
        "max": max_consec,
        "limit_up_count": len(stocks),
    }


def get_em_turnover():
    """沪深两市成交额合计；东方财富失败时 fallback 新浪财经"""
    url = (
        "http://push2.eastmoney.com/api/qt/ulist.np/get"
        "?secids=1.000001,0.399001&fields=f6&fltt=2"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    total = 0.0
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            total += safe_float(item.get("f6"))
    if total > 0:
        return total

    # fallback: 新浪财经
    print("[INFO] 东方财富成交额接口受限，切换新浪财经备用...")
    text = fetch_text(
        "https://hq.sinajs.cn/list=sh000001,sz399001",
        headers={"Referer": "https://finance.sina.com.cn/"},
    )
    raw = parse_sina_hq(text)
    total = 0.0
    for name in ["上证指数", "深证成指"]:
        total += raw.get(name, {}).get("turnover", 0)
    return total if total > 0 else None


def get_em_margin():
    """东方财富融资融券余额：使用 RPTA_WEB_MARGIN_DAILYTRADE 官方每日总量接口，
    直接获取最近 5 个交易日的融资余额、融券余额、两融余额（单位:亿），
    并自动计算与上一交易日的差额和变化率。"""
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPTA_WEB_MARGIN_DAILYTRADE&columns=ALL"
        "&pageNumber=1&pageSize=5"
        "&sortColumns=STATISTICS_DATE&sortTypes=-1"
    )
    data = fetch_json(
        url,
        headers={"Referer": "https://data.eastmoney.com/rzrq/zhtjday.html"},
        timeout=20,
    )
    if not data or not data.get("result") or not data["result"].get("data"):
        return None
    rows = data["result"]["data"]
    if not rows:
        return None

    cur = rows[0]
    total = safe_float(cur.get("MARGIN_BALANCE")) * 1e8  # 亿 -> 元
    rzye = safe_float(cur.get("FIN_BALANCE")) * 1e8
    rqye = safe_float(cur.get("LOAN_BALANCE")) * 1e8
    date = (cur.get("STATISTICS_DATE") or "")[:10]

    diff_total = None
    diff_pct = None
    prev_date = None
    if len(rows) >= 2:
        prev = rows[1]
        prev_total = safe_float(prev.get("MARGIN_BALANCE")) * 1e8
        prev_date = (prev.get("STATISTICS_DATE") or "")[:10]
        if prev_total > 0:
            diff_total = total - prev_total
            diff_pct = diff_total / prev_total * 100

    return {
        "value": total,
        "rzye": rzye,
        "rqye": rqye,
        "date": date,
        "prev_date": prev_date,
        "diff": diff_total,
        "diff_pct": diff_pct,
        "source": "eastmoney_official",
    }


def get_em_north_flow():
    """北向资金：港交所已停止实时披露净买入额，尝试获取最新可得数据"""
    # 东方财富沪深港通历史数据接口
    url = (
        "https://datacenter-web.eastmoney.com/api/data/v1/get"
        "?reportName=RPT_MUTUAL_DEAL_HISTORY&columns=ALL"
        "&pageNumber=1&pageSize=5&sortColumns=TRADE_DATE&sortTypes=-1"
        "&filter=(MUTUAL_TYPE%3D%22001%22)"
    )
    data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/hsgtcg/"})
    if data and data.get("result") and data["result"].get("data"):
        for row in data["result"]["data"]:
            net = row.get("NET_DEAL_AMT") or row.get("FUND_INFLOW")
            if net is not None:
                return {
                    "value": safe_float(net) / 1e4,  # 转为亿
                    "date": row.get("TRADE_DATE", "")[:10],
                }
    return {"value": None, "date": None}


def get_futures_basis():
    """股指期货升贴水：期货主力合约 - 现货指数"""
    # 期货主力合约
    url = (
        "http://push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=300&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:8"
        "&fields=f2,f3,f12,f14,f18,f20,f21"
    )
    data = fetch_json(url, headers=EM_HEADERS)
    futures = {}
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            if "主力" in name:
                if "IF" in name:
                    futures["IF"] = safe_float(item.get("f2"))
                elif "IC" in name:
                    futures["IC"] = safe_float(item.get("f2"))
                elif "IH" in name:
                    futures["IH"] = safe_float(item.get("f2"))
                elif "IM" in name:
                    futures["IM"] = safe_float(item.get("f2"))

    # 现货指数
    spot = get_em_indices()
    mapping = {
        "IF": ("沪深300", "IF"),
        "IC": ("中证500", "IC"),
        "IH": ("上证50", "IH"),
        "IM": ("中证1000", "IM"),
    }
    result = []
    for key, (spot_name, fut_name) in mapping.items():
        s_price = spot.get(spot_name, {}).get("price", 0)
        f_price = futures.get(key, 0)
        if s_price > 0 and f_price > 0:
            basis = f_price - s_price
            basis_pct = basis / s_price * 100
            result.append({
                "name": fut_name,
                "basis": basis,
                "basis_pct": basis_pct,
                "future": f_price,
                "spot": s_price,
            })
    return result


def get_ipo_count():
    """近期 IPO 数量：东方财富新股申购数据"""
    try:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            "?reportName=RPTA_APP_IPOAPPLY&columns=APPLY_DATE,SECURITY_CODE,SECURITY_NAME"
            "&pageNumber=1&pageSize=500&sortColumns=APPLY_DATE&sortTypes=-1"
        )
        data = fetch_json(url, headers={"Referer": "https://data.eastmoney.com/xg/"}, timeout=20)
        if data and data.get("result") and data["result"].get("data"):
            today = NOW.date()
            week_ago = today - timedelta(days=7)
            week_later = today + timedelta(days=7)
            count = 0
            names = []
            for row in data["result"]["data"]:
                apply = row.get("APPLY_DATE", "")[:10]
                if not apply:
                    continue
                try:
                    d = datetime.strptime(apply, "%Y-%m-%d").date()
                except Exception:
                    continue
                if week_ago <= d <= week_later:
                    count += 1
                    names.append(row.get("SECURITY_NAME", ""))
            if count > 0:
                note = f"近两周申购：{', '.join(names[:5])}{'等' if count > 5 else ''}"
                return {"count": count, "source": "eastmoney", "note": note}
    except Exception as e:
        print(f"[WARN] IPO API failed: {e}")

    # 预设兜底
    return {"count": 6, "source": "preset", "note": "本周约 6 只（需手动核对）"}


# ---------------------------------------------------------------------------
# 数据获取 — yfinance (美股/美债/商品/DXY/汇率)
# ---------------------------------------------------------------------------

def get_yf_single(ticker, retries=3):
    """单个 yfinance Ticker 获取，带重试
    返回: {price: 当前价, prev: 昨收, change: 日涨跌幅%}
    对美债收益率，price 本身就是收益率数值，外部再用 (price-prev)*100 换算 bp
    """
    try:
        import yfinance as yf
    except ImportError:
        print("[ERROR] yfinance not installed")
        return None
    for attempt in range(retries):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d")
            if hist is not None and not hist.empty and len(hist) >= 2:
                cur = safe_float(hist["Close"].iloc[-1])
                prev = safe_float(hist["Close"].iloc[-2])
                if cur > 0 and prev > 0:
                    chg = (cur - prev) / prev * 100
                    return {"price": cur, "prev": prev, "change": chg}
            elif hist is not None and not hist.empty:
                cur = safe_float(hist["Close"].iloc[-1])
                if cur > 0:
                    return {"price": cur, "prev": cur, "change": 0}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            print(f"[WARN] yfinance {ticker} failed: {e}")
    return None


def get_yf_quotes():
    """逐个获取 yfinance 行情"""
    if os.environ.get("SKIP_YF"):
        print("[INFO] SKIP_YF set, skipping yfinance in local dev...")
        return {}
    tickers = [
        ("^DJI", "道琼斯"),
        ("^IXIC", "纳斯达克"),
        ("^GSPC", "标普500"),
        ("^VIX", "VIX恐慌指数"),
        # 美债收益率不再通过 yfinance 获取：^IRX 是 13 周 T-bill，^TNX/^TYX 也不如财政部官方 CSV 准确
        # 统一使用 get_treasury_yields() 获取
        ("GC=F", "黄金"),
        ("SI=F", "白银"),
        ("HG=F", "铜"),
        ("CL=F", "WTI原油"),
    """美国财政部官方美债收益率数据（CSV 格式，免费、无需 API key）

    数据来源: https://home.treasury.gov/resource-center/data-chart-center/interest-rates/
    包含完整期限: 1M/1.5M/2M/3M/4M/6M/1Y/2Y/3Y/5Y/7Y/10Y/20Y/30Y
    优势: 比 yfinance 更准确（yfinance 的 ^IRX 是 13 周 T-bill，不是 2 年期）

    返回: {"美债2Y": {price, prev, change, source, date}, "美债10Y": {...}, "美债30Y": {...}}
    """
    import csv as _csv
    from io import StringIO

    year = datetime.now().year
    # 注意: _format=csv 是必要参数，不能夹杂空 page 参数
    url = (
        f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value={year}&_format=csv"
    )

    try:
        hdrs = {
    if len(rows) < 2:
        print("[WARN] Treasury CSV: insufficient rows")
        return None

    # 按日期降序排序，确保 rows[0] 永远是最新交易日
    def _parse_date(s):
        if not s:
            return datetime.min.replace(tzinfo=None)
        try:
            return datetime.strptime(str(s).strip(), "%m/%d/%Y")
        except Exception:
            return datetime.min.replace(tzinfo=None)

    rows.sort(key=lambda r: _parse_date(r.get("Date", "")), reverse=True)

    latest = rows[0]
    prev = rows[1] if len(rows) > 1 else None
    latest_date = latest.get("Date", "")

    def _val(row, col):
        if not row:
            return None
        # 兼容列名带空格或下划线变体
        candidates = [col, col.replace(" ", ""), col.replace("Yr", "Y")]
        for c in candidates:
            v = row.get(c)
            if v not in (None, "", "N/A"):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    result = {}
    # 财政部列名: "2 Yr", "10 Yr", "30 Yr"（带空格）
    mapping = [
            result[label] = {
                "price": cur,
                "prev": pre if pre is not None else cur,
                "change": change,
                "source": "us_treasury_official",
                "date": latest_date,
            }

    if result:
        print(f"   Treasury yields from US Treasury official source ({latest_date}):")
        for label, val in result.items():
            bp = (val["price"] - val["prev"]) * 100
            sign = "+" if bp >= 0 else ""
            print(f"     {label}: {val['price']:.3f}% ({sign}{bp:.1f}bp)")
    "道琼斯": {"price": 52218.58, "prev": 52224.64, "change": -0.01},
    "纳斯达克": {"price": 25690.90, "prev": 25837.21, "change": -0.57},
    "标普500": {"price": 7498.96, "prev": 7509.20, "change": -0.14},
    "VIX恐慌指数": {"price": 17.79, "prev": 18.65, "change": -4.61},
    "美债10Y": {"price": 4.71, "prev": 4.72, "change": -0.21, "source": "fallback", "date": "2026-08-18"},
    "美债30Y": {"price": 5.28, "prev": 5.31, "change": -0.57, "source": "fallback", "date": "2026-08-18"},
    "美债2Y": {"price": 4.19, "prev": 4.19, "change": 0.0, "source": "fallback", "date": "2026-08-18"},
    "黄金": {"price": 4129.94, "prev": 4151.90, "change": -0.53},
    "白银": {"price": 60.11, "prev": 60.30, "change": -0.31},
    "铜": {"price": 6.52, "prev": 6.49, "change": 0.45},
    "WTI原油": {"price": 87.73, "prev": 86.83, "change": 1.04},
            # 收益率本身上行=数值变大，按中国股市“涨红跌绿”显示
            cls = "up" if chg > 0 else "down"
            # price 本身就是收益率%，差值*100 = bp
            bp = (v.get("price", 0) - v.get("prev", v["price"])) * 100
            source = v.get("source", "")
            date = v.get("date", "")
            if source == "us_treasury_official":
                extra = f"美国财政部官方 {date}"
            elif source == "fallback":
                extra = f"兜底数据 {date}（建议核实）"
            else:
                extra = f"{source} {date}".strip()
            bond_cards += build_card(
                label, f"{v['price']:.3f}%",
                f'<span class="change {cls}">{bp:+.1f}bp</span>',
                extra)
    t2 = q.get("美债2Y")
    t10 = q.get("美债10Y")
    if t2 and t10:
        spread = t10["price"] - t2["price"]
        bond_cards += build_card(
            "10Y-2Y利差", f"{spread:.3f}%",
            '<span class="change neutral">收益率曲线</span>',
            "基于上述数据源")

    # --- 风险指标 ---
    risk_cards = ""
    if vix:
<div class="chart-box"><div class="chart-title">美债收益率曲线 (%)</div><div id="chart4" style="width:100%;min-height:240px"></div></div>
<div class="chart-box"><div class="chart-title">风险指标</div><div id="chart5" style="width:100%;min-height:240px"></div></div>

<div class="footer">
<p>数据来源: 东方财富(push2/datacenter) / 新浪财经 / Yahoo Finance / CoinGecko / Alternative.me / 美国财政部官方 CSV | GitHub Actions 每日自动生成 | Cloudflare Pages 托管</p>
<p>中国股市颜色规则: 涨红跌绿 | 月度宏观数据来自东方财富数据中心API | 涨跌家数使用上证A指(000002)+深证A指(399107)全市场统计 | 两融余额使用东方财富官方日总量(MARGIN_BALANCE)并对比上一交易日 | 美债收益率优先使用美国财政部官方 CSV，yfinance 的 ^IRX 是 13 周 T-bill 不作为 2Y 数据源 | 生成时间: {NOW_STR}</p>
</div>

</div>
<script>
    all_data["etf"] = get_em_etf_flow()
    if all_data["etf"]:
        print(f"   ETF合计: {all_data['etf']['total']/1e8:+.1f}亿")

    # 9. yfinance 美股/商品/外汇（不含美债，避免 ^IRX/^TNX/^TYX 误标）
    print(">> 获取 yfinance 行情...")
    all_data["yf_quotes"] = get_yf_quotes()

    # 9a. 美债收益率 — 优先使用美国财政部官方 CSV（yfinance 的 ^IRX 是 13 周 T-bill，非 2Y）
    print(">> 获取美债收益率（美国财政部官方 CSV）...")
    treasury_data = get_treasury_yields()
    if treasury_data:
        for key, val in treasury_data.items():
            all_data["yf_quotes"][key] = val
        print(f"   Treasury 官方数据已覆盖 {len(treasury_data)} 个期限")
    else:
        print("   [WARN] Treasury 官方 CSV 获取失败，将使用兜底数据")

    # 9b. yfinance 失败时尝试新浪财经备用（新浪不覆盖美债收益率）
    if len(all_data["yf_quotes"]) < 10:
        print(">> yfinance 获取不完整，尝试新浪财经备用...")
        sina_ext = get_sina_external()
        for key, val in sina_ext.items():
            if key not in all_data["yf_quotes"]:
                all_data["yf_quotes"][key] = val
                print(f"   sina {key}: {val['price']:.4f} ({fmt_pct(val['change'])})")

    # 网络不可用时用 fallback 补全，避免看板空白；补全时明确标记来源
    for key, val in EXTERNAL_FALLBACK.items():
        if key not in all_data["yf_quotes"]:
            fallback_val = dict(val)
            fallback_val.setdefault("source", "fallback")
            all_data["yf_quotes"][key] = fallback_val
            print(f"   fallback {key}: {fallback_val['price']}")
    print(f"   获取到 {len(all_data['yf_quotes'])} 个品种")

    # 10. 加密货币
    print(">> 获取 CoinGecko 加密货币...")
