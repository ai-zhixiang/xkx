"""
享客虾 — 主程序 v0.5
个人AI秘书 · 微信原生 · 订阅制 · 微信支付
"""
import os
import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.models import init_db, AsyncSessionLocal, Plan
from app.routes.admin import router as admin_router
from app.routes.wechat import router as wechat_router
from app.routes.public import router as public_router
from app.routes.pay import router as pay_router
from app.routes.auth import router as auth_router
from app.routes.menu import router as menu_router
from app.routes.upload import router as upload_router
from app.routes.generate import router as generate_router
from app.routes.documents import router as documents_router
from app.routes.analytics import router as analytics_router
from app.routes.bot_gateway import router as bot_gateway_router
from app.routes.resources import router as resources_router
from app.scheduler import start_scheduler
from app.bot.qqbot import run_qq_bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[享客虾] 连接数据库...")
    await init_db()
    print("[享客虾] 数据库就绪")

    async with AsyncSessionLocal() as session:
        from sqlalchemy import select, func
        result = await session.execute(select(func.count(Plan.id)))
        if result.scalar() == 0:
            from app.models import SEED_PLANS
            for p in SEED_PLANS:
                session.add(Plan(**p))
            await session.commit()
            print(f"[享客虾] 已填充 {len(SEED_PLANS)} 个套餐")

    start_scheduler()

    # 启动 QQ Bot（后台任务，不影响微信主服务）
    if os.getenv('QQ_BOT_APPID'):
        asyncio.create_task(run_qq_bot())
        print('[享客虾] QQ Bot 后台任务已启动')

    yield
    print("[享客虾] 服务停止")


app = FastAPI(
    title="享客虾 · AI秘书",
    version="0.5.2",
    description="微信里的私人AI秘书 · 享客虾，虾客行",
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

app.include_router(auth_router)
app.include_router(pay_router)
app.include_router(public_router)
app.include_router(admin_router, prefix='/api/admin')
app.include_router(wechat_router, prefix='/api/wechat')
app.include_router(menu_router)
app.include_router(upload_router)
app.include_router(generate_router)
app.include_router(documents_router, prefix='/api')
app.include_router(analytics_router, prefix='/api/analytics')
app.include_router(bot_gateway_router)  # 前缀已在 router 中定义
app.include_router(resources_router)
app.mount('/static', StaticFiles(directory='app/static'), name='static')
app.mount('/agents', StaticFiles(directory='/home/ubuntu/weclaw-1/agents'), name='agents')  # v0.5.0: 用户文件服务


@app.get('/', response_class=HTMLResponse)
async def landing():
    with open('app/templates/landing.html', 'r', encoding='utf-8') as f:
        return f.read()
@app.get('/bind', response_class=HTMLResponse)
async def bind_page(sync: str = '', token: str = '', openid: str = '', nickname: str = '', avatar: str = ''):
    html = open('app/templates/bind.html', 'r', encoding='utf-8').read()
    if openid and nickname:
        nick_safe = nickname.replace("'", "\\'").replace('<', '&lt;')
        av_safe = avatar.replace("'", "\\'") if avatar else ''
        inject = f'''<script>
(function(){{
  var oid = '{openid}';
  var nick = '{nick_safe}';
  var av = '{av_safe}';
  if(oid && nick){{
    try{{
      localStorage.setItem('wx_openid', oid);
      localStorage.setItem('wx_nickname', nick);
      if(av) localStorage.setItem('wx_avatar', av);
    }}catch(e){{}}
  }}
}})();
</script>'''
        html = html.replace('</head>', inject + '</head>')
    resp = HTMLResponse(content=html)
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get('/admin', response_class=HTMLResponse)
async def admin_page():
    with open('app/templates/admin.html', 'r', encoding='utf-8') as f:
        return f.read()




@app.get("/activate", response_class=HTMLResponse)
async def activate_page():
    with open("app/templates/activate.html", "r", encoding="utf-8") as f:
        html = f.read()
        return HTMLResponse(html)

@app.get('/admin', response_class=HTMLResponse)
async def admin_page():
    with open('app/templates/admin.html', 'r', encoding='utf-8') as f:
        return f.read()


@app.get('/go/s/{openid}')
async def go_subscribe(openid: str):
    """短链接：服务号 OAuth → subscribe 页"""
    from urllib.parse import quote
    
    # 始终跳 OAuth 获取服务号 openid（snsapi_base 静默授权）
    # OAuth 回调后会存 openid 到 channel_bindings，再跳 subscribe 页
    wechat_appid = os.getenv('WECHAT_APPID', 'wx79c1f7db6d290510')
    cb_url = f"https://ai.pangoozn.com/api/pay/oauth-callback?from_url={quote('https://ai.pangoozn.com/go/s/' + quote(openid, safe=''))}"
    oauth_url = f"https://open.weixin.qq.com/connect/oauth2/authorize?appid={wechat_appid}&redirect_uri={quote(cb_url, safe='')}&response_type=code&scope=snsapi_userinfo&state={openid}#wechat_redirect"
    return RedirectResponse(url=oauth_url)

@app.get('/api/pay/oauth-callback')
async def oauth_callback(code: str = '', state: str = '', from_url: str = ''):
    """服务号 OAuth 回调：换 openid → 拿昵称 → 存 channel_bindings → 跳回"""
    from urllib.parse import quote, unquote
    from fastapi import Query
    import httpx
    
    _from = from_url or ''
    
    if not code:
        return RedirectResponse(url=unquote(_from) if _from else 'https://ai.pangoozn.com/subscribe')
    
    wechat_appid = os.getenv('WECHAT_APPID', 'wx79c1f7db6d290510')
    wechat_secret = os.getenv('WECHAT_APPSECRET', '')
    
    # ── 1. 用 code 换 access_token + openid ──
    _svc_openid = ''
    _nickname = ''
    try:
        async with httpx.AsyncClient(timeout=10) as _c:
            tr = await _c.get(
                f"https://api.weixin.qq.com/sns/oauth2/access_token"
                f"?appid={wechat_appid}&secret={wechat_secret}&code={code}&grant_type=authorization_code"
            )
            td = tr.json()
            _svc_openid = td.get('openid', '')
            at = td.get('access_token', '')
            
            # ── 2. 用 snsapi_userinfo 拿昵称 ──
            if at:
                ur = await _c.get(
                    f"https://api.weixin.qq.com/sns/userinfo"
                    f"?access_token={at}&openid={_svc_openid}&lang=zh_CN"
                )
                ud = ur.json()
                if ud.get('nickname'):
                    _nickname = ud['nickname']
    except:
        pass
    
    if not _svc_openid:
        _svc_openid = state.split('@')[0]
    
    # ── 3. 存服务号 openid + nickname 到 channel_bindings ──
    if _svc_openid:
        try:
            from app.models import AsyncSessionLocal
            from sqlalchemy import text as sa_text
            async with AsyncSessionLocal() as _db:
                if _nickname:
                    await _db.execute(
                        sa_text("UPDATE channel_bindings SET openid = :svc, nickname = :nick WHERE channel_user_id LIKE :oid"),
                        {"svc": _svc_openid, "nick": _nickname, "oid": state.split('@')[0] + "%"}
                    )
                else:
                    await _db.execute(
                        sa_text("UPDATE channel_bindings SET openid = :svc WHERE channel_user_id LIKE :oid"),
                        {"svc": _svc_openid, "oid": state.split('@')[0] + "%"}
                    )
                await _db.commit()
        except:
            pass
    
    # ── 4. 昵称：优先微信拿到的，其次 DB，最后兜底 ──
    if not _nickname:
        try:
            from app.models import AsyncSessionLocal
            from sqlalchemy import text as sa_text
            async with AsyncSessionLocal() as _db2:
                _nr = await _db2.execute(
                    sa_text("SELECT nickname FROM channel_bindings WHERE channel_user_id LIKE :oid LIMIT 1"),
                    {"oid": state.split('@')[0] + "%"}
                )
                _nrow = _nr.fetchone()
                if _nrow and _nrow[0]:
                    _nickname = _nrow[0]
        except:
            pass
    
    if not _nickname:
        _nickname = "虾友"
    
    from urllib.parse import quote as _q
    return RedirectResponse(url=f"/subscribe?openid={_q(_svc_openid)}&nickname={_q(_nickname)}")

@app.get('/subscribe', response_class=HTMLResponse)
async def subscribe_page(openid: str = '', nickname: str = '', plan: str = ''):
    # 没有 openid → 跳转 OAuth 获取身份
    if not openid:
        wechat_appid = os.getenv('WECHAT_APPID', 'wx79c1f7db6d290510')
        from urllib.parse import quote
        cb_url = "https://ai.pangoozn.com/api/pay/oauth-callback?from_url=" + quote("https://ai.pangoozn.com/subscribe")
        oauth_url = f"https://open.weixin.qq.com/connect/oauth2/authorize?appid={wechat_appid}&redirect_uri={quote(cb_url, safe='')}&response_type=code&scope=snsapi_userinfo&state=subscribe#wechat_redirect"
        return RedirectResponse(url=oauth_url)
    
    with open('app/static/subscribe.html', 'r', encoding='utf-8') as f:
        html = f.read()
    
    # 查会员信息
    current_plan_name = ""
    current_expires_at = ""
    is_member = False
    xiake_points = 0
    points_expires_at = ""
    if openid:
        try:
            from app.models import AsyncSessionLocal
            from sqlalchemy import text as sa_text
            from datetime import date
            async with AsyncSessionLocal() as db:
                r = await db.execute(
                    sa_text("SELECT p.name as plan_name, s.expires_at, s.xiake_points, s.points_expires_at "
                            "FROM subscribers s LEFT JOIN plans p ON s.plan_id = p.id "
                            "WHERE s.openid LIKE :oid AND s.status = 'ACTIVE' ORDER BY s.id DESC LIMIT 1"),
                    {"oid": openid + "%"}
                )
                row = r.fetchone()
                if row and row[1] and row[1] >= date.today():
                    current_plan_name = row[0] or ""
                    current_expires_at = str(row[1])
                    xiake_points = row[2] or 0
                    points_expires_at = str(row[3]) if row[3] else ""
                    is_member = True
        except:
            pass
    
    data = {
        "openid": openid,
        "service_openid": '',  # 由 OAuth 回调直接传正确 openid 到 URL
        "nickname": nickname,
        "plan": plan,
        "current_plan_name": current_plan_name,
        "current_expires_at": current_expires_at,
        "is_member": is_member,
        "xiake_points": xiake_points,
        "points_expires_at": points_expires_at,
    }
    import json
    inject = f'<script>window.__SUBSCRIBE_DATA = {json.dumps(data, ensure_ascii=False)};</script>'
    html = html.replace('</head>', inject + '</head>')
    return HTMLResponse(html)


@app.get('/activate', response_class=HTMLResponse)
async def activate_page():
    with open('app/templates/activate.html', 'r', encoding='utf-8') as f:
        html = f.read()
        return HTMLResponse(html)