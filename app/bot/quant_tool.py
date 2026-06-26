"""
量化查询工具 — 享客虾 Bot 工具集
读取本地量化日报数据，返回格式化信号摘要
"""
import json
import os
from datetime import datetime

HOME = os.path.expanduser("~")
STOCK_QUANT = os.path.join(HOME, "stock-quant")
CRYPTO_QUANT = os.path.join(HOME, "crypto-quant")


def read_quant_signals():
    """读取最新 A股/美股/港股 量化信号"""
    signals = {}
    snapshot_path = os.path.join(STOCK_QUANT, "snapshot.json")
    if os.path.exists(snapshot_path):
        try:
            with open(snapshot_path) as f:
                signals = json.load(f)
        except Exception:
            pass
    return signals


def read_latest_article():
    """读取最新发布的量化日报信息"""
    path = os.path.join(STOCK_QUANT, "latest_article.json")
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def read_crypto_signals():
    """读取加密币信号"""
    signal_path = os.path.join(CRYPTO_QUANT, "signals.json")
    if os.path.exists(signal_path):
        try:
            with open(signal_path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


def format_quant_summary():
    """格式化量化日报摘要"""
    signals = read_quant_signals()
    if not signals:
        return "📊 暂无量化数据（可能今日尚未采集）"

    lines = []
    date_str = signals.get("date", signals.get("time", ""))
    lines.append(f"📊 四市量化信号 · {date_str}")
    lines.append(f"━━━━━━━━━━━━━━━━")

    for market in ["A股", "美股", "港股"]:
        mdata = signals.get(market, {})
        if mdata:
            lines.append(f"\n【{market}】")
            results = mdata.get("results", [])
            if results:
                for s in results[:5]:
                    name = s.get("name", "?")
                    cp = s.get("change_pct", 0)
                    if isinstance(cp, (int, float)):
                        arrow = "📈" if cp > 0 else "📉"
                        lines.append(f"  {arrow} {name}: {cp:+.2f}%")
                    else:
                        lines.append(f"  {name}: {cp}")
            summary = mdata.get("summary", "")
            if summary:
                lines.append(f"  {summary[:100]}")

    # 加密币
    crypto = read_crypto_signals()
    if crypto:
        lines.append(f"\n【加密币】")
        if "summary" in crypto:
            lines.append(f"  {crypto['summary'][:100]}")

    lines.append(f"\n💡 回复「详细信号」获取完整分析")
    return "\n".join(lines)


def format_crypto_detail():
    """加密币详细信号"""
    crypto = read_crypto_signals()
    if not crypto:
        return "暂无加密币数据"
    
    if "coins" in crypto:
        lines = ["🪙 加密币信号"]
        for coin in crypto["coins"][:5]:
            name = coin.get("symbol", coin.get("name", "?"))
            price = coin.get("price", "?")
            rsi = coin.get("rsi", coin.get("indicators", {}).get("rsi", "?"))
            lines.append(f"  {name} ${price} | RSI: {rsi}")
        return "\n".join(lines)
    
    return json.dumps(crypto, ensure_ascii=False, indent=2)[:500]


if __name__ == "__main__":
    print(format_quant_summary())
