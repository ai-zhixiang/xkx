# WeClawd v1.0 架构与部署手册

> 版本：v1.0 | 日期：2026-06-13

---

## 一、服务器规划

### 物理拓扑

```
                     ┌─────────────────────────────────┐
                     │        DNS: pangoozn.com         │
                     │   阿里万网 → 深圳 162.14.111.56   │
                     └────────────┬────────────────────┘
                                  │
                    ┌─────────────▼──────────────┐
                    │   主站 (深圳 162.14.111.56)    │
                    │   nginx SSL 统一入口          │
                    │   Certbot 自动续签             │
                    │                              │
                    │   :80    → 301 → :443         │
                    │   :443   → nginx reverse proxy │
                    └──────────┬───────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                    │
          ▼                    ▼                    ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  MD-1 (广州)      │  │  Bear2 (上海)    │  │  HK (香港)       │
│  81.71.99.46     │  │  124.222.215.111 │  │  124.156.173.120 │
│                  │  │                  │  │                  │
│  Bot 生产接入     │  │  v13 开发测试     │  │  Lucky Card 海外  │
│  weclawd-1:8001  │  │  v12:8007        │  │  hicard.world     │
│  connector       │  │  v13:8006        │  │                  │
│  bridge:8642     │  │                  │  │                  │
│  msgboard:8012   │  │                  │  │                  │
│  Hermes:8089     │  │                  │  │                  │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

### 域名路由

| 域名 | 指向 | 用途 |
|:-----|:-----|:-----|
| hai.pangoozn.com | 主站 → MD-1 nginx | 嗨卡生产站 |
| ai.pangoozn.com | 主站 → MD-1 nginx | 享客虾 Bot 管理 |
| dev.pangoozn.com | 主站 → MD-1 nginx → Bear2 | 开发测试站 |
| weclaw.pangoozn.com | *(规划)* | 微侠 APP API |

---

## 二、服务清单

### MD-1 (81.71.99.46)

| 服务 | 端口 | systemd 名 | 状态 | 说明 |
|:-----|:----:|:-----------|:----:|:-----|
| weclawd-1 | 8001 | weclawd-1.service | ✅ | Bot 网关（消息路由/OAuth/熔断） |
| weclaw-connector | — | weclaw-connector.service | ✅ | iLink 长轮询连接器 |
| hermes-bridge-md1 | 8642 | hermes-bridge-md1.service | ✅ | AI Agent 桥梁 |
| hermes | 8089 | hermes.service | ✅ | Hermes Agent |
| hermes-worker | — | hermes-worker.service | ✅ | 自主开发 Agent |
| msgboard | 8012 | msgboard.service | ✅ | 微侠令消息板 |
| PostgreSQL | 5432 | — | ✅ | 主库（weclawd） |

### 主站 (162.14.111.56)

| 服务 | 端口 | systemd 名 | 状态 | 说明 |
|:-----|:----:|:-----------|:----:|:-----|
| nginx | 80/443 | nginx | ✅ | SSL 反向代理 |
| weclawd (旧) | 8001 | weclawd.service | ✅ | 旧版网关（停用中） |
| hermes-bridge | 8642 | hermes-bridge.service | ✅ | AI Bridge（刚修复重启循环） |
| luckycards-v12 | 8007 | luckycards-v12.service | ✅ | 嗨卡生产站 |
| PostgreSQL | 5432 | — | ✅ | xiaolongxia 库 |

---

## 三、部署步骤

### 3.1 全新部署 weclawd-1

```bash
# 1. 克隆代码
git clone git@github.com:ai-zhixiang/xkx.git /home/ubuntu/weclaw-1
cd /home/ubuntu/weclaw-1

# 2. 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置环境变量
cp .env.example .env
# 编辑 .env：DATABASE_URL、API_KEY 等

# 4. 创建数据库
sudo -u postgres createdb weclawd
sudo -u postgres psql -c "CREATE USER lucky WITH PASSWORD 'lucky_pass';"
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE weclawd TO lucky;"

# 5. 安装 systemd 服务
sudo cp deploy/weclawd-1.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable weclawd-1
sudo systemctl start weclawd-1

# 6. 安装连接器
sudo cp deploy/weclaw-connector.service /etc/systemd/system/
sudo systemctl enable weclaw-connector
sudo systemctl start weclaw-connector

# 7. 配置 nginx 反向代理
sudo cp deploy/nginx-weclawd.conf /etc/nginx/sites-enabled/
sudo systemctl reload nginx
```

### 3.2 部署 hermes-bridge

```bash
# 1. 复制脚本
cp hermes_bridge.py /home/ubuntu/hermes_bridge.py

# 2. 安装 systemd 服务
sudo cp deploy/hermes-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable hermes-bridge
sudo systemctl start hermes-bridge

# 3. 验证
curl http://127.0.0.1:8642/health
# 返回: {"status":"ok","version":5,...}
```

### 3.3 部署 weclawd-2 (用户 API)

```bash
# 1. 创建目录
sudo mkdir -p /opt/weclaw-api
sudo chown ubuntu:ubuntu /opt/weclaw-api

# 2. 克隆代码
git clone git@github.com:ai-zhixiang/weclaw.git /opt/weclaw-api
cd /opt/weclaw-api/backend

# 3. 安装依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. 安装服务
sudo cp deploy/weclawd-2.service /etc/systemd/system/
sudo systemctl enable weclawd-2
sudo systemctl start weclawd-2
```

---

## 四、运维手册

### 4.1 日常检查

```bash
# 全链路健康检查
ssh md-1 'echo "bridge:"; curl -s -m 5 http://127.0.0.1:8642/health; \
          echo "weclawd:"; curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/docs; \
          echo "connector:"; systemctl is-active weclaw-connector.service'

# 查看连接器最近消息
ssh md-1 'journalctl -u weclaw-connector.service --since "10 min ago" --no-pager | grep -E "📤|📩|⚠️"'

# 查看熔断器状态
ssh md-1 'journalctl -u weclawd-1.service --since "30 min ago" --no-pager | grep "\[CB\]"'
```

### 4.2 重启流程

```bash
# 安全重启 hermes-bridge（不丢 session）
ssh md-1 'sudo systemctl restart hermes-bridge-md1.service'
sleep 5
ssh md-1 'curl -s http://127.0.0.1:8642/health'

# 重启 weclawd-1（熔断器复位）
ssh md-1 'sudo systemctl restart weclawd-1.service'
```

### 4.3 故障处理

| 症状 | 可能原因 | 解决 |
|:-----|:---------|:-----|
| Bot 回复"服务暂时不可用" | Hermes Bridge 挂死或超时 | `systemctl restart hermes-bridge-md1` |
| 连接器报"⚠️ 网关无回复" | weclawd-1 卡住 | `systemctl restart weclawd-1` |
| 消息延迟 > 2 分钟 | Agent 冷启动（新 session） | 正常，首请求加载 136 技能 |
| Bridge health 超时 | 事件循环被同步调用阻塞 | 检查 agent.chat() 是否在 run_in_executor 中 |
| Bot token 过期 | iLink session 断开 | 重新扫码激活 |

### 4.4 日志位置

| 服务 | 日志查看 |
|:-----|:---------|
| weclawd-1 | `journalctl -u weclawd-1.service -f` |
| connector | `journalctl -u weclaw-connector.service -f` |
| hermes-bridge | `journalctl -u hermes-bridge-md1.service -f` |
| gateway | `tail -f ~/.hermes/logs/gateway.log` |

---

## 五、监控告警

### 当前 Cron

| Job | 频率 | 说明 |
|:----|:-----|:-----|
| API 余额巡检 | 每日 10:00 | DeepSeek / 豆包 / OpenRouter 余额，低于阈值告警 |
| 嗨卡日报 | 每日 08:00 | 站点头部数据日报 |
| 磁盘监控 | 每 30 分钟 | 磁盘 >80% 告警（MD-1 已配） |

### 待补充

- [ ] weclawd-1 进程健康监控（探活 + 自动重启）
- [ ] 连接器心跳监控（connector 失联告警）
- [ ] 消息延迟监控（消息发送 → 回复 > 5 分钟告警）
- [ ] 熔断器触发告警（连续熔断通知管理员）
- [ ] 节点磁盘 90% 自动清理
