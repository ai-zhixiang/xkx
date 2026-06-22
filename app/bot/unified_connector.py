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
import sys
import tempfile
import time
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import asyncpg
import httpx

# ═══════════════════════════════════════════════════════════════
# 消息可靠性模块（P0~P1 移植自 Hermes WeixinAdapter）
# ═══════════════════════════════════════════════════════════════

from deduplicator import MessageDeduplicator
from text_splitter import split_text_for_weixin_delivery, format_weixin_message
from typing_cache import TypingTicketCache

# ═══════════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════════

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = 1  # OpenClaw 用的版本号

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
SHORT_POLL_TIMEOUT_MS = 5_000  # 心跳短轮询
CONFIG_TIMEOUT_MS = 10_000    # getConfig 超时

SESSION_EXPIRED_ERRCODE = -14
SESSION_PAUSE_MS = 10 * 60 * 1000      # session 过期暂停 10min (原 1h → P1 缩短)
HEARTBEAT_INTERVAL_MS = 5 * 60 * 1000   # 5 分钟无活动 → 发心跳

TYPING_START = 1
TYPING_STOP = 2

# 消抖合并 + 失败重试
_pending_merges = {}
_pending_retries = {}
MERGE_WINDOW = 3

MAX_CONSECUTIVE_FAILURES = 3
BACKOFF_DELAY_MS = 30_000
RETRY_DELAY_MS = 2_000

STATE_DIR = Path.home() / ".hermes" / "bot_connectors"
SYNC_BUF_DIR = STATE_DIR / "sync_buf"
PAUSE_DIR = STATE_DIR / "pause"
CONFIG_DIR = Path.home() / "weclaw-1" / "config"

STATE_DIR.mkdir(parents=True, exist_ok=True)
SYNC_BUF_DIR.mkdir(parents=True, exist_ok=True)
PAUSE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# 媒体类型（用于 iLink CDN 上传）
MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

# iLink item 类型
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VIDEO = 3
ITEM_FILE = 4
ITEM_VOICE = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES128(key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB 解密，自动去除 PKCS7 padding"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    cipher = Cipher(algorithms.AES128(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    # PKCS7 unpad
    pad_len = padded[-1]
    if 0 < pad_len <= 16:
        return padded[:-pad_len]
    return padded


def _aes_padded_size(size: int) -> int:
    block = 16
    return ((size + block - 1) // block) * block


# ═══════════════════════════════════════════════════════════════
# Bot 配置 — 从 DB 动态加载，不再硬编码
# ═══════════════════════════════════════════════════════════════

DB_DSN = "postgresql://lucky:lucky_pass@localhost:5432/weclawd"
GATEWAY_URL = "http://127.0.0.1:8001"
DB_RELOAD_INTERVAL = 30  # 每 30 秒检查 DB 是否有新 Bot

running_tasks: dict[str, asyncio.Task] = {}  # bot_id → Task
bot_tokens: dict[str, str] = {}              # bot_id → token（用于检测 token 变更）


async def load_bots_from_db() -> list[tuple[str, str]]:
    """从 DB 读取所有活跃 Bot 的 (bot_id, bot_token)"""
    try:
        conn = await asyncpg.connect(DB_DSN)
        try:
            rows = await conn.fetch(
                "SELECT bot_id, bot_token FROM bot_accounts "
                "WHERE is_active = true AND bot_token IS NOT NULL "
                "AND bot_token != ''"
            )
            return [(r["bot_id"], r["bot_token"]) for r in rows]
        finally:
            await conn.close()
    except Exception as e:
        root_log = logging.getLogger("unified-connector")
        root_log.error(f"DB 查询失败: {e}")
        return []


async def sync_bots_from_db(shutdown: asyncio.Event):
    """定期同步 DB 中的 Bot 列表 — 增删 Bot 不停机"""
    root_log = logging.getLogger("unified-connector")

    while not shutdown.is_set():
        try:
            bots = await load_bots_from_db()
            current_ids = {bid for bid, _ in bots}
            running_ids = set(running_tasks.keys())

            # 🆕 新 Bot — 启动
            for bot_id, token in bots:
                if bot_id not in running_ids:
                    root_log.info(f"🆕 DB 发现新 Bot: {bot_id}")
                    bot_tokens[bot_id] = token
                    task = asyncio.create_task(
                        run_bot(bot_id, token, shutdown))
                    running_tasks[bot_id] = task

            # 🔄 Token 已变更 — 重启
            for bot_id, token in bots:
                old_token = bot_tokens.get(bot_id)
                if old_token and old_token != token and bot_id in running_tasks:
                    root_log.info(f"🔄 Bot token 变更，重启: {bot_id}")
                    running_tasks[bot_id].cancel()
                    bot_tokens[bot_id] = token
                    await asyncio.sleep(2)
                    task = asyncio.create_task(
                        run_bot(bot_id, token, shutdown))
                    running_tasks[bot_id] = task

            # ❌ 已停用的 Bot — 停止
            for bot_id in running_ids - current_ids:
                root_log.info(f"❌ Bot 已停用，关闭: {bot_id}")
                running_tasks[bot_id].cancel()
                del running_tasks[bot_id]
                bot_tokens.pop(bot_id, None)

            # 清理已完成但未从 dict 移除的任务
            for bot_id in list(running_tasks.keys()):
                if running_tasks[bot_id].done():
                    try:
                        running_tasks[bot_id].result()
                    except asyncio.CancelledError:
                        pass
                    except Exception as e:
                        root_log.warning(f"Bot {bot_id} 异常退出: {e}")
                    del running_tasks[bot_id]
                    bot_tokens.pop(bot_id, None)

        except Exception as e:
            root_log.error(f"sync_bots 异常: {e}")

        # 等下一次同步，但也会被 shutdown 打断
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().create_future(),
                timeout=DB_RELOAD_INTERVAL)
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            break

# ═══════════════════════════════════════════════════════════════
# 暗号管理（配置文件热加载）
# ═══════════════════════════════════════════════════════════════

CODE_PHRASE_FILE = CONFIG_DIR / "access_codes.json"

_DEFAULT_CODES = {
    "codes": [
        {"code": "天王盖地虎", "reply": "OpenClaw 是SB！", "level": "admin"},
        {"code": "宝塔镇河妖", "reply": "微侠真牛逼！", "level": "admin"},
        {"code": "微侠真牛逼", "reply": "天王盖地虎！同志！", "level": "admin"},
        {"code": "openclaw是sb", "reply": "宝塔镇河妖！收到！", "level": "admin"},
    ]
}


def _ensure_code_file():
    if not CODE_PHRASE_FILE.exists():
        CODE_PHRASE_FILE.write_text(json.dumps(_DEFAULT_CODES, ensure_ascii=False, indent=2))


def load_code_phrases() -> list[dict]:
    """从配置文件加载暗号列表，热加载"""  # noqa: D400
    _ensure_code_file()
    try:
        data = json.loads(CODE_PHRASE_FILE.read_text())
        return data.get("codes", [])
    except (json.JSONDecodeError, FileNotFoundError):
        return _DEFAULT_CODES["codes"]


def match_code_phrase(text: str) -> Optional[dict]:
    """匹配暗号，匹配成功返回 {'reply': str, 'level': str}"""  # noqa: D400
    import re
    stripped = re.sub(r'[，。！？、；：""''\s]', '', text).lower()
    codes = load_code_phrases()
    for entry in codes:
        code = re.sub(r'[，。！？、；：""''\s]', '', entry["code"]).lower()
        if code in stripped:
            return entry
    return None


# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════

def _make_logger(name: str, log_file: Optional[Path] = None) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s",
                            datefmt="%H:%M:%S")

    # 文件 handler
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)

    # stderr（进 journalctl）
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(sh)
    return log


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _build_base_info() -> dict:
    return {"channel_version": "2.4.4"}


def _build_headers(token: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "AuthorizationType": "ilink_bot_token",
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }


# ═══════════════════════════════════════════════════════════════
# Session 状态持久化
# ═══════════════════════════════════════════════════════════════

def _safe_key(s: str) -> str:
    return s.replace("@", "_at_").replace(".", "_dot_")


def _sync_path(bot_id: str) -> Path:
    return SYNC_BUF_DIR / f"{_safe_key(bot_id)}.json"


def _pause_path(bot_id: str) -> Path:
    return PAUSE_DIR / f"{_safe_key(bot_id)}.json"


def load_sync_buf(bot_id: str) -> str:
    try:
        return json.loads(_sync_path(bot_id).read_text()).get("get_updates_buf", "")
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def save_sync_buf(bot_id: str, buf: str):
    _sync_path(bot_id).write_text(json.dumps({"get_updates_buf": buf}))


def is_paused(bot_id: str) -> bool:
    try:
        until = json.loads(_pause_path(bot_id).read_text()).get("paused_until", 0)
        if time.time() * 1000 < until:
            return True
        _pause_path(bot_id).unlink(missing_ok=True)
        return False
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def pause_bot(bot_id: str, duration_ms: int = SESSION_PAUSE_MS):
    until = int(time.time() * 1000 + duration_ms)
    _pause_path(bot_id).write_text(
        json.dumps({"paused_until": until, "paused_at": int(time.time() * 1000)}))


def clear_pause(bot_id: str):
    _pause_path(bot_id).unlink(missing_ok=True)


def pause_remaining_ms(bot_id: str) -> int:
    try:
        until = json.loads(_pause_path(bot_id).read_text()).get("paused_until", 0)
        return max(0, until - int(time.time() * 1000))
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


# ═══════════════════════════════════════════════════════════════
# iLink API
# ═══════════════════════════════════════════════════════════════

async def _ilink_post(endpoint: str, payload: dict, token: str,
                      timeout_ms: int = API_TIMEOUT_MS,
                      client: Optional[httpx.AsyncClient] = None) -> dict:
    body = json.dumps({**payload, "base_info": _build_base_info()}, separators=(",", ":"))
    close = client is None
    if close:
        client = httpx.AsyncClient(timeout=timeout_ms / 1000)
    try:
        r = await client.post(f"{ILINK_BASE_URL}/{endpoint}",
                              content=body, headers=_build_headers(token))
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    finally:
        if close:
            await client.aclose()


async def notify_start(token: str) -> bool:
    try:
        await _ilink_post("ilink/bot/msg/notifystart", {}, token, timeout_ms=10_000)
        return True
    except Exception:
        return False


async def notify_stop(token: str) -> bool:
    try:
        await _ilink_post("ilink/bot/msg/notifystop", {}, token, timeout_ms=5_000)
        return True
    except Exception:
        return False


async def get_updates(token: str, sync_buf: str, timeout_ms: int,
                      client: httpx.AsyncClient) -> dict:
    body = json.dumps({
        "get_updates_buf": sync_buf,
        "base_info": _build_base_info(),
    }, separators=(",", ":"))
    try:
        r = await client.post(f"{ILINK_BASE_URL}/ilink/bot/getupdates",
                              content=body, headers=_build_headers(token),
                              timeout=timeout_ms / 1000)
        if r.status_code != 200:
            return {"ret": -1, "errcode": r.status_code}
        return r.json()
    except httpx.TimeoutException:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def send_msg(token: str, to_user_id: str, text: str,
                   context_token: str = "",
                   bot: Optional['BotSession'] = None):
    """发送消息 — 支持 MEDIA:/path 文件直传 + 文本分块 (P0) + typing 指示器 (P1)"""
    root_log = logging.getLogger("unified-connector")

    # 检测 MEDIA: 前缀 → 走文件上传
    media_match = re.match(r'^\s*MEDIA:\s*(.+?)\s*$', text, re.DOTALL)
    if media_match:
        path = media_match.group(1).strip().rstrip('`"\'')
        path = os.path.expanduser(path)
        if os.path.isfile(path):
            try:
                await _send_file(token, to_user_id, path, context_token)
            except Exception as e:
                root_log.warning(f"[send_msg] 文件发送失败 {path}: {e}")
                # fallback: 发 HTTP 链接
                await _send_text(token, to_user_id,
                    f"📎 文件发送失败（已上传至服务器）\n请手动下载: file://{path}",
                    context_token)
            return
        else:
            root_log.warning(f"[send_msg] MEDIA 文件不存在: {path}")

    # ── 混合文本中的 MEDIA: 引用提取 ──
    media_paths = re.findall(r"MEDIA:\s*(\S+)", text)
    file_sent = False
    if media_paths:
        text = re.sub(r"\n?MEDIA:\s*\S+\n?", "\n", text).strip()
        for mpath in media_paths:
            mpath = os.path.expanduser(mpath.strip().rstrip("'\""))
            if os.path.isfile(mpath):
                try:
                    await _send_file(token, to_user_id, mpath, context_token)
                    root_log.info(f"[send_msg] 混合文本 MEDIA 发送成功: {mpath}")
                    file_sent = True
                except Exception as e:
                    root_log.warning(f"[send_msg] 混合文本 MEDIA 发送失败 {mpath}: {e}")
                    await _send_text(token, to_user_id,
                        f"\U0001f4ce 文件发送失败（已上传至服务器）\\n请手动下载: file://{mpath}",
                        context_token)
            else:
                root_log.warning(f"[send_msg] MEDIA 文件不存在 (混合文本): {mpath}")
        if not text or not text.strip():
            if file_sent:
                return
            text = "\U0001f4ce 文件"

    # ── P1: 发送 typing 指示器 ──
    if bot:
        asyncio.create_task(_send_typing_indicator(bot, to_user_id, TYPING_START))

    # ── P0: 文本分块发送 (移植 WeixinAdapter _split_text + _send_text_chunk) ──
    formatted = format_weixin_message(text)
    chunks = split_text_for_weixin_delivery(formatted, max_length=2000, split_per_line=False)
    if not chunks:
        chunks = [formatted[:2000]] if formatted else []

    for idx, chunk in enumerate(chunks):
        if not chunk or not chunk.strip():
            continue
        await _send_text(token, to_user_id, chunk, context_token)
        # 多分块之间延时，防 iLink 频率限制
        if idx < len(chunks) - 1:
            await asyncio.sleep(1.5)

    # ── P1: 停止 typing 指示器 ──
    if bot:
        asyncio.create_task(_send_typing_indicator(bot, to_user_id, TYPING_STOP))


async def _send_text(token: str, to_user_id: str, text: str,
                     context_token: str = ""):
    """发送纯文本消息"""
    msg = {
        "from_user_id": "", "to_user_id": to_user_id,
        "client_id": str(int(time.time() * 1000)),
        "message_type": MSG_TYPE_BOT, "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    try:
        await _ilink_post("ilink/bot/sendmessage", {"msg": msg}, token, timeout_ms=10_000)
    except Exception:
        pass


async def _send_file(token: str, to_user_id: str, path: str,
                     context_token: str = ""):
    """通过 iLink CDN 上传并发送文件"""
    plaintext = Path(path).read_bytes()
    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)
    rawsize = len(plaintext)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    filesize = _aes_padded_size(rawsize)
    aeskey_hex = aes_key.hex()

    # 1. 获取 iLink 上传 URL
    upload_resp = await _ilink_post("ilink/bot/getuploadurl", {
        "filekey": filekey,
        "media_type": _guess_media_type(path),
        "to_user_id": to_user_id,
        "rawsize": rawsize,
        "rawfilemd5": rawfilemd5,
        "filesize": filesize,
        "no_need_thumb": True,
        "aeskey": aeskey_hex,
    }, token, timeout_ms=15_000)

    upload_param = upload_resp.get("upload_param", "")
    upload_full_url = upload_resp.get("upload_full_url", "")

    # 2. AES 加密
    ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

    # 3. CDN 上传
    if upload_full_url:
        upload_url = upload_full_url
    elif upload_param:
        upload_url = f"{ILINK_BASE_URL}/upload?encrypted_query_param={upload_param}&filekey={filekey}"
    else:
        raise RuntimeError(f"getUploadUrl 无 upload_param/upload_full_url: {upload_resp}")

    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(upload_url, content=ciphertext,
                         headers={"Content-Type": "application/octet-stream"})
        if r.status_code != 200:
            raise RuntimeError(f"CDN 上传失败 HTTP {r.status_code}: {r.text[:200]}")
        encrypted_query_param = r.headers.get("x-encrypted-param", "")
        if not encrypted_query_param:
            raise RuntimeError(f"CDN 上传缺少 x-encrypted-param: {r.text[:200]}")

    # 4. 构建媒体消息
    aes_key_for_api = base64.b64encode(aeskey_hex.encode("ascii")).decode("ascii")
    item = _build_media_item(path, encrypted_query_param, aes_key_for_api, len(ciphertext), rawsize, rawfilemd5)

    msg = {
        "from_user_id": "", "to_user_id": to_user_id,
        "client_id": str(int(time.time() * 1000)),
        "message_type": MSG_TYPE_BOT, "message_state": MSG_STATE_FINISH,
        "item_list": [item],
    }
    if context_token:
        msg["context_token"] = context_token

    await _ilink_post("ilink/bot/sendmessage", {"msg": msg}, token, timeout_ms=10_000)


def _guess_media_type(path: str) -> int:
    mime = mimetypes.guess_type(path)[0] or ""
    if mime.startswith("image/"):
        return MEDIA_IMAGE
    if mime.startswith("video/"):
        return MEDIA_VIDEO
    if mime.startswith("audio/"):
        return MEDIA_FILE  # 音频以文件形式发送（非 voice）
    return MEDIA_FILE


def _build_media_item(path: str, encrypted_query_param: str,
                      aes_key_for_api: str, ciphertext_size: int,
                      plaintext_size: int, rawfilemd5: str) -> dict:
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
            "len": str(plaintext_size),
        },
    }


def _dl_log():
    """Get download logger with stdout handler"""
    log = logging.getLogger("weclaw.download")
    if not log.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s"))
        log.addHandler(h)
        log.setLevel(logging.DEBUG)
    return log

async def _download_media(media: dict, save_dir: Path, filename: str) -> Optional[Path]:
    """从 iLink CDN 下载并解密媒体文件（图片/文件/视频/语音）
    
    优先使用 full_url（iLink 提供的直接下载地址），
    降级使用 ILINK_BASE_URL/download?encrypted_query_param=...
    """
    log = _dl_log()
    full_url = media.get("full_url", "")
    enc_query = media.get("encrypt_query_param", "")
    aes_key_b64 = media.get("aes_key", "")

    if not aes_key_b64:
        log.warning("无 aes_key，无法解密: file=%s", filename)
        return None

    if full_url:
        url = full_url
        log.info("用 full_url 下载: %s", full_url[:80])
    elif enc_query:
        url = f"{ILINK_BASE_URL}/download?encrypted_query_param={enc_query}"
        log.info("用 ILINK 下载: param=%s", enc_query[:30])
    else:
        log.warning("既无 full_url 也无 encrypt_query_param: file=%s", filename)
        return None

    try:
        # 上传端编码: aeskey_hex = aes_key.hex() → base64.b64encode(aeskey_hex)
        # 所以下载端解码: base64 → ASCII hex string → bytes.fromhex
        aes_key_hex_b64 = base64.b64decode(aes_key_b64).decode("ascii")
        aes_key = bytes.fromhex(aes_key_hex_b64)
        log.info("密钥解码: len(b64)=%d hex=%s... key=%dB", len(aes_key_b64), aes_key_hex_b64[:16], len(aes_key))
        async with httpx.AsyncClient(timeout=120, verify=False) as c:
            r = await c.get(url, headers={"User-Agent": "iLink"})
            log.info("下载响应: status=%d size=%d", r.status_code, len(r.content))
            if r.status_code != 200:
                log.warning("下载失败: status=%d url=%s", r.status_code, url[:60])
                return None
            plain = _aes128_ecb_decrypt(r.content, aes_key)
            save_dir.mkdir(parents=True, exist_ok=True)
            fpath = save_dir / filename
            fpath.write_bytes(plain)
            log.info("✅ 保存成功: %s (%d bytes)", fpath, len(plain))
            return fpath
    except Exception as e:
        log.error("下载失败: file=%s err=%s", filename, e)
        return None


# ═══════════════════════════════════════════════════════════════
# 网关转发
# ═══════════════════════════════════════════════════════════════

async def forward_to_gateway(bot_id: str, user_id: str, content: str,
                              msg_id: str, client: httpx.AsyncClient,
                              media_path: Optional[str] = None) -> Optional[str]:
    try:
        body = {"bot_id": bot_id, "user_id": user_id, "content": content, "msg_id": msg_id}
        if media_path:
            body["media_path"] = media_path
        r = await client.post(
            f"{GATEWAY_URL}/api/bot/webhook",
            json=body,
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
# Bot Session 管理（三层防护核心）
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

    # ── Layer 2: 启动预热 — 先 notifyStop 清残留 ──
    if await notify_stop(token):
        log.info("  预热 notifyStop ✓")
    await asyncio.sleep(2)

    # ── Layer 1: 心跳循环 ──
    async with httpx.AsyncClient(timeout=45) as client:
        while not shutdown.is_set():
            try:
                await _run_session(bot, client, shutdown)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("❌ session 异常崩溃: %s", e)
                bot.consecutive_failures += 1
                if bot.consecutive_failures >= 3:
                    log.error("连续崩溃 3 次，暂停 10 分钟")
                    await _wait_for(600, shutdown)
                    bot.consecutive_failures = 0

    # ── Layer 2: 优雅关闭 — notifyStop ──
    log.info("🤖 关闭中...")
    await notify_stop(token)
    log.info("🤖 已停止")


async def _run_session(bot: BotSession, client: httpx.AsyncClient, shutdown: asyncio.Event):
    """一次 session 生命周期"""

    log = bot.log
    bot.last_activity = time.time()

    # 如果有历史 sync_buf，恢复
    bot.sync_buf = load_sync_buf(bot.bot_id)
    if bot.sync_buf:
        log.info("  恢复 sync_buf (%d bytes)", len(bot.sync_buf))

    # notifyStart
    if not await notify_start(bot.token):
        log.warning("  notifyStart 失败，重试...")
        await _wait_for(5, shutdown)
        if not await notify_start(bot.token):
            log.error("  notifyStart 连续失败，跳过")
            return

    log.info("✅ notifyStart ✓")

    # 主轮询循环
    next_timeout = LONG_POLL_TIMEOUT_MS
    heartbeat_task = None
    heartbeat_interval = 0

    while not shutdown.is_set():
        # ── Layer 3: 自愈 — 检查暂停状态 ──
        if is_paused(bot.bot_id):
            remaining = pause_remaining_ms(bot.bot_id)
            log.info("⏳ session 暂停中，剩余 %d 分钟", remaining // 60_000)
            await _wait_for(min(remaining / 1000, 600), shutdown)
            clear_pause(bot.bot_id)
            bot.consecutive_failures = 0
            # 重新初始化
            await notify_stop(bot.token)
            await asyncio.sleep(2)
            return  # 外层会重新进入 _run_session

        try:
            # 长轮询 getupdates
            resp = await get_updates(bot.token, bot.sync_buf, next_timeout, client)

            ret = resp.get("ret", 0)
            errcode = resp.get("errcode", 0)
            is_error = (ret is not None and ret != 0) or (errcode is not None and errcode != 0)

            if is_error:
                # ── Layer 3: 自愈 — session 过期处理 ──
                if errcode == SESSION_EXPIRED_ERRCODE or ret == SESSION_EXPIRED_ERRCODE:
                    log.error("🚫 session 过期 (errcode=%s)，暂停 %d 分钟", errcode, SESSION_PAUSE_MS // 60_000)
                    pause_bot(bot.bot_id)
                    await _wait_for(10, shutdown)
                    bot.consecutive_failures = 0
                    return  # 外层会重进 _run_session

                bot.consecutive_failures += 1
                log.warning("getUpdates 错误 ret=%s err=%s (%d/3)",
                            ret, errcode, bot.consecutive_failures)
                if bot.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    delay = BACKOFF_DELAY_MS / 1000
                    log.warning("连续 %d 次失败，退避 %ds", MAX_CONSECUTIVE_FAILURES, delay)
                    bot.consecutive_failures = 0
                    await _wait_for(delay, shutdown)
                else:
                    await _wait_for(RETRY_DELAY_MS / 1000, shutdown)
                continue

            # ── 成功轮询 ──
            bot.consecutive_failures = 0
            bot.last_activity = time.time()

            # 自适应 timeout
            if resp.get("longpolling_timeout_ms"):
                next_timeout = resp["longpolling_timeout_ms"]

            # sync_buf 持久化
            new_buf = resp.get("get_updates_buf", "")
            if new_buf and new_buf != bot.sync_buf:
                save_sync_buf(bot.bot_id, new_buf)
                bot.sync_buf = new_buf

            # ── Layer 1: 心跳保活 — 活动后重置心跳计数器 ──
            heartbeat_interval = 0

            # ── 处理消息 ──
            for msg in resp.get("msgs", []):
                from_user = msg.get("from_user_id", "")
                if not from_user:
                    continue

                ctx = msg.get("context_token", "")
                if ctx:
                    bot.context_tokens[from_user] = ctx

                # ── P0: 消息去重 (MessageDeduplicator) ──
                msg_id = str(msg.get("message_id") or msg.get("msg_id") or "")
                if msg_id and bot.dedup.is_duplicate(msg_id):
                    log.debug("  去重跳过 msg_id=%s", msg_id[:16])
                    continue

                text, media_path = await _extract_msg(msg, bot, from_user)
                if not text:
                    continue
                log.info("📩 %s: %s", from_user[:20], text[:60])

                # P0: 暗号先行（跳过合并和网关，直接回）
                code_match = match_code_phrase(text)
                if code_match:
                    reply = code_match["reply"]
                    log.info("🔑 暗号匹配: %s -> %s", text[:12], reply)
                    await send_msg(bot.token, from_user, reply, ctx, bot=bot)
                    continue

                # P1: 消抖合并 - 3秒窗口内同一用户消息合并为一条
                if from_user in _pending_merges:
                    merge = _pending_merges[from_user]
                    merge["texts"].append(text)
                    merge["ctx"] = ctx
                    merge["task"].cancel()
                    log.debug("  消抖: 追加(共%d条)", len(merge["texts"]))
                else:
                    _pending_merges[from_user] = {"texts": [text], "ctx": ctx, "task": None}

                async def _do_flush(uid, bot_id_val, token_val, client_val):
                    await asyncio.sleep(MERGE_WINDOW)
                    merge = _pending_merges.pop(uid, None)
                    if not merge or not merge["texts"]:
                        return
                    merged_text = "\n".join(merge["texts"])
                    merge_ctx = merge["ctx"]

                    # 先重试该 Bot 的待回复消息
                    if bot_id_val in _pending_retries:
                        retry_list = _pending_retries.pop(bot_id_val, [])
                        for r_uid, r_text, r_ctx in retry_list:
                            r_reply = await forward_to_gateway(bot_id_val, r_uid, r_text, "", client_val)
                            if r_reply:
                                await send_msg(token_val, r_uid, r_reply, r_ctx)
                            else:
                                log.warning("重试仍失败: %s", r_uid[:20])

                    # P2: typing
                    asyncio.create_task(_send_typing_indicator(bot, uid, TYPING_START))

                    # P3: 转发网关
                    try:
                        reply = await forward_to_gateway(bot_id_val, uid, merged_text, "", client_val)
                    except Exception as e:
                        log.warning("转发异常: %s", e)
                        reply = None

                    if reply:
                        log.info("📤 %s", reply[:40])
                        await send_msg(token_val, uid, reply, merge_ctx, bot=bot)
                    else:
                        log.warning("网关无回复(排队待重试): %s", uid[:20])
                        _pending_retries.setdefault(bot_id_val, []).append((uid, merged_text, merge_ctx))

                flush_task = asyncio.create_task(_do_flush(from_user, bot.bot_id, bot.token, client))
                _pending_merges[from_user]["task"] = flush_task

        except asyncio.CancelledError:
            raise
        except Exception as e:
            bot.consecutive_failures += 1
            log.error("轮询异常 (%d/3): %s", bot.consecutive_failures, e)
            if bot.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                await _wait_for(BACKOFF_DELAY_MS / 1000, shutdown)
                bot.consecutive_failures = 0
            else:
                await _wait_for(RETRY_DELAY_MS / 1000, shutdown)




MEDIA_SAVE_DIR = Path.home() / "weclaw_media"

async def _extract_msg(msg: dict, bot: BotSession, from_user: str) -> tuple:
    """从消息中提取文本并下载媒体文件。返回 (text, media_path_or_None)"""
    texts = []
    media_path = None
    for item in msg.get("item_list", []):
        t = item.get("type", 0)
        # Debug: log full item for non-text types
        if t != 1:
            bot.log.info("📎 媒体消息 item: type=%s keys=%s", t, list(item.keys()))
        if t == 1:
            txt = (item.get("text_item") or {}).get("text", "")
            if txt:
                texts.append(txt)
        elif t == 2:
            img = item.get("image_item") or {}
            media = img.get("media") or {}
            fname = f"img_{from_user[:8]}_{int(time.time())}.jpg"
            texts.append("[图片]")
            dl = await _download_media(media, MEDIA_SAVE_DIR, fname)
            if dl:
                media_path = str(dl)
        elif t == 3:
            video = item.get("video_item") or {}
            dur = video.get("play_length", 0)
            media = video.get("media") or {}
            fname = f"video_{from_user[:8]}_{int(time.time())}.mp4"
            texts.append(("[视频 %.1fs]" % (int(dur)/1000)) if dur else "[视频]")
            dl = await _download_media(media, MEDIA_SAVE_DIR, fname)
            if dl:
                media_path = str(dl)
        elif t == 4:
            file_item = item.get("file_item") or {}
            fname = file_item.get("file_name", "file")
            fsize = file_item.get("len", "")
            media = file_item.get("media") or {}
            bot.log.info("📎 文件 media keys: %s aes_key=%s", list(media.keys()), bool(media.get("aes_key")))
            texts.append("[文件: " + fname + "]" + (" (" + fsize + "B)" if fsize else ""))
            dl = await _download_media(media, MEDIA_SAVE_DIR, fname)
            if dl:
                media_path = str(dl)
                texts[-1] = texts[-1][:-1] + " ↓已下载]"  # append download indicator
        elif t == 5:
            voice = item.get("voice_item") or {}
            dur = voice.get("voice_length", 0)
            media = voice.get("media") or {}
            fname = f"voice_{from_user[:8]}_{int(time.time())}.silk"
            texts.append(("[语音 %.1fs]" % (int(dur)/1000)) if dur else "[语音]")
            dl = await _download_media(media, MEDIA_SAVE_DIR, fname)
            if dl:
                media_path = str(dl)
    return (" ".join(texts) if texts else "", media_path)

async def _wait_for(seconds: float, shutdown: asyncio.Event):
    """带 shutdown 检测的等待"""
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().create_future(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        raise


# ═══════════════════════════════════════════════════════════════
# Typing 指示器 (P1 — 移植自 WeixinAdapter)
# ═══════════════════════════════════════════════════════════════

async def _ilink_get_config(token: str, user_id: str,
                            context_token: Optional[str] = None) -> dict:
    """调用 iLink getconfig 获取 typing_ticket"""
    payload: dict = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _ilink_post("ilink/bot/getconfig", payload, token,
                             timeout_ms=CONFIG_TIMEOUT_MS)


async def _maybe_fetch_typing_ticket(bot: BotSession, user_id: str,
                                     context_token: Optional[str]) -> None:
    """异步获取 typing ticket 并缓存"""
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
    """发送 typing 指示器 (开始/停止)"""
    if not bot.token:
        return
    typing_ticket = bot.typing_cache.get(to_user_id)
    if not typing_ticket:
        # 无 ticket 时尝试在线获取（只试一次，不阻塞队列）
        try:
            await _maybe_fetch_typing_ticket(bot, to_user_id, None)
            typing_ticket = bot.typing_cache.get(to_user_id)
        except Exception:
            pass
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
                      to_user_id[:20], exc)


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

    # 确保暗号配置文件存在
    _ensure_code_file()

    shutdown = asyncio.Event()

    # SIGTERM/SIGINT — 优雅关闭
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

    # 首次从 DB 加载
    bots = await load_bots_from_db()
    root_log.info(f"DB 加载到 {len(bots)} 个活跃 Bot:")
    for bot_id, token in bots:
        t = asyncio.create_task(run_bot(bot_id, token, shutdown))
        running_tasks[bot_id] = t
        bot_tokens[bot_id] = token
        root_log.info(f"  ✅ {bot_id}")

    # 启动 DB 同步协程（后台运行，30s 轮询）
    sync_task = asyncio.create_task(sync_bots_from_db(shutdown))

    root_log.info(f"所有 Bot 已启动（{len(running_tasks)} 个），等待消息...")
    root_log.info(f"DB 同步后台运行中，新 Bot 扫码后最快 {DB_RELOAD_INTERVAL}s 接入")

    await shutdown.wait()

    # 逐一 notifyStop（从 running_tasks 里找 token）
    root_log.info("🛑 正在关闭所有 Bot session...")
    for bot_id, task in list(running_tasks.items()):
        token = bot_tokens.get(bot_id, "")
        if token:
            try:
                await notify_stop(token)
                root_log.info("  %s notifyStop ✓", bot_id[:20])
            except Exception:
                pass
        task.cancel()

    # 取消 sync 任务
    sync_task.cancel()

    # 等待所有任务结束
    all_tasks = list(running_tasks.values()) + [sync_task]
    if all_tasks:
        await asyncio.gather(*all_tasks, return_exceptions=True)

    root_log.info("统一连接器已停止")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
