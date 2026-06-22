# 🦞 享客虾多租户 AI Agent 引擎 v1

> **英文名：** WeClaw（WeChat + Claw）  
> **代号：** weclaw-1  
> **版本：** v0.5.2  
> **状态：** 线上运营

享客虾（虾客行）—— 微信里的私人 AI 秘书。基于 **FastAPI + PostgreSQL + Hermes Agent** 构建的多租户 AI Agent 引擎，为每位付费用户分配独立的 Agent 子进程，提供搜、写、记、生成文档等完整 AI 能力。支持微信原生交互 + QQ Bot 双通道，微信支付订阅制运营。

---

## 技术栈

| 层级 | 技术 | 用途 |
|------|------|------|
| Web 框架 | **FastAPI** + Uvicorn | 异步 Web 服务 |
| 数据库 | **PostgreSQL** + asyncpg + SQLAlchemy 2.0 | 用户、订单、对话持久化 |
| AI 引擎 | **DeepSeek** API（deepseek-chat） | Agent 底层 LLM |
| Agent 框架 | **Hermes Agent**（多租户定制版） | 每个付费用户独立子进程 |
| 消息队列 | **Redis** | 微信 access_token 共享缓存 |
| 定时任务 | **APScheduler** | 订阅到期检测、续费提醒 |
| 支付 | **微信支付** JSAPI v3 | 订阅套餐支付 |
| 微信 Bot | **iLink（微信开放平台）** | 微信原生 Bot 连接 |
| QQ Bot | **QQ 开放平台** WebSocket | QQ 通道 Bot |
| 文档生成 | **WeasyPrint / wkhtmltopdf** | PDF 文档生成（标准版及以上） |
| 搜索 | **DuckDuckGo** HTML 搜索 | 实时信息搜索 |

---

## 核心特性

### 🧠 多租户 Agent 引擎
- 每位付费用户分配**独立隔离的 Agent 子进程**（Worker v6）
- 隔离的 `HERMES_HOME` 目录（含 memory/sessions/skills/logs/files/workspace）
- 空闲 **15 分钟自动回收**，最大 **20 个并发**进程
- 异常崩溃自动重建，无感恢复

### 🗂 两级记忆系统
- **近期对话**：最近 N 条对话历史注入 system prompt（basic=5条，standard=15条）
- **长期记忆**：`user_facts.json` 持久化用户偏好/个人信息，自动去重与截断

### 🛠 工具能力（套餐分级）
| 工具 | 基础版 | 标准版 |
|------|--------|--------|
| 🔍 **DuckDuckGo 搜索** | ✅ | ✅ |
| 🌐 **网页/PDF 读取**（fetch_url） | ✅ | ✅ |
| 💾 **文件记忆**（save_memory） | ✅ | ✅ |
| 📂 **文件管理**（增删查） | ✅ | ✅ |
| 📄 **PDF 文档生成** | ❌ | ✅ |
| 🔄 **最大推理轮次** | 5 | 15 |

### 💳 微信支付订阅制
- **基础版**：¥9.9/月 · 500 条/月
- **标准版**：¥19.9/月 · 2000 条/月
- **专业版**：¥199/月 · 无限（筹备中）
- 支持月卡/季卡/年卡，首年一折推广价
- 套餐原价锚定（展示折扣），到期自动标记过期
- 3 天内到期用户自动提醒

### 🤖 多通道交互
- **微信原生**：公众号消息回调 + iLink Bot 微信开放平台
- **QQ Bot**：QQ 开放平台 WebSocket 通道
- **Bot 网关**：统一管理多个 iLink Bot 账号，支持注册/绑定/解绑/健康检查

### 🔧 管理后台
- 订阅用户管理（列表/搜索/状态筛选）
- 套餐管理（增删改/上下架）
- 订单管理（支付/退款记录）
- 访问统计（落地页 PV/UV / 转化率）
- 通知推送

---

## 套餐体系

| 套餐 | 推广价 | 原价 | 月消息量 | 功能 | 状态 |
|------|--------|------|---------|------|------|
| 基础月卡 | ¥9.9 | ¥99 | 500 条 | 搜索+记忆+文件 | ✅ 上架 |
| 基础季卡 | ¥24.9 | ¥249 | 500 条 | 同上 | ✅ 上架 |
| 基础年卡 | ¥79 | ¥790 | 500 条 | 同上 | ✅ 上架 |
| 标准月卡 | ¥19.9 | ¥199 | 2000 条 | 含 PDF 生成 | ✅ 上架 |
| 标准季卡 | ¥49.9 | ¥499 | 2000 条 | 含 PDF 生成 | ✅ 上架 |
| 标准年卡 | ¥168 | ¥1680 | 2000 条 | 含 PDF 生成 | ✅ 上架 |
| 专业月卡 | ¥199 | ¥1990 | 无限 | 全部能力 | ⏳ 筹备中 |

---

## 快速启动

### 环境要求
- Python ≥ 3.11
- PostgreSQL ≥ 14
- Redis ≥ 6（可选，用于微信 access_token 共享）
- pdftotext（可选，用于 PDF 文本提取）

### 安装

```bash
# 1. 克隆项目
git clone <repo-url> ~/weclaw-1
cd ~/weclaw-1

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env，填入以下配置：
#   DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/weclawd
#   DEEPSEEK_API_KEY=sk-xxx
#   WECHAT_APPID=wx...
#   WECHAT_APPSECRET=...
#   WXPAY_MCHID=...
#   WXPAY_API_V3_KEY=...
#   BASE_URL=https://your-domain.com
#   REDIS_URL=redis://localhost:6379/0（可选）
```

### 启动

```bash
# 开发模式
uvicorn app.main:app --reload --host 0.0.0.0 --port 8089

# 生产模式
uvicorn app.main:app --host 0.0.0.0 --port 8089 --workers 4
```

服务启动后：
- 数据库自动初始化（建表 + 套餐种子数据）
- APScheduler 定时任务开始运行（到期检测 02:00 / 续费提醒 09:00）
- 如果配置了 QQ Bot，自动启动 QQ WebSocket 连接

### 健康检查

```bash
curl http://localhost:8089/api/health
# → {"status":"ok","version":"0.5.2","service":"享客虾 · AI秘书"}
```

---

## 项目结构

```
weclaw-1/
├── app/
│   ├── main.py                     # FastAPI 入口：路由注册、静态文件、着陆页
│   ├── models.py                   # SQLAlchemy ORM 模型 + 种子数据
│   ├── scheduler.py                # APScheduler 定时任务（到期检测/续费提醒）
│   ├── agent/
│   │   ├── __init__.py             # 导出 AgentSessionManager
│   │   ├── hermes_worker.py        # AI Worker v7 子进程（stdin/stdout JSON 通信）
│   │   └── session_manager.py      # 多租户会话池（生命周期管理）
│   ├── bot/
│   │   ├── unified_connector.py    # iLink Bot 统一连接器（心跳保活/自愈看门狗）
│   │   ├── qqbot.py                # QQ Bot 通道（WebSocket）
│   │   ├── connector.py            # Bot 连接层
│   │   ├── quota.py                # 用量配额检查
│   │   ├── deduplicator.py         # 消息去重
│   │   ├── text_splitter.py        # 微信文本分段
│   │   ├── typing_cache.py         # 正在输入状态缓存
│   │   ├── fix_warmup.py           # 预热修复脚本
│   │   └── test_ilink.py           # iLink 连接测试
│   ├── routes/
│   │   ├── admin.py                # 管理后台 API（用户/套餐/订单管理）
│   │   ├── wechat.py               # 微信消息回调（XML 解析/AI 回复）
│   │   ├── pay.py                  # 微信支付 JSAPI（下单/回调/退款）
│   │   ├── auth.py                 # OAuth 认证
│   │   ├── public.py               # 公开页面路由
│   │   ├── menu.py                 # 微信公众号菜单管理
│   │   ├── upload.py               # 文件上传接口
│   │   ├── generate.py             # 文档生成接口
│   │   ├── documents.py            # 文档管理接口（列表/详情/删除）
│   │   ├── analytics.py            # 落地页访问统计埋点
│   │   ├── vision_proxy.py         # 视觉能力代理
│   │   └── bot_gateway.py          # Bot 网关（iLink Bot 注册/绑定/健康检查）
│   ├── services/
│   │   ├── ai.py                   # 共享 AI 对话服务（多通道复用）
│   │   └── file_handler.py         # 文件处理服务
│   ├── static/                     # 静态资源（PDF/图片等）
│   └── templates/                  # HTML 模板（landing/admin）
├── agents/                         # 用户隔离目录（按 session_hash）
│   └── {hash16}/
│       ├── memory/user_facts.json  # 长期记忆文件
│       ├── files/                  # 用户上传文件
│       ├── workspace/              # 用户工作区
│       ├── sessions/               # 会话缓存
│       ├── skills/                 # Agent skills
│       └── logs/                   # Agent 日志
├── config/
│   └── access_codes.json           # 访问码配置
├── docs/
│   ├── WeClaw-PRD-v2.0.md          # 产品需求文档
│   ├── weclawd-v2-架构设计.md       # 架构设计文档
│   └── 享客虾产品发布计划-v1.0.md    # 发布计划
├── scripts/                        # 运维/修复/测试脚本
│   ├── healthcheck_chain.py        # 健康检查链
│   ├── fix_sql.py / fix_sql2.py    # 数据库修复
│   ├── pay_mock.py / pay_mock2.py  # 支付模拟
│   ├── add_mock_api.py             # Mock API 添加
│   ├── update_prompt.py            # Prompt 更新
│   └── fix_regex.py                # 正则修复
├── logs/                           # 运行日志
├── .gitignore
├── requirements.txt
└── README.md                       # 本文件
```

---

## 架构概览

```
┌──────────────────────────────────────────────────────────┐
│                   用户入口层                              │
│   微信公众号 ←→ 微信消息回调         QQ Bot ←→ WebSocket │
│          ↕                              ↕               │
│    ┌────────────────────────────────────────┐           │
│    │          Bot 网关层 (bot_gateway)       │           │
│    │    iLink Bot 注册 · 绑定 · 健康检查     │           │
│    └────────────┬───────────────────────────┘           │
│                 ↕                                        │
│    ┌────────────────────────────────────────┐           │
│    │          FastAPI 应用层 (main)          │           │
│    │  routes/ · 认证 · 支付 · 管理 · 统计    │           │
│    └────────────┬───────────────────────────┘           │
│                 ↕                                        │
│    ┌────────────────────────────────────────┐           │
│    │       AI 引擎层 (Agent Engine)          │           │
│    └────────────┬───────────────────────────┘           │
│                 ↕                                        │
│    ┌────────────────────────────────────────┐           │
│    │   多租户会话池 (session_manager)        │           │
│    │   用户1: Agent子进程 → HERMES_HOME_1   │           │
│    │   用户2: Agent子进程 → HERMES_HOME_2   │           │
│    │   ... (最大20并发)                      │           │
│    └────────────┬───────────────────────────┘           │
│                 ↕                                        │
│    ┌────────────────────────────────────────┐           │
│    │      Hermes Worker (hermes_worker)     │           │
│    │    stdin/stdout JSON 通信              │           │
│    │    DeepSeek API · DuckDuckGo · 记忆    │           │
│    │    PDF生成 · 文件管理 · URL读取         │           │
│    └────────────────────────────────────────┘           │
│                 ↕                                        │
│    ┌────────────────────────────────────────┐           │
│    │         数据持久化层                    │           │
│    │   PostgreSQL: 用户/订单/对话/统计      │           │
│    │   Redis: access_token 共享缓存         │           │
│    │   文件系统: user_facts.json · PDFs     │           │
│    └────────────────────────────────────────┘           │
└──────────────────────────────────────────────────────────┘
```

---

## 关键模块说明

### Agent 子进程通信协议
Worker 子进程通过 **stdin/stdout JSON 行协议** 与主进程通信：

```json
// 主进程 → Worker（stdin）
{"type": "chat", "message": "帮我查一下天气", "history": [...]}

// Worker → 主进程（stdout）
{"type": "progress", "content": "🔍 正在搜索..."}
{"type": "response", "content": "今天天气晴朗..."}
{"type": "done"}
```

### 会话生命周期管理
```
用户发消息 → 检查会话池 → [不存在] → 创建 AgentSession → 启动子进程
                                         → [存在/空闲] → 复用
                                         → [超15min] → 回收 → 下次重建
→ 发送消息 → 等待响应（最大 90s）→ 返回 → 更新 last_active
```

### 微信消息处理流程
```
微信服务器 → XML 回调 → wechat.py
    ├─ 验证签名（SHA1）
    ├─ 查找/创建用户
    ├─ 检查订阅状态 → [未订阅] → 返回引导文案
    ├─ 检查用量配额 → [超额] → 返回续费引导
    └─ 转发至 Agent 引擎 → 写入对话历史 → 回复用户
```

---

## 开发指南

### 添加新路由
1. 在 `app/routes/` 下创建新模块
2. 使用 `APIRouter` 定义路由
3. 在 `app/main.py` 中注册：`app.include_router(your_router)`

### 添加新 Agent 工具
1. 在 `app/agent/hermes_worker.py` 的 `TOOLS` 列表中添加工具定义
2. 实现对应的异步处理函数
3. 在主循环的 `tool_calls` 分支中添加路由

### 数据库迁移
当前使用 `Base.metadata.create_all` 自动建表。生产环境建议切换到 Alembic：

```bash
alembic init migrations
alembic revision --autogenerate -m "描述"
alembic upgrade head
```

---

## 维护命令

```bash
# 查看运行状态
curl http://localhost:8089/api/health

# 查看日志
tail -f logs/gateway.log
tail -f logs/connector.log

# 手动触发到期检测（调试）
python -c "import asyncio; from app.scheduler import check_expired_subscribers; asyncio.run(check_expired_subscribers())"

# 清理所有 Agent 会话（重启后）
pkill -f "hermes_worker.py" 2>/dev/null; rm -rf /tmp/hermes/memory/*

# 查看活跃会话数
curl http://localhost:8089/api/admin/sessions  # 需认证
```

---

## 许可

© 2025-2026 享客虾（WeClaw）。保留所有权利。
