"""
Bot Connector — iLink 微信 Bot 连接器

每个 Bot 一个进程，连接 iLink 长轮询接收消息，
转发到享客虾网关 webhook 处理，发送回复。

用法:
  python3 connector.py <bot_id> [--gateway URL] [--log FILE]
"""

import sys, os, time, json, struct, secrets, base64, hashlib, logging
from pathlib import Path
from datetime import datetime
from typing import Optional

# ===== iLink API 常量 =====
ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
CHANNEL_VERSION = "2.2.0"
API_TIMEOUT_MS = 15_000
LONG_POLL_TIMEOUT_MS = 35_000
MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2
ITEM_TEXT = 1

# ===== 工具函数 =====

def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")

def _headers(token: str, body: str) -> dict:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "Authorization": f"Bearer {token}",
    }

def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION}


class BotConnector:
    """连接到 iLink 并转发消息到网关"""

    def __init__(self, bot_id: str, bot_token: str, gateway_url: str = None):
        self.bot_id = bot_id
        self.bot_token = bot_token
        self.gateway_url = (gateway_url or "http://127.0.0.1:8001").rstrip("/")
        self.running = False
        self.sync_buf = ""
        self._context_tokens = {}  # userId -> context_token (来自 iLink 消息，回复时必须回传)
        self._log = logging.getLogger(f"bot:{bot_id[:12]}")

    def _ilink_post(self, endpoint: str, payload: dict, timeout_ms: int = API_TIMEOUT_MS) -> dict:
        """调 iLink API (POST)"""
        import httpx
        body = json.dumps({**payload, "base_info": _base_info()}, separators=(",", ":"))
        url = f"{ILINK_BASE_URL}/{endpoint}"
        headers = _headers(self.bot_token, body)
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=timeout_ms / 1000)
            if resp.status_code != 200:
                raise RuntimeError(f"iLink {endpoint} HTTP {resp.status_code}: {resp.text[:200]}")
            return resp.json()
        except httpx.TimeoutException:
            return {}

    def _get_updates(self) -> dict:
        """长轮询获取消息"""
        try:
            return self._ilink_post(
                "ilink/bot/getupdates",
                {"get_updates_buf": self.sync_buf},
                timeout_ms=LONG_POLL_TIMEOUT_MS + 5000,
            )
        except Exception as e:
            self._log.warning("getUpdates 失败: %s", e)
            return {}

    def _forward_to_gateway(self, user_id: str, content: str, msg_id: str) -> Optional[str]:
        """转发消息到网关 webhook，返回回复文本"""
        import httpx
        try:
            resp = httpx.post(
                f"{self.gateway_url}/api/bot/webhook",
                json={
                    "bot_id": self.bot_id,
                    "user_id": user_id,
                    "content": content,
                    "msg_id": msg_id,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") and data.get("response"):
                    return data["response"]
            else:
                self._log.warning("网关返回 %s: %s", resp.status_code, resp.text[:100])
        except Exception as e:
            self._log.error("转发到网关失败: %s", e)
        return None

    def _send_message(self, to_user_id: str, text: str) -> bool:
        """通过 iLink 发送消息"""
        try:
            ctx_token = self._context_tokens.get(to_user_id, "")
            msg = {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": str(int(time.time() * 1000)),
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
            }
            # iLink 要求回复时必须回传 incoming message 的 context_token
            if ctx_token:
                msg["context_token"] = ctx_token
            payload = {"msg": msg}
            resp = self._ilink_post("ilink/bot/sendmessage", payload)
            ret = resp.get("ret", 0)
            if ret != 0:
                self._log.warning("发送消息失败 ret=%s: %s", ret, resp.get("errmsg", ""))
                return False
            return True
        except Exception as e:
            self._log.error("发送消息异常: %s", e)
            return False

    def _extract_text(self, msg: dict) -> str:
        """从 iLink 消息中提取文本"""
        items = msg.get("item_list") or []
        for item in items:
            if item.get("type") == ITEM_TEXT:
                text_item = item.get("text_item") or {}
                return text_item.get("text", "")
        return ""

    def run(self):
        """主循环"""
        self.running = True
        consecutive_failures = 0
        self._log.info("🤖 Bot 连接器启动: bot_id=%s", self.bot_id)
        self._log.info("   网关: %s", self.gateway_url)

        # 通知 iLink 本 Bot 上线（类似 OpenClaw 的 notifyStart）
        try:
            self._ilink_post("ilink/bot/msg/notifystart", {}, timeout_ms=5000)
            self._log.info("   notifyStart 成功")
        except Exception as e:
            self._log.warning("   notifyStart 失败（不影响运行）: %s", e)

        while self.running:
            try:
                response = self._get_updates()
                if not response:
                    consecutive_failures += 1
                    time.sleep(BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                # 检查错误
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if ret == -14 or errcode == -14:
                        self._log.error("Session 过期，重置 sync_buf + 重连")
                        # 清掉过期 sync_buf，用空 buf 建新 session
                        old_buf = self.sync_buf
                        self.sync_buf = ""
                        # 重发 notifyStart
                        try:
                            self._ilink_post("ilink/bot/msg/notifystart", {}, timeout_ms=5000)
                            self._log.info("   notifyStart 重发完成 (buf len=%d)", len(old_buf))
                        except Exception as e:
                            self._log.warning("   notifyStart 重发失败: %s", e)
                        time.sleep(3)
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    self._log.warning("轮询错误 ret=%s errcode=%s (%d/3)", ret, errcode, consecutive_failures)
                    time.sleep(BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0

                # 更新 sync_buf
                new_buf = str(response.get("get_updates_buf") or "")
                if new_buf:
                    self.sync_buf = new_buf

                # 逐条处理消息
                for msg in response.get("msgs") or []:
                    self._process_message(msg)

            except KeyboardInterrupt:
                self._log.info("收到中断信号，停止")
                break
            except Exception as e:
                consecutive_failures += 1
                self._log.error("轮询异常 (%d/3): %s", consecutive_failures, e)
                time.sleep(BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

        self._log.info("🤖 Bot 连接器停止")

    def _process_message(self, msg: dict):
        """处理单条消息"""
        sender_id = str(msg.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self.bot_id:
            return

        msg_id = str(msg.get("message_id") or "").strip()
        text = self._extract_text(msg)
        if not text:
            return

        self._log.info("📩 收到消息 from=%s: %s", sender_id[:20], text[:50])

        # 保存 context_token — iLink 要求回复时必须回传
        ctx_token = msg.get("context_token", "")
        if ctx_token:
            self._context_tokens[sender_id] = ctx_token
            self._log.debug("   context_token saved for %s", sender_id[:20])

        # 转发到网关
        reply = self._forward_to_gateway(sender_id, text, msg_id)

        if reply:
            self._log.info("📤 回复: %s", reply[:40])
            self._send_message(sender_id, reply)


# ===== 进程管理 =====

def get_connectors_dir() -> Path:
    return Path.home() / ".hermes" / "bot_connectors"

def save_connector_pid(bot_id: str, pid: int):
    d = get_connectors_dir()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{bot_id}.pid", "w") as f:
        f.write(str(pid))

def read_connector_pid(bot_id: str) -> Optional[int]:
    f = get_connectors_dir() / f"{bot_id}.pid"
    if f.exists():
        try:
            return int(f.read_text().strip())
        except (ValueError, OSError):
            pass
    return None

def is_connector_running(bot_id: str) -> bool:
    pid = read_connector_pid(bot_id)
    if pid:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            pass
    return False

def save_sync_buf(bot_id: str, sync_buf: str):
    f = get_connectors_dir() / f"{bot_id}.sync"
    f.write_text(sync_buf)

def load_sync_buf(bot_id: str) -> str:
    f = get_connectors_dir() / f"{bot_id}.sync"
    if f.exists():
        return f.read_text()
    return ""


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="iLink Bot 连接器")
    parser.add_argument("action", choices=["start", "stop", "status", "run"],
                        help="操作: start(后台) / stop / status / run(前台)")
    parser.add_argument("bot_id", nargs="?", help="Bot ID")
    parser.add_argument("--gateway", default="http://127.0.0.1:8001", help="享客虾网关地址")
    parser.add_argument("--token", help="Bot Token（run/start 需要，或从网关取）")
    parser.add_argument("--log", help="日志文件路径")

    args = parser.parse_args()

    if args.action == "status":
        if args.bot_id:
            running = is_connector_running(args.bot_id)
            print(f"{'✅' if running else '❌'} {args.bot_id} {'运行中' if running else '已停止'}")
        else:
            d = get_connectors_dir()
            if d.exists():
                for f in sorted(d.glob("*.pid")):
                    bot = f.stem
                    pid = read_connector_pid(bot)
                    alive = is_connector_running(bot)
                    print(f"{'✅' if alive else '❌'} {bot} PID={pid or '-'}")
        sys.exit(0)

    if args.action == "stop":
        if not args.bot_id:
            print("❌ 需要指定 bot_id")
            sys.exit(1)
        pid = read_connector_pid(args.bot_id)
        if pid:
            try:
                os.kill(pid, 15)
                print(f"⏹️  已发送停止信号给 {args.bot_id} (PID={pid})")
            except OSError:
                print(f"❌ Bot {args.bot_id} 未运行")
        else:
            print(f"❌ Bot {args.bot_id} 未运行")
        # 清理 pid 文件
        (get_connectors_dir() / f"{args.bot_id}.pid").unlink(missing_ok=True)
        sys.exit(0)

    if args.action in ("start", "run"):
        if not args.bot_id:
            print("❌ 需要指定 bot_id")
            sys.exit(1)

        # 尝试从网关获取 token
        bot_token = args.token
        if not bot_token:
            import httpx
            try:
                resp = httpx.get(f"{args.gateway}/api/bot/list", timeout=10)
                if resp.status_code == 200:
                    bots = resp.json().get("bots", [])
                    for b in bots:
                        if b["bot_id"] == args.bot_id:
                            bot_token = b.get("bot_token", "")
                            break
            except Exception:
                pass

        if not bot_token:
            print("❌ 未提供 bot_token 且无法从网关获取")
            sys.exit(1)

        if args.action == "start":
            # 后台模式
            pid = os.fork()
            if pid > 0:
                save_connector_pid(args.bot_id, pid)
                print(f"🚀 Bot {args.bot_id} 已启动 (PID={pid})")
                sys.exit(0)
            # 子进程继续
            os.setsid()

        # 日志配置
        log_level = logging.INFO
        if args.log:
            logging.basicConfig(
                level=log_level,
                format="%(asctime)s [%(name)s] %(message)s",
                filename=args.log,
            )
        else:
            logging.basicConfig(
                level=log_level,
                format="%(asctime)s [%(name)s] %(message)s",
            )

        # 加载 sync_buf
        sb = load_sync_buf(args.bot_id)

        # 启动连接器
        connector = BotConnector(args.bot_id, bot_token, args.gateway)
        connector.sync_buf = sb
        try:
            connector.run()
        except KeyboardInterrupt:
            pass
        finally:
            save_sync_buf(args.bot_id, connector.sync_buf)
