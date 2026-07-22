# Keepalive 部署文档

## 环境

| 项目 | 说明 |
|------|------|
| 服务器 | 享客虾专服 139.155.158.18（VM-0-14-ubuntu） |
| 用户 | ubuntu |
| 目录 | `/home/ubuntu/weclaw-keepalive/` |
| Python | 3.10+（虚拟环境 `.venv/`） |
| 数据库 | PostgreSQL weclawd（本地） |

## 依赖

```bash
pip install aiohttp asyncpg
```

## systemd 服务

### keepalive.service

路径：`/home/ubuntu/weclaw-keepalive/keepalive.service`

```ini
[Unit]
Description=WeClaw Keepalive Bot Service
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/weclaw-keepalive
ExecStart=/home/ubuntu/weclaw-keepalive/.venv/bin/python keepalive_service.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

安装：

```bash
sudo cp /home/ubuntu/weclaw-keepalive/keepalive.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable keepalive
sudo systemctl start keepalive
```

### SSH Tunnel（主站 → 139）

主站通过 SSH Tunnel 访问 139:9100：

```bash
# systemd: hermes-tunnel-139.service
[Unit]
Description=SSH Tunnel to 139 keepalive
After=network.target

[Service]
Type=simple
User=ubuntu
ExecStart=/usr/bin/ssh -o StrictHostKeyChecking=no \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -L 19100:127.0.0.1:9100 \
  -N ubuntu@139.155.158.18
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

## 配置文件

### PostgreSQL 连接

`keepalive_service.py` 顶部定义 `DB_DSN`：

```python
DB_DSN = "postgresql://weclawd:password@localhost:5432/weclawd"
```

### Bot Token 管理

Bot token 存储在 `bot_accounts` 表中。通过 Web 端扫码绑定后自动写入。

## API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/push-daily` | POST | 推送每日精读到所有订阅用户 |
| `/api/send` | POST | 给指定用户发送消息（需 bot_id） |
| `/api/welcome` | POST | 发送欢迎消息给指定用户 |
| `/api/subscription-confirmed` | POST | 订阅确认消息 |
| `/api/reload` | POST | 重新加载 Bot 列表 |
| `/api/health` | GET | 健康检查 |

### push-daily 请求格式

```json
{
  "title": "M/D",
  "link": "https://hai.pangoozn.com/go/daily",
  "sections": [
    {
      "emoji": "🤖",
      "label": "主题标题",
      "summary": "一句话摘要"
    }
  ]
}
```

## 日志

```bash
# 查看实时日志
journalctl -u keepalive -f

# 查看最近 100 行
journalctl -u keepalive --no-pager -n 100

# 查看推送相关日志
journalctl -u keepalive --no-pager | grep "push-daily"
```

## 运维命令

```bash
# 重启
sudo systemctl restart keepalive

# 查看状态
sudo systemctl status keepalive

# 重新加载 Bot 列表（扫码新 bot 后）
curl -X POST http://127.0.0.1:9100/api/reload

# 健康检查
curl http://127.0.0.1:9100/api/health
```

## 推送窗口说明

iLink Bot 与微信服务号一致：**48 小时推送窗口**。

- 用户超 48 小时不主动对话 → 推送失败
- 用户任意发一条消息 → 窗口刷新，再续 48 小时
- Bot 主动推送不算交互，不刷新窗口
- `send_text()` 检测 `errcode=-14` 判为 session 过期

## 日常巡检

1. 检查 Bot 连接是否正常：`journalctl -u keepalive --no-pager -n 20 | grep "bot:"`
2. 检查推送是否正常：`journalctl -u keepalive --no-pager | grep "push-daily"`
3. 检查 SSH Tunnel：`ps aux | grep "19100" | grep -v grep`
4. 检查订阅用户：`psql -d weclawd -c "SELECT count(*) FROM subscribers WHERE daily_push=true"`
