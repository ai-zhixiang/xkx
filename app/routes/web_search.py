"""联网搜索模块 — Bing（腾讯云可达）"""
import logging
import urllib.parse

import httpx
from bs4 import BeautifulSoup

SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
SEARCH_TIMEOUT = 15
MAX_RESULTS = 5


async def web_search(query: str) -> str:
    """搜索 Bing 返回格式化结果"""
    if not query or not query.strip():
        return "[搜索失败: 查询为空]"
    try:
        q = urllib.parse.quote(query.strip())
        url = f"https://www.bing.com/search?q={q}&count={MAX_RESULTS}&mkt=zh-CN"
        async with httpx.AsyncClient(timeout=SEARCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers=SEARCH_HEADERS)
            if resp.status_code != 200:
                return f"[搜索失败: HTTP {resp.status_code}]"

            soup = BeautifulSoup(resp.text, "lxml")
            items = []
            for li in soup.select("li.b_algo")[:MAX_RESULTS]:
                a = li.select_one("h2 a")
                if not a:
                    continue
                title = a.get_text(strip=True)
                link = a.get("href", "")
                desc_el = li.select_one(".b_caption p")
                desc = desc_el.get_text(strip=True) if desc_el else ""
                items.append(f"  {title}\n  {desc[:200]}\n  {link}")

            if not items:
                # 尝试备用解析
                for li in soup.select(".b_algo")[:MAX_RESULTS]:
                    a = li.select_one("a")
                    if not a:
                        continue
                    title = a.get_text(strip=True)
                    link = a.get("href", "")
                    items.append(f"  {title}\n  {link}")

            if not items:
                return f"[搜索 '{query}' 无结果]"

            result = "📡 搜索结果（" + query.strip() + "）：\n" + "\n\n".join(items)
            return result

    except Exception as e:
        logging.getLogger("bot-gateway").warning(f"Web search error: {e}")
        return f"[搜索异常: {e}]"
