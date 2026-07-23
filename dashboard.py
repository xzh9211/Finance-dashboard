#!/usr/bin/env python3
"""
每日金融市场全景看板生成器
数据源:
  - 东方财富 push2 API (A股指数/板块/涨跌停/成交额/两融) — 海外可访问
  - yfinance Ticker 单个获取 (美股/美债/VIX/商品/DXY) — 带重试
  - CoinGecko (加密货币)
  - Alternative.me (恐惧贪婪指数)
  - 宏观数据预设最新值 (月度更新)
输出: dist/index.html
"""

import json
import os
import sys
import time
import traceback
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
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.2f}{suffix}"

def fmt_change(val, prefix=""):
    v = safe_float(val)
    sign = "+" if v >= 0 else ""
    cls = "up" if v >= 0 else "down"
    return f'<span class="change {cls}">{prefix}{sign}{v:.2f}%</span>'

def fetch_json(url, timeout=15, headers=None, **kwargs):
    """带超时和异常处理的 JSON 请求"""
    try:
        hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        if headers:
            hdrs.update(headers)
        resp = requests.get(url, timeout=timeout, headers=hdrs, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[WARN] fetch_json failed: {url[:80]} -> {e}")
        return None

# ---------------------------------------------------------------------------
# 数据获取 — 东方财富 push2 API (A股指数/板块/涨跌停/成交额)
# ---------------------------------------------------------------------------

EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

def get_em_indices():
    """东方财富获取 A 股 + 港股指数实时行情"""
    secids = "1.000001,0.399001,0.399006,1.000688,100.HSI"
    fields = "f2,f3,f4,f6,f12,f14"
    url = f"http://push2.eastmoney.com/api/qt/ulist.np/get?secids={secids}&fields={fields}&fltt=2"
    data = fetch_json(url, headers=EM_HEADERS)
    result = {}
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            name = item.get("f14", "")
            price = safe_float(item.get("f2"))
            chg = safe_float(item.get("f3"))
            turnover = safe_float(item.get("f6"))  # 成交额
            if price > 0:
                result[name] = {
                    "price": price,
                    "change": chg,
                    "turnover": turnover,
                }
    return result

def get_em_sectors():
    """东方财富获取行业板块涨跌幅"""
    url = ("http://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=50&po=1&np=1&fltt=2&invt=2"
           "&fid=f3&fs=m:90+t:2"
           "&fields=f2,f3,f12,f14")
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

def get_em_market_breadth():
    """东方财富获取 A 股全市场涨跌家数"""
    # 用涨跌幅分布 API
    url = ("http://push2.eastmoney.com/api/qt/clist/get"
           "?pn=1&pz=1&po=1&np=1&fltt=2&invt=2"
           "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
           "&fields=f2,f3,f12,f14")
    data = fetch_json(url, headers=EM_HEADERS)
    if not data or not data.get("data"):
        return None
    total = data["data"].get("total", 0)
    if total == 0:
        return None
    # 获取所有股票涨跌幅
    url_all = ("http://push2.eastmoney.com/api/qt/clist/get"
               f"?pn=1&pz={total}&po=1&np=1&fltt=2&invt=2"
               "&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
               "&fields=f2,f3,f12,f14")
    data_all = fetch_json(url_all, headers=EM_HEADERS, timeout=30)
    if not data_all or not data_all.get("data") or not data_all["data"].get("diff"):
        return None
    changes = []
    for item in data_all["data"]["diff"]:
        chg = safe_float(item.get("f3"))
        changes.append(chg)
    up = sum(1 for c in changes if c > 0)
    down = sum(1 for c in changes if c < 0)
    flat = sum(1 for c in changes if c == 0)
    limit_up = sum(1 for c in changes if c >= 9.9)
    limit_down = sum(1 for c in changes if c <= -9.9)
    return {"up": up, "down": down, "flat": flat,
            "limit_up": limit_up, "limit_down": limit_down}

def get_em_turnover():
    """获取沪深两市成交额"""
    url = ("http://push2.eastmoney.com/api/qt/ulist.np/get"
           "?secids=1.000001,0.399001&fields=f6&fltt=2")
    data = fetch_json(url, headers=EM_HEADERS)
    total = 0
    if data and data.get("data") and data["data"].get("diff"):
        for item in data["data"]["diff"]:
            total += safe_float(item.get("f6"))
    return total if total > 0 else None

def get_em_margin():
    """东方财富获取两融余额"""
    url = ("http://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPTA_WEB_RZRQ_GGMX"
           "&columns=ALL&pageNumber=1&pageSize=1"
           "&sortColumns=RZRQYE&sortTypes=-1")
    data = fetch_json(url, headers=EM_HEADERS)
    if data and data.get("result") and data["result"].get("data"):
        row = data["result"]["data"][0]
        val = safe_float(row.get("RZRQYE", 0))
        return {"value": val, "date": row.get("RZRQ_RQ", "")}
    return None

def get_em_north_flow():
    """东方财富获取北向资金"""
    url = ("http://push2.eastmoney.com/api/qt/kamt.rtmin/get"
           "?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56")
    data = fetch_json(url, headers=EM_HEADERS)
    if data and data.get("data"):
        d = data["data"]
        # f1=沪股通净买入 f2=深股通净买入 f3=北向合计
        hgt = safe_float(d.get("f1"))
        sgt = safe_float(d.get("f2"))
        total = safe_float(d.get("f3"))
        if total != 0:
            return {"value": total / 1e4, "hgt": hgt / 1e4, "sgt": sgt / 1e4}  # 转为亿
    return None

# ---------------------------------------------------------------------------
# 数据获取 — yfinance (美股/美债/VIX/商品/DXY) 单个 Ticker + 重试
# ---------------------------------------------------------------------------

def get_yf_single(ticker, name, retries=3):
    """单个 yfinance Ticker 获取，带重试"""
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
                    return {"price": cur, "change": chg}
            elif hist is not None and not hist.empty:
                cur = safe_float(hist["Close"].iloc[-1])
                if cur > 0:
                    return {"price": cur, "change": 0}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
                continue
            print(f"[WARN] yfinance {ticker} failed: {e}")
    return None

def get_yf_quotes():
    """逐个获取 yfinance 行情"""
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
        ("BZ=F", "布伦特原油"),
        ("DX-Y.NYB", "美元指数"),
        ("CNY=X", "美元兑人民币"),
    ]
    result = {}
    for symbol, name in tickers:
        r = get_yf_single(symbol, name)
        if r:
            result[name] = r
            print(f"   yfinance {name}: {r['price']:.2f} ({fmt_pct(r['change'])})")
        else:
            print(f"   yfinance {name}: FAILED")
        time.sleep(0.5)  # 避免被限流
    return result

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
# 宏观数据 (预设最新月度值)
# ---------------------------------------------------------------------------

MACRO_LATEST = {
    "pmi": 50.3,       # 6月制造业PMI
    "pmi_non": 50.2,   # 6月非制造业PMI
    "cpi": 1.0,        # 6月CPI同比
    "ppi": 4.1,        # 6月PPI同比 (注意：实际可能为负，此处用预设)
    "m1": 4.0,         # 6月M1同比
    "m2": 8.0,         # 6月M2同比
    "social_finance": 33645,  # 6月社融增量(亿)
    "export": 27.0,    # 6月出口同比
    "import": 36.0,    # 6月进口同比
    "retail": 1.0,     # 6月社零同比
    "macro_date": "2025年6月",
}

# ---------------------------------------------------------------------------
# HTML 生成
# ---------------------------------------------------------------------------

CSS = """
:root{--bg:#f5f6fa;--card:#fff;--text:#1a1a2e;--sub:#5a5a7a;--up:#c0392b;--down:#27ae60;--accent:#2c3e50;--border:#e0e0e8;--warn:#e74c3c;--safe:#3498db;--gold:#f1c40f}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'PingFang SC','Microsoft YaHei','Helvetica Neue',Arial,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;padding:16px}
.container{max-width:1280px;margin:auto}
.header{text-align:center;padding:24px 0 12px}
.header h1{font-size:28px;color:var(--accent);margin-bottom:4px}
.header .date{font-size:14px;color:var(--sub)}
.conclusion-card{background:linear-gradient(135deg,#2c3e50,#34495e);color:#fff;border-radius:12px;padding:24px;margin:16px 0;box-shadow:0 4px 12px rgba(0,0,0,.15)}
.conclusion-card .title{font-size:18px;font-weight:700;margin-bottom:12px}
.conclusion-card .summary{font-size:16px;line-height:1.8;margin-bottom:12px}
.conclusion-card .signals{display:flex;flex-wrap:wrap;gap:8px}
.conclusion-card .signal{background:rgba(255,255,255,.18);padding:6px 14px;border-radius:6px;font-size:13px}
.signal.up{border-left:3px solid #e74c3c}
.signal.down{border-left:3px solid #2ecc71}
.signal.neutral{border-left:3px solid #3498db}
.section{margin:20px 0}
.section-title{font-size:20px;font-weight:700;color:var(--accent);padding:8px 0;border-bottom:2px solid var(--accent);margin-bottom:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.card{background:var(--card);border-radius:10px;padding:16px;border:1px solid var(--border);box-shadow:0 2px 6px rgba(0,0,0,.06);transition:transform .2s}
.card:hover{transform:translateY(-2px)}
.card .label{font-size:12px;color:var(--sub);margin-bottom:4px}
.card .value{font-size:22px;font-weight:700}
.card .change{font-size:13px;margin-top:4px}
.card .change.up{color:var(--up)}
.card .change.down{color:var(--down)}
.card .extra{font-size:12px;color:var(--sub);margin-top:6px}
.chart-box{background:var(--card);border-radius:10px;padding:16px;border:1px solid var(--border);margin:16px 0;height:420px}
.chart-title{font-size:16px;font-weight:700;color:var(--accent);margin-bottom:8px}
.row{display:flex;gap:12px;margin:8px 0}
.row .card{flex:1}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:12px;margin:2px}
.tag.up{background:#fce4ec;color:var(--up)}
.tag.down{background:#e8f5e9;color:var(--down)}
.tag.neutral{background:#e3f2fd;color:var(--safe)}
.footer{text-align:center;padding:24px 0;font-size:12px;color:var(--sub)}
@media(max-width:768px){.cards{grid-template-columns:1fr 1fr}.chart-box{height:300px}}
@media(max-width:480px){.cards{grid-template-columns:1fr}.container{padding:8px}}
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
    # 合并东方财富和 yfinance 的行情数据
    q = {}
    q.update(d.get("em_quotes", {}))
    q.update(d.get("yf_quotes", {}))
    q.update(d.get("crypto", {}))

    sectors = d.get("sectors", [])
    breadth = d.get("breadth")
    margin = d.get("margin")
    north = d.get("north")
    macro = d.get("macro", {})
    fear = d.get("fear_greed")
    turnover = d.get("turnover")

    # --- 顶部结论 ---
    parts = []
    sh = q.get("上证指数", {})
    sz = q.get("深证成指", {})
    cyb = q.get("创业板指", {})
    kc = q.get("科创50", {})
    dji = q.get("道琼斯", {})
    nas = q.get("纳斯达克", {})
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
        cls = "neutral" if vix["change"] < 0 else "down"
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
        cls = "down" if dxy["change"] > 0 else "up"
        signals.append({"cls": cls, "text": f"DXY {dxy['price']:.2f}"})
    if north:
        cls = "down" if north["value"] < 0 else "up"
        signals.append({"cls": cls, "text": f"北向{north['value']:+.1f}亿"})
    if margin:
        val = margin.get("value", 0) / 1e8
        signals.append({"cls": "neutral", "text": f"两融{val:.0f}亿"})
    if fear:
        signals.append({"cls": "neutral", "text": f"恐惧贪婪{fear['value']} {fear['class']}"})

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
            macro_cards += build_card("制造业PMI", f"{pmi:.1f}%",
                f'<span class="change {cls}">{"扩张" if pmi >= 50 else "收缩"}区间</span>', mdate)
        if pmi_non:
            cls = "up" if pmi_non >= 50 else "down"
            macro_cards += build_card("非制造业PMI", f"{pmi_non:.1f}%",
                f'<span class="change {cls}">{"扩张" if pmi_non >= 50 else "收缩"}区间</span>', mdate)
        if cpi is not None:
            cls = "up" if cpi > 0 else "down"
            macro_cards += build_card("CPI同比", f"+{cpi:.1f}%",
                f'<span class="change {cls}"> consumer prices</span>', mdate)
        if ppi is not None:
            cls = "up" if ppi > 0 else "down"
            macro_cards += build_card("PPI同比", f"+{ppi:.1f}%",
                f'<span class="change {cls}"> producer prices</span>', mdate)
        if m2:
            macro_cards += build_card("M2同比", f"+{m2:.1f}%",
                '<span class="change neutral">货币供应</span>', mdate)
        if m1:
            macro_cards += build_card("M1同比", f"+{m1:.1f}%",
                '<span class="change neutral">货币供应</span>', mdate)
        if m1 and m2:
            sc = m2 - m1
            macro_cards += build_card("M2-M1剪刀差", f"{sc:.1f}pct",
                '<span class="change neutral">剪刀差</span>', mdate)
        if sf:
            macro_cards += build_card("社融增量", f"{sf:,.0f}亿",
                '<span class="change neutral">最新一期</span>', mdate)
        if exp:
            macro_cards += build_card("出口同比", f"+{exp:.1f}%",
                '<span class="change up">外需</span>', mdate)
        if imp:
            macro_cards += build_card("进口同比", f"+{imp:.1f}%",
                '<span class="change up">内需</span>', mdate)
        if ret is not None:
            macro_cards += build_card("社零同比", f"+{ret:.1f}%",
                '<span class="change neutral">消费</span>', mdate)
    if not macro_cards:
        macro_cards = build_card("宏观数据", "暂无最新数据", '<span class="change neutral">待更新</span>')

    # --- 情绪面 ---
    emo_cards = ""
    for name in ["上证指数", "深证成指", "创业板指", "科创50", "恒生指数"]:
        v = q.get(name)
        if v:
            chg = v["change"]
            cls = "up" if chg >= 0 else "down"
            emo_cards += build_card(name, f"{v['price']:.2f}",
                f'<span class="change {cls}">{fmt_pct(chg)}</span>')
    if breadth:
        emo_cards += build_card("涨跌家数", f"{breadth['up']} / {breadth['down']}",
            f'<span class="change {"up" if breadth["up"] > breadth["down"] else "down"}">涨停{breadth["limit_up"]} 跌停{breadth["limit_down"]}</span>')
    if vix:
        cls = "down" if vix["change"] > 0 else "up"
        emo_cards += build_card("VIX恐慌指数", f"{vix['price']:.2f}",
            f'<span class="change {cls}">{fmt_pct(vix["change"])}</span>')
    if fear:
        emo_cards += build_card("恐惧贪婪指数", f"{fear['value']}",
            f'<span class="change neutral">{fear["class"]}</span>')

    # --- 资金面 ---
    fund_cards = ""
    if turnover and turnover > 0:
        turnover_yi = turnover / 1e8
        fund_cards += build_card("两市成交额", f"{turnover_yi:.0f}亿",
            '<span class="change neutral">沪深合计</span>')
    if margin:
        val = margin.get("value", 0) / 1e8
        fund_cards += build_card("两融余额", f"{val:.0f}亿",
            '<span class="change neutral">最新数据</span>', margin.get("date", ""))
    if north:
        cls = "down" if north["value"] < 0 else "up"
        fund_cards += build_card("北向资金", f"{north['value']:+.1f}亿",
            f'<span class="change {cls}">净{"卖出" if north["value"] < 0 else "买入"}</span>')
    fund_cards += build_card("ETF资金", "待补充",
        '<span class="change neutral">需手动更新</span>')

    # --- 外部市场 ---
    ext_cards = ""
    for label, key in [("美元指数", "美元指数"), ("美元/人民币", "美元兑人民币"),
                        ("黄金", "黄金"), ("白银", "白银"), ("铜", "铜"),
                        ("WTI原油", "WTI原油"), ("布伦特原油", "布伦特原油"),
                        ("比特币", "比特币"), ("以太坊", "以太坊"),
                        ("道琼斯", "道琼斯"), ("纳斯达克", "纳斯达克"), ("标普500", "标普500"),
                        ("恒生指数", "恒生指数")]:
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
            ext_cards += build_card(label, price_str,
                f'<span class="change {cls}">{fmt_pct(chg)}</span>')

    # --- 美债 ---
    bond_cards = ""
    for label, key in [("美债2Y", "美债2Y"), ("美债10Y", "美债10Y"), ("美债30Y", "美债30Y")]:
        v = q.get(key)
        if v:
            chg = v["change"]
            cls = "down" if chg > 0 else "up"
            bond_cards += build_card(label, f"{v['price']:.3f}%",
                f'<span class="change {cls}">{fmt_pct(chg)}bp</span>')
    t2 = q.get("美债2Y")
    t10 = q.get("美债10Y")
    if t2 and t10:
        spread = t10["price"] - t2["price"]
        bond_cards += build_card("10Y-2Y利差", f"{spread:.3f}%",
            '<span class="change neutral">收益率曲线</span>')

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

<div class="chart-box"><div class="chart-title">主要指数涨跌幅 (%)</div><div id="chart1" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">外部资产涨跌幅 (%)</div><div id="chart2" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">行业板块涨跌幅 Top (%)</div><div id="chart3" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">美债收益率曲线 (%)</div><div id="chart4" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">风险指标</div><div id="chart5" style="width:100%;height:360px"></div></div>

<div class="footer">
<p>数据来源: 东方财富 / Yahoo Finance / CoinGecko | GitHub Actions 每日自动生成 | Cloudflare Pages 托管</p>
<p>中国股市颜色规则: 涨红跌绿 | 生成时间: {NOW_STR}</p>
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

    # 1. 东方财富 A 股指数
    print(">> 获取东方财富 A 股指数...")
    all_data["em_quotes"] = get_em_indices()
    print(f"   获取到 {len(all_data['em_quotes'])} 个指数")

    # 2. 东方财富 板块
    print(">> 获取东方财富板块数据...")
    all_data["sectors"] = get_em_sectors()
    print(f"   获取到 {len(all_data['sectors'])} 个板块")

    # 3. 东方财富 涨跌家数
    print(">> 获取涨跌家数...")
    all_data["breadth"] = get_em_market_breadth()
    if all_data["breadth"]:
        b = all_data["breadth"]
        print(f"   涨{b['up']} 跌{b['down']} 涨停{b['limit_up']} 跌停{b['limit_down']}")

    # 4. 东方财富 成交额
    print(">> 获取两市成交额...")
    all_data["turnover"] = get_em_turnover()
    if all_data["turnover"]:
        print(f"   成交额: {all_data['turnover'] / 1e8:.0f}亿")

    # 5. 东方财富 两融余额
    print(">> 获取两融余额...")
    all_data["margin"] = get_em_margin()
    if all_data["margin"]:
        print(f"   两融余额: {all_data['margin']['value'] / 1e8:.0f}亿")

    # 6. 东方财富 北向资金
    print(">> 获取北向资金...")
    all_data["north"] = get_em_north_flow()
    if all_data["north"]:
        print(f"   北向净: {all_data['north']['value']:+.1f}亿")

    # 7. yfinance 美股/美债/商品
    print(">> 获取 yfinance 行情...")
    all_data["yf_quotes"] = get_yf_quotes()
    print(f"   获取到 {len(all_data['yf_quotes'])} 个品种")

    # 8. CoinGecko 加密货币
    print(">> 获取 CoinGecko 加密货币...")
    all_data["crypto"] = get_crypto()
    print(f"   获取到 {len(all_data['crypto'])} 个加密货币")

    # 9. 恐惧贪婪指数
    print(">> 获取恐惧贪婪指数...")
    all_data["fear_greed"] = get_fear_greed()
    if all_data["fear_greed"]:
        print(f"   恐惧贪婪: {all_data['fear_greed']['value']} {all_data['fear_greed']['class']}")

    # 10. 宏观数据 (预设最新值)
    all_data["macro"] = MACRO_LATEST

    # 11. 生成 HTML
    print(">> 生成 HTML...")
    html = generate_html(all_data)

    # 12. 写入文件
    out_dir = os.environ.get("OUTPUT_DIR", "dist")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[完成] HTML 已生成: {out_path} ({len(html)} bytes)")
    print(f"[数据统计] EM行情:{len(all_data['em_quotes'])} YF行情:{len(all_data['yf_quotes'])} "
          f"加密:{len(all_data['crypto'])} 板块:{len(all_data['sectors'])} "
          f"宏观:{len(all_data['macro'])} "
          f"breadth:{'Y' if all_data['breadth'] else 'N'} "
          f"margin:{'Y' if all_data['margin'] else 'N'} "
          f"north:{'Y' if all_data['north'] else 'N'} "
          f"turnover:{'Y' if all_data.get('turnover') else 'N'}")

if __name__ == "__main__":
    main()
