#!/home/ubuntu/.hermes/hermes-agent/venv/bin/python3
"""
享客虾 受限文件 MCP 服务器
只允许访问 /home/ubuntu/weclaw-keepalive/downloads/{user_id}/
"""
import os
import sys
import json
from pathlib import Path

from mcp.server import FastMCP

mcp = FastMCP("xiakexia-fs")

ALLOWED_BASE = Path("/home/ubuntu/weclaw-keepalive/downloads")
CMD_BASE = Path("/home/ubuntu/weclaw-keepalive")
TMP = Path("/tmp")


def _safe_resolve(user_id: str, rel_path: str) -> Path | None:
    """Verify and resolve a path under the user's directory.
    Returns the resolved Path or None if not allowed."""
    uid = user_id.split("@")[0] if user_id else "unknown"
    user_dir = ALLOWED_BASE / uid
    
    # Resolve relative to user directory
    target = (user_dir / rel_path).resolve()
    
    try:
        target.relative_to(user_dir)
        return target
    except ValueError:
        pass
    
    # Also allow /tmp for temp files
    try:
        target.relative_to(TMP)
        return target
    except ValueError:
        pass
    
    return None


@mcp.tool()
def xiakexia_list(user_id: str, path: str = "") -> str:
    """列出用户的文件目录。user_id=用户OpenID(含@im.wechat后缀), path=子目录路径(可选)"""
    safe = _safe_resolve(user_id, path)
    if not safe:
        return "错误: 路径不在允许范围内"
    if not safe.exists():
        return f"路径不存在: {safe}"
    if safe.is_file():
        sz = safe.stat().st_size
        return f"{safe.name} ({sz//1024}KB)"
    
    lines = []
    for entry in sorted(safe.iterdir()):
        sz = ""
        if entry.is_file():
            sz = f" ({entry.stat().st_size//1024}KB)"
        elif entry.is_dir():
            sz = " (dir)"
        lines.append(f"  {entry.name}{sz}")
    
    if not lines:
        return "（空目录）"
    return "\n".join(lines)


@mcp.tool()
def xiakexia_read(user_id: str, path: str) -> str:
    """读取用户目录下的文件内容。user_id=用户OpenID(含@im.wechat后缀), path=文件路径"""
    safe = _safe_resolve(user_id, path)
    if not safe:
        return "错误: 路径不在允许范围内"
    if not safe.exists() or not safe.is_file():
        return f"文件不存在: {safe}"
    
    max_size = 512 * 1024  # 512KB max
    if safe.stat().st_size > max_size:
        return f"文件过大 ({safe.stat().st_size//1024}KB)，仅读取前512KB"
    
    try:
        text = safe.read_text(encoding="utf-8", errors="replace")
        if len(text) > 10000:
            text = text[:10000] + f"\n... (截断, 共{len(text)}字符)"
        return text
    except Exception as e:
        return f"读取失败: {e}"


@mcp.tool()
def xiakexia_info(user_id: str, path: str = "") -> str:
    """获取文件/目录信息。user_id=用户OpenID(含@im.wechat后缀), path=路径(可选)"""
    safe = _safe_resolve(user_id, path)
    if not safe:
        return "错误: 路径不在允许范围内"
    if not safe.exists():
        return f"路径不存在: {safe}"
    
    uid = user_id.split("@")[0] if user_id else "unknown"
    user_dir = ALLOWED_BASE / uid
    quota = 2 * 1024 * 1024 * 1024  # 2GB
    
    used = 0
    if user_dir.exists():
        for f in user_dir.rglob("*"):
            if f.is_file():
                used += f.stat().st_size
    
    return json.dumps({
        "name": safe.name,
        "type": "dir" if safe.is_dir() else "file",
        "size": safe.stat().st_size if safe.is_file() else 0,
        "size_kb": safe.stat().st_size // 1024 if safe.is_file() else 0,
        "user_dir": str(user_dir),
        "disk_used_mb": used // (1024*1024),
        "disk_quota_mb": quota // (1024*1024),
        "disk_pct": round(used / quota * 100, 1) if quota > 0 else 0,
    }, ensure_ascii=False)


@mcp.tool()
def xiakexia_write(user_id: str, path: str, content: str) -> str:
    """创建或覆盖用户目录下的文件。user_id=用户OpenID(含@im.wechat后缀), path=文件路径(如 project/notes.md), content=文件内容"""
    safe = _safe_resolve(user_id, path)
    if not safe:
        return "错误: 路径不在允许范围内"
    
    try:
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")
        return json.dumps({
            "ok": True,
            "path": str(safe),
            "size": len(content),
            "name": safe.name,
        }, ensure_ascii=False)
    except Exception as e:
        return f"写入失败: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")

# ══════════════════════════════════════════════
# 微侠令工具 — 调用 weclawd :8001/api/orders"
# ══════════════════════════════════════════════
import urllib.request
import urllib.error

ORDER_API = "http://127.0.0.1:8001/api/orders"

def _api_call(method: str, path: str, data: dict = None) -> dict:
    url = ORDER_API + path
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"code": -1, "error": f"HTTP {e.code}: {e.read().decode()}"}
    except Exception as e:
        return {"code": -1, "error": str(e)}

@mcp.tool()
def order_list(user_id: str) -> str:
    """查微侠令：列出用户的所有待办/进行中的令。user_id=用户QQ号"""
    result = _api_call("GET", f"?user_id={user_id}&channel=qqbot")
    items = result.get("data", {}).get("items", [])
    if not items:
        return "暂无待办的令"
    lines = ["📋 微侠令列表："]
    for o in items:
        lines.append(f"  [{o['status']}] {o['title']} (ID: {o['id'][:8]})")
    return "\n".join(lines)

@mcp.tool()
def order_create(title: str, user_id: str, description: str = "") -> str:
    """创建微侠令。title=令的标题, user_id=用户QQ号, description=详细描述(可选)"""
    result = _api_call("POST", "", {
        "title": title,
        "user_id": user_id,
        "channel": "qqbot",
        "description": description
    })
    if result.get("code") == 0:
        oid = result["data"]["id"][:8]
        return f"✅ 微侠令已创建：{title} (ID: {oid})"
    return f"❌ 创建失败：{result.get('error', '未知错误')}"

@mcp.tool()
def order_complete(order_id: str, result_summary: str) -> str:
    """完成微侠令。order_id=令ID(前8位即可), result_summary=执行结果"""
    # 先查完整ID
    resp = _api_call("GET", f"/{order_id}")
    oid = resp.get("data", {}).get("id", order_id) if resp.get("code") == 0 else order_id
    
    result = _api_call("PATCH", f"/{oid}/complete", {"result": result_summary})
    if result.get("code") == 0:
        return f"✅ 微侠令已完成：{result_summary[:100]}"
    return f"❌ 完成失败：{result.get('error', '未知错误')}"

@mcp.tool()
def order_find(keyword: str, user_id: str = "") -> str:
    """按任务名查微侠令。keyword=任务名关键词(如"微侠桌面版"), user_id=用户QQ号(可选)"""
    # 跨实例协同：查所有令，不按 user_id 过滤
    result = _api_call("GET", "/all")
    items = result.get("data", {}).get("items", [])
    
    # 匹配标题
    matched = [o for o in items if keyword.lower() in o.get("title", "").lower()]
    
    if not matched:
        # 也搜描述
        matched = [o for o in items if keyword.lower() in o.get("content", "") or o.get("description", "").lower()]
    
    if not matched:
        return f"未找到包含「{keyword}」的微侠令"
    
    lines = [f"📋 匹配「{keyword}」的微侠令："]
    for o in matched:
        desc = o.get("content", "") or o.get("description", "")[:80]
        lines.append(f"\n  [{o['status']}] {o['title']}")
        if desc:
            lines.append(f"     {desc}")
        lines.append(f"     ID: {o['id'][:8]}")
    return "\n".join(lines)


@mcp.tool()
def project_list() -> str:
    """列出所有共享项目"""
    try:
        import urllib.request
        req = urllib.request.Request("https://hai.pangoozn.com/api/projects/list?nickname=%E9%93%AD%E9%81%93")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        projects = data.get("projects", [])
        if not projects:
            return "暂无项目"
        lines = ["\U0001f4cb 共享项目列表："]
        for p in projects:
            lines.append("  \u2022 " + p)
        return "\n".join(lines)
    except Exception as e:
        return "查询失败: " + str(e)

@mcp.tool()
def project_enter(project: str) -> str:
    """进入一个共享项目，查看项目详情。project=项目名（如'春申君列传项目'）"""
    try:
        import urllib.request
        body = json.dumps({"project": project, "nickname": "铭道"}).encode()
        req = urllib.request.Request(
            "https://hai.pangoozn.com/api/projects/enter",
            data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return "进入项目失败: " + str(e)

@mcp.tool()
def project_files(project: str) -> str:
    """查看共享项目中的文件列表。project=项目名"""
    try:
        import urllib.request, urllib.parse
        url = "https://hai.pangoozn.com/api/projects/files?project=" + urllib.parse.quote(project) + "&nickname=%E9%93%AD%E9%81%93"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return "查询项目文件失败: " + str(e)

@mcp.tool()
def project_read(project: str, file: str) -> str:
    """读取共享项目中的文件内容。project=项目名, file=文件名"""
    try:
        import urllib.request, urllib.parse
        url = "https://hai.pangoozn.com/api/projects/read?project=" + urllib.parse.quote(project) + "&file=" + urllib.parse.quote(file) + "&nickname=%E9%93%AD%E9%81%93"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        content = data.get("content", "")
        if content:
            if len(content) > 2000:
                content = content[:2000] + "\n\n...(截断，文件较大)"
            matched = data.get("matched_file", "")
            if matched:
                return f"[匹配到: {matched}]\n{'='*40}\n{content}"
            return content
        hint = data.get("hint", "文件不存在")
        return hint
    except Exception as e:
        return "读取文件失败: " + str(e)


@mcp.tool()
def project_search(keyword: str) -> str:
    """在所有共享项目中搜索文件。keyword=搜索关键词"""
    try:
        import urllib.request
        body = json.dumps({"keyword": keyword, "nickname": "铭道"}).encode()
        req = urllib.request.Request(
            "https://hai.pangoozn.com/api/projects/search",
            data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return json.dumps(data, ensure_ascii=False, indent=2)
    except Exception as e:
        return "搜索失败: " + str(e)

@mcp.tool()
def project_lookup(keyword: str) -> str:
    """查找并读取项目文件。当用户说"看看XXX"或"查找XXX"时使用此工具。
    自动搜索所有项目中的文件，找到匹配的返回内容。keyword=文件名关键词（如"邹远志"或"投资概要"）"""
    try:
        import urllib.request, urllib.parse
        # 第一步：搜索匹配文件
        body = json.dumps({"keyword": keyword, "nickname": "铭道"}).encode()
        req = urllib.request.Request(
            "https://hai.pangoozn.com/api/projects/search",
            data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=15) as resp:
            search_data = json.loads(resp.read())
        
        results = search_data.get("results", [])
        if not results:
            return f"未找到包含「{keyword}」的文件。请确认文件名或发送文件过来归档。"
        
        # 有多个匹配时，列出所有匹配
        if len(results) > 1:
            lines = [f"找到 {len(results)} 个匹配文件："]
            for r in results:
                lines.append(f"  📄 {r['project']}/{r['file']}")
            lines.append("\n请输入精确文件名查看内容")
            return "\n".join(lines)
        
        # 只有一个匹配，直接读取内容
        match = results[0]
        proj = match["project"]
        fname = match["file"]
        
        read_url = "https://hai.pangoozn.com/api/projects/read?project=" + urllib.parse.quote(proj) + "&file=" + urllib.parse.quote(fname) + "&nickname=%E9%93%AD%E9%81%93"
        req2 = urllib.request.Request(read_url)
        with urllib.request.urlopen(req2, timeout=15) as resp:
            read_data = json.loads(resp.read())
        
        content = read_data.get("content", "")
        if len(content) > 3000:
            content = content[:3000] + "\n\n...(截断，完整文件请用精确文件名读取)"
        
        return f"📄 {proj}/{fname}\n{'='*40}\n{content}"
    except Exception as e:
        return f"查找失败: {str(e)}"
