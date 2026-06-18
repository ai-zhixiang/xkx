"""微侠 WeClaw — 轻量 Agent Loop
DeepSeek 直调 + tool calling + 上下文管理"""
import json, os, logging
from typing import Optional
import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .context import build_messages, save_messages, update_user_memory

logger = logging.getLogger("weclawd.agent")

DEEPSEEK_API = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-flash"
MAX_TOKENS = 1024
MAX_ITERATIONS = 5  # 最多 tool calling 轮次

# Bot 场景下的工具定义
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_music",
            "description": "搜索嗨卡曲库中的音乐/歌曲，按歌名或风格搜索",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词，歌名或风格"}
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "make_card",
            "description": "引导用户去制作AI嗨卡（电子贺卡），返回嗨卡页面链接",
            "parameters": {
                "type": "object",
                "properties": {
                    "theme": {"type": "string", "description": "贺卡主题/场景，如'生日''祝福''感谢'"}
                },
                "required": ["theme"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_songs",
            "description": "根据用户喜好推荐歌曲列表，返回歌名+风格",
            "parameters": {
                "type": "object",
                "properties": {
                    "style": {"type": "string", "description": "音乐风格，如'古风''流行''摇滚'"},
                    "count": {"type": "integer", "description": "推荐数量，默认3"},
                },
                "required": ["style"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_knowledge",
            "description": "查询享客虾产品知识库，了解功能介绍和使用方法",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "用户想问的问题"}
                },
                "required": ["question"],
            },
        },
    },
]


async def call_deepseek(
    messages: list[dict],
    tools: Optional[list] = None,
    api_key: str = "",
) -> dict:
    """调 DeepSeek API"""
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    if tools:
        payload["tools"] = tools

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            DEEPSEEK_API,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        if resp.status_code != 200:
            logger.error(f"DeepSeek API error: {resp.status_code} {resp.text[:200]}")
            return {"error": f"API error {resp.status_code}"}
        return resp.json()


async def execute_tool(name: str, args: dict) -> str:
    """执行本地工具"""
    if name == "search_music":
        keyword = args.get("keyword", "")
        # 调嗨卡音乐搜索 API
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://hai.pangoozn.com/api/music/list?keyword={keyword}&limit=5"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tracks = data.get("tracks", data.get("data", []))
                    if tracks:
                        results = []
                        for t in tracks[:5]:
                            results.append(f"{t.get('name','?')} - {t.get('artist','')} ({t.get('style','')})")
                        return "找到以下歌曲:\n" + "\n".join(results)
                    return "没有找到匹配的歌曲"
        except Exception as e:
            return f"搜索失败: {e}"
        return "搜索功能暂不可用"

    elif name == "make_card":
        theme = args.get("theme", "祝福")
        return json.dumps({
            "action": "redirect",
            "url": f"https://hai.pangoozn.com/static/hai.html?theme={theme}",
            "message": f"来给TA做一张{theme}主题的嗨卡吧！💝",
        })

    elif name == "recommend_songs":
        style = args.get("style", "流行")
        count = args.get("count", 3)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://hai.pangoozn.com/api/music/recommend?style={style}&limit={count}"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    tracks = data.get("tracks", data.get("data", []))
                    if tracks:
                        results = []
                        for t in tracks[:count]:
                            results.append(f"🎵 {t.get('name','?')} - {t.get('style','')}")
                        return f"为你推荐{style}风格歌曲:\n" + "\n".join(results)
                    return f"没有找到{style}风格的歌曲"
        except Exception as e:
            return f"推荐失败: {e}"
        return "推荐功能暂不可用"

    elif name == "query_knowledge":
        return ("🦞 享客虾是AI嗨卡里的AI创作伙伴。我能帮你:\n"
                "1. 制作AI贺卡（嗨卡）- 输入主题自动配诗配乐\n"
                "2. 搜歌和推荐音乐 - 海量曲库\n"
                "3. 写歌创作 - AI帮你写词作曲\n"
                "4. 日常聊天陪伴")
    return f"未知工具: {name}"


async def agent_process(
    db: AsyncSession,
    session_id: str,
    user_id: str,
    content: str,
    user_nickname: str = "",
    extra_context: str = "",
    api_key: str = "",
) -> str:
    """轻量 Agent Loop：取上下文 → DeepSeek → 工具调用 → 回复"""
    # 1. 构建 messages
    messages = await build_messages(db, session_id, user_id, content, user_nickname, extra_context)

    # 2. 保存用户消息
    await save_messages(db, session_id, [{"role": "user", "content": content}])

    # 3. Agent loop
    for iteration in range(MAX_ITERATIONS):
        response = await call_deepseek(messages, tools=TOOLS, api_key=api_key)

        if response.get("error"):
            logger.error(f"Agent loop error: {response['error']}")
            return f"🤖 服务暂时不可用，请稍后重试。"

        choice = response["choices"][0]
        msg = choice["message"]

        # 保存 assistant 消息
        assistant_entry = {"role": "assistant", "content": msg.get("content", "")}
        if msg.get("tool_calls"):
            assistant_entry["tool_calls"] = msg["tool_calls"]
        await save_messages(db, session_id, [assistant_entry])

        # 没有 tool_calls → 最终回复
        if not msg.get("tool_calls"):
            return msg.get("content", "")

        # 有 tool_calls → 执行工具
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            fn_name = fn.get("name", "")
            try:
                fn_args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                fn_args = {}

            logger.info(f"🔧 tool_call: {fn_name}({fn_args})")
            tool_result = await execute_tool(fn_name, fn_args)

            # 添加 tool result
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result,
            }
            messages.append(tool_msg)
            await save_messages(db, session_id, [tool_msg])

        # 继续下一轮

    logger.warning(f"Agent loop exceeded MAX_ITERATIONS ({MAX_ITERATIONS})")
    return "处理超时了，请再说一遍？"
