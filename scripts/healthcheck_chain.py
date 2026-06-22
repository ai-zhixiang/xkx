#!/usr/bin/env python3
"""全链路心跳：检查连接器 → 网关 → Hermes Bridge 是否通畅（跳过全Agent调用）
   仅使用轻量级检查，避免因业务请求排队导致误报"""
import json
import os
import socket
import sys
import time
import urllib.request
import urllib.error

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 8001
HERMES_URL = "http://127.0.0.1:8642/health"
CB_FILE = "/tmp/hermes_cb_open"
FAILURES = []

def fail(label: str, detail: str = ""):
    FAILURES.append((label, detail))

def tcp_check(host: str, port: int, timeout: int = 3) -> bool:
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False

# 1. 熔断器状态
if os.path.exists(CB_FILE):
    age = time.time() - float(open(CB_FILE).read().strip())
    fail("熔断器", f"已打开 {age:.0f}s")

# 2. Gateway TCP 可达
if not tcp_check(GATEWAY_HOST, GATEWAY_PORT):
    fail("Gateway 端口", f"{GATEWAY_HOST}:{GATEWAY_PORT} 不可达")

# 3. Hermes Bridge health (轻量 HTTP 检查)
try:
    r = urllib.request.urlopen(HERMES_URL, timeout=5)
    data = json.loads(r.read().decode())
    if data.get("status") != "ok":
        fail("Hermes Bridge", f"health={data.get('status')}")
except Exception as e:
    fail("Hermes Bridge", str(e))

# 4. Gateway webhook 轻量检查（仅验证端点能响应，不走完整 Agent）
try:
    req = urllib.request.Request(
        f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/api/bot/webhook",
        data=json.dumps({
            "bot_id": "ac7c0d4bd963@im.bot",
            "user_id": "o9cq804SZ_hKioeNnMev",
            "content": "ping",
        }).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=20)
    result = json.loads(resp.read().decode())
    if not result.get("success"):
        fail("Gateway→Bridge", result.get("response", str(result.get("error", "")))[:100])
except Exception as e:
    fail("Gateway→Bridge", str(e))

if FAILURES:
    print("⚠️ 全链路异常:")
    for label, detail in FAILURES:
        print(f"  ❌ {label}" + (f": {detail}" if detail else ""))
    sys.exit(1)
