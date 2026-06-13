# WeClawd v1.0 产品设计文档

> 版本：v1.0 | 日期：2026-06-13 | 状态：公测待发布

---

## 一、产品定位

**WeClawd** = 享客虾 Bot 网关 + 用户 API 的统一服务端。是一套**微信 Bot 多租户接入、消息路由、AI 大脑、用户管理**的后端系统。

### 一句话

> 连接微信 Bot 与 Hermes AI Agent 的网关中间件，让每个用户拥有自己的 AI 助手。

### 解决的问题

| 问题 | 方案 |
|:----|:-----|
| 微信 Bot 直连 Hermes 不稳定 | weclawd-1 网关做消息路由 + 熔断保护 |
| 多用户共享 Bot 无隔离 | bot_accounts + channel_bindings 多租户 |
| Agent 上下文丢失 | hermes_bridge 做 session 持久化 + 记忆注入 |
| 无付费闭环 | daily_quota + subscribers + sub_orders 计费体系 |

---

## 二、系统架构

```
┌──────────────┐     ┌──────────────────┐     ┌───────────────┐
│  微信用户 A   │────▶│  unified_connector│────▶│  weclawd-1    │
│  (iLink Bot)  │     │  (长轮询/消息泵)  │     │  (:8001)      │
└──────────────┘     └──────────────────┘     │  Bot 网关      │
                                              │  OAuth/路由    │
┌──────────────┐                              │  熔断保护      │
│  微信用户 B   │────▶│  unified_connector│     └──────┬────────┘
│  (iLink Bot)  │     │  (独立进程/隔离)  │            │
└──────────────┘                              ┌──────▼────────┐
                                              │  Hermes Bridge │
┌──────────────┐     ┌──────────────────┐     │  (:8642)      │
│  Android APP  │────▶│  weclawd-2       │     │  Agent 缓存   │
│  (WeClaw)     │     │  (:8005)         │     │  session 持久 │
└──────────────┘     │  用户 API         │     └──────┬────────┘
                     │  订阅/聊天/文件   │            │
                     └──────────────────┘     ┌──────▼────────┐
                                              │  DeepSeek API  │
                                              │  (AI 推理)     │
                                              └───────────────┘
```

### 三台服务器分工

| 服务器 | 角色 | 运行服务 |
|:-------|:-----|:---------|
| **主站** 162.14.111.56 | 统一入口 + 管理通道 | nginx SSL, weclawd(:8001 旧版), Hermes Bridge |
| **MD-1** 81.71.99.46 | Bot 生产接入 | weclawd-1(:8001), weclaw-connector, hermes-bridge-md1(:8642), msgboard(:8012) |
| **Bear2** 124.222.215.111 | 开发测试 | v12/v13 开发版, 测试 DB |

---

## 三、核心模块

### 3.1 weclawd-1 — Bot 网关 (:8001)

**代码位置**: `/home/ubuntu/weclaw-1/app/routes/bot_gateway.py`

**职责**:
- 接收 unified_connector 的 webhook 推送
- 四级消息调度（L0暗号 → L1轻量 → L2常规 → L3深度任务）
- Hermes Bridge 调用（带熔断保护 + 超时分级）
- OAuth 绑定流程（扫码 → 短链接 → 微信授权 → 绑定）
- Bot 注册/激活/二维码管理
- 对话记录持久化（conversation_messages 表）
- 每日配额控制（daily_quota 表）

**超时配置**:

| 级别 | 场景 | 超时 |
|:----|:-----|:----:|
| L0 | 暗号匹配 | 即时（零成本） |
| L1 | 问候/状态/短查询 | 60s |
| L2 | 常规对话 | 300s |
| L3 | 写文档/分析/修改文件 | 600s |

**熔断器参数**:
- `failure_threshold=3`：连续 3 次失败打开熔断
- `recovery_timeout=120`：120 秒后半开重试
- 熔断期间：直接返回"服务暂时不可用"，**不降级 DeepSeek**（无上下文无意义）

### 3.2 unified_connector — iLink 连接器

**代码位置**: `/home/ubuntu/weclaw-1/app/bot/unified_connector.py`

**职责**:
- 与 iLink WeChat Bot API 长轮询
- 消息接收（文本/图片/文件/语音）
- 消息发送（文本/MEDIA 文件直推）
- 输入状态指示（typing indicator keep-alive）
- 媒体文件下载/加密解密
- 心跳保活 + session 管理

**架构要点**:
- 每个 Bot 一个独立连接器进程 → 完全隔离
- 长轮询间隔 1s（get_updates）
- Media 文件通过 iLink CDN 上传/下载
- 支持 AES-128-ECB 加密的媒体文件

### 3.3 hermes_bridge — AI Agent 桥梁 (:8642)

**代码位置**: `/home/ubuntu/hermes_bridge.py`

**职责**:
- FastAPI 服务，暴露 OpenAI-compatible `/v1/chat/completions` 端点
- Agent 缓存管理（LRU，最多 50 个 session）
- Session 持久化（bot_session_messages 表，7 天 TTL）
- 持久记忆注入（MEMORY.md + USER.md）
- Token 压缩（>80K tokens 时自动压缩历史）
- 空闲回收（2 小时无活动自动清理 Agent）

**关键设计**:
- `agent.chat()` 通过 `run_in_executor` 跑在**线程池**，不阻塞事件循环
- Session 级别互斥锁（同一用户串行处理）
- 预预热机制（启动时创建 __prewarm__ Agent 避免冷启动）

### 3.4 weclawd-2 — 用户 API (:8005)

**代码位置**: `/opt/weclaw-api/app/main.py`

**职责**:
- 用户注册/登录（手机号 + 验证码）
- 聊天 API
- 订阅管理（查询 Plan、检查状态）
- Android APP 后端全 API

---

## 四、数据模型

### 4.1 bot_accounts — Bot 账号
| 字段 | 类型 | 说明 |
|:-----|:-----|:-----|
| bot_id | VARCHAR(100) PK | iLink Bot ID |
| bot_token | VARCHAR(255) | iLink 认证 Token |
| user_id | VARCHAR(100) | 绑定的微信用户 ID |
| nickname | VARCHAR(100) | 用户昵称 |
| backend | VARCHAR(50) | 后端类型：hermes / deepseek |
| is_active | BOOLEAN | 是否启用 |

### 4.2 channel_bindings — 通道绑定
| 字段 | 类型 | 说明 |
|:-----|:-----|:-----|
| channel_type | VARCHAR(32) | ilink / weixin / qq |
| channel_user_id | VARCHAR(128) | 通道用户 ID |
| openid | VARCHAR(128) | 微信 OpenID |
| nickname | VARCHAR(128) | 微信昵称 |
| phone | VARCHAR(32) | 手机号 |
| user_account_id | BIGINT FK | 关联 user_accounts |

### 4.3 daily_quota — 每日配额
| 字段 | 类型 | 说明 |
|:-----|:-----|:-----|
| user_id | VARCHAR(128) PK | 用户 ID |
| quota_date | DATE PK | 日期 |
| used | INTEGER | 已用次数 |

### 4.4 conversation_messages — 对话记录
| 字段 | 类型 | 说明 |
|:-----|:-----|:-----|
| user_id, bot_id | VARCHAR | 复合索引 |
| role, content | TEXT | 角色和消息内容 |
| created_at | TIMESTAMP | 时间戳 |

### 4.5 bot_session_messages — Agent Session
| 字段 | 类型 | 说明 |
|:-----|:-----|:-----|
| session_id | VARCHAR | Hermes session ID |
| role, content | TEXT | 角色和消息 |
| created_at | TIMESTAMP | 带索引，7 天自动清理 |

---

## 五、API 清单

### Bot 管理
| 端点 | 方法 | 说明 |
|:-----|:----|:-----|
| `/api/bot/register` | POST | 注册 Bot |
| `/api/bot/list` | GET | 列出所有 Bot |
| `/api/bot/deactivate/{bot_id}` | POST | 停用 Bot |
| `/api/bot/qrcode` | GET | 生成绑定二维码 |
| `/api/bot/webhook` | POST | 接收连接器消息 |
| `/api/bot/bind` | POST | 通道绑定 |
| `/api/bot/b/{code}` | GET | 短链接重定向 |

### 用户 API
| 端点 | 方法 | 说明 |
|:-----|:----|:-----|
| `/api/auth/register` | POST | 手机号注册 |
| `/api/auth/login` | POST | 登录 |
| `/api/chat/send` | POST | 发送消息 |
| `/api/subscription/status` | GET | 订阅状态 |
| `/api/subscription/plans` | GET | 套餐列表 |

---

## 六、安全与隔离

### 用户隔离
- **Bot 隔离**: 每个用户独立 Bot（bot_accounts 表）
- **Session 隔离**: session_id = `{bot_id}:{openid}`，互不干扰
- **配额隔离**: daily_quota 按 user_id + date 独立计数
- **通道绑定**: channel_bindings 绑定微信身份到内部账号

### 熔断保护
- Hermes 超时 → 熔断器打开 → 返回友好错误
- **取消 DeepSeek 降级**: 裸 DS 调用丢失技能/记忆/上下文
- 熔断恢复后自动切回 Hermes

---

## 七、开发状态

### ✅ 已完成
- weclawd-1 Bot 网关（消息路由 / OAuth / 熔断保护）
- unified_connector iLink 长轮询连接器
- hermes_bridge Agent 缓存 + Session 持久化
- hermes_bridge 线程池修复（不阻塞事件循环）
- 四级消息调度 + 分级超时
- 对话记录持久化
- 二维码扫码绑定流程
- weclawd-2 用户 API（注册/登录/订阅）
- 三服务器架构 + nginx 反向代理

### 🔄 待优化
- 连接器进程看门狗（自动恢复死掉的连接器）
- 配额告警（接近限额时推送消息）
- Admin 管理后台（用户/Bot/配额管理）
- 连接器 systemd 单元标准化
- 主站 hermes-bridge systemd 清理重启循环
