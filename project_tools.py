"""
项目操作 MCP 工具 — 共享项目文件读写。

所有 API 调用需要 nickname 参数。
Agent 从 system prompt 中的 【当前用户昵称: XXX】 提取并传入。
"""

import json
import urllib.request
import urllib.parse

BASE = "https://hai.pangoozn.com/api/projects"


def _call(url: str, data: dict = None) -> dict:
    """调项目 API，返回 dict。"""
    try:
        if data:
            body = json.dumps(data).encode()
            req = urllib.request.Request(url, data=body, method="POST")
        else:
            req = urllib.request.Request(url)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        try:
            detail = json.loads(err).get("detail", err)
        except Exception:
            detail = err
        return {"error": True, "detail": detail, "status": e.code}
    except Exception as e:
        return {"error": True, "detail": str(e)}


@mcp.tool()
def project_list(nickname: str = "") -> str:
    """列出当前用户有权访问的项目。nickname=当前用户昵称（从上下文获取）"""
    if not nickname:
        return "⚠️ 需要用户昵称才能查询，请稍后再试"
    data = _call(f"{BASE}/list?nickname={urllib.parse.quote(nickname)}")
    if data.get("error"):
        return f"查询失败: {data['detail']}"
    projects = data.get("projects", [])
    if not projects:
        return "你还没有加入任何项目"
    return "📁 你的项目：\n" + "\n".join(f"  • {p}" for p in projects)


@mcp.tool()
def project_enter(name: str, nickname: str = "") -> str:
    """进入一个项目。name=项目名, nickname=当前用户昵称（从上下文获取）"""
    if not nickname:
        return "⚠️ 需要用户昵称"
    data = _call(f"{BASE}/enter", {"name": name, "nickname": nickname, "channel": "hermes"})
    if data.get("error"):
        return f"进入失败: {data['detail']}"
    return f"✅ 已进入「{name}」"


@mcp.tool()
def project_files(project: str = "", dir: str = "", nickname: str = "") -> str:
    """查看项目中的文件列表。project=项目名(留空用当前项目), dir=子目录, nickname=当前用户昵称"""
    if not nickname:
        return "⚠️ 需要用户昵称"
    params = f"?nickname={urllib.parse.quote(nickname)}"
    if project:
        params += f"&project={urllib.parse.quote(project)}"
    if dir:
        params += f"&dir={urllib.parse.quote(dir)}"
    data = _call(f"{BASE}/files{params}")
    if data.get("error"):
        return f"查询失败: {data['detail']}"
    entries = data.get("entries", [])
    proj = data.get("project", "?")
    cur_dir = data.get("dir", "")
    if not entries:
        return f"📂 {proj} — 空目录"
    lines = [f"📂 {proj}" + (f"/{cur_dir}" if cur_dir else "")]
    for e in entries:
        icon = "📁" if e.get("type") == "dir" else "📄"
        sz = e.get("size", 0)
        szs = f"{sz/1024:.0f}KB" if sz > 1024 else f"{sz}B"
        lines.append(f"  {icon} {e['name']} ({szs})")
    return "\n".join(lines)


@mcp.tool()
def project_read(project: str, file: str, nickname: str = "") -> str:
    """读取项目中的文件内容。project=项目名, file=文件路径, nickname=当前用户昵称"""
    if not nickname:
        return "⚠️ 需要用户昵称"
    params = f"?project={urllib.parse.quote(project)}&file={urllib.parse.quote(file)}&nickname={urllib.parse.quote(nickname)}"
    data = _call(f"{BASE}/read{params}")
    if data.get("error"):
        return f"读取失败: {data['detail']}"
    content = data.get("content", "")
    if len(content) > 3000:
        content = content[:3000] + "\n\n...(截断，文件较大)"
    return content


@mcp.tool()
def project_search(keyword: str, nickname: str = "") -> str:
    """在有权访问的项目中搜索文件。keyword=关键词, nickname=当前用户昵称"""
    if not nickname:
        return "⚠️ 需要用户昵称"
    data = _call(f"{BASE}/search", {"keyword": keyword, "nickname": nickname})
    if data.get("error"):
        return f"搜索失败: {data['detail']}"
    results = data.get("results", [])
    if not results:
        return f"未找到包含「{keyword}」的文件"
    lines = [f"🔍 找到 {len(results)} 个文件："]
    for r in results[:20]:
        lines.append(f"  📄 {r['project']}/{r['path']}")
    return "\n".join(lines)


@mcp.tool()
def project_merge(merges: list, nickname: str = "") -> str:
    """合并多个项目到目标项目（仅超级管理员可操作）。merges=[{target:'微侠',sources:['微侠_WeClaw','微侠桌面']},...], nickname=当前用户昵称"""
    if not nickname:
        return "⚠️ 需要用户昵称"
    data = _call(f"{BASE}/merge", {"merges": merges, "nickname": nickname})
    if data.get("error"):
        return f"合并失败: {data['detail']}"
    results = data.get("results", [])
    lines = ["✅ 合并完成："]
    for r in results:
        if r.get("errors"):
            lines.append(f"  ⚠️ {r['target']}: {' | '.join(r['errors'])}")
        else:
            lines.append(f"  ✅ {r['target']} ← {', '.join(r['moved'])}")
    return "\n".join(lines)
