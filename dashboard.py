#!/usr/bin/env python3
"""
每日金融市场全景看板生成器
数据源: yfinance (主) + akshare (辅) + requests (补充)
输出: index.html
"""

import json
import os
import sys
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
        print(f"[WARN] fetch_json failed: {url} -> {e}")
        return None

# ---------------------------------------------------------------------------
# 数据获取 — yfinance
# ---------------------------------------------------------------------------

def get_yf_quotes():
    """用 yfinance 批量获取行情快照"""
    try:
        import yfinance as yf
    except ImportError:
        print("[ERROR] yfinance not installed")
        return {}

    # yfinance ticker -> 人类可读名称
    tickers = {
        "000001.SS": "上证指数",
        "399001.SZ": "深证成指",
        "399006.SZ": "创业板指",
        "000688.SS": "科创50",
        "^HSI": "恒生指数",
        "^DJI": "道琼斯",
        "^IXIC": "纳斯达克",
        "^GSPC": "标普500",
        "^VIX": "VIX恐慌指数",
        "^TNX": "美债10Y",
        "^TYX": "美债30Y",
        "^IRX": "美债2Y",
        "GC=F": "黄金",
        "SI=F": "白银",
        "HG=F": "铜",
        "CL=F": "WTI原油",
        "BZ=F": "布伦特原油",
        "DX-Y.NYB": "美元指数",
        "CNY=X": "美元兑人民币",
        "HKDCNY=X": "港元兑人民币",
        "BTC-USD": "比特币",
        "ETH-USD": "以太坊",
    }

    result = {}
    symbols = list(tickers.keys())
    try:
        data = yf.download(symbols, period="5d", progress=False, threads=True)
        if data.empty:
            print("[WARN] yfinance returned empty data")
            return result

        # 取最近两个交易日
        hist = data["Close"]
        if len(hist) < 2:
            print("[WARN] yfinance insufficient history")
            return result

        latest = hist.iloc[-1]
        prev = hist.iloc[-2]

        for sym, name in tickers.items():
            try:
                if sym in latest.index and sym in prev.index:
                    cur = safe_float(latest[sym])
                    old = safe_float(prev[sym])
                    if cur > 0 and old > 0:
                        chg = (cur - old) / old * 100
                        result[name] = {"price": cur, "change": chg}
            except Exception:
                continue
    except Exception as e:
        print(f"[ERROR] yfinance download failed: {e}")
        traceback.print_exc()

    return result

# ---------------------------------------------------------------------------
# 数据获取 — akshare (A 股板块 / 涨跌停 / 两融 / 北向)
# ---------------------------------------------------------------------------

def get_ak_sectors():
    """获取申万/东财行业板块涨幅"""
    try:
        import akshare as ak
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return []
        # 按涨跌幅排序
        col = None
        for c in df.columns:
            if "涨跌幅" in c:
                col = c
                break
        if col is None:
            return []
        df = df.sort_values(by=col, ascending=False)
        top = df.head(8).to_dict("records")
        bottom = df.tail(5).to_dict("records")
        sectors = []
        for r in top + bottom:
            name = ""
            for c in df.columns:
                if "板块名" in c or "名称" in c:
                    name = r[c]
                    break
            chg = safe_float(r.get(col, 0))
            sectors.append({"name": name, "change": chg})
        return sectors
    except Exception as e:
        print(f"[WARN] akshare sectors failed: {e}")
        return []

def get_ak_margin():
    """获取两融余额"""
    try:
        import akshare as ak
        df = ak.stock_margin_underlying_info_szse()
        if df is not None and not df.empty:
            return df
    except Exception:
        pass
    try:
        import akshare as ak
        df = ak.stock_margin_sse(start_date=(NOW - timedelta(days=10)).strftime("%Y%m%d"))
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else None
            val = safe_float(latest.get("融资融券余额", 0))
            prev_val = safe_float(prev.get("融资融券余额", 0)) if prev is not None else 0
            chg = val - prev_val
            return {"value": val, "change": chg}
    except Exception as e:
        print(f"[WARN] akshare margin failed: {e}")
    return None

def get_ak_north():
    """获取北向资金"""
    try:
        import akshare as ak
        df = ak.stock_hsgt_north_net_flow_in_em(symbol="北上")
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            val = safe_float(latest.get("当日成交净买额", 0)) / 1e8
            return {"value": val}
    except Exception as e:
        print(f"[WARN] akshare north failed: {e}")
    return None

def get_ak_macro():
    """获取宏观经济数据 (PMI / CPI / PPI / M1 / M2)"""
    macro = {}
    # PMI
    try:
        import akshare as ak
        df = ak.macro_china_pmi()
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            macro["pmi"] = safe_float(latest.get("制造业PMI", latest.iloc[1] if len(latest) > 1 else 0))
    except Exception:
        pass
    # CPI
    try:
        import akshare as ak
        df = ak.macro_china_cpi_yearly()
        if df is not None and not df.empty:
            macro["cpi"] = safe_float(df.iloc[-1, 1])
    except Exception:
        pass
    # PPI
    try:
        import akshare as ak
        df = ak.macro_china_ppi_yearly()
        if df is not None and not df.empty:
            macro["ppi"] = safe_float(df.iloc[-1, 1])
    except Exception:
        pass
    # M1 M2
    try:
        import akshare as ak
        df = ak.macro_china_money_supply()
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            macro["m2"] = safe_float(latest.get("M2同比增长", 0))
            macro["m1"] = safe_float(latest.get("M1同比增长", 0))
    except Exception:
        pass
    return macro

def get_ak_market_breadth():
    """获取涨跌家数"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return None
        col = None
        for c in df.columns:
            if "涨跌幅" in c:
                col = c
                break
        if col is None:
            return None
        changes = df[col].astype(float)
        up = int((changes > 0).sum())
        down = int((changes < 0).sum())
        flat = int((changes == 0).sum())
        limit_up = int((changes >= 9.9).sum())
        limit_down = int((changes <= -9.9).sum())
        return {"up": up, "down": down, "flat": flat, "limit_up": limit_up, "limit_down": limit_down}
    except Exception as e:
        print(f"[WARN] market breadth failed: {e}")
        return None

# ---------------------------------------------------------------------------
# 数据获取 — 补充 API
# ---------------------------------------------------------------------------

def get_crypto():
    """CoinGecko 加密货币"""
    data = fetch_json(
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
    )
    result = {}
    if data:
        try:
            btc_price = data["bitcoin"]["usd"]
            btc_chg = data["bitcoin"].get("usd_24h_change", 0)
            result["比特币"] = {"price": btc_price, "change": btc_chg}
        except (KeyError, TypeError):
            pass
        try:
            eth_price = data["ethereum"]["usd"]
            eth_chg = data["ethereum"].get("usd_24h_change", 0)
            result["以太坊"] = {"price": eth_price, "change": eth_chg}
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
    q = d.get("quotes", {})
    sectors = d.get("sectors", [])
    breadth = d.get("breadth")
    margin = d.get("margin")
    north = d.get("north")
    macro = d.get("macro", {})
    fear = d.get("fear_greed")

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
        parts.append(f"VIX{vix['price']:.1f}({fmt_pct(vix['change'])})")
    if gold:
        parts.append(f"黄金${gold['price']:.0f}({fmt_pct(gold['change'])})")
    if oil:
        parts.append(f"WTI${oil['price']:.1f}({fmt_pct(oil['change'])})")
    if tnk:
        parts.append(f"美债10Y {tnk['price']:.2f}%")

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
    if north:
        cls = "down" if north["value"] < 0 else "up"
        signals.append({"cls": cls, "text": f"北向{north['value']:+.1f}亿"})
    if margin and margin.get("change"):
        chg = margin["change"] / 1e8
        cls = "down" if chg < 0 else "up"
        signals.append({"cls": cls, "text": f"两融{chg:+.0f}亿"})
    if fear:
        signals.append({"cls": "neutral", "text": f"恐惧贪婪{fear['value']} {fear['class']}"})

    # --- 经济基本面 ---
    macro_cards = ""
    if macro:
        pmi = macro.get("pmi")
        cpi = macro.get("cpi")
        ppi = macro.get("ppi")
        m1 = macro.get("m1")
        m2 = macro.get("m2")
        macro_cards = ""
        if pmi:
            cls = "up" if pmi >= 50 else "down"
            macro_cards += build_card("制造业PMI", f"{pmi:.1f}%", f'<span class="change {cls}">{"扩张" if pmi >= 50 else "收缩"}区间</span>')
        if cpi:
            macro_cards += build_card("CPI同比", f"+{cpi:.1f}%", '<span class="change neutral">最新数据</span>')
        if ppi:
            macro_cards += build_card("PPI同比", f"+{ppi:.1f}%", '<span class="change neutral">最新数据</span>')
        if m2:
            macro_cards += build_card("M2同比", f"+{m2:.1f}%", '<span class="change neutral">最新数据</span>')
        if m1:
            macro_cards += build_card("M1同比", f"+{m1:.1f}%", '<span class="change neutral">最新数据</span>')
        if m1 and m2:
            sc = m2 - m1
            macro_cards += build_card("M2-M1剪刀差", f"{sc:.1f}pct", '<span class="change neutral">剪刀差</span>')
    if not macro_cards:
        macro_cards = build_card("宏观数据", "暂无最新数据", '<span class="change neutral">待更新</span>')

    # --- 情绪面 ---
    emo_cards = ""
    for name in ["上证指数", "深证成指", "创业板指", "科创50", "恒生指数"]:
        v = q.get(name)
        if v:
            chg = v["change"]
            cls = "up" if chg >= 0 else "down"
            emo_cards += build_card(name, f"{v['price']:.2f}", f'<span class="change {cls}">{fmt_pct(chg)}</span>')
    if breadth:
        emo_cards += build_card("涨跌家数", f"{breadth['up']} / {breadth['down']}", f'<span class="change {"up" if breadth["up"] > breadth["down"] else "down"}">涨停{breadth["limit_up"]} 跌停{breadth["limit_down"]}</span>')
    if vix:
        cls = "down" if vix["change"] > 0 else "up"
        emo_cards += build_card("VIX恐慌指数", f"{vix['price']:.2f}", f'<span class="change {cls}">{fmt_pct(vix["change"])}</span>')
    if fear:
        emo_cards += build_card("恐惧贪婪指数", f"{fear['value']}", f'<span class="change neutral">{fear["class"]}</span>')

    # --- 资金面 ---
    fund_cards = ""
    if margin:
        val = margin.get("value", 0) / 1e8
        chg = margin.get("change", 0) / 1e8
        cls = "down" if chg < 0 else "up"
        fund_cards += build_card("两融余额", f"{val:.0f}亿", f'<span class="change {cls}">较前日{chg:+.0f}亿</span>')
    if north:
        cls = "down" if north["value"] < 0 else "up"
        fund_cards += build_card("北向资金", f"{north['value']:+.1f}亿", f'<span class="change {cls}">净{"卖出" if north["value"] < 0 else "买入"}</span>')
    # 成交额 — yfinance 不直接提供, 用板块数据补
    fund_cards += build_card("两市成交额", "暂无数据", '<span class="change neutral">待补充</span>')
    fund_cards += build_card("ETF资金", "暂无最新数据", '<span class="change neutral">待补充</span>')

    # --- 外部市场 ---
    ext_cards = ""
    for label, key in [("美元指数", "美元指数"), ("美元/人民币", "美元兑人民币"),
                        ("伦敦金", "黄金"), ("白银", "白银"), ("LME铜", "铜"),
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
            ext_cards += build_card(label, price_str, f'<span class="change {cls}">{fmt_pct(chg)}</span>')

    # --- 美债 ---
    bond_cards = ""
    for label, key in [("美债2Y", "美债2Y"), ("美债10Y", "美债10Y"), ("美债30Y", "美债30Y")]:
        v = q.get(key)
        if v:
            chg = v["change"]
            cls = "down" if chg > 0 else "up"  # 收益率上升 = 利空
            bond_cards += build_card(label, f"{v['price']:.3f}%", f'<span class="change {cls}">{fmt_pct(chg)}bp</span>')
    # 10Y-2Y 利差
    t2 = q.get("美债2Y")
    t10 = q.get("美债10Y")
    if t2 and t10:
        spread = t10["price"] - t2["price"]
        bond_cards += build_card("10Y-2Y利差", f"{spread:.3f}%", '<span class="change neutral">收益率曲线</span>')

    # --- 板块图表数据 ---
    top_sectors = [s for s in sectors if s["change"] > 0][:8]
    bottom_sectors = [s for s in sectors if s["change"] < 0][-5:] if len(sectors) > 8 else []

    # --- ECharts 图表 ---
    # 图1: A股+美股指数涨跌幅
    idx_data = []
    for name in ["上证指数", "深证成指", "创业板指", "科创50", "恒生指数", "道琼斯", "纳斯达克", "标普500"]:
        v = q.get(name)
        if v:
            idx_data.append({"name": name, "value": round(v["change"], 2)})

    # 图2: 外部资产涨跌幅
    ext_data = []
    for name in ["黄金", "白银", "铜", "WTI原油", "布伦特原油", "美元指数", "比特币", "以太坊"]:
        v = q.get(name)
        if v:
            ext_data.append({"name": name, "value": round(v["change"], 2)})

    # 图3: 板块涨跌幅
    sec_data = [{"name": s["name"], "value": round(s["change"], 2)} for s in sectors[:13]] if sectors else []

    # 图4: 美债收益率曲线
    bond_data = []
    for label, key in [("2Y", "美债2Y"), ("10Y", "美债10Y"), ("30Y", "美债30Y")]:
        v = q.get(key)
        if v:
            bond_data.append({"name": label, "value": round(v["price"], 3)})

    # 图5: VIX & 恐惧贪婪
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

<!-- 一、经济基本面 -->
<div class="section">
<div class="section-title">一、经济基本面</div>
<div class="cards">{macro_cards}</div>
</div>

<!-- 二、情绪面 -->
<div class="section">
<div class="section-title">二、情绪面</div>
<div class="cards">{emo_cards}</div>
</div>

<!-- 三、资金面 -->
<div class="section">
<div class="section-title">三、资金面</div>
<div class="cards">{fund_cards}</div>
</div>

<!-- 四、外部市场 -->
<div class="section">
<div class="section-title">四、外部市场</div>
<div class="cards">{ext_cards}</div>
</div>

<!-- 五、美债收益率 -->
<div class="section">
<div class="section-title">五、美债收益率</div>
<div class="cards">{bond_cards}</div>
</div>

<!-- 图表区域 -->
<div class="chart-box"><div class="chart-title">主要指数涨跌幅 (%)</div><div id="chart1" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">外部资产涨跌幅 (%)</div><div id="chart2" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">行业板块涨跌幅 Top (%)</div><div id="chart3" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">美债收益率曲线 (%)</div><div id="chart4" style="width:100%;height:360px"></div></div>
<div class="chart-box"><div class="chart-title">风险指标</div><div id="chart5" style="width:100%;height:360px"></div></div>

<div class="footer">
<p>数据来源: Yahoo Finance / akshare / CoinGecko | GitHub Actions 每日自动生成 | Cloudflare Pages 托管</p>
<p>中国股市颜色规则: 涨红跌绿 | 生成时间: {NOW_STR}</p>
</div>

</div>
<script>
var idxData = {chart_idx};
var extData = {chart_ext};
var secData = {chart_sec};
var bondData = {chart_bond};
var riskData = {chart_risk};

function makeBar(elId, data, title){{
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

if(idxData.length > 0) makeBar('chart1', idxData, '主要指数');
if(extData.length > 0) makeBar('chart2', extData, '外部资产');
if(secData.length > 0) makeHBBar('chart3', secData);
if(bondData.length > 0) makeBar('chart4', bondData, '美债收益率');
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

    # 1. yfinance 行情
    print(">> 获取 yfinance 行情...")
    all_data["quotes"] = get_yf_quotes()
    print(f"   获取到 {len(all_data['quotes'])} 个品种")

    # 2. CoinGecko 加密货币 (补充)
    if "比特币" not in all_data["quotes"] or "以太坊" not in all_data["quotes"]:
        print(">> 获取 CoinGecko 加密货币...")
        crypto = get_crypto()
        all_data["quotes"].update(crypto)

    # 3. akshare 板块
    print(">> 获取 akshare 板块数据...")
    all_data["sectors"] = get_ak_sectors()

    # 4. akshare 涨跌家数
    print(">> 获取涨跌家数...")
    all_data["breadth"] = get_ak_market_breadth()

    # 5. akshare 两融
    print(">> 获取两融余额...")
    all_data["margin"] = get_ak_margin()

    # 6. akshare 北向资金
    print(">> 获取北向资金...")
    all_data["north"] = get_ak_north()

    # 7. akshare 宏观
    print(">> 获取宏观经济数据...")
    all_data["macro"] = get_ak_macro()

    # 8. 恐惧贪婪指数
    print(">> 获取恐惧贪婪指数...")
    all_data["fear_greed"] = get_fear_greed()

    # 9. 生成 HTML
    print(">> 生成 HTML...")
    html = generate_html(all_data)

    # 10. 写入文件
    out_path = os.environ.get("OUTPUT_PATH", "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[完成] HTML 已生成: {out_path} ({len(html)} bytes)")
    print(f"[数据统计] 行情:{len(all_data['quotes'])} 板块:{len(all_data['sectors'])} "
          f"宏观:{len(all_data['macro'])} "
          f"breadth:{'Y' if all_data['breadth'] else 'N'} "
          f"margin:{'Y' if all_data['margin'] else 'N'} "
          f"north:{'Y' if all_data['north'] else 'N'}")

if __name__ == "__main__":
    main()
