# 🦞 享客虾 — 架构说明

> 更新时间：2026-06-28

---

## 服务器拓扑

```
┌───────────────────────────────────────────────────────────┐
│  成都主站 (162.14.111.56)                                  │
│  Hermes AI 主脑 · 嗨卡生产站                                │
│  hai.pangoozn.com                                          │
└────────────┬──────────────────────────────────┬────────────┘
             │ nginx 反向代理                    │
             ▼                                  ▼
┌──────────────────────────┐  ┌──────────────────────────────┐
│  MD-1 广州 (81.71.99.46) │  │  享客虾专服 139.155.158.18   │
│  文档库 · 网关代理        │  │  对外客户服务 · Bot 接入     │
│  dev.pangoozn.com         │  │  ai.pangoozn.com            │
└──────────────────────────┘  └──────────────────────────────┘
```

---

## 139 服务器 — 享客虾 Bot 服务架构

### 四层架构

```
┌──────────────────────────────────────────────────────────┐
│                         用户                              │
│             微信 Bot 聊天界面 (@享客虾 AI)                │
└──────────────────────────┬───────────────────────────────┘
                           │ iLink WebSocket
                           ▼
┌──────────────────────────────────────────────────────────┐
│  ① keepalive (:9100)                                     │
│  保活层 — iLink 长轮询 · 暗号秒回 · session 管理          │
│  职责：get_updates → 暗号匹配 → 欢迎消息 → 转发 Agent    │
│        不调 Hermes，不处理 AI                              │
└──────────────────────────┬───────────────────────────────┘
                           │ HTTP POST
                           ▼
┌──────────────────────────────────────────────────────────┐
│  ② agent-connector (:9101)                               │
│  Agent 交互层 — 转发消息到网关 · 等待回复 · 返回结果      │
│  职责：收 keepalive 消息 → 调 bot_gateway → 回回复       │
└──────────────────────────┬───────────────────────────────┘
                           │ HTTP POST
                           ▼
┌──────────────────────────────────────────────────────────┐
│  ③ weclawd-1 / bot_gateway (:8001)                       │
│  Bot 网关 — FastAPI + PostgreSQL                          │
│  职责：会员检查 · 配额管理 · 绑手机 · 支付 · 订阅        │
│        消息路由到 Hermes                                  │
└──────────────────────────┬───────────────────────────────┘
                           │ HTTP POST
                           ▼
┌──────────────────────────────────────────────────────────┐
│  ④ Hermes (:8642)                                        │
│  AI 大脑 — 独立实例 · DeepSeek v4-flash                  │
│  不共享主站 Hermes                                        │
└──────────────────────────────────────────────────────────┘
```

### 数据流（用户发消息到回复）

```
用户发"hi"
  → iLink WebSocket 收到消息
  → keepalive get_updates 轮询拿到
  → 暗号匹配？(天王盖地虎 → 秒回，不经过 AI)
  → 欢迎消息？（首次 → 发"欢迎你，虾友·XX"）
  → 会员/配额检查？（免费用户每日 50 条）
     ├─ 超限 → 直接回复"今日免费额度已用完，开通会员..."
     └─ 正常 → 继续
  → typing ticket 获取 + 发送 typing START
  → POST → agent-connector (:9101)
  → POST → bot_gateway (:8001)
  → POST → Hermes (:8642)
  → DeepSeek v4-flash 处理
  → 回复原路返回
  → keepalive 发送 typing STOP
  → send_text 推回 iLink → 用户看到回复
```

---

## 数据库 — weclawd

| 表 | 用途 |
|:---|:---|
| `bot_accounts` | Bot 凭证（token、状态） |
| `channel_bindings` | 用户绑定（Bot user_id → openid → nickname） |
| `subscribers` | 会员订阅（状态、过期时间、配额） |
| `plans` | 套餐定价（月卡/年卡） |
| `sub_orders` | 支付订单 |
| `daily_quota` | 每日免费配额计数 |
| `chat_conversations` | 对话历史 |
| `conversation_messages` | 消息记录 |

---

## 商业模式

| 层级 | 价格 | 对话配额 | 功能 |
|:---|:---:|:--------:|:---|
| 🆓 免费 | ¥0 | 50条/天 | 基础对话 |
| 💎 月卡 | ¥99 (公测¥9.9) | 不限 | 全部 AI 能力 |
| 💎 年卡 | ¥999 (公测¥99.9) | 不限 | 全部 AI 能力 |

---

## 关键文件路径

| 服务 | 路径 |
|:---|:---|
| keepalive | `/home/ubuntu/weclaw-keepalive/keepalive_service.py` |
| agent-connector | `/home/ubuntu/weclaw-keepalive/agent_connector.py` |
| bot_gateway | `/home/ubuntu/weclaw-1/app/routes/bot_gateway.py` |
| 支付 | `/home/ubuntu/weclaw-1/app/routes/pay.py` |
| 模板 | `/home/ubuntu/weclaw-1/app/templates/` |
| 静态 | `/home/ubuntu/weclaw-1/app/static/` |
| 开通页 | `/home/ubuntu/weclaw-1/app/static/subscribe.html` |
| 配额逻辑 | `/home/ubuntu/weclaw-1/app/bot/quota.py` |

---

## 域名

| 域名 | 指向 | 用途 |
|:---|:---:|:---|
| `ai.pangoozn.com` | 139.155.158.18 | 享客虾 Bot 入口 |
| `hai.pangoozn.com` | MD-1 → 成都 | 嗨卡生产站 |
| `dev.pangoozn.com` | MD-1 | 开发测试 |
| `weclaw.pangoozn.com` | 139.155.158.18 | (不对外) |
