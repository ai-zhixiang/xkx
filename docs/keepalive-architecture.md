# Keepalive — 享客虾 Bot 推送引擎

## 概述

Keepalive 是享客虾 Bot 的核心推送引擎，运行在享客虾专服（139.155.158.18）。它管理 iLink Bot 的长连接、消息收发、会员配额、文件推送和每日精读广播。

## 架构

```
┌──────────────────┐     SSH Tunnel      ┌────────────────────────┐
│ 主站             │  :19100→139:9100    │ 享客虾专服            │
│ (162.14.111.56)  │ ──────────────────▶ │ (139.155.158.18)      │
│                  │                     │                        │
│ Cron: 每日精读   │                     │ keepalive_service      │
│  → 生成文章      │                     │  ├─ Bot 长连接池       │
│  → POST push-daily│                    │  ├─ 消息路由管线       │
│                  │                     │  ├─ 会员配额管理       │
└──────────────────┘                     │  ├─ 文件推送           │
                                         │  └─ 每日精读广播       │
┌──────────────────┐     iLink WebSocket │                        │
│ WeChat 用户      │ ◀───────────────── │ Web API: :9100         │
│  ←→ keepalive   │                     │  ├─ /api/push-daily    │
│  发消息/收推送   │                     │  ├─ /api/send          │
└──────────────────┘                     │  ├─ /api/welcome       │
                                         │  └─ /api/reload        │
┌──────────────────┐                     │                        │
│ Hermes 大脑      │   agent_connector   │ 关联模块:              │
│  (本机/MD-1)     │ ◀───────────────── │  agent_connector.py    │
│                  │                     │  order_matcher.py      │
└──────────────────┘                     │  xiakexia_mcp_server.py│
                                         └────────────────────────┘
```

## 核心模块

### 1. Bot 会话管理器

每个 Bot ID 对应一条 iLink WebSocket 长连接，由 `_run_session()` 管理：

- **三层异常隔离**：
  - 第一层：网络层异常（`get_updates` 失败），`consecutive_failures` 计数
  - 第二层：消息处理循环异常，只打日志不中断保活
  - 第三层：各 handler 内部 try/except
- **心跳保活**：`notify_keepalive` 定时刷新 session
- **自动重连**：网络断开后自动重新连接

### 2. 消息路由管线

```
iLink 消息 → _extract_text()
  → 暗号检查（天王盖地虎等）
  → 口令检查（口令-/项目-）
  → 令指令（!前缀）
  → @weclaw 唤醒 → 文件处理
  → 会员配额检查 → AI 对话路由
```

### 3. 文件推送

- 通过 iLink CDN 上传并分发文件
- 自动按用户分目录存储（`downloads/{user_id}/`）
- 支持文中嵌入多条 `MEDIA:` 协议路径
- 文件类型包括：图片、视频、PDF、PPT、语音等

### 4. 每日精读推送

端点：`POST /api/push-daily`

接收 `title + link + sections[]` 卡片格式，推送纯文本卡片到所有 `daily_push=true` 的订阅用户：

1. 查询 `subscribers WHERE daily_push=true`
2. JOIN `channel_bindings` 获取 `channel_user_id`
3. 从 `_running_bots` 按 `user_id` 匹配对应 Bot
4. 逐个用户调用 `send_text()` 发送卡片消息
5. 超时/过期 session 自动跳过

### 5. 会员配额

- 查询 `weclawd.subscribers` 表获取会员状态
- 每日免费额度（按 IP 或 openid）
- 到期提醒：推送中附加剩余天数

## 关键技术决策

### 为什么独立引擎而非 Hermes 内置推送？

| 方案 | 问题 |
|------|------|
| Hermes deliver: "origin" | iLink 限流（ret=-2），无用户-bot 映射，不处理 session 过期 |
| Keepalive 独立引擎 | 自主管理 Bot 连接池，精准用户匹配，可控重试 |

### 48 小时推送窗口

与微信服务号一致：用户超 48 小时不主动对话则推送窗口关闭。

- **刷新条件**：仅用户主动发消息（bot 推送不算）
- **过期检测**：`send_text()` 检测 `errcode=-14`
- **跳过策略**：push-daily 自动跳过过期 session

### 推送通道

主站 → 139 通过 SSH Tunnel 转发：

```bash
# systemd: hermes-tunnel-139.service
ssh -L 19100:127.0.0.1:9100 -N ubuntu@139.155.158.18
```

Cron 推送地址：`http://127.0.0.1:19100/api/push-daily`

## 数据表

### subscribers（weclawd 库）

| 字段 | 说明 |
|------|------|
| openid | 微信 openid（唯一标识） |
| nickname | 用户昵称 |
| status | ACTIVE / PENDING / EXPIRED |
| plan_id | 会员套餐 ID |
| expires_at | 会员到期日 |
| daily_push | 是否接收每日推送 |
| points_* | 积分/点数系统 |

### channel_bindings

| 字段 | 说明 |
|------|------|
| channel_type | ilink / weixin |
| channel_user_id | iLink 用户 ID |
| openid | 对应微信 openid |
| nickname | 用户昵称 |
| is_active | 是否激活 |

### bot_accounts

| 字段 | 说明 |
|------|------|
| bot_id | iLink Bot ID |
| user_id | 关联的用户 ID（channel_user_id） |
| token | iLink Bot token |
| is_active | 是否运行中 |

## 部署

见 [keepalive-deployment.md](keepalive-deployment.md)
