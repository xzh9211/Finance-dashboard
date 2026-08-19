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
        ("BZ=F", "布伦特原油"),
        ("DX-Y.NYB", "美元指数"),
        ("CNY=X", "美元兑人民币"),
    ]
    result = {}
    for symbol, name in tickers:
        r = get_yf_single(symbol)
        if r:
            result[name] = r
            print(f"   yfinance {name}: {r['price']:.2f} ({fmt_pct(r['change'])})")
        else:
            print(f"   yfinance {name}: FAILED")
        time.sleep(0.5)
    return result


def get_treasury_yields():
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/csv,*/*",
        }
        resp = requests.get(url, timeout=20, headers=hdrs)
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        print(f"[WARN] Treasury CSV fetch failed: {e}")
        return None

    if not text or len(text) < 100:
        print("[WARN] Treasury CSV response too short")
        return None

    try:
        reader = _csv.DictReader(StringIO(text))
        rows = list(reader)
    except Exception as e:
        print(f"[WARN] Treasury CSV parse failed: {e}")
        return None

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
        ("美债2Y",  "2 Yr"),
        ("美债10Y", "10 Yr"),
        ("美债30Y", "30 Yr"),
    ]
    for label, col in mapping:
        cur = _val(latest, col)
        pre = _val(prev, col)
        if cur is not None and cur > 0:
            change = ((cur - pre) / pre * 100) if (pre and pre > 0) else 0.0
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
        return result
    return None


def parse_sina_external(text):
    """解析新浪外汇/全球指数/商品行情
    返回: {标准名称: {price, prev, change}}
    """
    result = {}
    if not text:
        return result

    # 标准名称 -> (新浪代码, 字段解析方式, 价格缩放)
    # 美股指数字段: 名称,最新,涨跌幅%,时间,涨跌额,昨收,...
    # 商品字段: 最新,涨跌额,买,卖,最高,最低,时间,昨收,开盘价,...,名称
    # 外汇字段: 时间,最新?,...,昨收?,名称,日期
    mapping = {
        "道琼斯": ("gb_dji", "us_index", 1),
        "纳斯达克": ("gb_ixic", "us_index", 1),
        "标普500": ("gb_inx", "us_index", 1),
        "黄金": ("hf_GC", "commodity", 1),
        "白银": ("hf_SI", "commodity", 1),
        "铜": ("hf_HG", "commodity", 0.01),  # 美分/磅 -> 美元/磅
        "WTI原油": ("hf_CL", "commodity", 1),
        "布伦特原油": ("hf_OIL", "commodity", 1),
        "美元兑人民币": ("USDCNY", "forex", 1),
    }
    code_to_name = {v[0]: (k, v[1], v[2]) for k, v in mapping.items()}

    for line in text.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.search(r'var\s+hq_str_(\w+)="(.*?)"', line)
        if not m:
            continue
        code = m.group(1)
        content = m.group(2)
        if code not in code_to_name or not content:
            continue
        name, parse_type, scale = code_to_name[code]
        parts = content.split(",")
        try:
            if parse_type == "us_index":
                # 道琼斯,52218.5781,-0.01,...
                price = safe_float(parts[1]) * scale
                chg = safe_float(parts[2])
                prev = price / (1 + chg / 100) if chg != 0 else safe_float(parts[5])
            elif parse_type == "commodity":
                # 最新,涨跌额,买,卖,最高,最低,时间,昨收,开盘价,...
                price = safe_float(parts[0]) * scale
                prev = safe_float(parts[7]) * scale
                chg = (price - prev) / prev * 100 if prev > 0 else 0
            elif parse_type == "forex":
                # 时间,最新,卖出,买入,成交量,最高,开盘,最低,昨收,名称,日期
                price = safe_float(parts[1]) * scale
                prev = safe_float(parts[8]) * scale
                chg = (price - prev) / prev * 100 if prev > 0 else 0
            else:
                continue
            if price > 0:
                result[name] = {"price": price, "prev": prev, "change": chg}
        except Exception as e:
            print(f"[WARN] parse sina {code} failed: {e}")
            continue
    return result


def get_sina_external():
    """新浪财经获取外部市场备用数据"""
    codes = "gb_dji,gb_ixic,gb_inx,hf_GC,hf_SI,hf_HG,hf_CL,hf_OIL,USDCNY"
    url = f"https://hq.sinajs.cn/list={codes}"
    text = fetch_text(url, headers={"Referer": "https://finance.sina.com.cn/"})
    return parse_sina_external(text)


# ---------------------------------------------------------------------------
# 数据获取 — CoinGecko + Alternative.me
# ---------------------------------------------------------------------------

def get_crypto():
    """CoinGecko 加密货币"""
    data = fetch_json(
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
    )
    result = {}
    if data:
        try:
            result["比特币"] = {
                "price": data["bitcoin"]["usd"],
                "change": data["bitcoin"].get("usd_24h_change", 0),
            }
        except (KeyError, TypeError):
            pass
        try:
            result["以太坊"] = {
                "price": data["ethereum"]["usd"],
                "change": data["ethereum"].get("usd_24h_change", 0),
            }
        except (KeyError, TypeError):
            pass
    return result


def get_fear_greed():
    """恐惧贪婪指数"""
    data = fetch_json("https://api.alternative.me/fng/?limit=1")
    if data and "data" in data and len(data["data"]) > 0:
        try:
            val = int(data["data"][0]["value"])
            cls = data["data"][0]["value_classification"]
            return {"value": val, "class": cls}
        except (KeyError, IndexError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# 月度宏观/机构/房地产预设（最新可得数据）
# ---------------------------------------------------------------------------

MACRO_LATEST = {
    "pmi": 50.3,
    "pmi_non": 50.2,
    "cpi": 1.0,
    "ppi": 4.1,
    "m1": 4.0,
    "m2": 8.0,
    "social_finance": 208400,  # 2026 年上半年社融增量累计（亿元），央行公布
    "export": 27.0,
    "import": 36.0,
    "retail": 1.0,
    "macro_date": "2026年6月",
}

# 公募股票型基金仓位（好买基金估算，月度）
FUND_POSITION = {
    "value": 86.5,  # 股票型基金仓位 %
    "change": -0.3,  # 较上期变化 pct
    "date": "2026年6月",
}

# 非银存款（月度）
NON_BANK_DEPOSIT = {
    "value": 28.6,  # 万亿元
    "change": 0.8,  # 环比增减 万亿元
    "date": "2026年6月",
}

# 房地产关键指标（月度）
REAL_ESTATE = {
    "invest_yoy": -18.0,  # 房地产开发投资同比 %
    "sales_area_yoy": -11.6,  # 商品房销售面积同比 %
    "sales_amount_yoy": -13.6,  # 商品房销售额同比 %
    "date": "2026年1-6月",
}

# IPO 近期预设（当自动抓取失败时使用）
IPO_PRESET = {"count": 6, "note": "本周约 6 只新股申购"}


# ---------------------------------------------------------------------------
# 数据获取 — 东方财富宏观数据 API（权威月度数据）
# ---------------------------------------------------------------------------

MACRO_APIS = [
    # (reportName, 字段映射, 目标key)
    ("RPT_ECONOMY_CPI", {"same": "NATIONAL_SAME", "seq": "NATIONAL_SEQUENTIAL"}, "cpi"),
    ("RPT_ECONOMY_PPI", {"same": "BASE_SAME"}, "ppi"),
    ("RPT_ECONOMY_PMI", {"make": "MAKE_INDEX", "nmake": "NMAKE_INDEX"}, "pmi"),
    ("RPT_ECONOMY_CURRENCY_SUPPLY", {"m2": "BASIC_CURRENCY_SAME", "m1": "CURRENCY_SAME"}, "money"),
    ("RPT_ECONOMY_RMB_LOAN", {"loan": "RMB_LOAN"}, "loan"),
    ("RPT_ECONOMY_CUSTOMS", {"export": "EXIT_BASE_SAME", "import": "IMPORT_BASE_SAME"}, "trade"),
    ("RPT_ECONOMY_TOTAL_RETAIL", {"retail": "RETAIL_TOTAL_SAME"}, "retail"),
]


def get_em_macro():
    """从东方财富数据中心获取最新月度宏观数据；失败时返回预设值"""
    result = dict(MACRO_LATEST)
    result["source"] = "preset"
    latest_date = None

    def fetch_report(report_name):
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get"
            f"?reportName={report_name}&columns=ALL"
            "&pageNumber=1&pageSize=3"
            "&sortColumns=REPORT_DATE&sortTypes=-1"
        )
        return fetch_json(url, headers={"Referer": "https://data.eastmoney.com/"}, timeout=20)

    def parse_date(s):
        if not s:
            return None
        try:
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    try:
        # CPI
        data = fetch_report("RPT_ECONOMY_CPI")
        if data and data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            result["cpi"] = safe_float(row.get("NATIONAL_SAME"))
            result["cpi_sequential"] = safe_float(row.get("NATIONAL_SEQUENTIAL"))
            latest_date = parse_date(row.get("REPORT_DATE"))

        # PPI
        data = fetch_report("RPT_ECONOMY_PPI")
        if data and data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            result["ppi"] = safe_float(row.get("BASE_SAME"))

        # PMI
        data = fetch_report("RPT_ECONOMY_PMI")
        if data and data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            result["pmi"] = safe_float(row.get("MAKE_INDEX"))
            result["pmi_non"] = safe_float(row.get("NMAKE_INDEX"))

        # M2/M1
        data = fetch_report("RPT_ECONOMY_CURRENCY_SUPPLY")
        if data and data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            result["m2"] = safe_float(row.get("BASIC_CURRENCY_SAME"))
            result["m1"] = safe_float(row.get("CURRENCY_SAME"))

        # 新增信贷
        data = fetch_report("RPT_ECONOMY_RMB_LOAN")
        if data and data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            result["rmb_loan"] = safe_float(row.get("RMB_LOAN"))

        # 进出口
        data = fetch_report("RPT_ECONOMY_CUSTOMS")
        if data and data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            result["export"] = safe_float(row.get("EXIT_BASE_SAME"))
            result["import"] = safe_float(row.get("IMPORT_BASE_SAME"))

        # 社零
        data = fetch_report("RPT_ECONOMY_TOTAL_RETAIL")
        if data and data.get("result") and data["result"].get("data"):
            row = data["result"]["data"][0]
            result["retail"] = safe_float(row.get("RETAIL_TOTAL_SAME"))

        if latest_date:
            result["macro_date"] = f"{latest_date.year}年{latest_date.month}月"
        result["source"] = "eastmoney"
    except Exception as e:
        print(f"[WARN] 东方财富宏观API异常，使用预设值: {e}")

    return result

# 外部市场/加密 fallback（当 yfinance / CoinGecko 网络不可用时使用最近可得数据）
# 必须包含 prev（昨收/前一交易日），用于计算美债 bp 变化
# 注意: 美债数据已被 get_treasury_yields() 覆盖；以下为最后兜底（美国财政部 2026-08-17）
EXTERNAL_FALLBACK = {
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
    "布伦特原油": {"price": 91.14, "prev": 90.18, "change": 1.07},
    "美元指数": {"price": 101.18, "prev": 101.35, "change": -0.17},
    "美元兑人民币": {"price": 6.7679, "prev": 6.7684, "change": -0.01},
}

CRYPTO_FALLBACK = {
    "比特币": {"price": 66040.0, "change": 0.85},
    "以太坊": {"price": 1925.0, "change": 1.20},
}


# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f5f6fa;--card:#fff;--text:#1a1a2e;--sub:#5a5a7a;--up:#c0392b;--down:#27ae60;--accent:#2c3e50;--border:#e0e0e8;--warn:#e74c3c;--safe:#3498db;--gold:#f1c40f}
*{margin:0;padding:0;box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{font-family:'PingFang SC','Microsoft YaHei','Helvetica Neue',Arial,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;padding:16px}
.container{max-width:1280px;margin:auto;padding:0 4px}
.header{text-align:center;padding:24px 0 12px}
.header h1{font-size:28px;color:var(--accent);margin-bottom:6px}
.header .date{font-size:14px;color:var(--sub)}
.conclusion-card{background:linear-gradient(135deg,#2c3e50,#34495e);color:#fff;border-radius:12px;padding:24px;margin:16px 0;box-shadow:0 4px 12px rgba(0,0,0,.15)}
.conclusion-card .title{font-size:18px;font-weight:700;margin-bottom:12px}
.conclusion-card .summary{font-size:16px;line-height:1.8;margin-bottom:12px;word-break:break-word}
.conclusion-card .signals{display:flex;flex-wrap:wrap;gap:8px}
.conclusion-card .signal{background:rgba(255,255,255,.18);padding:6px 14px;border-radius:6px;font-size:13px;white-space:nowrap}
.signal.up{border-left:3px solid #e74c3c}
.signal.down{border-left:3px solid #2ecc71}
.signal.neutral{border-left:3px solid #3498db}
.section{margin:20px 0}
.section-title{font-size:20px;font-weight:700;color:var(--accent);padding:8px 0;border-bottom:2px solid var(--accent);margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.card{background:var(--card);border-radius:10px;padding:16px;border:1px solid var(--border);box-shadow:0 2px 6px rgba(0,0,0,.06);transition:transform .2s;word-break:break-word;min-width:0}
.card:hover{transform:translateY(-2px)}
.card .label{font-size:12px;color:var(--sub);margin-bottom:4px;line-height:1.4}
.card .value{font-size:22px;font-weight:700;line-height:1.3}
.card .change{font-size:13px;margin-top:4px;display:block}
.card .change.up{color:var(--up)}
.card .change.down{color:var(--down)}
.card .extra{font-size:12px;color:var(--sub);margin-top:6px;line-height:1.4}
.chart-box{background:var(--card);border-radius:10px;padding:16px;border:1px solid var(--border);margin:16px 0;height:380px;overflow:hidden}
.chart-title{font-size:16px;font-weight:700;color:var(--accent);margin-bottom:8px}
.row{display:flex;gap:12px;margin:8px 0}
.row .card{flex:1}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;margin:2px}
.tag.up{background:#fce4ec;color:var(--up)}
.tag.down{background:#e8f5e9;color:var(--down)}
.tag.neutral{background:#e3f2fd;color:var(--safe)}
.footer{text-align:center;padding:24px 0;font-size:12px;color:var(--sub);line-height:1.8}
.note{font-size:11px;color:#888;margin-top:4px}

/* 平板 */
@media(max-width:900px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .chart-box{height:340px}
}

/* 大手机/小平板 */
@media(max-width:768px){
  body{padding:12px}
  .header{padding:18px 0 8px}
  .header h1{font-size:22px}
  .header .date{font-size:12px}
  .section-title{font-size:16px;margin-bottom:12px}
  .cards{grid-template-columns:repeat(2,1fr);gap:10px}
  .card{padding:12px}
  .card .value{font-size:18px}
  .card .change,.card .extra,.card .label{font-size:11px}
  .chart-box{height:300px;padding:12px}
  .chart-title{font-size:14px}
  .conclusion-card{padding:16px}
  .conclusion-card .title{font-size:15px}
  .conclusion-card .summary{font-size:14px;line-height:1.7}
  .conclusion-card .signal{font-size:12px;padding:5px 10px}
  .footer{padding:18px 0;font-size:11px}
}

/* 小手机 */
@media(max-width:480px){
  body{padding:8px}
  .container{padding:0}
  .header{padding:14px 0 6px}
  .header h1{font-size:20px}
  .cards{grid-template-columns:1fr}
  .chart-box{height:260px;margin:12px 0}
  .conclusion-card{margin:12px 0;padding:14px}
  .conclusion-card .signals{gap:6px}
  .section{margin:16px 0}
}

/* 超小屏折叠屏 */
@media(max-width:360px){
  .header h1{font-size:18px}
  .card .value{font-size:16px}
  .chart-box{height:240px}
}
"""


def build_card(label, value, change_html="", extra=""):
    return f"""<div class="card">
<div class="label">{label}</div>
<div class="value">{value}</div>
{change_html}
<div class="extra">{extra}</div>
</div>"""


def build_conclusion(summary, signals):
    sig_html = "\n".join(
        f'<span class="signal {s["cls"]}">{s["text"]}</span>' for s in signals
    )
    return f"""<div class="conclusion-card">
<div class="title">今日市场一句话总结</div>
<div class="summary">{summary}</div>
<div class="signals">{sig_html}</div>
</div>"""


def generate_html(d):
    """d = 全部数据字典, 返回完整 HTML 字符串"""
    q = {}
    q.update(d.get("em_quotes", {}))
    q.update(d.get("yf_quotes", {}))
    q.update(d.get("crypto", {}))

    sectors = d.get("sectors", [])
    breadth = d.get("breadth")
    margin = d.get("margin")
    north = d.get("north")
    etf = d.get("etf")
    macro = d.get("macro", {})
    fear = d.get("fear_greed")
    turnover = d.get("turnover")
    limit_stats = d.get("limit_stats")
    futures_basis = d.get("futures_basis")
    ipo = d.get("ipo")

    # --- 顶部结论 ---
    parts = []
    sh = q.get("上证指数", {})
    cyb = q.get("创业板指", {})
    kc = q.get("科创50", {})
    vix = q.get("VIX恐慌指数", {})
    gold = q.get("黄金", {})
    oil = q.get("WTI原油", {})
    tnk = q.get("美债10Y", {})
    dxy = q.get("美元指数", {})
    btc = q.get("比特币", {})

    if sh:
        parts.append(f"上证{sh['price']:.2f}({fmt_pct(sh['change'])})")
    if cyb:
        parts.append(f"创业板{cyb['price']:.2f}({fmt_pct(cyb['change'])})")
    if kc:
        parts.append(f"科创50{kc['price']:.2f}({fmt_pct(kc['change'])})")
    if vix:
        parts.append(f"VIX{vix['price']:.1f}")
    if gold:
        parts.append(f"黄金${gold['price']:.0f}({fmt_pct(gold['change'])})")
    if oil:
        parts.append(f"WTI${oil['price']:.1f}({fmt_pct(oil['change'])})")
    if tnk:
        parts.append(f"美债10Y {tnk['price']:.2f}%")
    if dxy:
        parts.append(f"DXY {dxy['price']:.2f}")

    summary = "、".join(parts) + "。" if parts else "数据获取中，请稍后刷新。"

    signals = []
    if sh:
        cls = "up" if sh["change"] >= 0 else "down"
        signals.append({"cls": cls, "text": f"上证 {fmt_pct(sh['change'])}"})
    if cyb:
        cls = "up" if cyb["change"] >= 0 else "down"
        signals.append({"cls": cls, "text": f"创业板 {fmt_pct(cyb['change'])}"})
    if kc:
        cls = "up" if kc["change"] >= 0 else "down"
        signals.append({"cls": cls, "text": f"科创50 {fmt_pct(kc['change'])}"})
    if vix:
        cls = "up" if vix["change"] > 0 else "down"
        signals.append({"cls": cls, "text": f"VIX {vix['price']:.1f}"})
    if gold:
        cls = "up" if gold["change"] >= 0 else "down"
        signals.append({"cls": cls, "text": f"黄金${gold['price']:.0f}"})
    if oil:
        cls = "up" if oil["change"] >= 0 else "down"
        signals.append({"cls": cls, "text": f"WTI${oil['price']:.1f}"})
    if tnk:
        signals.append({"cls": "neutral", "text": f"美债10Y {tnk['price']:.2f}%"})
    if dxy:
        cls = "up" if dxy["change"] > 0 else "down"
        signals.append({"cls": cls, "text": f"DXY {dxy['price']:.2f}"})
    if north and north.get("value") is not None:
        cls = "down" if north["value"] < 0 else "up"
        signals.append({"cls": cls, "text": f"北向{north['value']:+.1f}亿"})
    if margin:
        val = margin.get("value", 0) / 1e8
        signals.append({"cls": "neutral", "text": f"两融{val:.0f}亿"})
    if etf:
        val = etf.get("total", 0) / 1e8
        cls = "up" if val >= 0 else "down"
        signals.append({"cls": cls, "text": f"ETF{val:+.1f}亿"})
    if fear:
        signals.append({"cls": "neutral", "text": f"恐贪{fear['value']} {fear['class']}"})

    # --- 经济基本面 ---
    macro_cards = ""
    if macro:
        pmi = macro.get("pmi")
        pmi_non = macro.get("pmi_non")
        cpi = macro.get("cpi")
        ppi = macro.get("ppi")
        m1 = macro.get("m1")
        m2 = macro.get("m2")
        sf = macro.get("social_finance")
        exp = macro.get("export")
        imp = macro.get("import")
        ret = macro.get("retail")
        mdate = macro.get("macro_date", "")

        if pmi:
            cls = "up" if pmi >= 50 else "down"
            macro_cards += build_card(
                "制造业PMI", f"{pmi:.1f}%",
                f'<span class="change {cls}">{"扩张" if pmi >= 50 else "收缩"}区间</span>', mdate)
        if pmi_non:
            cls = "up" if pmi_non >= 50 else "down"
            macro_cards += build_card(
                "非制造业PMI", f"{pmi_non:.1f}%",
                f'<span class="change {cls}">{"扩张" if pmi_non >= 50 else "收缩"}区间</span>', mdate)
        if cpi is not None:
            cls = "up" if cpi > 0 else "down"
            macro_cards += build_card(
                "CPI同比", f"+{cpi:.1f}%",
                f'<span class="change {cls}"> consumer prices</span>', mdate)
        if ppi is not None:
            cls = "up" if ppi > 0 else "down"
            macro_cards += build_card(
                "PPI同比", f"+{ppi:.1f}%",
                f'<span class="change {cls}"> producer prices</span>', mdate)
        if m2:
            macro_cards += build_card(
                "M2同比", f"+{m2:.1f}%",
                '<span class="change neutral">货币供应</span>', mdate)
        if m1:
            macro_cards += build_card(
                "M1同比", f"+{m1:.1f}%",
                '<span class="change neutral">货币供应</span>', mdate)
        if m1 and m2:
            sc = m2 - m1
            macro_cards += build_card(
                "M2-M1剪刀差", f"{sc:.1f}pct",
                '<span class="change neutral">剪刀差</span>', mdate)
        if sf:
            # 社融数字较大，以“万亿”展示
            sf_wan = sf / 10000
            macro_cards += build_card(
                "社融增量", f"{sf_wan:.2f}万亿",
                '<span class="change neutral">央行公布上半年累计</span>', mdate)
        if "rmb_loan" in macro and macro["rmb_loan"]:
            macro_cards += build_card(
                "新增信贷", f"{macro['rmb_loan']:,.0f}亿",
                '<span class="change neutral">人民币贷款</span>', mdate)
        if exp:
            macro_cards += build_card(
                "出口同比", f"+{exp:.1f}%",
                '<span class="change up">外需</span>', mdate)
        if imp:
            macro_cards += build_card(
                "进口同比", f"+{imp:.1f}%",
                '<span class="change up">内需</span>', mdate)
        if ret is not None:
            macro_cards += build_card(
                "社零同比", f"+{ret:.1f}%",
                '<span class="change neutral">消费</span>', mdate)

    # 房地产
    re = d.get("real_estate", {})
    if re:
        macro_cards += build_card(
            "房地产投资同比", f"{re.get('invest_yoy'):+.1f}%",
            '<span class="change neutral">房地产开发投资</span>', re.get("date", ""))
        macro_cards += build_card(
            "商品房销售面积同比", f"{re.get('sales_area_yoy'):+.1f}%",
            '<span class="change neutral">销售面积</span>', re.get("date", ""))
        macro_cards += build_card(
            "商品房销售额同比", f"{re.get('sales_amount_yoy'):+.1f}%",
            '<span class="change neutral">销售额</span>', re.get("date", ""))

    if not macro_cards:
        macro_cards = build_card("宏观数据", "暂无最新数据", '<span class="change neutral">待更新</span>')

    # --- 情绪面 ---
    emo_cards = ""
    for name in ["上证指数", "深证成指", "创业板指", "科创50", "恒生指数"]:
        v = q.get(name)
        if v:
            chg = v["change"]
            cls = "up" if chg >= 0 else "down"
            emo_cards += build_card(
                name, f"{v['price']:.2f}",
                f'<span class="change {cls}">{fmt_pct(chg)}</span>')

    if breadth:
        source_note = ""
        if breadth.get("source") == "eastmoney_full_market":
            source_note = "沪深两市全市场"
            if breadth.get("limit_source") == "eastmoney_margin_estimate":
                source_note += " | 涨跌停为两融样本估算"
            elif breadth.get("limit_source") == "none":
                source_note += " | 涨跌停暂无数据"
        elif breadth.get("source") == "eastmoney_margin_estimate":
            source_note = f"两融标的样本{breadth.get('sample', 0)}只（估算）"
        emo_cards += build_card(
            "涨跌家数", f"{breadth['up']} / {breadth['down']}",
            f'<span class="change {"up" if breadth["up"] > breadth["down"] else "down"}">'
            f'涨停{breadth["limit_up"]} 跌停{breadth["limit_down"]}</span>',
            source_note)

    if limit_stats:
        consec_text = f"最高{limit_stats['max']}连板"
        emo_cards += build_card(
            "连板强度", f"2板{limit_stats['two']} / 3板{limit_stats['three']} / 4板+{limit_stats['four_plus']}",
            f'<span class="change neutral">{consec_text}</span>')

    if vix:
        # VIX 数值上涨=恐慌升温，按“涨红跌绿”显示
        cls = "up" if vix["change"] > 0 else "down"
        emo_cards += build_card(
            "VIX恐慌指数", f"{vix['price']:.2f}",
            f'<span class="change {cls}">{fmt_pct(vix["change"])}</span>')

    if fear:
        emo_cards += build_card(
            "恐惧贪婪指数", f"{fear['value']}",
            f'<span class="change neutral">{fear["class"]}</span>')

    # 热门板块 Top5 涨跌（进攻/防守判断）
    if sectors:
        top5 = sectors[:5]
        bottom5 = sectors[-5:]
        top_text = " / ".join([f"{s['name']}{s['change']:+.2f}%" for s in top5])
        bottom_text = " / ".join([f"{s['name']}{s['change']:+.2f}%" for s in bottom5])
        offensive = any(k in top5[0]["name"] for k in ["科技", "电子", "半导体", "芯片", "AI", "计算机", "通信", "新能源"])
        bias = "偏进攻" if offensive else "偏防守"
        emo_cards += build_card(
            "领涨板块 Top5", top_text,
            f'<span class="change {"up" if top5[0]["change"] >= 0 else "down"}">{bias}</span>')
        emo_cards += build_card(
            "领跌板块 Top5", bottom_text,
            f'<span class="change {"down" if bottom5[-1]["change"] < 0 else "up"}">承压方向</span>')

    # --- 资金面 ---
    fund_cards = ""
    if turnover and turnover > 0:
        turnover_yi = turnover / 1e8
        fund_cards += build_card(
            "两市成交额", f"{turnover_yi/1e4:.2f}万亿",
            f'<span class="change neutral">沪深合计 {turnover_yi:,.0f} 亿</span>')

    if margin:
        val = margin.get("value", 0) / 1e8
        rzye = margin.get("rzye", 0) / 1e8
        rqye = margin.get("rqye", 0) / 1e8
        diff = margin.get("diff")
        diff_pct = margin.get("diff_pct")
        prev_date = margin.get("prev_date", "")

        if diff is not None and diff_pct is not None:
            if diff > 0:
                diff_cls, sign, label = "up", "+", "较上日增"
            elif diff < 0:
                diff_cls, sign, label = "down", "", "较上日减"
            else:
                diff_cls, sign, label = "neutral", "", "与上日持平"
            change_html = (
                f'<span class="change {diff_cls}">{label}{abs(diff)/1e8:,.0f}亿 '
                f'({sign}{diff_pct:.2f}%)</span>'
            )
            extra = f'{margin.get("date", "")}（官方总量）'
            if prev_date:
                extra += f' · 对比{prev_date}'
        else:
            change_html = (
                f'<span class="change neutral">融资{rzye:,.0f}亿 / 融券{rqye:,.0f}亿</span>'
            )
            extra = f'{margin.get("date", "")}（官方总量）'

        fund_cards += build_card(
            "两融余额", f"{val:,.0f}亿", change_html, extra)

    if north:
        if north.get("value") is not None:
            cls = "down" if north["value"] < 0 else "up"
            fund_cards += build_card(
                "北向资金", f"{north['value']:+.1f}亿",
                f'<span class="change {cls}">净{"卖出" if north["value"] < 0 else "买入"}</span>',
                f'{north.get("date", "")}（港交所收盘后公布）')
        else:
            fund_cards += build_card(
                "北向资金", "暂停实时披露",
                '<span class="change neutral">港交所自2024-05-13起不再实时披露</span>')

    if etf:
        val = etf.get("total", 0) / 1e8
        cls = "up" if val >= 0 else "down"
        top_etf = etf.get("top", [])
        top_text = " / ".join([f"{x['name'][:6]}{x['flow']/1e8:+.1f}亿" for x in top_etf[:3]])
        fund_cards += build_card(
            "ETF资金流向", f"{val:+.1f}亿",
            f'<span class="change {cls}">前15只ETF合计</span>',
            top_text)
    else:
        fund_cards += build_card(
            "ETF资金流向", "--",
            '<span class="change neutral">东方财富ETF接口暂不可用（云端正常）</span>')

    # 公募仓位
    fp = d.get("fund_position", {})
    if fp:
        cls = "up" if fp.get("change", 0) >= 0 else "down"
        fund_cards += build_card(
            "公募基金仓位", f"{fp.get('value'):.1f}%",
            f'<span class="change {cls}">{fmt_pct(fp.get("change", 0))}</span>',
            f'{fp.get("date", "")}（股票型估算）')

    # 非银存款
    nb = d.get("non_bank_deposit", {})
    if nb:
        cls = "up" if nb.get("change", 0) >= 0 else "down"
        fund_cards += build_card(
            "非银存款", f"{nb.get('value'):.1f}万亿",
            f'<span class="change {cls}">环比{fmt_pct(nb.get("change", 0), suffix="万亿")}</span>',
            nb.get("date", ""))

    # --- 外部市场 ---
    ext_cards = ""
    for label, key in [
        ("美元指数", "美元指数"),
        ("美元/人民币", "美元兑人民币"),
        ("黄金", "黄金"),
        ("白银", "白银"),
        ("铜", "铜"),
        ("WTI原油", "WTI原油"),
        ("布伦特原油", "布伦特原油"),
        ("比特币", "比特币"),
        ("以太坊", "以太坊"),
        ("道琼斯", "道琼斯"),
        ("纳斯达克", "纳斯达克"),
        ("标普500", "标普500"),
        ("恒生指数", "恒生指数"),
    ]:
        v = q.get(key)
        if v:
            chg = v["change"]
            cls = "up" if chg >= 0 else "down"
            if key in ("比特币", "以太坊"):
                price_str = f"${v['price']:,.0f}"
            elif key in ("黄金", "白银", "铜", "WTI原油", "布伦特原油"):
                price_str = f"${v['price']:.2f}"
            elif key in ("美元指数",):
                price_str = f"{v['price']:.2f}"
            elif key == "美元兑人民币":
                price_str = f"{v['price']:.4f}"
            else:
                price_str = f"{v['price']:,.2f}"
            ext_cards += build_card(
                label, price_str,
                f'<span class="change {cls}">{fmt_pct(chg)}</span>')

    # --- 美债收益率 ---
    bond_cards = ""
    for label, key in [("美债2Y", "美债2Y"), ("美债10Y", "美债10Y"), ("美债30Y", "美债30Y")]:
        v = q.get(key)
        if v:
            chg = v["change"]
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
        cls = "up" if vix["change"] > 0 else "down"
        risk_cards += build_card(
            "VIX恐慌指数", f"{vix['price']:.2f}",
            f'<span class="change {cls}">{fmt_pct(vix["change"])}</span>')
    if fear:
        risk_cards += build_card(
            "恐惧贪婪指数", f"{fear['value']}",
            f'<span class="change neutral">{fear["class"]}</span>')
    if breadth:
        ratio = breadth["up"] / max(breadth["down"], 1)
        cls = "up" if ratio >= 1 else "down"
        risk_cards += build_card(
            "涨跌比", f"{ratio:.2f}",
            f'<span class="change {cls}">涨{breadth["up"]} / 跌{breadth["down"]}</span>')
    if futures_basis:
        basis_text = " / ".join(
            [f"{b['name']}{b['basis_pct']:+.2f}%" for b in futures_basis]
        )
        risk_cards += build_card(
            "股指期货升贴水", basis_text,
            '<span class="change neutral">期货-现货</span>')
    if ipo:
        risk_cards += build_card(
            "IPO数量", f"{ipo.get('count', '--')}只",
            '<span class="change neutral">近两周申购</span>',
            ipo.get("note", ""))

    # --- ECharts 图表数据 ---
    idx_data = []
    for name in ["上证指数", "深证成指", "创业板指", "科创50", "恒生指数", "道琼斯", "纳斯达克", "标普500"]:
        v = q.get(name)
        if v:
            idx_data.append({"name": name, "value": round(v["change"], 2)})

    ext_data = []
    for name in ["黄金", "白银", "铜", "WTI原油", "布伦特原油", "美元指数", "比特币", "以太坊"]:
        v = q.get(name)
        if v:
            ext_data.append({"name": name, "value": round(v["change"], 2)})

    sec_data = [{"name": s["name"], "value": round(s["change"], 2)} for s in sectors[:15]] if sectors else []

    bond_data = []
    for label, key in [("2Y", "美债2Y"), ("10Y", "美债10Y"), ("30Y", "美债30Y")]:
        v = q.get(key)
        if v:
            bond_data.append({"name": label, "value": round(v["price"], 3)})

    risk_data = []
    if vix:
        risk_data.append({"name": "VIX", "value": round(vix["price"], 2)})
    if fear:
        risk_data.append({"name": "恐惧贪婪", "value": fear["value"]})

    chart_idx = json.dumps(idx_data, ensure_ascii=False)
    chart_ext = json.dumps(ext_data, ensure_ascii=False)
    chart_sec = json.dumps(sec_data, ensure_ascii=False)
    chart_bond = json.dumps(bond_data, ensure_ascii=False)
    chart_risk = json.dumps(risk_data, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>金融市场全景看板 — {TODAY_STR}</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<div class="header">
<h1>金融市场全景看板</h1>
<div class="date">数据截止：{TODAY_STR} | 生成时间：{NOW_STR} | GitHub Actions 自动生成</div>
</div>

{build_conclusion(summary, signals)}

<div class="section">
<div class="section-title">一、经济基本面</div>
<div class="cards">{macro_cards}</div>
</div>

<div class="section">
<div class="section-title">二、情绪面</div>
<div class="cards">{emo_cards}</div>
</div>

<div class="section">
<div class="section-title">三、资金面</div>
<div class="cards">{fund_cards}</div>
</div>

<div class="section">
<div class="section-title">四、外部市场</div>
<div class="cards">{ext_cards}</div>
</div>

<div class="section">
<div class="section-title">五、美债收益率</div>
<div class="cards">{bond_cards}</div>
</div>

<div class="section">
<div class="section-title">六、风险指标</div>
<div class="cards">{risk_cards}</div>
</div>

<div class="chart-box"><div class="chart-title">主要指数涨跌幅 (%)</div><div id="chart1" style="width:100%;min-height:240px"></div></div>
<div class="chart-box"><div class="chart-title">外部资产涨跌幅 (%)</div><div id="chart2" style="width:100%;min-height:240px"></div></div>
<div class="chart-box"><div class="chart-title">行业板块涨跌幅 Top (%)</div><div id="chart3" style="width:100%;min-height:240px"></div></div>
<div class="chart-box"><div class="chart-title">美债收益率曲线 (%)</div><div id="chart4" style="width:100%;min-height:240px"></div></div>
<div class="chart-box"><div class="chart-title">风险指标</div><div id="chart5" style="width:100%;min-height:240px"></div></div>

<div class="footer">
<p>数据来源: 东方财富(push2/datacenter) / 新浪财经 / Yahoo Finance / CoinGecko / Alternative.me / 美国财政部官方 CSV | GitHub Actions 每日自动生成 | Cloudflare Pages 托管</p>
<p>中国股市颜色规则: 涨红跌绿 | 月度宏观数据来自东方财富数据中心API | 涨跌家数使用上证A指(000002)+深证A指(399107)全市场统计 | 两融余额使用东方财富官方日总量(MARGIN_BALANCE)并对比上一交易日 | 美债收益率优先使用美国财政部官方 CSV，yfinance 的 ^IRX 是 13 周 T-bill 不作为 2Y 数据源 | 生成时间: {NOW_STR}</p>
</div>

</div>
<script>
var idxData = {chart_idx};
var extData = {chart_ext};
var secData = {chart_sec};
var bondData = {chart_bond};
var riskData = {chart_risk};

function makeBar(elId, data){{
  var chart = echarts.init(document.getElementById(elId));
  var names = data.map(function(d){{return d.name;}});
  var vals = data.map(function(d){{return d.value;}});
  var colors = vals.map(function(v){{return v >= 0 ? '#c0392b' : '#27ae60';}});
  chart.setOption({{
    tooltip: {{trigger: 'axis', formatter: function(p){{return p[0].name + ': ' + p[0].value + '%';}}}},
    grid: {{left: '3%', right: '4%', bottom: '3%', containLabel: true}},
    xAxis: {{type: 'category', data: names, axisLabel: {{rotate: 30, fontSize: 11}}}},
    yAxis: {{type: 'value', axisLabel: {{formatter: '{{value}}%'}}}},
    series: [{{type: 'bar', data: vals.map(function(v, i){{return {{value: v, itemStyle: {{color: colors[i]}}}};}}), barWidth: '50%'}}]
  }});
  window.addEventListener('resize', function(){{chart.resize();}});
}}

function makeHBBar(elId, data){{
  var chart = echarts.init(document.getElementById(elId));
  var names = data.map(function(d){{return d.name;}});
  var vals = data.map(function(d){{return d.value;}});
  var colors = vals.map(function(v){{return v >= 0 ? '#c0392b' : '#27ae60';}});
  chart.setOption({{
    tooltip: {{trigger: 'axis'}},
    grid: {{left: '3%', right: '8%', bottom: '3%', containLabel: true}},
    xAxis: {{type: 'value', axisLabel: {{formatter: '{{value}}%'}}}},
    yAxis: {{type: 'category', data: names, inverse: true, axisLabel: {{fontSize: 11}}}},
    series: [{{type: 'bar', data: vals.map(function(v, i){{return {{value: v, itemStyle: {{color: colors[i]}}}};}})}}]
  }});
  window.addEventListener('resize', function(){{chart.resize();}});
}}

function makeGauge(elId, data){{
  var chart = echarts.init(document.getElementById(elId));
  var items = data.map(function(d){{
    return {{
      name: d.name,
      value: d.value,
      title: {{offsetCenter: ['0%', '20%']}},
      detail: {{offsetCenter: ['0%', '45%']}}
    }};
  }});
  chart.setOption({{
    tooltip: {{formatter: '{{b}}: {{c}}'}},
    series: [{{
      type: 'gauge',
      anchor: {{show: true, size: 12, itemStyle: {{color: '#777'}}}},
      pointer: {{offsetCenter: [0, 0]}},
      data: items,
      detail: {{valueAnimation: true, fontSize: 20, offsetCenter: [0, '70%']}},
      title: {{fontSize: 12}}
    }}]
  }});
  window.addEventListener('resize', function(){{chart.resize();}});
}}

if(idxData.length > 0) makeBar('chart1', idxData);
if(extData.length > 0) makeBar('chart2', extData);
if(secData.length > 0) makeHBBar('chart3', secData);
if(bondData.length > 0) makeBar('chart4', bondData);
if(riskData.length > 0) makeGauge('chart5', riskData);
</script>
</body>
</html>"""
    return html


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def main():
    print(f"[{NOW_STR}] 开始生成金融市场全景看板...")

    all_data = {}

    # 1. A 股 + 港股 + 宽基指数
    print(">> 获取东方财富指数行情...")
    all_data["em_quotes"] = get_em_indices()
    print(f"   获取到 {len(all_data['em_quotes'])} 个指数")

    # 2. 行业板块
    print(">> 获取东方财富板块数据...")
    all_data["sectors"] = get_em_sectors()
    print(f"   获取到 {len(all_data['sectors'])} 个板块")

    # 3. 涨跌家数
    print(">> 获取涨跌家数...")
    all_data["breadth"] = get_em_market_breadth()
    if all_data["breadth"]:
        b = all_data["breadth"]
        print(f"   涨{b['up']} 跌{b['down']} 涨停{b['limit_up']} 跌停{b['limit_down']}")

    # 4. 成交额
    print(">> 获取两市成交额...")
    all_data["turnover"] = get_em_turnover()
    if all_data["turnover"]:
        print(f"   成交额: {all_data['turnover'] / 1e8:.0f}亿")

    # 5. 连板强度
    print(">> 获取连板强度...")
    all_data["limit_stats"] = get_limit_up_stats()
    if all_data["limit_stats"]:
        ls = all_data["limit_stats"]
        print(f"   涨停{ls['limit_up_count']}只，最高{ls['max']}连板，2板{ls['two']} 3板{ls['three']} 4板+{ls['four_plus']}")

    # 6. 两融余额
    print(">> 获取两融余额...")
    all_data["margin"] = get_em_margin()
    if all_data["margin"]:
        m = all_data["margin"]
        diff_str = ""
        if m.get("diff") is not None:
            diff_str = f" | 较{m['prev_date']} {m['diff']/1e8:+,.0f}亿 ({m['diff_pct']:+.2f}%)"
        print(f"   两融余额 {m['date']}: {m['value']/1e8:,.0f}亿 (融资{m['rzye']/1e8:,.0f}亿 融券{m['rqye']/1e8:,.0f}亿){diff_str}")

    # 7. 北向资金
    print(">> 获取北向资金...")
    all_data["north"] = get_em_north_flow()
    if all_data["north"] and all_data["north"].get("value") is not None:
        print(f"   北向净: {all_data['north']['value']:+.1f}亿")
    else:
        print("   北向资金实时数据已停止披露")

    # 8. ETF 资金流向
    print(">> 获取ETF资金流向...")
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
    all_data["crypto"] = get_crypto()
    for key, val in CRYPTO_FALLBACK.items():
        if key not in all_data["crypto"]:
            all_data["crypto"][key] = val
            print(f"   fallback {key}: {val['price']}")
    print(f"   获取到 {len(all_data['crypto'])} 个加密货币")

    # 11. 恐惧贪婪指数
    print(">> 获取恐惧贪婪指数...")
    all_data["fear_greed"] = get_fear_greed()
    if all_data["fear_greed"]:
        print(f"   恐惧贪婪: {all_data['fear_greed']['value']} {all_data['fear_greed']['class']}")

    # 12. 股指期货升贴水
    print(">> 获取股指期货升贴水...")
    all_data["futures_basis"] = get_futures_basis()
    if all_data["futures_basis"]:
        for b in all_data["futures_basis"]:
            print(f"   {b['name']}: {b['basis_pct']:+.3f}%")

    # 13. IPO 数量
    print(">> 获取IPO数量...")
    all_data["ipo"] = get_ipo_count()
    print(f"   IPO: {all_data['ipo']}")

    # 14. 宏观/公募/房地产/非银存款
    print(">> 获取东方财富宏观数据...")
    all_data["macro"] = get_em_macro()
    print(f"   宏观数据日期: {all_data['macro'].get('macro_date')}, 来源: {all_data['macro'].get('source')}")
    all_data["fund_position"] = FUND_POSITION
    all_data["non_bank_deposit"] = NON_BANK_DEPOSIT
    all_data["real_estate"] = REAL_ESTATE

    # 15. 生成 HTML
    print(">> 生成 HTML...")
    html = generate_html(all_data)

    # 16. 写入文件
    # GitHub Actions 可通过 OUTPUT_PATH 指定完整路径，如 dist/index.html
    out_path = os.environ.get("OUTPUT_PATH", "dist/index.html")
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[完成] HTML 已生成: {out_path} ({len(html)} bytes)")
    print(f"[数据统计] EM行情:{len(all_data['em_quotes'])} YF行情:{len(all_data['yf_quotes'])} "
          f"加密:{len(all_data['crypto'])} 板块:{len(all_data['sectors'])} "
          f"宏观:{len(all_data['macro'])} "
          f"breadth:{'Y' if all_data['breadth'] else 'N'} "
          f"margin:{'Y' if all_data['margin'] else 'N'} "
          f"north:{'Y' if all_data['north'] else 'N'} "
          f"etf:{'Y' if all_data.get('etf') else 'N'} "
          f"turnover:{'Y' if all_data.get('turnover') else 'N'} "
          f"limit_stats:{'Y' if all_data.get('limit_stats') else 'N'} "
          f"futures_basis:{'Y' if all_data.get('futures_basis') else 'N'}")


if __name__ == "__main__":
    main()
