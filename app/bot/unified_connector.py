#!/usr/bin/env python3
"""
Unified Bot Connector — 单 asyncio 进程管理所有 iLink Bot session。

三层防护:
  ① 心跳保活 — 静默超 5 分钟自动短轮询，防 iLink 回收
  ② 优雅启停 — notifyStart/notifyStop 全链路，重启不丢 session
  ③ 自愈看门狗 — session 异常自动重启流程，不等人

架构:
  Bot A ─┐
  Bot B ─┼→ 统一连接器 → 网关(:8001) → Hermes(:8089)
  Bot C ─┘
"""

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import secrets
import re
import signal
import subprocess
import sys
import tempfile
import time
import struct
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path
from typing import Optional

try:
    import httpx
except ImportError:
    httpx = None

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

STATE_DIR = Path(__file__).parent.parent / "data" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

ARCHIVE_DIR = Path(__file__).parent.parent / "archive"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# iLink API
ILINK_BASE = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://ilinkai.weixin.qq.com/cdn"

# Typing
TYPING_START = 1
TYPING_STOP = 2
CONFIG_TIMEOUT_MS = 5000

# 稳定性参数
POLL_INTERVAL_MS = 2000
HEARTBEAT_INTERVAL_SECONDS = 300
BACKOFF_DELAY_MS = 30000
RETRY_DELAY_MS = 3000
MAX_CONSECUTIVE_FAILURES = 10
DB_RELOAD_INTERVAL = 30
UI_MSG_TTL_SECONDS = 300
TYPING_MAX_DURATION = 120

# iLink item types
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VIDEO = 3
ITEM_VOICE = 4
ITEM_FILE = 5
ITEM_MINI_PROGRAM = 6

# 本地配置文件路径
LOCAL_CONFIG_PATH = Path(__file__).parent.parent / "config" / "local.json"

# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════


def _make_logger(name: str, log_file: Optional[Path] = None) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%m-%d %H:%M:%S",
    )
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    # always add stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.DEBUG)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    return logger


root_log = _make_logger("unified-connector")

_sync_buf_dir = STATE_DIR / "sync_buf"
_sync_buf_dir.mkdir(parents=True, exist_ok=True)


def load_sync_buf(bot_id: str) -> str:
    f = _sync_buf_dir / f"{bot_id}.txt"
    if f.exists():
        return f.read_text(encoding="utf-8")
    return ""


def save_sync_buf(bot_id: str, buf: str):
    f = _sync_buf_dir / f"{bot_id}.txt"
    f.write_text(buf, encoding="utf-8")


def load_bots_from_db_raw() -> list[tuple[str, str]]:
    """直接从 DB 读活跃 bot"""
    bot_list: list[tuple[str, str]] = []
    for _ in range(2):
        try:
            r = subprocess.run(
                ["/home/ubuntu/weclaw-1/.venv/bin/python3", "-c",
                 "import json, psycopg2; "
                 "c=psycopg2.connect(host='127.0.0.1', dbname='weclawd', user='lucky', password='lucky_pass'); "
                 "cur=c.cursor(); "
                 "cur.execute(\"SELECT bot_id, bot_token FROM bot_accounts WHERE is_active=true AND bot_token IS NOT NULL AND bot_token != ''\"); "
                 "print(json.dumps(cur.fetchall())); "
                 "c.close()"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout.strip())
                for bot_id, token in data:
                    if bot_id and token:
                        bot_list.append((bot_id.strip(), token.strip()))
                break
        except Exception:
            pass
        time.sleep(0.5)
    return bot_list


class MessageDeduplicator:
    """消息去重 — 基于 msg_id 的 5min TTL 缓存"""
    def __init__(self, ttl_seconds: int = 300):
        self._seen: dict[str, float] = {}
        self._ttl = ttl_seconds

    def is_duplicate(self, msg_id: str) -> bool:
        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if now - v < self._ttl}
        if msg_id in self._seen:
            return True
        self._seen[msg_id] = now
        return False


class TypingTicketCache:
    """Typing Ticket 缓存，单用户 60s TTL"""
    def __init__(self, ttl_seconds: int = 60):
        self._cache: dict[str, tuple[str, float]] = {}
        self._ttl = ttl_seconds

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if entry and time.time() - entry[1] < self._ttl:
            return entry[0]
        return None

    def set(self, user_id: str, ticket: str):
        self._cache[user_id] = (ticket, time.time())


# ═══════════════════════════════════════════════════════════════
# 暗号匹配
# ═══════════════════════════════════════════════════════════════

_ILLEGAL_PATTERNS = re.compile(
    r"[，。！？、；：\"\"''\s]"
)

_code_phrases: list[dict] = []


def _ensure_code_file():
    global _code_phrases
    p = LOCAL_CONFIG_PATH
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            _code_phrases = data.get("code_phrases", [])
            root_log.info(f"暗号表加载: {len(_code_phrases)} 条 (from local.json)")
            return
        except Exception as e:
            root_log.warning(f"local.json 加载失败: {e}")
    _code_phrases = [
        {"input": "天王盖地虎", "reply": "OpenClaw 是SB!"},
        {"input": "宝塔镇河妖", "reply": "微侠真牛逼!"},
        {"input": "微侠真牛逼", "reply": "天王盖地虎!同志!"},
        {"input": "openclaw是sb", "reply": "宝塔镇河妖!收到!"},
        {"input": "openclaw is sb", "reply": "宝塔镇河妖!收到!"},
        {"input": "侠客行", "reply": "客从何处来，虾行万里去"},
    ]
    root_log.info(f"暗号表: {len(_code_phrases)} 条 (default)")


def match_code_phrase(text: str) -> Optional[dict]:
    """匹配暗号 — 去标点、小写后对比"""
    cleaned = _ILLEGAL_PATTERNS.sub("", text).lower().strip()
    for cp in _code_phrases:
        cp_clean = _ILLEGAL_PATTERNS.sub("", cp["input"]).lower().strip()
        if cleaned == cp_clean or cleaned.startswith(cp_clean):
            return cp
    return None


def _check_and_reply_code(text: str) -> Optional[str]:
    """检查暗号并返回暗号回复（不含暗号时返回 None）"""
    m = match_code_phrase(text)
    if m:
        return m["reply"]
    return None


# ═══════════════════════════════════════════════════════════════
# iLink HTTP 客户端
# ═══════════════════════════════════════════════════════════════

def _random_wechat_uin() -> str:
    """生成随机 X-WECHAT-UIN（每请求不同）"""
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


async def _ilink_post(
    path: str, payload: dict, token: str, *, timeout_ms: int = 10000
) -> dict:
    """iLink POST 请求（与 connector.py 一致的 header 格式）"""
    url = f"{ILINK_BASE}/{path}"
    body = json.dumps({**payload, "base_info": {"app_id": ILINK_APP_ID, "sdk_version": str(ILINK_APP_CLIENT_VERSION)}}, separators=(",", ":"))
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        "Authorization": f"Bearer {token}",
    }
    if httpx is None:
        raise RuntimeError("httpx not installed")
    async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 1) as c:
        r = await c.post(url, content=body, headers=headers)
        if r.status_code != 200:
            raise RuntimeError(f"iLink {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()


async def _ilink_get(
    path: str, token: str, *, params: dict = None, timeout_ms: int = 10000
) -> dict:
    """iLink GET 请求"""
    url = f"{ILINK_BASE}/{path}"
    headers = {"X-Wechat-Token": token}
    async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 1) as c:
        r = await c.get(url, headers=headers, params=params or {})
        if r.status_code != 200:
            raise RuntimeError(f"iLink {path} -> {r.status_code}: {r.text[:200]}")
        return r.json()


# ═══════════════════════════════════════════════════════════════
# iLink Bot Session 生命周期
# ═══════════════════════════════════════════════════════════════

ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0  # Hermes 原生值 131584


async def notify_start(token: str) -> dict:
    return await _ilink_post(
        "ilink/bot/msg/notifystart",
        {"channel_version": "2.2.0"},
        token,
        timeout_ms=10000,
    )


async def notify_stop(token: str) -> dict:
    return await _ilink_post(
        "ilink/bot/msg/notifystop",
        {},
        token,
        timeout_ms=5000,
    )


async def get_updates(
    token: str,
    sync_buf: str = "",
    timeout_ms: int = 30000,
) -> dict:
    payload: dict = {"get_updates_buf": sync_buf}
    return await _ilink_post(
        "ilink/bot/getupdates",
        payload,
        token,
        timeout_ms=timeout_ms + 5000,
    )


# ═══════════════════════════════════════════════════════════════
# 消息发送
# ═══════════════════════════════════════════════════════════════


async def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 加密，PKCS7 填充"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(padded) + encryptor.finalize()


async def _upload_file(
    path: str, encrypted_query_param: str, aes_key: bytes
) -> tuple[str, int]:
    """上传文件到 iLink CDN"""
    raw = open(path, "rb").read()
    filekey = hashlib.md5(raw).hexdigest()
    ciphertext = await _aes128_ecb_encrypt(raw, aes_key)
    upload_url = f"{WEIXIN_CDN_BASE_URL}/upload?encrypted_query_param={encrypted_query_param}&filekey={filekey}"
    async with httpx.AsyncClient(timeout=120) as c:
        resp = await c.post(upload_url, content=ciphertext)
        if resp.status_code != 200:
            raise RuntimeError(f"CDN upload failed: {resp.status_code} {resp.text[:200]}")
    return filekey, len(raw)


def _build_media_item(
    path: str,
    encrypted_query_param: str,
    aes_key_for_api: str,
    ciphertext_size: int,
    rawsize: int,
    rawfilemd5: str,
) -> dict:
    mime = mimetypes.guess_type(path)[0] or ""
    filename = Path(path).name

    if mime.startswith("image/"):
        return {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": {"encrypt_query_param": encrypted_query_param,
                          "aes_key": aes_key_for_api, "encrypt_type": 1},
                "mid_size": ciphertext_size,
            },
        }
    if mime.startswith("video/"):
        return {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": {"encrypt_query_param": encrypted_query_param,
                          "aes_key": aes_key_for_api, "encrypt_type": 1},
                "video_size": ciphertext_size,
                "play_length": 0,
                "video_md5": rawfilemd5,
            },
        }
    # 默认作为文件发送
    return {
        "type": ITEM_FILE,
        "file_item": {
            "media": {"encrypt_query_param": encrypted_query_param,
                      "aes_key": aes_key_for_api, "encrypt_type": 1},
            "file_name": filename,
            "len": str(rawsize),
            "md5": rawfilemd5,
        },
    }


async def _upload_and_build_item(
    path: str, rawsize: int, rawfilemd5: str
) -> Optional[dict]:
    """上传文件到 iLink CDN 并构造 item dict"""
    if not os.path.exists(path):
        return None
    aes_key = secrets.token_bytes(16)
    aeskey_hex = aes_key.hex()

    # 获取上传凭证
    resp = await _ilink_post(
        "ilink/bot/msg/applyupload",
        {"md5": rawfilemd5, "raw_size": rawsize},
        "",  # token: 将在外层拼接
        timeout_ms=15000,
    )
    encrypted_query_param = resp.get("encrypted_query_param", "")
    if not encrypted_query_param:
        return None

    # 上传
    filekey, _ = await _upload_file(path, encrypted_query_param, aes_key)
    aes_key_for_api = base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii")
    return _build_media_item(path, encrypted_query_param, aes_key_for_api, len(open(path, "rb").read()), rawsize, rawfilemd5)


async def send_msg(
    token: str,
    to_user_id: str,
    text: str,
    context_token: Optional[str] = None,
    *,
    bot=None,
) -> Optional[dict]:
    """发送消息（支持 MEDIA: 前缀文件）"""
    # 提取 MEDIA 路径
    media_paths = re.findall(r"MEDIA:\s*(\S+)", text)
    clean_text = re.sub(r"MEDIA:\s*\S+", "", text).strip()
    item_list = []

    # 构建文本 item
    if clean_text:
        item_list.append({"type": ITEM_TEXT, "text_item": {"text": clean_text}})

    # 媒体文件
    for path in media_paths:
        if not os.path.exists(path):
            if bot:
                bot.log.warning(f"MEDIA 文件不存在: {path}")
            continue
        rawsize = os.path.getsize(path)
        rawfilemd5 = hashlib.md5(open(path, "rb").read()).hexdigest()
        # 上传 + 构造 item
        item = await _upload_and_build_item_real(path, rawsize, rawfilemd5)
        if item:
            item_list.append(item)

    if not item_list:
        item_list.append({"type": ITEM_TEXT, "text_item": {"text": ""}})

    payload: dict = {
        "ilink_user_id": to_user_id,
        "item_list": item_list,
        "message_state": 2,
    }
    if context_token:
        payload["context_token"] = context_token

    return await _ilink_post("ilink/bot/msg/sendmsg", payload, token, timeout_ms=15000)


async def _send_smart_welcome(bot, to_user: str, ctx: str):
    """智能欢迎 — 检查会员状态：会员显到期信息，免费显额度+开通链接"""
    import subprocess, json as _json
    sub = None
    try:
        r = subprocess.run(
            ["/home/ubuntu/weclaw-1/.venv/bin/python3", "-c",
             "import json, psycopg2; "
             "c=psycopg2.connect(host='127.0.0.1', dbname='weclawd', user='lucky', password='lucky_pass'); "
             "cur=c.cursor(); "
             "cur.execute('SELECT status, plan_id, expires_at, messages_limit, messages_used FROM subscribers WHERE openid = %s', ('"
             + to_user + "',)); "
             "row=cur.fetchone(); "
             "print(json.dumps(row, default=str) if row else 'null'); "
             "c.close()"],
            capture_output=True, text=True, timeout=10
        )
        out = r.stdout.strip()
        if out:
            sub = _json.loads(out)
    except Exception as e:
        bot.log.warning(f"  [welcome] 查询订阅状态失败: {e}")

    if sub and sub[0] == "ACTIVE" and sub[3] and sub[4] is not None and sub[3] > sub[4]:
        # 会员
        _, _, expires_at, limit_, used_ = sub
        expires = expires_at.split("T")[0] if expires_at and "T" in str(expires_at) else str(expires_at or "?")
        remaining = (limit_ or 50) - (used_ or 0)
        welcome_msg = (
            "\U0001f99e **欢迎回来！享客虾会员**\n\n"
            "\U0001f4c5 会员到期：**" + expires + "**\n"
            "\U0001f4ca 本月剩余：**" + str(remaining) + "** 条\n\n"
            "\U0001f3b5 AI 写歌、做嗨卡\n"
            "\U0001f4ca 四市量化信号\n"
            "\U0001f50d 联网搜索\n\n"
            "\U0001f449 续费→ https://ai.pangoozn.com/xkx/ \U0001f99e"
        )
    else:
        free_remaining = 50
        if sub and sub[4] is not None:
            free_remaining = max(0, 50 - sub[4])
        welcome_msg = (
            "\U0001f99e **欢迎来到享客虾！**\n\n"
            "我是你的 AI 创作伙伴\n"
            "\u2022 \U0001f4ac 聊天、搜索、咨询\n"
            "\u2022 \U0001f3b5 AI 写歌、做嗨卡\n"
            "\u2022 \U0001f4ca 四市量化信号\n\n"
            "\U0001f381 今日免费剩余：**" + str(free_remaining) + "** 条\n"
            "\U0001f449 开通会员不限量→ https://ai.pangoozn.com/xkx/\n\n"
            "试试发暗号\"天王盖地虎\" \U0001f99e"
        )

    for i in range(3):
        try:
            r = await send_msg(bot.token, to_user, welcome_msg, ctx, bot=bot)
            if r and r.get("errcode", 0) == 0:
                bot.log.info(f"\U0001f389 欢迎消息已推送 {to_user[:20]}")
                return
            bot.log.warning(f"  欢迎推送 ret={i+1}: {r}")
        except Exception as e:
            bot.log.warning(f"  欢迎推送异常 ret={i+1}: {e}")
        await asyncio.sleep(1)
    bot.log.error(f"  欢迎推送失败 3次重试: {to_user[:20]}")


async def _upload_and_build_item_real(
    path: str, rawsize: int, rawfilemd5: str
) -> Optional[dict]:
    aes_key = secrets.token_bytes(16)
    aeskey_hex = aes_key.hex()
    resp = await _ilink_post(
        "ilink/bot/msg/applyupload",
        {"md5": rawfilemd5, "raw_size": rawsize},
        "",
        timeout_ms=15000,
    )
    encrypted_query_param = resp.get("encrypted_query_param", "")
    if not encrypted_query_param:
        return None
    filekey, _ = await _upload_file(path, encrypted_query_param, aes_key)
    aes_key_for_api = base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii")
    return _build_media_item(path, encrypted_query_param, aes_key_for_api, os.path.getsize(path), rawsize, rawfilemd5)


# ═══════════════════════════════════════════════════════════════
# 网关桥接
# ═══════════════════════════════════════════════════════════════

GATEWAY_URL = "http://127.0.0.1:8001"

async def forward_to_gateway(
    bot_id: str, user_id: str, content: str,
    msg_id: str, client: httpx.AsyncClient
) -> Optional[str]:
    try:
        r = await client.post(
            f"{GATEWAY_URL}/api/bot/webhook",
            json={"bot_id": bot_id, "user_id": user_id, "content": content, "msg_id": msg_id},
            timeout=300,
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("success") and d.get("response"):
                return d["response"]
    except Exception:
        pass
    return None


# ═══════════════════════════════════════════════════════════════
# Bot Session 管理
# ═══════════════════════════════════════════════════════════════

@dataclass
class BotSession:
    bot_id: str
    token: str
    log: logging.Logger
    sync_buf: str = ""
    last_activity: float = 0.0
    consecutive_failures: int = 0
    context_tokens: dict = None
    dedup: MessageDeduplicator = None
    typing_cache: TypingTicketCache = None

    def __post_init__(self):
        self.context_tokens = self.context_tokens or {}
        self.dedup = self.dedup or MessageDeduplicator(ttl_seconds=300)
        self.typing_cache = self.typing_cache or TypingTicketCache()


async def run_bot(bot_id: str, token: str, shutdown: asyncio.Event):
    """Bot session 主循环 — 包含三层防护"""

    log_file = STATE_DIR / f"{bot_id}.log"
    log = _make_logger(f"bot:{bot_id[:14]}", log_file)
    bot = BotSession(bot_id=bot_id, token=token, log=log)
    log.info("🤖 启动: %s", bot_id)

    # ── Layer 2: 启动预热 — 仅已有 session 才 notifyStop（清残留）──
    # 有 clean_shutdown 标记说明上次是优雅关闭，跳过冗余 notifyStop
    clean_file = STATE_DIR / "clean_shutdown"
    has_clean = clean_file.exists()
    if has_clean:
        try:
            clean_file.unlink()
        except Exception:
            pass

    has_old_session = bool(load_sync_buf(bot.bot_id))
    if has_old_session and not has_clean:
        log.info("  已有旧 sync_buf → notifyStop 清残留")
        try:
            await notify_stop(token)
        except Exception:
            pass
        await asyncio.sleep(1)
    elif has_old_session and has_clean:
        log.info("  干净关闭 → 跳过 notifyStop")

    # notifyStart
    try:
        r = await notify_start(token)
        log.info("  notifyStart → %s", r.get("errcode", "?"))
    except Exception as e:
        log.warning("  notifyStart 失败: %s", e)

    # 恢复 sync_buf
    bot.sync_buf = load_sync_buf(bot.bot_id)
    if bot.sync_buf:
        log.info(f"  恢复 sync_buf: {len(bot.sync_buf)} chars")

    # 共享 httpx 客户端
    async with httpx.AsyncClient(timeout=300) as client:
        # ── 循环 ──
        consecutive_errors = 0
        while not shutdown.is_set():
            try:
                updates = await get_updates(token, bot.sync_buf)
                bot.last_activity = time.time()

                errcode = updates.get("errcode", 0)
                if errcode == -14:
                    # Session 过期 — 通知看门狗重启
                    log.error("Session 过期 (errcode=-14)，触发自愈")
                    return

                if errcode != 0:
                    log.warning(f"Sync 异常 errcode={errcode}: {updates.get('errmsg', '')[:80]}")
                    continue

                # 更新 sync_buf
                new_sync_buf = updates.get("sync_buf", "")
                if new_sync_buf and new_sync_buf != bot.sync_buf:
                    bot.sync_buf = new_sync_buf
                    save_sync_buf(bot.bot_id, new_sync_buf)

                # 处理消息
                msg_list = updates.get("msg_list", [])
                if not msg_list:
                    continue

                for msg in msg_list:
                    try:
                        await _process_message(msg, bot, client)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        log.error(f"处理消息异常: {e}")

                consecutive_errors = 0

            except asyncio.TimeoutError:
                # sync 超时正常，继续轮询
                pass
            except httpx.TimeoutException:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                log.error(f"轮询异常 ({consecutive_errors}/{MAX_CONSECUTIVE_FAILURES}): {e}")
                if consecutive_errors >= MAX_CONSECUTIVE_FAILURES:
                    await _wait_for(BACKOFF_DELAY_MS / 1000, shutdown)
                    consecutive_errors = 0
                else:
                    await _wait_for(RETRY_DELAY_MS / 1000, shutdown)


async def _process_message(msg: dict, bot: BotSession, client: httpx.AsyncClient):
    """处理单条消息"""
    log = bot.log

    from_user = msg.get("from_user", "") or msg.get("sender", "")
    if not from_user:
        return

    # context_token
    ctx = msg.get("context_token", "") or ""
    if ctx:
        bot.context_tokens[from_user] = ctx

    # ── 消息去重 ──
    msg_id = str(msg.get("message_id") or msg.get("msg_id") or "")
    if msg_id and bot.dedup.is_duplicate(msg_id):
        log.debug("  去重跳过 msg_id=%s", msg_id[:16])
        return

    text = _extract_text(msg)

    # ── 语音消息 ──
    if text and text.startswith("[语音]"):
        asyncio.create_task(_archive_voice(msg, from_user, bot))

    # ── 图片消息：下载归档 ──
    if text and text.startswith("[图片]"):
        asyncio.create_task(_archive_image(msg, from_user, bot))

    # ── 视频消息 ──
    if text and text.startswith("[视频]"):
        asyncio.create_task(_archive_video(msg, from_user, bot))

    if not text:
        return

    # ── P1: 异步获取 typing ticket ──
    if not bot.typing_cache.get(from_user):
        asyncio.create_task(_maybe_fetch_typing_ticket(bot, from_user, ctx or None))

    # 先匹配暗号
    code_match = match_code_phrase(text)
    if code_match:
        reply = code_match["reply"]
        log.info("🔐 暗号匹配: %s → %s", text[:12], reply)
        if await _check_routing(from_user):
            await send_msg(bot.token, from_user, reply, ctx, bot=bot)
        else:
            log.info("  ⏭ 备机静默（暗号）")
        return

    # 路由检查
    if not await _check_routing(from_user):
        log.info("  ⏭ 备机静默")
        return

    # ── P2: typing ──
    asyncio.create_task(_send_typing_indicator(bot, from_user, TYPING_START))
    typing_heartbeat_start = time.time()
    typing_heartbeat_task = None

    async def _typing_heartbeat():
        while time.time() - typing_heartbeat_start < TYPING_MAX_DURATION:
            await asyncio.sleep(15)
            await _send_typing_indicator(bot, from_user, TYPING_START)

    typing_heartbeat_task = asyncio.create_task(_typing_heartbeat())

    # 转发网关
    try:
        reply = await forward_to_gateway(
            bot.bot_id, from_user, text, msg.get("msg_id", ""), client)
    finally:
        if typing_heartbeat_task:
            typing_heartbeat_task.cancel()

    if reply:
        log.info("📤 %s", reply[:40])
        await send_msg(bot.token, from_user, reply, ctx, bot=bot)
    else:
        log.warning("⚠️ 网关无回复")


def _extract_text(msg: dict) -> str:
    """提取消息文本"""
    for item in msg.get("item_list", []):
        t = item.get("type")
        if t == ITEM_TEXT:
            return (item.get("text_item") or {}).get("text", "")
        if t == ITEM_IMAGE:
            return "[图片]"
        if t == ITEM_VOICE:
            voice = item.get("voice_item") or {}
            for key in ("asr_refer_text", "recog_text", "recognition", "text"):
                val = voice.get(key, "") or ""
                if val.strip():
                    return "[语音] " + val.strip()
            for key in ("asr_result", "recog_text", "recognition_result"):
                val = msg.get(key, "") or ""
                if val.strip():
                    return "[语音] " + val.strip()
            return "[语音] (识别失败)"
        if t in (ITEM_VIDEO, ITEM_FILE):
            return "[视频]"
    return ""


def _extract_media(msg: dict) -> Optional[dict]:
    """从消息中提取媒体信息（用于后续下载）"""
    for item in msg.get("item_list", []):
        t = item.get("type")
        if t == ITEM_IMAGE:
            img = item.get("image_item") or {}
            media = img.get("media") or {}
            return {
                "type": "image",
                "encrypt_query_param": media.get("encrypt_query_param", ""),
                "aes_key": media.get("aes_key", ""),
                "mid_size": img.get("mid_size", 0),
            }
        if t == ITEM_VOICE:
            voice = item.get("voice_item") or {}
            media = voice.get("media") or {}
            return {
                "type": "voice",
                "encrypt_query_param": media.get("encrypt_query_param", ""),
                "aes_key": media.get("aes_key", ""),
            }
        if t in (ITEM_VIDEO, ITEM_FILE):
            media_item = item.get("video_item") or item.get("file_item") or {}
            media = media_item.get("media") or {}
            return {
                "type": "video" if t == ITEM_VIDEO else "file",
                "encrypt_query_param": media.get("encrypt_query_param", ""),
                "aes_key": media.get("aes_key", ""),
            }
    return None


async def _download_media(encrypt_query_param: str, aes_key_b64: str, save_path: str) -> bool:
    """从 iLink CDN 下载并解密媒体文件"""
    if not encrypt_query_param or not aes_key_b64:
        return False
    url = f"{WEIXIN_CDN_BASE_URL}/download?encrypted_query_param={encrypt_query_param}"
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            resp = await c.get(url)
            if resp.status_code != 200:
                return False
            ciphertext = resp.content
        # 解密 AES-128-ECB
        aes_key_hex = base64.b64decode(aes_key_b64).decode("ascii")
        aes_key = bytes.fromhex(aes_key_hex)
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        cipher = Cipher(algorithms.AES(aes_key), modes.ECB())
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        # 写入文件
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, "wb") as f:
            f.write(plaintext)
        return True
    except Exception:
        return False


async def _archive_image(msg: dict, from_user: str, bot) -> Optional[str]:
    """下载并归档图片"""
    media = _extract_media(msg)
    if not media or media["type"] != "image":
        return ""
    date_str = datetime.now().strftime("%Y%m%d")
    save_dir = ARCHIVE_DIR / "image" / date_str
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = str(save_dir / f"{from_user.split('@')[0]}_{int(time.time())}.jpg")
    ok = await _download_media(media["encrypt_query_param"], media["aes_key"], save_path)
    if ok:
        bot.log.info(f"📷 图片归档: {save_path} ({os.path.getsize(save_path)} bytes)")
    return save_path if ok else ""


async def _archive_video(msg: dict, from_user: str, bot) -> str:
    """下载并归档视频"""
    media = _extract_media(msg)
    if not media or media["type"] != "video":
        return ""
    date_str = datetime.now().strftime("%Y%m%d")
    save_dir = ARCHIVE_DIR / "video" / date_str
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = str(save_dir / f"{from_user.split('@')[0]}_{int(time.time())}.mp4")
    ok = await _download_media(media["encrypt_query_param"], media["aes_key"], save_path)
    if ok:
        bot.log.info(f"🎬 视频归档: {save_path} ({os.path.getsize(save_path)} bytes)")
    return save_path if ok else ""


async def _archive_voice(msg: dict, from_user: str, bot) -> str:
    """下载并归档语音"""
    media = _extract_media(msg)
    if not media or media["type"] != "voice":
        return ""
    date_str = datetime.now().strftime("%Y%m%d")
    save_dir = ARCHIVE_DIR / "voice" / date_str
    save_dir.mkdir(parents=True, exist_ok=True)
    save_path = str(save_dir / f"{from_user.split('@')[0]}_{int(time.time())}.silk")
    ok = await _download_media(media["encrypt_query_param"], media["aes_key"], save_path)
    if ok:
        bot.log.info(f"🎤 语音归档: {save_path} ({os.path.getsize(save_path)} bytes)")
    return save_path if ok else ""


async def _wait_for(seconds: float, shutdown: asyncio.Event):
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().create_future(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        raise


# ═══════════════════════════════════════════════════════════════
# Typing 指示器
# ═══════════════════════════════════════════════════════════════

async def _ilink_get_config(token: str, user_id: str,
                            context_token: Optional[str] = None) -> dict:
    payload: dict = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _ilink_post("ilink/bot/getconfig", payload, token,
                             timeout_ms=CONFIG_TIMEOUT_MS)


async def _maybe_fetch_typing_ticket(bot: BotSession, user_id: str,
                                     context_token: Optional[str]) -> None:
    if not bot.token:
        return
    if bot.typing_cache.get(user_id):
        return
    try:
        response = await _ilink_get_config(bot.token, user_id, context_token)
        typing_ticket = str(response.get("typing_ticket") or "")
        if typing_ticket:
            bot.typing_cache.set(user_id, typing_ticket)
    except Exception as exc:
        bot.log.debug("getConfig typing ticket failed for %s: %s",
                      user_id[:20], exc)


async def _send_typing_indicator(bot: BotSession, to_user_id: str,
                                 status: int) -> None:
    if not bot.token:
        return
    typing_ticket = bot.typing_cache.get(to_user_id)
    if not typing_ticket:
        return
    try:
        await _ilink_post("ilink/bot/sendtyping", {
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        }, bot.token, timeout_ms=CONFIG_TIMEOUT_MS)
    except Exception as exc:
        bot.log.debug("typing %s failed for %s: %s",
                      "start" if status == TYPING_START else "stop",
                      user_id[:20], exc)


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

async def main():
    root_log = _make_logger("unified-connector")
    root_log.info("=" * 50)
    root_log.info("统一连接器启动 — DB 动态加载模式")
    root_log.info("三层防护: ①心跳保活 ②优雅启停 ③自愈看门狗")
    root_log.info("DB 刷新周期: %ds — 扫码激活后自动接入", DB_RELOAD_INTERVAL)
    root_log.info("=" * 50)

    _ensure_code_file()
    shutdown = asyncio.Event()

    loop = asyncio.get_event_loop()
    sig_received = False

    def _signal():
        nonlocal sig_received
        if sig_received:
            root_log.warning("二次信号，强制退出")
            sys.exit(1)
        sig_received = True
        root_log.info("📴 收到关闭信号，graceful shutdown...")
        shutdown.set()

    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _signal)
        except NotImplementedError:
            pass

    bots = await load_bots_from_db()
    root_log.info(f"DB 加载到 {len(bots)} 个活跃 Bot:")
    for bot_id, token in bots:
        t = asyncio.create_task(run_bot(bot_id, token, shutdown))
        running_tasks[bot_id] = t
        bot_tokens[bot_id] = token
        root_log.info(f"  ✅ {bot_id}")

    sync_task = asyncio.create_task(sync_bots_from_db(shutdown))

    root_log.info(f"所有 Bot 已启动（{len(running_tasks)} 个），等待消息...")
    root_log.info(f"DB 同步后台运行中，新 Bot 扫码后最快 {DB_RELOAD_INTERVAL}s 接入")

    await shutdown.wait()

    root_log.info("正在停止 Bot 轮询（取消任务）...")
    all_tasks = list(running_tasks.values()) + [sync_task]
    for t in all_tasks:
        t.cancel()
    await asyncio.gather(*all_tasks, return_exceptions=True)

    root_log.info("跳过 notifyStop — 保留 iLink session 以支持快速重启")
    # ❌ 不调 notifyStop，让 TCP 断开即保活
    # for bot_id, task in list(running_tasks.items()):
    #     token = bot_tokens.get(bot_id, "")
    #     if token:
    #         try:
    #             await notify_stop(token)
    #             root_log.info("  %s notifyStop ✓", bot_id[:20])
    #         except Exception as e:
    #             root_log.warning("  %s notifyStop 失败: %s", bot_id[:20], e)

    # 标记干净关闭，下次启动跳过冗余 notifyStop
    try:
        Path(STATE_DIR / "clean_shutdown").touch()
    except Exception:
        pass

    root_log.info("统一连接器已停止")


# ═══════════════════════════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════════════════════════

SERVER_ID = "md1"
AUTH_CENTER_URL = "https://ai.pangoozn.com/api/auth-center/api/routing/lookup"
AUTH_CENTER_LOCAL = "http://127.0.0.1:8015/api/routing/lookup"

_routing_cache: dict[str, tuple[str, float]] = {}

async def _check_routing(from_user: str) -> bool:
    now = time.time()
    cached = _routing_cache.get(from_user)
    if cached and now < cached[1]:
        primary = cached[0]
        return primary == SERVER_ID
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(AUTH_CENTER_LOCAL, json={"user_id": from_user})
            if resp.status_code == 200:
                data = resp.json()
                primary = data.get("primary_server", "")
                _routing_cache[from_user] = (primary, now + 30)
                return primary == SERVER_ID
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(AUTH_CENTER_URL, json={"user_id": from_user})
            if resp.status_code == 200:
                data = resp.json()
                primary = data.get("primary_server", "")
                _routing_cache[from_user] = (primary, now + 30)
                return primary == SERVER_ID
    except Exception:
        pass
    return True


running_tasks: dict[str, asyncio.Task] = {}
bot_tokens: dict[str, str] = {}


async def load_bots_from_db() -> list[tuple[str, str]]:
    """从 DB 加载活跃 Bot（异步适配）"""
    return await asyncio.get_event_loop().run_in_executor(None, load_bots_from_db_raw)


async def sync_bots_from_db(shutdown: asyncio.Event):
    """后台 DB 同步 — 新 Bot 扫码后自动接入"""
    while not shutdown.is_set():
        try:
            await asyncio.sleep(DB_RELOAD_INTERVAL)
            bots = await load_bots_from_db()
            for bot_id, token in bots:
                if bot_id not in running_tasks:
                    root_log.info(f"💡 检测到新 Bot: {bot_id}，自动接入")
                    t = asyncio.create_task(run_bot(bot_id, token, shutdown))
                    running_tasks[bot_id] = t
                    bot_tokens[bot_id] = token
        except asyncio.CancelledError:
            break
        except Exception as e:
            root_log.warning(f"DB 同步异常: {e}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
