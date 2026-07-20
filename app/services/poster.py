"""推广海报生成 v4 — 享客虾品牌风格 · 真 🦞 emoji 图片"""
import io
import qrcode
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import os

# ── 品牌色 ──
WARM = (245, 240, 232)
GREEN = (91, 140, 62)
GREEN_DARK = (60, 100, 40)
DARK = (51, 51, 51)
LIGHT_TEXT = (138, 122, 100)
WHITE = (255, 255, 255)
SUBTLE_LINE = (220, 215, 205)

# ── 🦞 图标路径 ──
LOBSTER_PATH = "/home/ubuntu/weclaw-1/static/lobster.png"

# ── 字体 ──
FONT_CACHE = {}

def _font(size, bold=False):
    key = (size, bold)
    if key in FONT_CACHE:
        return FONT_CACHE[key]
    if bold:
        paths = ["/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"]
    else:
        paths = ["/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
    for p in paths:
        if os.path.exists(p):
            try:
                f = ImageFont.truetype(p, size, index=2)
                FONT_CACHE[key] = f
                return f
            except Exception:
                continue
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
        FONT_CACHE[key] = f
        return f
    except:
        f = ImageFont.load_default()
        FONT_CACHE[key] = f
        return f


def _round_corners(im: Image.Image, r: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, im.width, im.height), radius=r, fill=255)
    result = im.copy()
    result.putalpha(mask)
    return result


def _draw_gradient_warm(draw, width, height):
    for y in range(height):
        ratio = y / height
        r = int(WARM[0] - (WARM[0] - 240) * ratio)
        g = int(WARM[1] - (WARM[1] - 235) * ratio)
        b = int(WARM[2] - (WARM[2] - 225) * ratio)
        draw.line([(0, y), (width, y)], fill=(r, g, b))


def generate_og_wide(code: str, nickname: str = "", avatar_bytes: bytes = None) -> bytes:
    """生成 1200×630 宽幅 OG 图 — 居中大LOGO风格，朋友圈+聊天都适合"""
    width, height = 1200, 630
    img = Image.new("RGB", (width, height), WARM)
    draw = ImageDraw.Draw(img)
    _draw_gradient_warm(draw, width, height)

    # ── 顶部绿条 ──
    draw.rectangle([(0, 0), (width, 6)], fill=GREEN)

    # ── 中央大 LOGO — 龙虾图标放大为主角 ──
    cx = width // 2

    if os.path.exists(LOBSTER_PATH):
        lobster_img = Image.open(LOBSTER_PATH).convert("RGBA")
        lobster_size = 180  # 从 90 放大到 180
        lobster_img = lobster_img.resize((lobster_size, lobster_size), Image.LANCZOS)
        img.paste(lobster_img, (cx - lobster_size // 2, 100), lobster_img)

    # ── 品牌（紧跟在龙虾下方） ──
    draw.text((cx, 295), "享客虾", fill=GREEN, font=_font(38, bold=True), anchor="mt")

    # ── 标语 ──
    draw.text((cx, 350), "就是聊天这么简单", fill=DARK, font=_font(24), anchor="mt")

    # ── 绿色强调线 ──
    draw.line([(cx - 60, 275), (cx + 60, 275)], fill=GREEN, width=2)

    # ── 功能标签（一排） ──
    tags = ["写歌", "贺卡", "笔记", "行情"]
    tag_gap = 12
    tag_w = 80
    tag_h = 36
    tag_y = 400
    total_w = len(tags) * tag_w + (len(tags) - 1) * tag_gap
    tag_start = cx - total_w // 2
    for i, tag in enumerate(tags):
        tx = tag_start + i * (tag_w + tag_gap)
        draw.rounded_rectangle([tx, tag_y, tx + tag_w, tag_y + tag_h], radius=18, fill=GREEN)
        draw.text((tx + tag_w // 2, tag_y + tag_h // 2), tag, fill=WHITE, font=_font(16, bold=True), anchor="mm")

    # ── 底部推荐人 ──
    av_size = 48
    av_y = 480
    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar = avatar.resize((av_size, av_size), Image.LANCZOS)
            mask = Image.new("L", (av_size, av_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, av_size, av_size), fill=255)
            mask = mask.filter(ImageFilter.SMOOTH)
            circular = Image.new("RGBA", (av_size, av_size), (0, 0, 0, 0))
            circular.paste(avatar, (0, 0), mask)
            av_x = cx - av_size - 6
            draw.ellipse(
                [(av_x - 2, av_y - av_size // 2 - 2),
                 (av_x + av_size + 2, av_y + av_size // 2 + 2)],
                outline=GREEN, width=2
            )
            img.paste(circular, (av_x, av_y - av_size // 2), circular)
        except:
            avatar_bytes = None

    name_x = cx + 10 if avatar_bytes else cx
    if nickname:
        draw.text((name_x, av_y - 8), nickname, fill=DARK, font=_font(16, bold=True), anchor="lm")
        draw.text((name_x, av_y + 14), "邀您试试", fill=GREEN, font=_font(13), anchor="lm")
    else:
        pass

    # ── 底部品牌 ──
    draw.text((cx, height - 20), "享客虾 · 创作就是聊天", fill=LIGHT_TEXT, font=_font(13), anchor="mt")

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

def generate_poster(code: str, nickname: str = "", avatar_bytes: bytes = None) -> bytes:
    """生成推广海报 PNG — 享客虾品牌风格"""
    width, height = 600, 900
    img = Image.new("RGB", (width, height), WARM)
    draw = ImageDraw.Draw(img)

    # ── 渐变暖底 ──
    _draw_gradient_warm(draw, width, height)

    # ── 顶部绿条 ──
    draw.rectangle([(0, 0), (width, 6)], fill=GREEN)

    # ── 品牌区 ──
    if os.path.exists(LOBSTER_PATH):
        lobster_img = Image.open(LOBSTER_PATH).convert("RGBA")
        lobster_size = 38
        lobster_img = lobster_img.resize((lobster_size, lobster_size), Image.LANCZOS)
        lobster_x = width // 2 - 88
        img.paste(lobster_img, (lobster_x, 20), lobster_img)
        draw.text((lobster_x + lobster_size + 6, 37), "享客虾", fill=GREEN, font=_font(32, bold=True), anchor="lm")
    else:
        draw.text((width // 2, 36), "享客虾", fill=GREEN, font=_font(32, bold=True), anchor="mt")
    draw.text((width // 2, 76), "享客虾，虾客行", fill=LIGHT_TEXT, font=_font(14), anchor="mt")

    # ── 绿色装饰线 ──
    draw.line([(width // 2 - 80, 100), (width // 2 + 80, 100)], fill=GREEN, width=1)

    # ── 头像 ──
    avatar_size = 90
    avatar_cy = 155

    if avatar_bytes:
        try:
            avatar = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            avatar = avatar.resize((avatar_size, avatar_size), Image.LANCZOS)
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            mask_draw = ImageDraw.Draw(mask)
            mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)
            mask = mask.filter(ImageFilter.SMOOTH)
            circular = Image.new("RGBA", (avatar_size, avatar_size), (0, 0, 0, 0))
            circular.paste(avatar, (0, 0), mask)
            draw.ellipse(
                [(width // 2 - avatar_size // 2 - 3, avatar_cy - avatar_size // 2 - 3),
                 (width // 2 + avatar_size // 2 + 3, avatar_cy + avatar_size // 2 + 3)],
                outline=GREEN, width=3
            )
            img.paste(circular, (width // 2 - avatar_size // 2, avatar_cy - avatar_size // 2), circular)
        except:
            avatar_bytes = None

    if not avatar_bytes:
        if os.path.exists(LOBSTER_PATH):
            emoji_av = Image.open(LOBSTER_PATH).convert("RGBA")
            emoji_av = emoji_av.resize((avatar_size, avatar_size), Image.LANCZOS)
            draw.ellipse(
                [(width // 2 - avatar_size // 2 - 3, avatar_cy - avatar_size // 2 - 3),
                 (width // 2 + avatar_size // 2 + 3, avatar_cy + avatar_size // 2 + 3)],
                outline=WHITE, width=3
            )
            draw.ellipse(
                [(width // 2 - avatar_size // 2 - 2, avatar_cy - avatar_size // 2 + 2),
                 (width // 2 + avatar_size // 2 + 2, avatar_cy + avatar_size // 2 + 4)],
                fill=(200, 195, 185, 80)
            )
            img.paste(emoji_av, (width // 2 - avatar_size // 2, avatar_cy - avatar_size // 2), emoji_av)
        else:
            draw.ellipse(
                [(width // 2 - avatar_size // 2, avatar_cy - avatar_size // 2),
                 (width // 2 + avatar_size // 2, avatar_cy + avatar_size // 2)],
                fill=GREEN
            )

    # ── 昵称 ──
    nick_y = avatar_cy + avatar_size // 2 + 16
    if nickname:
        draw.text((width // 2, nick_y), nickname, fill=DARK, font=_font(26, bold=True), anchor="mt")
        draw.text((width // 2, nick_y + 32), "邀你一起养虾", fill=GREEN, font=_font(16), anchor="mt")
    else:
        draw.text((width // 2, nick_y), "邀你一起养虾", fill=GREEN, font=_font(18), anchor="mt")

    # ── 推广主卡（加强版）──
    card_y = nick_y + 72
    card_h = 120
    card_w = width - 80
    card_x = (width - card_w) // 2

    card = Image.new("RGBA", (card_w, card_h), (255, 255, 255, 240))
    card = _round_corners(card, 14)
    img.paste(card, (card_x, card_y), card)
    # 绿色左边条 + 边框
    draw.rectangle([card_x, card_y + 8, card_x + 6, card_y + card_h - 8], fill=GREEN)
    draw.rounded_rectangle(
        [card_x, card_y, card_x + card_w, card_y + card_h],
        radius=14, outline=GREEN, width=1
    )
    card_draw = ImageDraw.Draw(img)
    card_draw.text((width // 2 - 4, card_y + 22), "在微信里养只 AI 虾", fill=DARK, font=_font(22, bold=True), anchor="mt")
    card_draw.text((width // 2 - 4, card_y + 58), "写歌 · 做贺卡 · 写笔记 · 看行情", fill=GREEN, font=_font(15, bold=True), anchor="mt")
    card_draw.text((width // 2, card_y + 92), "AI 创作 · 就是聊天这样简单", fill=(200, 120, 30), font=_font(17, bold=True), anchor="mt")

    # ── QR 码 ──
    link = f"https://ai.pangoozn.com/xkx/bind?ref={code}"
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=DARK, back_color="white").convert("RGB")
    qr_size = 200
    qr_img = qr_img.resize((qr_size, qr_size), Image.LANCZOS)

    qr_y = card_y + card_h + 28
    qr_x = (width - qr_size) // 2

    pad = 20
    qr_card_w = qr_size + pad * 2
    qr_card_h = qr_size + pad * 2 + 35
    qr_card = Image.new("RGBA", (qr_card_w, qr_card_h), (255, 255, 255, 240))
    qr_card = _round_corners(qr_card, 16)
    img.paste(qr_card, (qr_x - pad, qr_y - pad), qr_card)
    draw.rounded_rectangle(
        [qr_x - pad, qr_y - pad, qr_x + qr_size + pad, qr_y + qr_size + pad + 35],
        radius=16, outline=SUBTLE_LINE, width=1
    )

    qr_bg = Image.new("RGB", (qr_size, qr_size), WHITE)
    img.paste(qr_bg, (qr_x, qr_y))
    img.paste(qr_img, (qr_x, qr_y))

    draw.text((width // 2, qr_y + qr_size + 28), "长按识别 · 免费开养", fill=GREEN, font=_font(15, bold=True), anchor="mt")

    # ── 底部标语 ──
    slogan_y = qr_y + qr_size + 68
    draw.text((width // 2, slogan_y), "享客虾 · 创作就是聊天", fill=LIGHT_TEXT, font=_font(14), anchor="mt")

    # ── 底栏 ──
    draw.line([(width // 2 - 40, height - 48), (width // 2 + 40, height - 48)], fill=SUBTLE_LINE, width=1)
    draw.text((width // 2, height - 28), "享客虾 · 智享家", fill=(180, 175, 165), font=_font(11), anchor="mt")

    # ── 输出 ──
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
