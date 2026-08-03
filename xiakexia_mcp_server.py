#!/home/ubuntu/.hermes/hermes-agent/venv/bin/python3
"""
享客虾 受限文件 MCP 服务器
只允许访问 /home/ubuntu/weclaw-keepalive/downloads/{user_id}/
"""
import os
import sys
import json
import urllib.request
import urllib.parse
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
        return "目录不存在"
    files = []
    for item in safe.iterdir():
        kind = "📁" if item.is_dir() else "📄"
        files.append(f"  {kind} {item.name}")
    return "\n".join(files) if files else "目录为空"


@mcp.tool()
def xiakexia_read(user_id: str, path: str) -> str:
    """读取用户文件内容。user_id=用户OpenID(含@im.wechat后缀), path=文件路径"""
    safe = _safe_resolve(user_id, path)
    if not safe:
        return "错误: 路径不在允许范围内"
    if not safe.exists():
        return "文件不存在"
    if not safe.is_file():
        return "路径不是文件"
    try:
        content = safe.read_text(encoding="utf-8")
        if len(content) > 3000:
            content = content[:3000] + "\n\n...(截断)"
        return content
    except Exception as e:
        return f"读取失败: {e}"


@mcp.tool()
def xiakexia_info(user_id: str, path: str = "") -> str:
    """查询用户目录空间使用情况"""
    safe = _safe_resolve(user_id, path)
    if not safe:
        return "错误: 路径不在允许范围内"
    total = 0
    count = 0
    for item in safe.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
            count += 1
    return f"文件数: {count}, 总大小: {total / 1024:.1f}KB"


@mcp.tool()
def xiakexia_write(user_id: str, path: str, content: str) -> str:
    """写入用户文件。user_id=用户OpenID(含@im.wechat后缀), path=文件路径, content=文件内容"""
    safe = _safe_resolve(user_id, path)
    if not safe:
        return "错误: 路径不在允许范围内"
    try:
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")
        return f"✅ 已写入 {safe.name} ({len(content)} 字符)"
    except Exception as e:
        return f"写入失败: {e}"


# ══════════════════════════════════════════════════════
# API 调用辅助
# ══════════════════════════════════════════════════════

def _api_call(method: str, path: str, data: dict = None) -> dict:
    """调用本地 API"""
    import urllib.request
    base = "http://127.0.0.1:8001"
    url = f"{base}{path}"
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════
# 微侠令 MCP 工具
# ══════════════════════════════════════════════════════

@mcp.tool()
def order_list(user_id: str) -> str:
    """查询用户的微侠令列表。user_id=用户OpenID"""
    result = _api_call("GET", f"/api/orders?user_id={user_id}")
    if "error" in result:
        return f"查询失败: {result['error']}"
    orders = result.get("orders", result.get("data", []))
    if not orders:
        return "暂无微侠令"
    lines = ["📋 微侠令列表："]
    for o in orders:
        oid = o.get("id", o.get("order_id", "?"))
        title = o.get("title", o.get("description", "?"))
        status = o.get("status", "?")
        lines.append(f"  #{str(oid)[:8]} [{status}] {title}")
    return "\n".join(lines)


@mcp.tool()
def order_create(title: str, user_id: str, description: str = "") -> str:
    """创建微侠令。title=令名(如"微侠桌面版"), user_id=用户OpenID, description=详细描述"""
    result = _api_call("POST", "/api/orders", {
        "title": title, "user_id": user_id, "description": description
    })
    if "error" in result:
        return f"创建失败: {result['error']}"
    return f"✅ 微侠令已创建: #{title}"


@mcp.tool()
def order_complete(order_id: str, result_summary: str) -> str:
    """完成任务令。order_id=令ID(或短ID), result_summary=完成摘要"""
    result = _api_call("POST", f"/api/orders/{order_id}/complete", {
        "result": result_summary
    })
    if "error" in result:
        return f"完成失败: {result['error']}"
    return f"✅ 微侠令 #{order_id} 已完成"


@mcp.tool()
def order_find(keyword: str, user_id: str = "") -> str:
    """搜索微侠令。keyword=关键词, user_id=用户OpenID(可选，不传则搜索全部)"""
    params = f"?keyword={keyword}"
    if user_id:
        params += f"&user_id={user_id}"
    result = _api_call("GET", f"/api/orders/search{params}")
    if "error" in result:
        return f"搜索失败: {result['error']}"
    orders = result.get("orders", result.get("data", []))
    if not orders:
        return "未找到匹配的微侠令"
    lines = [f"找到 {len(orders)} 个微侠令："]
    for o in orders:
        lines.append(f"  #{str(o.get('id',''))[:8]} [{o.get('status','?')}] {o.get('title','?')}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# 项目 MCP 工具
# ══════════════════════════════════════════════════════

@mcp.tool()
def project_list() -> str:
    """列出所有共享项目"""
    import urllib.request
    try:
        req = urllib.request.Request("https://hai.pangoozn.com/api/projects/list?nickname=%E9%93%AD%E9%81%93")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        projects = data.get("projects", [])
        if not projects:
            return "暂无项目"
        lines = ["📁 共享项目："]
        for p in projects:
            name = p.get("name", p.get("project", "?"))
            desc = p.get("description", "")
            if desc:
                lines.append(f"  📂 {name} — {desc[:40]}")
            else:
                lines.append(f"  📂 {name}")
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@mcp.tool()
def project_enter(project: str) -> str:
    """进入项目并设为活跃项目。project=项目名"""
    import urllib.request
    url = "https://hai.pangoozn.com/api/projects/enter?project=" + urllib.parse.quote(project) + "&nickname=%E9%93%AD%E9%81%93"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        msg = data.get("message", data.get("msg", "ok"))
        return f"📂 已进入项目: {project}"
    except Exception as e:
        return f"进入失败: {e}"


@mcp.tool()
def project_files(project: str) -> str:
    """列出项目文件。project=项目名"""
    import urllib.request
    url = "https://hai.pangoozn.com/api/projects/files?project=" + urllib.parse.quote(project) + "&nickname=%E9%93%AD%E9%81%93"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        files = data.get("files", [])
        if not files:
            return "项目为空"
        lines = [f"📁 {project}："]
        for f in files:
            fname = f.get("name", f.get("file", "?"))
            fsize = f.get("size", f.get("size", ""))
            lines.append(f"  📄 {fname}" + (f" ({fsize})" if fsize else ""))
        return "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@mcp.tool()
def project_read(project: str, file: str) -> str:
    """读取项目文件内容。project=项目名, file=文件名"""
    import urllib.request
    url = "https://hai.pangoozn.com/api/projects/read?project=" + urllib.parse.quote(project) + "&file=" + urllib.parse.quote(file) + "&nickname=%E9%93%AD%E9%81%93"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        content = data.get("content", "")
        if len(content) > 3000:
            content = content[:3000] + "\n\n...(截断)"
        return f"📄 {project}/{file}\n{'='*40}\n{content}"
    except Exception as e:
        return f"读取失败: {e}"


@mcp.tool()
def project_search(keyword: str) -> str:
    """搜索项目中的文件。keyword=关键词"""
    import urllib.request
    url = "https://hai.pangoozn.com/api/projects/search?keyword=" + urllib.parse.quote(keyword) + "&nickname=%E9%93%AD%E9%81%93"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = data.get("results", data.get("matches", []))
        if not results:
            return "未找到匹配文件"
        lines = [f"🔍 找到 {len(results)} 个文件："]
        for r in results:
            proj = r.get("project", "?")
            fname = r.get("file", r.get("name", "?"))
            lines.append(f"  📄 {proj}/{fname}")
        return "\n".join(lines)
    except Exception as e:
        return f"搜索失败: {e}"


@mcp.tool()
def project_lookup(keyword: str) -> str:
    """在项目中搜索关键词并直接读取最匹配的文件内容。
    适合用户说"看看XX文件"的场景。keyword=文件名或关键词"""
    import urllib.request
    try:
        # 搜索
        url = "https://hai.pangoozn.com/api/projects/search?keyword=" + urllib.parse.quote(keyword) + "&nickname=%E9%93%AD%E9%81%93"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        results = data.get("results", data.get("matches", []))
        if not results:
            return "未找到匹配文件"
        
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


# ══════════════════════════════════════════════════════
# 嗨卡 MCP 工具
# ══════════════════════════════════════════════════════

@mcp.tool()
def haica_create(openid: str, blessing: str = "", sender_name: str = "", recipient_name: str = "", image_url: str = "") -> str:
    """
    创建一张AI嗨卡（AI生日/节日贺卡）。
    openid=用户微信OpenID, blessing=祝福语(可选), sender_name=发送者昵称, recipient_name=接收者昵称, image_url=图片URL(可选)
    """
    result = _api_call("POST", "/api/haica/create", {
        "openid": openid,
        "blessing": blessing,
        "sender_name": sender_name or "虾友",
        "recipient_name": recipient_name or "朋友",
        "image_url": image_url,
    })
    if "error" in result:
        return f"嗨卡创建失败: {result['error']}"
    card_url = result.get("url", result.get("card_url", ""))
    card_id = result.get("card_id", "")
    if card_url:
        return f"✅ 嗨卡已创建！\n🔗 {card_url}"
    return f"✅ 嗨卡已创建 (ID: {card_id})"


# ══════════════════════════════════════════════════════
# MV 生成 MCP 工具
# ══════════════════════════════════════════════════════

MAIN_STATION = "https://hai.pangoozn.com"

def _main_api(method: str, path: str, data: dict = None) -> dict:
    """调用主站 API"""
    url = MAIN_STATION + path
    body = None
    headers = {}
    if data:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def song_list(keyword: str = "", limit: int = 10) -> str:
    """搜索享客虾歌曲库。keyword=歌曲名/关键词(可选，不传则返回最新歌曲), limit=返回数量(默认10)"""
    path = f"/api/music/list?limit={limit}"
    if keyword:
        path += f"&search={urllib.parse.quote(keyword)}"
    result = _main_api("GET", path)
    if "error" in result:
        return f"查询失败: {result['error']}"
    tracks = result.get("data", result.get("tracks", []))
    if not tracks:
        return "没有找到歌曲"
    lines = ["🎵 歌曲列表："]
    for t in tracks:
        name = t.get("name", "?")
        tid = t.get("id", "?")[:12]
        dur = t.get("duration_sec", 0)
        lines.append(f"  🆔 {tid} | {name} ({int(dur)}s)")
    lines.append("")
    lines.append("💡 用 song_to_mv(track_id='歌曲ID') 生成MV")
    return "\n".join(lines)


@mcp.tool()
def song_to_mv(track_id: str, photo_urls: str = "") -> str:
    """从歌曲生成MV视频。track_id=歌曲ID, photo_urls=可选用户照片URL(逗号分隔)"""
    data = {"track_id": track_id}
    if photo_urls:
        urls = [u.strip() for u in photo_urls.split(",") if u.strip()]
        data["photo_urls"] = urls
    result = _main_api("POST", "/api/mv/generate-json", data)
    if "error" in result:
        return f"MV生成失败: {result['error']}"
    player_url = result.get("player_url", "")
    video_url = result.get("video_url", "")
    mv_id = result.get("mv_id", "")
    dur = result.get("duration", 0)
    lines = [f"✅ MV生成成功！({int(dur)}s, {result.get('segments', 0)}个场景)"]
    if player_url:
        lines.append(f"🎬 播放器: {player_url}")
    if video_url:
        lines.append(f"📹 视频直链: {video_url}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════

if __name__ == "__main__":
    mcp.run(transport="stdio")
