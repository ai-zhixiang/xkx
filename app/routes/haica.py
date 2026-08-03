"""嗨卡 MCP - 一键生成分享卡片（翻卡版）"""
import os, json, uuid, shutil, re
from datetime import datetime
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
import asyncpg

DATABASE_URL = "postgresql://lucky:lucky_pass@localhost:5432/weclawd"
HAICA_DIR = "/home/ubuntu/weclaw-keepalive/static/haica"
os.makedirs(HAICA_DIR, exist_ok=True)

router = APIRouter(prefix="/api/haica")

class CreateReq(BaseModel):
    openid: str
    image_url: str = ""       # 已可公开访问的图片URL
    image_path: str = ""      # 本地图片路径（被复制到web目录）
    nickname: str = "虾友"
    blessing: str = ""        # 祝福语+诗文（bot已生成时传此字段，API不再重复生成）
    sender_name: str = ""     # 发送者（展示用）
    recipient_name: str = ""  # 接收者（展示用）

def _detect_mime(path: str) -> str:
    with open(path, "rb") as f:
        h = f.read(8)
    if h.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if h.startswith(b"\x89PNG"):
        return "image/png"
    if h.startswith(b"GIF8"):
        return "image/gif"
    if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"

@router.post("/create")
async def haica_create(req: CreateReq):
    """创建一张嗨卡（翻卡版），返回可分享链接"""
    # ── 虾点扣费：做嗨卡 2 虾点（生成前预检+扣费，余额不足拒绝）──
    _hconn0 = await asyncpg.connect(DATABASE_URL)
    try:
        from app.bot.resources import check_and_deduct_points as _h_deduct
        _h_ded = await _h_deduct(_hconn0, req.openid, "make_card")
    finally:
        await _hconn0.close()
    if not _h_ded.get("ok"):
        raise HTTPException(402, f"🦞 {_h_ded.get('message', '虾点不足，做嗨卡需 2 虾点')}")

    card_id = uuid.uuid4().hex[:12]
    card_dir = os.path.join(HAICA_DIR, card_id)
    os.makedirs(card_dir, exist_ok=True)

    # 处理图片
    img_url = req.image_url
    ext = "jpeg"
    if req.image_path and os.path.exists(req.image_path):
        ext = _detect_mime(req.image_path).split("/")[-1]
        dest = os.path.join(card_dir, f"front.{ext}")
        shutil.copy2(req.image_path, dest)
        img_url = f"/haica/img/{card_id}/front.{ext}"

    if not img_url:
        raise HTTPException(400, "请提供 image_url 或 image_path")

    # 查推广码
    code = ""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        sub = await conn.fetchrow("SELECT id FROM subscribers WHERE openid=$1", req.openid)
        if sub:
            row = await conn.fetchrow(
                "SELECT code FROM referral_codes WHERE subscriber_id=$1", sub["id"]
            )
            if row:
                code = row["code"]
        if not code:
            import random, string
            code = "HX" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    finally:
        await conn.close()

    abs_img_url = f"https://hai.pangoozn.com{img_url}" if img_url.startswith("/") else img_url
    share_url = f"https://hai.pangoozn.com/haica/{card_id}"

    # ── 翻卡HTML ──
    if req.blessing:
        sender_line = f"{req.sender_name or req.nickname} 想对你说" if req.recipient_name else f"{req.sender_name or req.nickname} 的祝福"
        card_title = f"{req.sender_name or req.nickname} 给你的嗨卡"
        og_title = f"{req.sender_name or req.nickname} 用享客虾做了张嗨卡 🦞"
        # blessing 处理：换行转<br>，绝句一句一行
        blessing_html = req.blessing.replace("\n", "<br>")
    else:
        sender_line = f"{req.nickname} 的享客虾嗨卡"
        card_title = f"{req.nickname} 的享客虾嗨卡"
        og_title = f"{req.nickname} 用享客虾做了张嗨卡 🦞"
        blessing_html = ""

    # 从 blessing 提取纯诗文（去掉可能的前缀）
    poem_lines = [l.strip() for l in req.blessing.split("\n") if l.strip()] if req.blessing else []
    # 过滤掉诗题行和空行
    poem_display = []
    for l in poem_lines:
        if re.match(r'^[《（「『].*[》）」』]?$', l):
            continue
        poem_display.append(l)

    ref_link = f"https://ai.pangoozn.com/xkx?ref={code}"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,maximum-scale=1.0,user-scalable=no">
<title>{card_title}</title>
<meta property="og:image" content="{abs_img_url}" />
<meta property="og:title" content="{og_title}" />
<meta property="og:description" content="轻触翻面，看看写了什么" />
<meta property="og:image:width" content="1200" />
<meta property="og:image:height" content="630" />
<meta property="og:url" content="{share_url}" />
<meta property="og:type" content="website" />
<meta name="wechat:card" content="summary_large_image" />
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:system-ui,-apple-system,sans-serif;background:#f5f0e8;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:30px 16px 40px}}
/* ── 卡片容器（3D 翻页） ── */
.scene{{perspective:1200px;width:100%;max-width:340px;margin:0 auto}}
.card{{position:relative;width:100%;padding-bottom:133%;cursor:pointer;transform-style:preserve-3d;transition:transform 0.7s cubic-bezier(.4,0,.2,1)}}
.card.flipped{{transform:rotateY(180deg)}}
.card-face{{position:absolute;top:0;left:0;width:100%;height:100%;backface-visibility:hidden;-webkit-backface-visibility:hidden;border-radius:16px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.1)}}
/* ── 正面：照片 ── */
.front{{background:#fff}}
.front img{{width:100%;height:100%;object-fit:cover;display:block}}
.tap-hint{{position:absolute;bottom:16px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.45);backdrop-filter:blur(6px);color:#fff;font-size:12px;padding:6px 16px;border-radius:20px;letter-spacing:1px;pointer-events:none;white-space:nowrap}}
/* ── 背面：祝福 ── */
.back{{background:linear-gradient(160deg,#fffdf7,#f8f4ec);transform:rotateY(180deg);display:flex;flex-direction:column;padding:28px 22px}}
.back-top{{flex-shrink:0;margin-bottom:12px}}
.back-top .sender-line{{font-size:16px;color:#c8781e;font-weight:600}}
.back-top .divider{{width:40px;height:3px;background:linear-gradient(90deg,#c8781e,transparent);border-radius:2px;margin-top:8px}}
.back-body{{flex:1;display:flex;flex-direction:column;justify-content:center}}
.blessing-text{{font-size:16px;color:#2c2c2c;line-height:2;text-align:center}}
.blessing-text br{{content:'';display:block;margin:4px 0}}
.back-footer{{flex-shrink:0;margin-top:12px;text-align:center}}
.back-footer .logo{{font-size:12px;color:#999}}
.back-footer .tagline{{font-size:11px;color:#bbb;margin-top:2px}}
/* ── 卡片下方推广区 ── */
.promo-area{{width:100%;max-width:340px;margin-top:24px;text-align:center}}
.promo-area .ref-label{{font-size:12px;color:#999;margin-bottom:6px}}
.promo-area .ref-code{{font-family:monospace;font-size:14px;color:#5B8C3E;letter-spacing:2px;background:#f0f7ee;display:inline-block;padding:6px 16px;border-radius:8px}}
.promo-area .btn{{display:block;width:100%;padding:14px;border-radius:12px;background:#5B8C3E;color:#fff;font-size:15px;font-weight:600;text-decoration:none;margin-top:16px;text-align:center}}
.promo-area .btn:hover{{background:#4a7a32}}
.promo-area .share-hint{{font-size:12px;color:#888;margin-top:10px;line-height:1.6}}
.promo-area .share-hint strong{{color:#5B8C3E}}
.footer{{font-size:11px;color:#ccc;margin-top:20px;text-align:center}}
</style>
</head>
<body>

<div class="scene">
  <div class="card" id="haicaCard" onclick="this.classList.toggle('flipped')">
    <!-- 正面 -->
    <div class="card-face front">
      <img src="{abs_img_url}" alt="嗨卡照片">
      <div class="tap-hint">轻触翻面</div>
    </div>
    <!-- 背面 -->
    <div class="card-face back">
      <div class="back-top">
        <div class="sender-line">❤️ {sender_line}</div>
        <div class="divider"></div>
      </div>
      <div class="back-body">
        <div class="blessing-text">{blessing_html}</div>
      </div>
      <div class="back-footer">
        <div class="logo">🦞 享客虾 · AI 创作伙伴</div>
        <div class="tagline">用享客虾搞AI创作，就是聊天这么简单！</div>
      </div>
    </div>
  </div>
</div>

<div class="promo-area">
  <div class="ref-label">✨ 推广码</div>
  <div class="ref-code">{code}</div>
  <a class="btn" href="{ref_link}">➕ 添加享客虾 Bot</a>
  <div class="share-hint">
    <strong>分享给好友</strong>，你和好友各得奖励 🎁
  </div>
</div>

<div class="footer">🦞 享客虾 · AI 创作伙伴</div>

</body>
</html>"""

    with open(os.path.join(card_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    return {
        "ok": True,
        "card_id": card_id,
        "url": share_url,
        "image_url": abs_img_url,
        "referral_code": code
    }

@router.get("/img/{card_id}/{filename}")
async def haica_img(card_id: str, filename: str):
    """提供嗨卡图片"""
    safe_name = os.path.basename(filename)
    img_path = os.path.join(HAICA_DIR, card_id, safe_name)
    if not os.path.exists(img_path):
        raise HTTPException(404, "图片不存在")
    return FileResponse(img_path)

@router.get("/card/{card_id}")
async def haica_card(card_id: str):
    """展示嗨卡页面"""
    card_path = os.path.join(HAICA_DIR, card_id, "index.html")
    if not os.path.exists(card_path):
        raise HTTPException(404, "卡片不存在")
    with open(card_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())
