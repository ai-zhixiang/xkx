"""
享客虾 — AI Agent 工作进程 v7（文件记忆 + 分级控制 + fetch_url/PDF）
通过 stdin/stdout JSON 通信。
"""
import sys
import os
import json
import asyncio
import threading
import queue
import httpx
import re
import hashlib
from pathlib import Path

DEEPSEEK_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
MODEL = 'deepseek-chat'
TIER = os.environ.get('XKX_TIER', 'basic')
MAX_ITER = 5 if TIER == 'basic' else 15
PDF_API_URL = os.environ.get('XKX_PDF_API', 'http://localhost:8001/api/generate/pdf')

# 分级记忆配置
TIER_CONFIG = {
    'basic':   {'max_facts': 5,  'history_len': 5,  'fact_max_chars': 80},
    'standard':{'max_facts': 20, 'history_len': 15, 'fact_max_chars': 200},
}
cfg = TIER_CONFIG.get(TIER, TIER_CONFIG['basic'])

# 记忆文件路径
MEMORY_DIR = Path(os.environ.get('HERMES_HOME', '/tmp/hermes')) / 'memory'
MEMORY_FILE = MEMORY_DIR / 'user_facts.json'

# v0.5.0: 用户文件隔离
FILES_DIR = Path(os.environ.get('FILES_DIR', '/tmp/xkx_files'))
WORKSPACE_DIR = Path(os.environ.get('WORKSPACE_DIR', '/tmp/xkx_workspace'))
AGENT_OPENID = os.environ.get('AGENT_OPENID', 'unknown')

# 文件配额
QUOTA_MAP = {'basic': 50*1024*1024, 'standard': 100*1024*1024, 'pro': 500*1024*1024}
DISK_QUOTA = QUOTA_MAP.get(TIER, 50*1024*1024)

def load_memory() -> list:
    """加载持久记忆"""
    try:
        if MEMORY_FILE.exists():
            data = json.loads(MEMORY_FILE.read_text())
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []

def save_memory(memories: list):
    """保存记忆到文件（自动截断）"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    trimmed = memories[-cfg['max_facts']:]  # 只保留最近 N 条
    MEMORY_FILE.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2))

# 加载记忆
MEMORIES = load_memory()

def build_system() -> str:
    """构建含记忆的 system prompt"""
    base = ("你是「享客虾」，微信私人AI秘书。专业温和简洁。能搜索实时信息、读取任何网页和PDF链接。"
            "生成文档/报告时，必须深入详尽，包含具体案例、历史细节、名言引用，不少于800字。"
            "使用Markdown格式：## 标题、- 列表、**加粗**、> 引用。"
            "⚠️ 重要规则：当用户发送任何URL/链接时，你的第一步操作必须是调用 fetch_url 工具。"
            "绝不说「无法访问」「不能读取」「无法打开」之类的话——fetch_url 可以读取网页和PDF。")
    
    if MEMORIES:
        facts = '\n'.join(f'- {m}' for m in MEMORIES[-cfg['max_facts']:])
        base += f"\n\n## 用户记忆（你已知的信息，自然融入对话，不要逐条复述）\n{facts}"
    
    return base

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索网页获取实时信息",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "保存一条用户信息到长期记忆。用户告诉你名字/偏好/重要事项时调用。每条不超过80字。",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "要记住的事实，简洁完整"}
                },
                "required": ["fact"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_document",
            "description": "将内容生成PDF文档并提供下载链接。必须用Markdown格式撰写内容：## 标题、- 列表、**加粗**、> 引用。生成前先搜索相关资料确保内容深度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "文档主标题"},
                    "subtitle": {"type": "string", "description": "文档副标题/一句话概括"},
                    "content": {"type": "string", "description": "文档正文（Markdown格式，须深入详尽含案例细节，不少于800字）"}
                },
                "required": ["title", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出用户已上传的所有文件",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "save_file",
            "description": "将文本内容保存为文件到用户目录",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名（含扩展名，如 report.md）"},
                    "content": {"type": "string", "description": "文件内容"}
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取用户目录下的文件内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名"}
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "获取网页/链接的内容。用户发给你链接时调用此工具读取内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要获取的完整URL"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除用户目录下的文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "文件名"}
                },
                "required": ["filename"]
            }
        }
    }
]

async def deepseek(msgs):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post("https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
            json={"model": MODEL, "messages": msgs, "tools": TOOLS, "temperature": 0.7, "max_tokens": 4096})
        return r.json()

async def search(q):
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://html.duckduckgo.com/html/",
                params={"q": q},
                headers={"User-Agent": "Mozilla/5.0"})
            s = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.DOTALL)
            return '搜索：\n' + '\n'.join(
                f'• {re.sub(r"<[^>]+>", "", x).strip()[:200]}' for x in s[:5]
            ) if s else "无结果"
    except Exception as e:
        return f"搜索失败:{e}"


async def generate_document(title: str, subtitle: str, content: str) -> str:
    """调用 PDF API 生成文档，返回下载链接"""
    if TIER == 'basic':
        return "文档生成仅限标准版及以上套餐，升级后可解锁此功能👉 https://hai.pangoozn.com/xkx/"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(PDF_API_URL,
                json={"title": title, "subtitle": subtitle, "content": content, "author": "享客虾AI秘书"})
            if r.status_code == 200:
                data = r.json()
                if data.get('ok'):
                    return f"📄 文档已生成：\n{data['url']}\n（{data.get('format','pdf').upper()}格式·{data.get('size',0)//1024}KB）"
            return f"生成失败：{r.text[:100]}"
    except Exception as e:
        return f"文档生成异常：{e}"


# ===== v0.5.0: 文件管理工具 =====

def _get_file_url(filename: str) -> str:
    return f"https://hai.pangoozn.com/xkx/agents/{hashlib.md5(AGENT_OPENID.encode()).hexdigest()[:16]}/files/{filename}"

def _check_disk_quota() -> tuple:
    """返回 (已用字节, 配额字节, 是否超限)"""
    total = 0
    if FILES_DIR.exists():
        for f in FILES_DIR.rglob('*'):
            if f.is_file():
                total += f.stat().st_size
    return total, DISK_QUOTA, total > DISK_QUOTA

def list_user_files() -> str:
    """列出所有文件"""
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(FILES_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
    if not files:
        return "📂 暂无文件"
    
    used, quota, over = _check_disk_quota()
    lines = [f"📂 文件库（{used//1024}KB/{quota//1024}KB）："]
    for i, f in enumerate(files, 1):
        if f.is_file() and not f.name.startswith('.'):
            kb = f.stat().st_size // 1024
            lines.append(f"{i}. {f.name} ({kb}KB)")
    return '\n'.join(lines)

def save_user_file(filename: str, content: str) -> str:
    """保存文件（含配额检查）"""
    if not filename:
        return "❌ 请提供文件名"
    
    # 安全检查：不允许路径穿越
    safe_name = Path(filename).name
    if safe_name != filename or '..' in filename:
        return "❌ 文件名不合法"
    
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    
    used, quota, over = _check_disk_quota()
    if over:
        return f"❌ 磁盘配额已满（{used//1024}KB/{quota//1024}KB），请删除旧文件后重试"
    
    filepath = FILES_DIR / safe_name
    filepath.write_text(content, encoding='utf-8')
    size_kb = filepath.stat().st_size // 1024
    url = _get_file_url(safe_name)
    return f"✅ 已保存：{safe_name}（{size_kb}KB）\n📥 {url}"

def read_user_file(filename: str) -> str:
    """读取文件内容"""
    safe_name = Path(filename).name
    filepath = FILES_DIR / safe_name
    if not filepath.exists():
        return f"❌ 文件不存在：{safe_name}"
    
    size = filepath.stat().st_size
    if size > 50 * 1024:  # 50KB 上限
        return f"📄 {safe_name}（{size//1024}KB）太大，请缩小后重试"
    
    try:
        content = filepath.read_text(encoding='utf-8')
        return f"📄 {safe_name}：\n\n{content[:4000]}"
    except UnicodeDecodeError:
        return f"📄 {safe_name} 是二进制文件，无法直接显示"

def delete_user_file(filename: str) -> str:
    """删除文件"""
    safe_name = Path(filename).name
    filepath = FILES_DIR / safe_name
    if not filepath.exists():
        return f"❌ 文件不存在：{safe_name}"
    
    filepath.unlink()
    return f"🗑 已删除：{safe_name}"


async def fetch_url(url: str) -> str:
    """获取网页内容"""
    if not url.startswith(('http://', 'https://')):
        return f"❌ 无效URL：{url}"
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; Xiakexia/1.0)',
                'Accept': 'text/html,text/plain,application/pdf,*/*'
            })
        ct = r.headers.get('content-type', '')
        if 'text/html' in ct:
            text = r.text
            text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL|re.IGNORECASE)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL|re.IGNORECASE)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 4000:
                text = text[:4000] + '…（内容过长已截断）'
            return f"🌐 {url}\n\n{text}"
        elif 'application/pdf' in ct:
            # 用 pdftotext 提取文字
            import tempfile, subprocess
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(r.content)
                tmp_path = tmp.name
            try:
                result = subprocess.run(
                    ['pdftotext', tmp_path, '-', '-l', '3'],
                    capture_output=True, text=True, timeout=10
                )
                text = result.stdout.strip()
                if text and len(text) > 30:
                    return f"📄 PDF内容（{url}）：\n\n{text[:4000]}"
                else:
                    return f"📄 PDF（{url}，{len(r.content)//1024}KB）无法提取文字"
            finally:
                os.unlink(tmp_path)
        elif 'text/plain' in ct or 'application/json' in ct:
            return f"🌐 {url}\n\n{r.text[:4000]}"
        else:
            return f"🌐 {url}\n（{ct} 类型，{len(r.content)} bytes，无法直接显示文字内容）"
    except Exception as e:
        return f"❌ 获取失败：{e}"

async def chat(msg: str, history: list) -> str:
    """对话处理（含历史和工具调用）"""
    global MEMORIES
    
    def progress(pct: str):
        """输出进度消息（stdout → 父进程读取后推送微信）"""
        sys.stdout.write(json.dumps({'type': 'progress', 'content': pct}, ensure_ascii=False) + '\n')
        sys.stdout.flush()
    
    msgs = [{"role": "system", "content": build_system()}]
    
    # 注入近期对话历史
    if history:
        for h in history[-cfg['history_len']:]:
            msgs.append({"role": h.get("role", "user"), "content": h.get("content", "")[:500]})
    
    msgs.append({"role": "user", "content": msg[:2000]})
    
    for _ in range(MAX_ITER):
        r = await deepseek(msgs)
        m = r.get("choices", [{}])[0].get("message", {})
        
        if not m.get("tool_calls"):
            return m.get("content", "") or "(空)"
        
        msgs.append(m)
        for tc in m["tool_calls"]:
            fn = tc["function"]
            name = fn.get("name", "")
            args = json.loads(fn.get("arguments", "{}"))
            
            if name == "web_search":
                progress('🔍 正在搜索...')
                res = await search(args.get("query", ""))
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            
            elif name == "save_memory":
                fact = args.get("fact", "")[:cfg['fact_max_chars']]
                if fact:
                    # 去重：相似内容只保留最新
                    MEMORIES = [m for m in MEMORIES if m[:30] != fact[:30]]
                    MEMORIES.append(fact)
                    save_memory(MEMORIES)
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": f"已记住：{fact}"})
            
            elif name == "save_document":
                progress('📄 正在生成文档...')
                res = await generate_document(
                    args.get("title", "文档"),
                    args.get("subtitle", ""),
                    args.get("content", "")
                )
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            
            elif name == "list_files":
                res = list_user_files()
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            
            elif name == "save_file":
                res = save_user_file(args.get("filename", ""), args.get("content", ""))
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            
            elif name == "read_file":
                res = read_user_file(args.get("filename", ""))
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            
            elif name == "delete_file":
                res = delete_user_file(args.get("filename", ""))
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
            
            elif name == "fetch_url":
                progress('🌐 正在读取网页/PDF...')
                res = await fetch_url(args.get("url", ""))
                msgs.append({"role": "tool", "tool_call_id": tc["id"], "content": res})
    
    return "超时，请简化重试"


# ===== 主线程：读 stdin → 队列 → 异步处理 → stdout =====
q = queue.Queue()

def stdin_reader():
    for line in sys.stdin:
        q.put(line.strip())
    q.put(None)

async def worker():
    threading.Thread(target=stdin_reader, daemon=True).start()
    while True:
        try:
            line = q.get(timeout=0.1)
        except queue.Empty:
            await asyncio.sleep(0.05)
            continue
        if line is None:
            break
        if not line:
            continue
        try:
            msg = json.loads(line)
        except:
            continue

        if msg.get('type') == 'chat':
            try:
                text = await asyncio.wait_for(
                    chat(msg['message'], msg.get('history', [])),
                    timeout=90  # 文档生成需更长时间
                )
            except asyncio.TimeoutError:
                text = '🦞 处理超时，请简化重试'
            except Exception as e:
                text = f'🦞 出错了：{e}'
            sys.stdout.write(json.dumps({'type': 'response', 'content': text}, ensure_ascii=False) + '\n')
            sys.stdout.write(json.dumps({'type': 'done'}) + '\n')
            sys.stdout.flush()
        elif msg.get('type') == 'quit':
            break

asyncio.run(worker())
