"""推广海报 — 两个页面：共享页（邀请卡片） + 海报管理页（海报+工具）"""
import httpx
from fastapi import APIRouter, Query
from fastapi.responses import Response, HTMLResponse
from app.services.poster import generate_poster, generate_og_wide
import asyncpg
import time
import os

DATABASE_URL = "postgresql://lucky:lucky_pass@localhost:5432/weclawd"
BASE_URL = "https://ai.pangoozn.com"
OG_DIR = "/home/ubuntu/weclaw-1/app/static/og/"

router = APIRouter(prefix="/promo", tags=["推广海报"])


async def get_subscriber_by_code(code: str):
    """根据推广码查询推荐人的头像和昵称"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT subscriber_id FROM referral_codes WHERE code = $1", code
        )
        if not row:
            return None, None
        sub = await conn.fetchrow(
            "SELECT nickname, avatar_url FROM subscribers WHERE id = $1",
            row["subscriber_id"]
        )
        if not sub:
            return None, None
        return sub["nickname"] or "虾客", sub["avatar_url"]
    finally:
        await conn.close()


async def _generate_poster_png(code: str) -> bytes:
    """生成海报 PNG 字节"""
    nickname, avatar_url = await get_subscriber_by_code(code)
    avatar_bytes = None
    if avatar_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(avatar_url)
                if resp.status_code == 200:
                    avatar_bytes = resp.content
        except Exception:
            pass
    return generate_poster(code, nickname or "虾客", avatar_bytes)


async def _generate_og_png(code: str) -> bytes:
    """生成方形 OG 图"""
    nickname, avatar_url = await get_subscriber_by_code(code)
    avatar_bytes = None
    if avatar_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(avatar_url)
                if resp.status_code == 200:
                    avatar_bytes = resp.content
        except Exception:
            pass
    return generate_og_wide(code, nickname or "虾客", avatar_bytes)


# ===== 海报图片 API（共用）=====

@router.get("/referral/image", response_class=Response)
async def referral_poster_image(
    code: str = Query(..., description="推广码"),
    download: bool = Query(False, description="是否触发下载"),
):
    """返回原始 PNG 图片"""
    png_bytes = await _generate_poster_png(code)
    disp = "attachment" if download else "inline"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "public, max-age=3600",
            "Content-Disposition": f'{disp}; filename="referral-{code}.png"'
        }
    )


@router.get("/referral/og-image", response_class=Response)
async def referral_og_image(
    code: str = Query(..., description="推广码"),
):
    """返回缓存的 1200x630 宽幅 OG 图（文件缓存，微信爬虫秒开）"""
    os.makedirs(OG_DIR, exist_ok=True)
    cache_path = os.path.join(OG_DIR, f"og_{code}.png")
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 1000:
        from fastapi.responses import FileResponse
        return FileResponse(
            cache_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    png_bytes = await _generate_og_png(code)
    try:
        with open(cache_path, 'wb') as f:
            f.write(png_bytes)
    except Exception:
        pass
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ===== 页面1：共享页（被分享的人看到）=====

INVITE_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>享客虾-{nickname}邀您微信养个虾</title>
<meta name="description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单">
<meta property="og:title" content="享客虾-{nickname}邀您微信养个虾" />
<meta property="og:description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单" />
<meta property="og:image" content="{og_image_url}" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta property="og:url" content="{page_url}" />
<meta property="og:type" content="website" />
<meta name="wechat:card" content="summary_large_image" />
<meta property="twitter:card" content="summary_large_image" />
<meta property="twitter:image" content="{og_image_url}" />
<style>
* {{ margin:0;padding:0;box-sizing:border-box }}
body {{
    background:#f5f0e8;font-family:-apple-system,"Noto Serif CJK SC",serif;
    min-height:100vh;display:flex;flex-direction:column;align-items:center;
    padding:40px 16px
}}
.card {{
    max-width:400px;width:100%;background:#fffdf7;border-radius:20px;
    box-shadow:0 4px 24px rgba(0,0,0,0.08);padding:36px 28px 28px;
    text-align:center;border-top:6px solid #5B8C3E
}}
.brand {{ display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:20px }}
.brand .emoji {{ font-size:28px }}
.brand .name {{ font-size:22px;font-weight:800;color:#5B8C3E }}
.brand .slogan {{ font-size:13px;color:#8a7a64;margin-top:2px }}
.avatar-wrap {{ margin:8px 0 10px }}
.avatar-wrap img {{ width:72px;height:72px;border-radius:50%;border:3px solid #5B8C3E;object-fit:cover }}
.avatar-fallback {{ width:72px;height:72px;border-radius:50%;background:#5B8C3E;color:#fff;font-size:32px;line-height:72px;display:inline-block;border:3px solid #5B8C3E }}
.invite-name {{ font-size:22px;font-weight:700;color:#2C2C2C }}
.invite-sub {{ font-size:15px;color:#5B8C3E;margin:4px 0 16px }}
.feature-box {{ background:#f8f6f2;border-radius:14px;padding:16px;margin:16px 0 20px }}
.feature-box .main {{ font-size:18px;font-weight:700;color:#2C2C2C }}
.feature-box .tags {{ font-size:14px;color:#5B8C3E;margin:6px 0 }}
.feature-box .sub {{ font-size:13px;color:#C8781E }}
.btn-big {{
    display:block;width:100%;padding:18px;border:none;border-radius:14px;
    background:linear-gradient(135deg,#5B8C3E,#4a7a33);color:#fff;
    font-size:18px;font-weight:700;cursor:pointer;text-decoration:none;
    letter-spacing:1px;box-shadow:0 4px 16px rgba(91,140,62,0.25)
}}
.footer {{ margin-top:20px;font-size:11px;color:#bbb5a8 }}
</style>
</head>
<body>
<div class="card">
    <div class="brand">
        <span class="emoji">🦞</span>
        <div><div class="name">享客虾</div><div class="slogan">享客虾，虾客行</div></div>
    </div>
    <div class="avatar-wrap" id="avatarWrap"></div>
    <div class="invite-name">{nickname}</div>
    <div class="invite-sub">邀你一起养虾</div>
    <div class="feature-box">
        <div class="main">在微信里养只 AI 虾</div>
        <div class="tags">写歌 · 做贺卡 · 写笔记 · 看行情</div>
        <div class="sub">AI 创作 · 就是聊天这样简单</div>
    </div>
    <a class="btn-big" id="addBotBtn" href="#">🦞 添加享客虾 Bot</a>
    <div class="footer">享客虾 · 智享家</div>
</div>
<script>
var code = '{code}';
var avatarUrl = '{avatar_url}';
var aw = document.getElementById('avatarWrap');
if (avatarUrl) {{
    aw.innerHTML = '<img src="' + avatarUrl + '" alt="avatar">';
}} else {{
    aw.innerHTML = '<div class="avatar-fallback">🦞</div>';
}}
document.getElementById('addBotBtn').href = '/xkx/bind?ref=' + code;
</script>
</body>
</html>"""


@router.get("/referral", response_class=HTMLResponse)
async def referral_invite_page(
    code: str = Query(None, description="推广码（无则自动生成）"),
    openid: str = Query(None, description="微信 OAuth 回调传过来的 openid"),
    nickname: str = Query(None, description="OAuth 回调传过来的微信昵称"),
    avatar: str = Query(None, description="OAuth 回调传过来的微信头像"),
):
    """共享页 — 判断身份后分流：推广人→海报页，被推广者→绑定页"""
    from fastapi.responses import RedirectResponse, HTMLResponse

    # 无 code 的入口：自动寻码或 OAuth
    if not code:
        if not openid:
            # 无 openid → 走 OAuth
            return RedirectResponse(
                url=f"{BASE_URL}/api/auth/wechat-redirect?target=/promo/referral"
            )
        # 有 openid → 查已有推广码，没有则自动生成
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            sub = await conn.fetchrow(
                "SELECT id FROM subscribers WHERE openid = $1", openid
            )
            if sub:
                code_row = await conn.fetchrow(
                    "SELECT code FROM referral_codes WHERE subscriber_id = $1", sub["id"]
                )
                if code_row:
                    code = code_row["code"]
                else:
                    import string, random
                    for _ in range(10):
                        code = 'XK' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
                        exists = await conn.fetchrow(
                            "SELECT id FROM referral_codes WHERE code = $1", code
                        )
                        if not exists:
                            break
                    await conn.execute(
                        "INSERT INTO referral_codes (subscriber_id, code) VALUES ($1, $2)",
                        sub["id"], code
                    )
        finally:
            await conn.close()

        if not code:
            return HTMLResponse("无法生成推广码", status_code=500)

        # 跳回带 code 的完整 URL
        params = f"code={code}&openid={openid}"
        if nickname:
            params += f"&nickname={nickname}"
        if avatar:
            params += f"&avatar={avatar}"
        return RedirectResponse(url=f"{BASE_URL}/promo/referral?{params}")

    # 有 code 但无 openid → 走 OAuth（但先渲染 OG 标签，供朋友圈爬虫用）
    if not openid:
        nickname, avatar_url = await get_subscriber_by_code(code)
        nickname = nickname or "虾客"
        page_url = f"{BASE_URL}/promo/referral?code={code}"
        og_version = int(time.time()) // 3600  # hourly cache buster
        og_image_url = f"{BASE_URL}/promo/referral/og-image?code={code}&v={og_version}"
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>享客虾-{nickname}邀您微信养个虾</title>
<meta name="description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单">
<meta property="og:title" content="享客虾-{nickname}邀您微信养个虾" />
<meta property="og:description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单" />
<meta property="og:image" content="{og_image_url}" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta property="og:url" content="{page_url}" />
<meta property="og:type" content="website" />
<script>
if (/MicroMessenger/i.test(navigator.userAgent)) {{
    window.location.href = "{BASE_URL}/api/auth/wechat-redirect?target=/promo/referral?code={code}";
}} else {{
    window.location.href = "{BASE_URL}/xkx/bind?ref={code}";
}}
</script>
</head><body></body></html>"""
        return HTMLResponse(content=html)

    # 有 openid → 比对归属
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # OAuth 回调带了 nickname/avatar → 更新用户的昵称和头像
        if openid and nickname:
            nick_to_use = nickname
            await conn.execute(
                "UPDATE channel_bindings SET nickname = $1 WHERE openid = $2 AND (nickname IS NULL OR nickname = '' OR nickname LIKE '虾友%' OR nickname LIKE '用户%')",
                nick_to_use, openid
            )
            # 通过 channel_bindings 找到 iLink user_id，再更新 bot_accounts
            cb_rows = await conn.fetch(
                "SELECT channel_user_id FROM channel_bindings WHERE openid = $1 AND channel_user_id IS NOT NULL",
                openid
            )
            for cb_row in cb_rows:
                cuid = cb_row["channel_user_id"]
                if "@" in cuid:
                    cuid_base = cuid.split("@")[0]
                    await conn.execute("""
                        UPDATE bot_accounts SET nickname = $1
                        WHERE (user_id = $2 OR user_id LIKE $3) AND is_active = true
                    """, nick_to_use, cuid, cuid_base + "@%")
            # 更新 subscribers 的昵称和头像
            await conn.execute(
                "UPDATE subscribers SET nickname = $1 WHERE openid = $2 AND (nickname IS NULL OR nickname = '' OR nickname LIKE '虾友%')",
                nick_to_use, openid
            )
            if avatar:
                await conn.execute(
                    "UPDATE subscribers SET avatar_url = $1 WHERE openid = $2",
                    avatar, openid
                )

        owner_row = await conn.fetchrow(
            "SELECT subscriber_id FROM referral_codes WHERE code = $1", code
        )
        if owner_row:
            owner_sub = await conn.fetchrow(
                "SELECT openid FROM subscribers WHERE id = $1", owner_row["subscriber_id"]
            )
            if owner_sub and owner_sub["openid"] == openid:
                # 推广人自己 → 海报页
                return RedirectResponse(url=f"{BASE_URL}/promo/poster?code={code}", status_code=302)

        # 其他人 → 绑定页
        return RedirectResponse(url=f"{BASE_URL}/xkx/bind?ref={code}", status_code=302)
    finally:
        await conn.close()


@router.get("/referral/info")
async def referral_info(
    code: str = Query(None, description="推广码"),
    openid: str = Query(None, description="微信 openid"),
    avatar: str = Query(None, description="OAuth 传来的头像 URL"),
):
    """返回推荐人信息（昵称+头像+推广码）"""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        if code:
            row = await conn.fetchrow(
                "SELECT s.nickname, s.avatar_url FROM referral_codes rc "
                "JOIN subscribers s ON rc.subscriber_id = s.id "
                "WHERE rc.code = $1", code
            )
            if row:
                return {"nickname": row[0] or "虾客", "avatar_url": row[1] or "", "has_code": True}
            return {"nickname": "虾客", "avatar_url": "", "has_code": False}

        if openid:
            # 查用户是否有推广码，无则自动生成
            row = await conn.fetchrow(
                "SELECT rc.code, s.nickname, s.avatar_url, s.id FROM subscribers s "
                "LEFT JOIN referral_codes rc ON rc.subscriber_id = s.id "
                "WHERE s.openid = $1", openid
            )
            if row:
                sid = row[3]
                code = row[0]
                nickname = row[1] or ""
                avatar_url = row[2] or ""
                # 昵称是默认值（虾客开头）→ 从微信拉取真实昵称
                if not nickname or nickname.startswith("虾客"):
                    try:
                        # 从 channel_bindings 获取真实昵称
                        cb = await conn.fetchrow(
                            "SELECT nickname, openid FROM channel_bindings WHERE openid = $1 AND nickname IS NOT NULL AND nickname != '' LIMIT 1",
                            openid
                        )
                        if cb and cb[0]:
                            nickname = cb[0]
                            await conn.execute(
                                "UPDATE subscribers SET nickname = $1 WHERE id = $2",
                                nickname, sid
                            )
                    except Exception:
                        pass
                # OAuth 传来的头像 → 存入 subscribers
                if avatar and not avatar_url:
                    try:
                        avatar_url = avatar
                        await conn.execute(
                            "UPDATE subscribers SET avatar_url = $1 WHERE id = $2",
                            avatar_url, sid
                        )
                    except Exception:
                        pass
                if not code:
                    # 自动生成推广码
                    import secrets, string
                    alphabet = string.ascii_uppercase + string.digits
                    code = ''.join(secrets.choice(alphabet) for _ in range(6))
                    await conn.execute(
                        "INSERT INTO referral_codes (code, subscriber_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                        code, sid
                    )
                # 查推广战绩
                cnt_row = await conn.fetchrow(
                    "SELECT COUNT(*) FROM referral_relations WHERE referrer_subscriber_id = $1", sid
                )
                cnt2_row = await conn.fetchrow(
                    "SELECT COUNT(*) FROM referral_relations rr "
                    "JOIN referral_relations r2 ON rr.referee_subscriber_id = r2.referrer_subscriber_id "
                    "WHERE rr.referrer_subscriber_id = $1", sid
                )
                referral_count = cnt_row[0] if cnt_row else 0
                level2_count = cnt2_row[0] if cnt2_row else 0
                return {
                    "nickname": nickname or "虾客",
                    "avatar_url": avatar_url or "",
                    "code": code,
                    "has_code": True,
                    "referral_count": referral_count,
                    "level2_count": level2_count,
                }
            return {"nickname": "", "avatar_url": "", "code": "", "has_code": False, "found": False}
    finally:
        await conn.close()


# ===== 页面2：海报管理页（推广人自己看）=====

POSTER_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>享客虾-{nickname}邀您微信养个虾</title>
<meta name="description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单">
<meta property="og:title" content="享客虾-{nickname}邀您微信养个虾" />
<meta property="og:description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单" />
<meta property="og:image" content="{og_image_url}" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta property="og:url" content="{page_url}" />
<meta property="og:type" content="website" />
<meta name="wechat:card" content="summary_large_image" />
<meta property="twitter:card" content="summary_large_image" />
<meta property="twitter:image" content="{og_image_url}" />
<style>
* {{ margin:0;padding:0;box-sizing:border-box }}
body {{
    background:#f5f0e8;font-family:-apple-system,"Noto Serif CJK SC",serif;
    min-height:100vh;display:flex;flex-direction:column;align-items:center;
    padding:20px 12px 40px
}}
.poster-wrapper {{ max-width:420px;width:100%;margin:0 auto;cursor:pointer }}
.poster-wrapper img {{ width:100%;height:auto;border-radius:16px;box-shadow:0 4px 24px rgba(0,0,0,0.10);display:block }}
.btn-area {{ max-width:420px;width:100%;margin:20px auto 0;display:flex;gap:10px }}
.btn {{ flex:1;padding:14px 0;border:none;border-radius:12px;font-size:16px;font-weight:600;cursor:pointer;text-align:center;text-decoration:none;display:block }}
.btn:active {{ opacity:0.7 }}
.btn-primary {{ background:#5B8C3E;color:#fff }}
.btn-ghost {{ background:#fff;color:#5B8C3E;border:1.5px solid #5B8C3E }}
.code-display {{ max-width:420px;width:100%;margin:16px auto 0;text-align:center;color:#8a7a64;font-size:13px;line-height:1.6 }}
.code-display strong {{ color:#5B8C3E;font-size:15px }}
.hint {{ max-width:420px;width:100%;margin:12px auto 0;text-align:center;color:#bbb5a8;font-size:12px }}
.overlay {{
    display:none;position:fixed;top:0;left:0;right:0;bottom:0;
    background:rgba(0,0,0,0.92);z-index:9999;
    justify-content:center;align-items:center;flex-direction:column
}}
.overlay.show {{ display:flex }}
.overlay img {{ max-width:90vw;max-height:80vh;border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,0.3) }}
.overlay .save-hint {{ color:#fff;font-size:15px;margin-top:20px;opacity:0.8 }}
.overlay .close-overlay {{
    position:absolute;top:16px;right:16px;width:40px;height:40px;
    background:rgba(255,255,255,0.15);border:none;border-radius:50%;
    color:#fff;font-size:22px;cursor:pointer;line-height:40px;text-align:center
}}
</style>
</head>
<body>
<div class="poster-wrapper" onclick="showOverlay()">
    <img id="posterImg" src="" alt="推广海报">
</div>
<div class="btn-area">
    <a class="btn btn-primary" id="saveBtn" href="javascript:void(0)" onclick="savePoster()">📥 保存海报</a>
    <button class="btn btn-ghost" id="statsBtn" onclick="goStats()">📊 推广战绩</button>
    <button class="btn btn-ghost" id="copyLinkBtn" onclick="copyLink()">🔗 复制链接</button>
</div>
<div class="code-display">
    推广码 <strong>{code}</strong><br>
    长按上方图片识别二维码 · 免费开养
</div>
<div class="talk-box" id="talkBox">
    <div class="talk-label">📋 分享话术（点击复制）</div>
    <div class="talk-text" id="talkText">最近在玩这个享客虾，写歌做贺卡什么的，就是聊天这么简单 👉 {page_url}</div>
</div>
<div class="hint">享客虾 · 智享家</div>
<style>
.talk-box {{ max-width:420px;width:100%;margin:12px auto 0;background:#fffdf7;border-radius:12px;padding:14px 16px;cursor:pointer;border:1px solid #e5dfd4;text-align:left }}
.talk-label {{ font-size:12px;color:#8a7a64;margin-bottom:6px }}
.talk-text {{ font-size:14px;color:#333;line-height:1.7 }}
.talk-box:active {{ opacity:0.7 }}
</style>

<!-- 保存海报弹窗 -->
<div class="overlay" id="posterOverlay" onclick="closeOverlay()">
    <button class="close-overlay" onclick="event.stopPropagation();closeOverlay()">✕</button>
    <img src="" alt="推广海报" onclick="event.stopPropagation()">
    <div class="save-hint">👆 长按图片保存到相册</div>
</div>

<script>
var fullscreenUrl = '{fullscreen_url}';
var pageUrl = '{page_url}';
function showOverlay() {{
    var ov = document.getElementById('posterOverlay');
    ov.querySelector('img').src = fullscreenUrl + '&t=' + Date.now();
    ov.classList.add('show');
}}
function closeOverlay() {{
    document.getElementById('posterOverlay').classList.remove('show');
}}
function savePoster() {{
    showOverlay();
}}
function loadPoster() {{
    document.getElementById('posterImg').src = fullscreenUrl + '&t=' + Date.now();
}}
function copyLink() {{
    copyTalk();
}}
function goStats() {{
    window.location.href = '/promo';
}}
function copyTalk() {{
    var talkText = document.getElementById('talkText').textContent;
    if (navigator.clipboard) {{
        navigator.clipboard.writeText(talkText).then(function() {{
            document.getElementById('copyLinkBtn').textContent = '✅ 已复制';
            setTimeout(function() {{ document.getElementById('copyLinkBtn').textContent = '🔗 复制链接'; }}, 2000);
        }});
    }} else {{
        var ta = document.createElement('textarea'); ta.value = talkText;
        document.body.appendChild(ta); ta.select(); document.execCommand('copy');
        document.body.removeChild(ta);
        document.getElementById('copyLinkBtn').textContent = '✅ 已复制';
        setTimeout(function() {{ document.getElementById('copyLinkBtn').textContent = '🔗 复制链接'; }}, 2000);
    }}
}}
// 点击话术框也复制
document.addEventListener('DOMContentLoaded', function() {{
    var tb = document.getElementById('talkBox');
    if (tb) {{
        tb.addEventListener('click', copyTalk);
    }}
    // 清除 URL 中的 OAuth 参数，避免分享出去带 openid
    var cleanUrl = '/promo/poster?code=' + '{code}';
    if (window.location.href.indexOf('openid=') !== -1) {{
        history.replaceState(null, '', cleanUrl);
    }}
}});
loadPoster();
</script>
</body>
</html>"""


@router.get("/poster", response_class=HTMLResponse)
async def referral_poster_page(
    code: str = Query(..., description="推广码"),
    openid: str = Query(None, description="微信 OAuth 回调传过来的 openid"),
):
    """海报管理页 — 推广人本人查看/保存海报，被分享者跳绑定页"""
    from fastapi.responses import RedirectResponse, HTMLResponse

    # 无 openid → 走 OAuth（但先渲染 OG 标签，供朋友圈爬虫用）
    if not openid:
        nickname, avatar_url = await get_subscriber_by_code(code)
        nickname = nickname or "虾客"
        page_url = f"{BASE_URL}/promo/referral?code={code}"
        og_version = int(time.time()) // 3600  # hourly cache buster
        og_image_url = f"{BASE_URL}/promo/referral/og-image?code={code}&v={og_version}"
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>享客虾-{nickname}邀您微信养个虾</title>
<meta name="description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单">
<meta property="og:title" content="享客虾-{nickname}邀您微信养个虾" />
<meta property="og:description" content="写歌、做贺卡、写笔记、看行情，就是聊天这么简单" />
<meta property="og:image" content="{og_image_url}" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta property="og:url" content="{page_url}" />
<meta property="og:type" content="website" />
<script>
if (/MicroMessenger/i.test(navigator.userAgent)) {{
    window.location.href = "{BASE_URL}/api/auth/wechat-redirect?target=/promo/poster?code={code}";
}} else {{
    window.location.href = "{BASE_URL}/xkx/bind?ref={code}";
}}
</script>
</head><body></body></html>"""
        return HTMLResponse(content=html)

    # 有 openid → 比对归属
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        owner_row = await conn.fetchrow(
            "SELECT subscriber_id FROM referral_codes WHERE code = $1", code
        )
        is_owner = False
        if owner_row:
            owner_sub = await conn.fetchrow(
                "SELECT openid FROM subscribers WHERE id = $1", owner_row["subscriber_id"]
            )
            if owner_sub and owner_sub["openid"] == openid:
                is_owner = True

        if not is_owner:
            # 被分享者 → 绑定页
            return RedirectResponse(url=f"{BASE_URL}/xkx/bind?ref={code}", status_code=302)

        # 推广人本人 → 显示海报页
        nickname, avatar_url = await get_subscriber_by_code(code)
        nickname = nickname or "虾客"
        page_url = f"{BASE_URL}/promo/referral?code={code}"
        og_version = int(time.time()) // 3600  # hourly cache buster
        og_image_url = f"{BASE_URL}/promo/referral/og-image?code={code}&v={og_version}"
        fullscreen_url = f"{BASE_URL}/promo/referral/image?code={code}"

        html = POSTER_PAGE.format(
            code=code,
            nickname=nickname,
            og_image_url=og_image_url,
            page_url=page_url,
            fullscreen_url=fullscreen_url,
        )
        return HTMLResponse(content=html)
    finally:
        await conn.close()
