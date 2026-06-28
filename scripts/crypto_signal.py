#!/usr/bin/env python3
"""享客虾 Bot - 虚拟币量化信号查询工具

调用方式:
  python3 crypto_signal.py                    # 全币种概览
  python3 crypto_signal.py BTC                # 单币种详情
  python3 crypto_signal.py --push            # 有信号时推送到 Bot

数据源: 香港服务器 (124.156.173.120) OKX
"""

import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone

SIGNALS_URL = "http://124.156.173.120/crypto/signals.json"
CRYPTO_HOME = os.path.expanduser("~/crypto-quant")
SIGNALS_CACHE = os.path.join(CRYPTO_HOME, "signals_cache.json")

EMOJI_MAP = {
    "BTC": "₿", "ETH": "♢", "SOL": "◎", "BNB": "◆",
    "WLD": "◈", "UNI": "🦄", "DOGE": "🐕", "XRP": "✕"
}

def fetch_signals():
    try:
        req = urllib.request.Request(SIGNALS_URL)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        # fallback to cache
        if os.path.exists(SIGNALS_CACHE):
            with open(SIGNALS_CACHE) as f:
                return json.load(f)
        return {"error": str(e), "exchange": "okx", "results": {}, "summary": {}}

def format_overview(data):
    if "error" in data:
        return f"❌ 信号查询失败: {data['error']}"

    results = data.get("results", {})
    summary = data.get("summary", {})
    ts = data.get("updated_at", "N/A")

    lines = [f"📊 虚拟币量化 | OKX | 更新 {ts}", ""]

    for sym, info in results.items():
        name = info.get("name", sym.split("/")[0])
        emoji = EMOJI_MAP.get(name, "•")
        price = info.get("price", 0)
        change = info.get("change_24h", "0%")
        rsi = info.get("rsi", 50)
        signals = info.get("signals", [])

        # 信号标
        sig_icon = "🟢" if len(signals) == 0 else "🟡" if len(signals) <= 2 else "🔴"

        lines.append(f"{emoji} {sig_icon} {name}  ${price:,.2f}  {change}")
        lines.append(f"   RSI: {rsi:.1f}")

        if signals:
            for s_type, s_desc in signals:
                lines.append(f"   ⚠ {s_type}: {s_desc}")

        lines.append("")

    lines.append(f"共 {summary.get('total', 0)} 个币种 | {summary.get('with_signals', 0)} 个有信号")
    return "\n".join(lines)

def format_detail(data, coin):
    results = data.get("results", {})
    # find by name or symbol
    info = None
    for sym, v in results.items():
        if v.get("name", "").upper() == coin.upper() or sym.upper().startswith(coin.upper()):
            info = v
            break
    if not info:
        return f"❌ 未找到 {coin.upper()}，支持的币种: BTC/ETH/SOL/BNB/WLD/UNI/DOGE/XRP"

    name = info.get("name", coin.upper())
    emoji = EMOJI_MAP.get(name, "•")
    price = info.get("price", 0)
    change = info.get("change_24h", "0%")
    rsi = info.get("rsi", 50)
    volume_ratio = info.get("volume_ratio", 0)
    signals = info.get("signals", [])

    # 额外明细
    detail = info.get("detail", {})

    lines = [
        f"{emoji} {name}/USDT  现价 ${price:,.2f}",
        f"   24h涨跌: {change}",
        f"   RSI(14): {rsi:.1f} — {'超买 🔴' if rsi > 70 else '超卖 🟢' if rsi < 30 else '正常 ✅'}",
        f"   成交量比: {volume_ratio:.2f}x均值{' 📈' if volume_ratio > 2 else ''}",
        "",
    ]

    if signals:
        lines.append("⚠ 信号:")
        for s_type, s_desc in signals:
            lines.append(f"   • {s_type}: {s_desc}")
        lines.append("")
    else:
        lines.append("目前无明显信号，市场平稳。")
        lines.append("")

    # 建议
    if rsi < 30:
        lines.append("💡 策略建议: 超卖区，关注反弹机会")
        lines.append(f"    轻仓试多，止损设 -5%")
    elif rsi > 70:
        lines.append("💡 策略建议: 超买区，注意回调风险")
        lines.append("    可分批止盈")
    elif rsi < 40:
        lines.append("💡 策略建议: 偏弱，等RSI回升到40+再考虑")
    elif rsi > 60:
        lines.append("💡 策略建议: 偏强，持有为主")
    else:
        lines.append("💡 策略建议: 观望，等方向明确")

    return "\n".join(lines)

def check_push_signals(data):
    """检查是否有需要推送的极端信号，返回推送文本或 None"""
    results = data.get("results", {})
    push_lines = []
    for sym, info in results.items():
        name = info.get("name", sym.split("/")[0])
        rsi = info.get("rsi", 50)
        signals = info.get("signals", [])
        price = info.get("price", 0)

        if rsi < 25:
            push_lines.append(f"🟢 {name} 深度超卖 RSI={rsi:.1f} ${price:,.2f}")
        elif rsi > 75:
            push_lines.append(f"🔴 {name} 深度超买 RSI={rsi:.1f} ${price:,.2f}")

        for s_type, s_desc in signals:
            if "金叉" in s_type:
                push_lines.append(f"🟢 {name} MACD金叉 ${price:,.2f}")
            elif "死叉" in s_type:
                push_lines.append(f"🔴 {name} MACD死叉 ${price:,.2f}")

    if push_lines:
        ts = data.get("updated_at", "N/A")
        text = f"🚨 量化信号 {ts}\n" + "\n".join(push_lines)
        text += "\n\n回复 币种名 查看详情，如 BTC"
        return text
    return None

if __name__ == "__main__":
    data = fetch_signals()

    if "--push" in sys.argv:
        text = check_push_signals(data)
        if text:
            print(text)
        # else silent - no push needed
        sys.exit(0)

    if len(sys.argv) > 1 and not sys.argv[1].startswith("--"):
        coin = sys.argv[1]
        print(format_detail(data, coin))
    else:
        print(format_overview(data))
