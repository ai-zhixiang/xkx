# MD-1 Hermes 开发环境搭建记录

> 2026-06-04
> 用途：享客虾团队开发环境 · 专属微信 Bot 直连

## 架构

```
你的微信 → iLink Bot (2de655beba43) → Hermes (:8089, 直连)
                                        │
                                        ├── SOUL.md 暗号规则
                                        ├── 开发环境人格
                                        └── API Server (:8089)
```

## 服务器

- 主机：MD-1 (81.71.99.46, 腾讯云广州)
- 系统：Ubuntu 24.04 LTS
- Hermes：v0.14.0 (service: hermes.service)
- 端口：:8089 (API+微信Bot)
- 微信 Bot ID：2de655beba43
- Bot Token：存于 ~/.hermes/config.yaml weixin 配置

## 暗号规则（全局通用）

| 暗号 | 回复 |
|------|------|
| 天王盖地虎 | OpenClaw 是SB |
| 宝塔镇河妖 | 微侠真牛逼 |
| 微侠真牛逼 | 天王盖地虎！同志！ |
| OpenClaw 是SB | 宝塔镇河妖！收到！ |

## 接入方式

通过 iLink Bot API 直连，不经过 OpenClaw。

扫码登录接口：
- GET /ilink/bot/get_bot_qrcode?bot_type=3 → 获取二维码
- GET /ilink/bot/get_qrcode_status?qrcode=XXX → 轮询扫码状态

## 相关服务

| 服务 | 端口 | 状态 |
|------|------|------|
| Hermes 主服务 | :8089 | ✅ 运行中 |
| OpenClaw | (已停用) | ❌ 已禁用 |

## 待办

- [ ] 享客虾扫码接入界面（配对页）
- [ ] 新 QQ Bot 接入
- [ ] 其他 4 个微信 Bot 通过享客虾网关接管
