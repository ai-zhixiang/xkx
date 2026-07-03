# 享客虾 Bot 项目管理 — 初始化脚本
# 1. 建 DB 表 projects + project_members
# 2. 创建共享目录 /shared/workspace/projects/

import subprocess, os

HOST = "139.155.158.18"
PW = "Qadyz5i1"

# 1. DB 建表
db_sql = """
CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL UNIQUE,
    description TEXT DEFAULT '',
    created_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_members (
    id SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id VARCHAR(128) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'editor',
    joined_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(project_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_pm_user ON project_members(user_id);
CREATE INDEX IF NOT EXISTS idx_pm_project ON project_members(project_id);
"""

cmds = [
    f"sshpass -p '{PW}' ssh -o StrictHostKeyChecking=no ubuntu@{HOST} 'sudo -u postgres psql -d weclawd -c \"{db_sql.replace(chr(10), ' ')}\" 2>&1'",
    f"sshpass -p '{PW}' ssh -o StrictHostKeyChecking=no ubuntu@{HOST} 'sudo mkdir -p /shared/workspace/projects && sudo chown -R ubuntu:ubuntu /shared/workspace && ls -la /shared/workspace/projects/'",
    f"sshpass -p '{PW}' ssh -o StrictHostKeyChecking=no ubuntu@{HOST} 'sudo -u postgres psql -d weclawd -c \"\\dt projects*\" 2>&1'",
]

for cmd in cmds:
    print(f"→ {cmd[:80]}...")
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    print(r.stdout[:500])
    if r.stderr:
        print(f"⚠ {r.stderr[:300]}")
    print()
