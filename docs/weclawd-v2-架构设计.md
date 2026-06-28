# 享客虾智能网关 v2 架构设计

## 1. 现状分析

### 当前链路痛点

```
用户 → weclawd-1(bot_gateway) → Hermes Bridge(:8642) → Hermes Agent
                                                             ↓
                                                    136个skill加载
                                                    记忆文件读取
                                                    agent.chat() 同步阻塞
```

| 问题 | 根因 | 影响 |
|------|------|------|
| 冷启动慢 | Hermes Agent 加载 136 个 skill + 记忆文件 | 30-60s |
| Session 依赖缓存 | agent cache 在内存，服务重启丢失 | 重启后打招呼 |
| 同步阻塞 | agent.chat() 是同步方法，阻塞 asyncio 事件循环 | /health 超时，请求排队 |
| 熔断器误杀 | _hermes_cb 连续失败后切降级 | 正常请求被拦 |
| iLink token 过期 | 腾讯侧控制，唯一修复方式是扫码 | 用户必须重新扫码 |

### 真实场景需求分析

通过分析 Bot 消息日志，90%+ 的用户消息属于：

| 级别 | 类型 | 占比 | 需要 Hermes? |
|------|------|------|-------------|
| L0 | 暗号(天王盖地虎等) | ~5% | ❌ 网关直回 |
| L1 | 问候/单字(在吗/嗨/嗯) | ~15% | ❌ DeepSeek 直调即可 |
| L2 | 聊天/查资料/做卡/找歌 | ~60% | ❌ 需要轻量工具，不需要完整 skill |
| L3 | 写代码/运维/分析文件 | ~20% | ✅ 需要完整 toolchain |

**结论：80% 的消息不需要经过 Hermes。**

## 2. 目标架构

```
用户 → weclawd(智能网关)
         │
         ├── 会话管理层 ─── PostgreSQL(对话历史+记忆)
         │
         ├── L0 暗号匹配 ──→ 直接回复
         │
         ├── L1-L2 轻量路由 ──→ DeepSeek API(直调)
         │       │
         │       └── 本地工具(查歌/做卡/推荐)
         │
         └── L3 深度路由 ──→ Hermes Bridge(按需)
```

### 核心原则

1. **Session 归 weclawd 管** — PostgreSQL 全量持久化，Hermes 只有内存 cache
2. **L0-L2 不经 Hermes** — 响应 < 5s，Hermes 挂了不影响
3. **L3 按需调 Hermes** — 保留完整工具链兜底
4. **记忆系统自建** — 用户偏好/习惯/历史注入 system prompt

## 3. Session 管理体系

### 3.1 数据模型

```sql
-- 对话消息（核心）
CREATE TABLE bot_session_messages (
    id BIGSERIAL PRIMARY KEY,
    session_id VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    tool_calls JSONB,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE INDEX idx_session_time ON bot_session_messages(session_id, created_at);

-- 用户记忆（长期）
CREATE TABLE user_memories (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(128) NOT NULL UNIQUE,
    preferences JSONB DEFAULT '{}',
    summary TEXT,
    facts TEXT[],
    updated_at TIMESTAMP DEFAULT NOW()
);
```

### 3.2 Session 重建流程

```
用户发消息
    ↓
① 取 session_id = bot_id:channel_user_id
    ↓
② 查 bot_session_messages WHERE session_id=? ORDER BY DESC LIMIT 50
    ↓
③ 查 user_memories WHERE user_id=?
    ↓
④ 拼 messages[] = [
       system: persona + 记忆 + 当前时间
       ...历史消息(倒序)
       user: 当前消息
    ]
    ↓
⑤ 调 DeepSeek（带 tools）
    ↓
⑥ 存 user+assistant 到 bot_session_messages
    ↓
⑦ 回复用户
```

### 3.3 iLink Token 过期处理

```
检测到 errcode=-14
    ↓
① 标记 bot_accounts.token_expired=true
    ↓
② 尝试自动 refresh（notifyStop→notifyStart）
    ↓
③ 如果失败：暂停 5 分钟，用户可发"激活"获新 QR
    ↓
④ 扫码后更新 token
    ↓
⑤ 取最近 3 条历史推送给用户恢复上下文
```

**核心：token 过期 ≠ 聊天丢失。Session 全在 DB，新 token 一来恢复。**

## 4. 轻量 Agent Loop

### 4.1 流程

```
function process_message(user_msg):
    1. 加载上下文（历史 + 记忆）
    2. 拼 messages[]
    3. 调 DeepSeek(带tools定义)

    while True:
        if response 有 tool_calls:
            for each tool_call:
                result = execute_tool(tool_call)
                messages.append(tool_result)
            response = call_deepseek(messages)
        else:
            break

    4. 存 messages 到 DB
    5. 返回最终回复
```

### 4.2 Bot 场景 Tools

```json
[
  {"name": "search_music", "description": "搜索音乐", "parameters": {"keyword": "string"}},
  {"name": "make_card", "description": "引导做嗨卡，返回链接", "parameters": {"theme": "string"}},
  {"name": "recommend_songs", "description": "推荐歌曲", "parameters": {"style": "string"}},
  {"name": "query_knowledge", "description": "查文档/知识库", "parameters": {"question": "string"}}
]
```

## 5. 按需调 Hermes

### 5.1 触发条件

```python
L3_TRIGGERS = [
    "写代码", "改代码", "修复bug", "部署",
    "ssh", "服务器", "nginx", "systemd",
    "读取文件", "搜索文件", "分析日志",
    "git", "commit", "cron", "运维",
]
```

### 5.2 降级策略

```python
async def call_hermes_l3(session_id, messages):
    try:
        return await _call_hermes(session_id, messages)
    except:
        return "这个任务需要服务器工具，当前引擎忙，请稍后再试。"
```

## 6. 实施计划

### Phase 1 — 核心骨架

| 模块 | 内容 |
|------|------|
| 记忆表 | 建 user_memories + 索引 |
| 上下文加载器 | context.py — 取历史+拼messages+注入记忆 |
| 轻量 Agent | agent_lite.py — DeepSeek 直调 + tool calling |
| 路由切换 | bot_gateway.py — L0-L2走新路，L3走老路 |

### Phase 2 — 会话永续

| 模块 | 内容 |
|------|------|
| 自动 refresh | -14 时自动 notifyStop→Start |
| 上下文恢复 | 新 token 扫码后自动推最近历史 |
| 跨 bot 合并 | 同 phone 打通 session |

### Phase 3 — 记忆进化

| 模块 | 内容 |
|------|------|
| 自动摘要 | 每周压缩历史写 summary |
| 偏好学习 | 从对话自动提取偏好存 preferences |
| 个性化 | 用户自定义 personality |

## 7. 架构对比

| 维度 | 当前 | 目标 |
|------|------|------|
| Session 存储 | bridge内存+DB | **全在 DB** |
| 冷启动速度 | 30-60s | **< 1s** |
| Hermes 挂了 | ❌ 全挂 | **L0-L2正常，L3降级** |
| 记忆持久化 | MEMORY.md 文件 | **PostgreSQL** |
| 响应延迟(L0-L2) | ~30s | **~3s** |
| 架构复杂度 | 2跳 | **1跳** |
