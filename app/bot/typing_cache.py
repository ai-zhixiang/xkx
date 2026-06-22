"""
Typing 指示器缓存模块

移植自 Hermes WeixinAdapter (gateway/platforms/weixin.py)
P1 — 显示 bot "正在输入..." 状态，提升用户体验
"""

import time
from typing import Dict, Optional, Tuple


class TypingTicketCache:
    """Short-lived typing ticket cache from getconfig.

    Usage::

        cache = TypingTicketCache(ttl_seconds=600.0)
        ticket = cache.get(user_id)
        if not ticket:
            # fetch via getConfig API
            cache.set(user_id, ticket)
    """

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())

    def clear(self) -> None:
        """Clear all cached typing tickets."""
        self._cache.clear()
