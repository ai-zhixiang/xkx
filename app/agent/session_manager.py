"""
享客虾 — Agent Session Manager
管理每个付费用户的 Hermes Agent 子进程生命周期。

特性：
- 为每个用户创建隔离的 HERMES_HOME
- 空闲 15 分钟自动回收子进程
- 最大并发 20 个进程
- 异常崩溃自动重建
"""
import os
import json
import time
import hashlib
import asyncio
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger('xkx.agent')

# 配置
HERMES_VENV = 'python3'  # 用系统 python3 运行 worker（更快、更稳定）
# 享客虾 v0.5.0: 每个付费用户独立 Agent 隔离目录
AGENTS_ROOT = Path(os.getenv('XKX_AGENTS_ROOT', '/home/ubuntu/weclaw-1/agents'))

# 套餐磁盘配额（字节）
QUOTA_MAP = {
    'basic':    50 * 1024 * 1024,   # 50MB
    'standard': 100 * 1024 * 1024,  # 100MB
    'pro':      500 * 1024 * 1024,  # 500MB
}
WORKER_SCRIPT = Path(__file__).parent / 'hermes_worker.py'
MAX_CONCURRENT = int(os.getenv('XKX_MAX_AGENTS', '20'))
IDLE_TIMEOUT = int(os.getenv('XKX_AGENT_IDLE_SEC', '900'))  # 15 min
DEEPSEEK_KEY = os.getenv('DEEPSEEK_API_KEY', 'os.getenv("DEEPSEEK_API_KEY")')

# 工具集映射
TOOLSETS = {
    'basic': 'xkx-basic',
    'standard': 'xkx-standard',
}

# 全局会话池
_sessions: dict[str, 'AgentSession'] = {}


class AgentSession:
    """单个用户的 Agent 会话"""

    def __init__(self, openid: str, tier: str = 'basic'):
        self.openid = openid
        self.tier = tier
        self.session_hash = hashlib.md5(openid.encode()).hexdigest()[:16]
        self.home = AGENTS_ROOT / self.session_hash
        self.process: Optional[asyncio.subprocess.Process] = None
        self.last_active = time.time()
        self.created_at = time.time()
        self._lock = asyncio.Lock()

    async def _ensure_home(self):
        """创建隔离的 HERMES_HOME + 文件/工作目录"""
        self.home.mkdir(parents=True, exist_ok=True)
        for sub in ['memory', 'sessions', 'skills', 'logs', 'files', 'workspace']:
            (self.home / sub).mkdir(exist_ok=True)
        
        # 初始化磁盘配额文件
        quota_file = self.home / '.quota'
        if not quota_file.exists():
            quota_file.write_text(str(QUOTA_MAP.get(self.tier, 50 * 1024 * 1024)))
    
    async def check_disk_quota(self) -> tuple[bool, int, int]:
        """检查用户文件目录磁盘使用
        返回: (是否超配额, 已用字节, 配额字节)
        """
        files_dir = self.home / 'files'
        quota = int((self.home / '.quota').read_text().strip())
        
        total = 0
        if files_dir.exists():
            for f in files_dir.rglob('*'):
                if f.is_file():
                    total += f.stat().st_size
        
        return total > quota, total, quota
    
    async def list_files(self) -> list[dict]:
        """列出用户 files/ 目录下的所有文件"""
        files_dir = self.home / 'files'
        if not files_dir.exists():
            return []
        
        result = []
        for f in sorted(files_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if f.is_file() and not f.name.startswith('.'):
                result.append({
                    'name': f.name,
                    'size': f.stat().st_size,
                    'mtime': f.stat().st_mtime,
                })
        return result
    
    def get_file_url(self, filename: str) -> str:
        """生成文件的外部可访问 URL"""
        return f'https://hai.pangoozn.com/xkx/agents/{self.session_hash}/files/{filename}'

        config = self.home / 'config.yaml'
        if not config.exists():
            config.write_text(f"""# 享客虾 · Hermes Agent 用户配置
# 用户: {self.openid[:12]}...
# 套餐: {self.tier}

model:
  provider: deepseek
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    api_mode: chat_completions
    model: deepseek-chat
agent:
  max_turns: {'15' if self.tier == 'standard' else '5'}
toolsets: [{TOOLSETS.get(self.tier, 'xkx-basic')}]
""")

    async def start(self):
        """启动 Hermes Worker 子进程（调用者需持有 _lock）"""
        if self.process and self.process.returncode is None:
            return  # 已运行

        await self._ensure_home()

        env = os.environ.copy()
        env['HERMES_HOME'] = str(self.home)
        env['HERMES_TOOLSETS'] = TOOLSETS.get(self.tier, 'xkx-basic')
        env['HERMES_MODEL'] = 'deepseek-chat'
        env['DEEPSEEK_API_KEY'] = DEEPSEEK_KEY
        env['XKX_TIER'] = self.tier
        env['AGENT_OPENID'] = self.openid
        env['FILES_DIR'] = str(self.home / 'files')
        env['WORKSPACE_DIR'] = str(self.home / 'workspace')

        logger.info(f'[Agent] 启动会话: {self.session_hash} (tier={self.tier})')
        self.process = await asyncio.create_subprocess_exec(
            HERMES_VENV, str(WORKER_SCRIPT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        self.last_active = time.time()

    async def send(self, message: str, history: list = None,
                   timeout: float = 60.0, kill_on_timeout: bool = False,
                   on_progress=None) -> str:
        """发送消息并等待完整回复。
        timeout: 每行读取超时秒数（默认10s，外层 asyncio.wait_for 控制总时间）
        kill_on_timeout: True=超时杀进程, False=保留进程供 wait_pending() 续读
        on_progress: 可选回调 async def(msg: str) — 收到进度消息时调用
        """
        async with self._lock:
            if not self.process or self.process.returncode is not None:
                await self.start()

            self.last_active = time.time()
            self._pending = True

            payload = json.dumps({
                'type': 'chat',
                'message': message,
                'history': history or [],
            }, ensure_ascii=False) + '\n'
            self.process.stdin.write(payload.encode())
            await self.process.stdin.drain()

            response_parts = []
            try:
                while True:
                    line = await asyncio.wait_for(
                        self.process.stdout.readline(),
                        timeout=min(timeout, 30.0)  # 每行最多30s，外层总控
                    )
                    if not line:
                        break
                    try:
                        resp = json.loads(line.decode().strip())
                    except json.JSONDecodeError:
                        continue
                    if resp.get('type') == 'response':
                        response_parts.append(resp.get('content', ''))
                    elif resp.get('type') == 'progress':
                        if on_progress:
                            try:
                                await on_progress(resp.get('content', ''))
                            except Exception:
                                pass
                    elif resp.get('type') == 'done':
                        break
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.warning(f'[Agent] 会话超时 ({timeout}s): {self.session_hash}, kill={kill_on_timeout}')
                if kill_on_timeout:
                    self.process.kill()
                    try:
                        await asyncio.wait_for(self.process.wait(), timeout=3)
                    except Exception:
                        pass
                    self.process = None
                    self._pending = False
                # kill_on_timeout=False 时保留 _pending=True 供 wait_pending() 续读
                raise
            finally:
                if kill_on_timeout:
                    self._pending = False

            return '\n'.join(response_parts)

    async def wait_pending(self, timeout: float = 60.0, on_progress=None) -> str:
        """等待正在进行中的 send() 响应完成（send() 超时后调用）。
        进程必须仍在运行且 _pending=True。
        on_progress: 可选回调 async def(msg: str)
        """
        async with self._lock:
            if not self.process or self.process.returncode is not None:
                raise RuntimeError('Agent 进程已退出')
            if not getattr(self, '_pending', False):
                raise RuntimeError('没有进行中的请求')

            response_parts = []
            try:
                while True:
                    line = await asyncio.wait_for(
                        self.process.stdout.readline(),
                        timeout=timeout
                    )
                    if not line:
                        break
                    try:
                        resp = json.loads(line.decode().strip())
                    except json.JSONDecodeError:
                        continue
                    if resp.get('type') == 'response':
                        response_parts.append(resp.get('content', ''))
                    elif resp.get('type') == 'progress':
                        if on_progress:
                            try:
                                await on_progress(resp.get('content', ''))
                            except Exception:
                                pass
                    elif resp.get('type') == 'done':
                        break
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.warning(f'[Agent] wait_pending 超时，强制终止: {self.session_hash}')
                self.process.kill()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=3)
                except Exception:
                    pass
                self.process = None
                raise
            finally:
                self._pending = False

            return '\n'.join(response_parts)

    async def stop(self):
        """停止子进程"""
        async with self._lock:
            if self.process and self.process.returncode is None:
                try:
                    self.process.stdin.write(b'{"type":"quit"}\n')
                    await self.process.stdin.drain()
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                except Exception:
                    self.process.kill()
            self.process = None

    def is_idle(self) -> bool:
        return (time.time() - self.last_active) > IDLE_TIMEOUT


class AgentSessionManager:
    """会话池管理器"""

    @staticmethod
    async def chat(openid: str, message: str, tier: str = 'basic', history: list = None) -> str:
        """付费用户接入点——返回 AI 回复"""
        # 回收空闲会话
        for oid, sess in list(_sessions.items()):
            if sess.is_idle():
                logger.info(f'[Agent] 回收空闲会话: {sess.session_hash}')
                await sess.stop()
                del _sessions[oid]

        # 限流
        active = sum(1 for s in _sessions.values() if s.process and s.process.returncode is None)
        if active >= MAX_CONCURRENT:
            # 强制回收最早的空闲会话
            oldest = min(_sessions.values(), key=lambda s: s.last_active)
            if oldest.is_idle() or True:  # 强制回收
                await oldest.stop()
                del _sessions[oldest.openid]

        # 获取或创建会话
        if openid not in _sessions:
            _sessions[openid] = AgentSession(openid, tier)

        session = _sessions[openid]
        try:
            return await session.send(message, history=history)
        except asyncio.TimeoutError:
            logger.warning(f'[Agent] Agent {session.session_hash} 响应超时')
            await session.stop()
            if openid in _sessions:
                del _sessions[openid]
            return '[享客虾] 正在处理中，请稍等几秒再发一次～'
        except Exception as e:
            logger.exception(f'[Agent] 会话异常: {session.session_hash}')
            # 清理失败会话，下次自动重建
            await session.stop()
            if openid in _sessions:
                del _sessions[openid]
            return f'[享客虾] 抱歉，AI引擎暂时遇到问题：{e}'


async def shutdown_all():
    """清理所有会话（服务停止时调用）"""
    for oid, sess in list(_sessions.items()):
        await sess.stop()
    _sessions.clear()
