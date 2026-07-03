# Patch keepalive_service.py — 添加项目命令处理
# 插入位置：会员指令（1.4）之后，欢迎消息（1.5）之前

import re

with open('/home/ubuntu/weclaw-keepalive/keepalive_service.py', 'r') as f:
    content = f.read()

# 找到 1.5 欢迎消息的注释，在其前面插入项目命令处理
marker = '# ── 1.5 欢迎消息（每个用户只发一次）──'
if marker not in content:
    print('ERROR: marker not found!')
    exit(1)

project_code = '''                # ── 1.4b 项目命令（本地处理，不经过 Agent）──
                _proj_cmd = text.strip()
                _proj_uid = from_user.split("@")[0]
                if _proj_cmd.startswith("创建项目"):
                    _proj_name = _proj_cmd[4:].strip()
                    if not _proj_name:
                        await send_text(bot.token, from_user, "请指定项目名称，如：创建项目 春申酒业", ctx, session)
                        continue
                    try:
                        import asyncpg
                        _pdb = await asyncpg.connect(DB_DSN)
                        try:
                            # 查是否已存在
                            _exist = await _pdb.fetchrow("SELECT id FROM projects WHERE name = $1", _proj_name)
                            if _exist:
                                await send_text(bot.token, from_user, f"项目「{_proj_name}」已存在", ctx, session)
                                continue
                            # 创建项目
                            await _pdb.execute(
                                "INSERT INTO projects (name, created_by) VALUES ($1, $2)",
                                _proj_name, _proj_uid
                            )
                            # 取项目 ID
                            _pid = await _pdb.fetchval("SELECT id FROM projects WHERE name = $1", _proj_name)
                            # 创建者自动加入为 owner
                            await _pdb.execute(
                                "INSERT INTO project_members (project_id, user_id, role) VALUES ($1, $2, 'owner') ON CONFLICT DO NOTHING",
                                _pid, _proj_uid
                            )
                            # 创建共享目录
                            import os
                            _dir = f"/shared/workspace/projects/{_proj_name}"
                            os.makedirs(_dir, exist_ok=True)
                        finally:
                            await _pdb.close()
                    except Exception as _pe:
                        root_log.warning("[项目] 创建失败: %s", _pe)
                        await send_text(bot.token, from_user, f"创建项目失败，请稍后再试", ctx, session)
                        continue
                    await send_text(bot.token, from_user, f"✅ 已创建项目「{_proj_name}」\\n📁 你在项目中，上传文件自动归入此项目", ctx, session)
                    # 记录当前项目到 bot 内存
                    if not hasattr(bot, 'current_projects'):
                        bot.current_projects = {}
                    bot.current_projects[_proj_uid] = _proj_name
                    continue

                if _proj_cmd.startswith("进入项目"):
                    _proj_name = _proj_cmd[4:].strip()
                    if not _proj_name:
                        await send_text(bot.token, from_user, "请指定项目名称，如：进入项目 春申酒业", ctx, session)
                        continue
                    try:
                        import asyncpg
                        _pdb = await asyncpg.connect(DB_DSN)
                        try:
                            # 查项目是否存在 + 用户是否有权限
                            _pid = await _pdb.fetchval("SELECT p.id FROM projects p JOIN project_members pm ON p.id = pm.project_id WHERE p.name = $1 AND pm.user_id = $2", _proj_name, _proj_uid)
                            if not _pid:
                                await send_text(bot.token, from_user, f"项目「{_proj_name}」不存在或你没有访问权限", ctx, session)
                                continue
                        finally:
                            await _pdb.close()
                    except Exception as _pe:
                        root_log.warning("[项目] 进入失败: %s", _pe)
                        await send_text(bot.token, from_user, "查询失败，请稍后再试", ctx, session)
                        continue
                    if not hasattr(bot, 'current_projects'):
                        bot.current_projects = {}
                    bot.current_projects[_proj_uid] = _proj_name
                    await send_text(bot.token, from_user, f"📂 已进入项目「{_proj_name}」\\n💡 发文件自动存入、回复「项目文件」查看清单", ctx, session)
                    continue

                if _proj_cmd in ("项目文件", "项目列表", "项目清单"):
                    _proj_name = ""
                    if hasattr(bot, 'current_projects'):
                        _proj_name = bot.current_projects.get(_proj_uid, "")
                    if not _proj_name:
                        # 没在项目中→列出所有项目
                        try:
                            import asyncpg
                            _pdb = await asyncpg.connect(DB_DSN)
                            try:
                                _rows = await _pdb.fetch("SELECT p.name FROM projects p JOIN project_members pm ON p.id = pm.project_id WHERE pm.user_id = $1 ORDER BY p.updated_at DESC", _proj_uid)
                                if _rows:
                                    names = "\\n".join(f"  📁 {r['name']}" for r in _rows)
                                    await send_text(bot.token, from_user, f"📂 你的项目：\\n{names}\\n\\n💡 输入「进入项目 名称」切换", ctx, session)
                                else:
                                    await send_text(bot.token, from_user, "你还没有项目\\n💡 输入「创建项目 名称」开始", ctx, session)
                            finally:
                                await _pdb.close()
                        except Exception as _pe:
                            root_log.warning("[项目] 列表查询失败: %s", _pe)
                            await send_text(bot.token, from_user, "查询失败", ctx, session)
                        continue
                    # 在项目中→列出项目文件
                    import os, glob
                    _dir = f"/shared/workspace/projects/{_proj_name}"
                    files = []
                    try:
                        for f in sorted(glob.glob(f"{_dir}/**", recursive=True)):
                            if os.path.isfile(f) and not f.endswith('.lock'):
                                fname = os.path.basename(f)
                                fsize = os.path.getsize(f)
                                size_str = f"（{fsize/1024:.0f}KB）" if fsize > 1024 else ""
                                # 检查锁
                                lock_path = f + ".lock"
                                locker = ""
                                if os.path.exists(lock_path):
                                    try:
                                        with open(lock_path) as lf:
                                            locker = f" 🔒 {lf.read().strip().split(chr(10))[0]}"
                                    except:
                                        locker = " 🔒"
                                files.append(f"  📄 {fname}{size_str}{locker}")
                    except:
                        pass
                    if files:
                        info = f"📂 项目「{_proj_name}」文件：\\n" + "\\n".join(files)
                    else:
                        info = f"📂 项目「{_proj_name}」暂无文件"
                    await send_text(bot.token, from_user, info, ctx, session)
                    continue

                if _proj_cmd.startswith("加入项目"):
                    _proj_name = _proj_cmd[4:].strip()
                    if not _proj_name:
                        await send_text(bot.token, from_user, "请指定项目名称，如：加入项目 春申酒业", ctx, session)
                        continue
                    try:
                        import asyncpg
                        _pdb = await asyncpg.connect(DB_DSN)
                        try:
                            # 查项目是否存在
                            _pid = await _pdb.fetchval("SELECT id FROM projects WHERE name = $1", _proj_name)
                            if not _pid:
                                await send_text(bot.token, from_user, f"项目「{_proj_name}」不存在，请先创建", ctx, session)
                                continue
                            # 加入
                            await _pdb.execute(
                                "INSERT INTO project_members (project_id, user_id, role) VALUES ($1, $2, 'editor') ON CONFLICT DO NOTHING",
                                _pid, _proj_uid
                            )
                        finally:
                            await _pdb.close()
                    except Exception as _pe:
                        root_log.warning("[项目] 加入失败: %s", _pe)
                        await send_text(bot.token, from_user, "加入失败", ctx, session)
                        continue
                    await send_text(bot.token, from_user, f"✅ 已加入项目「{_proj_name}」", ctx, session)
                    if not hasattr(bot, 'current_projects'):
                        bot.current_projects = {}
                    bot.current_projects[_proj_uid] = _proj_name
                    continue

                if _proj_cmd == "退出项目":
                    _proj_name = ""
                    if hasattr(bot, 'current_projects'):
                        _proj_name = bot.current_projects.pop(_proj_uid, "")
                    if not _proj_name:
                        await send_text(bot.token, from_user, "你当前不在任何项目中", ctx, session)
                        continue
                    await send_text(bot.token, from_user, f"已退出项目「{_proj_name}」", ctx, session)
                    continue

                # 上传文件时→如果在项目中→自动存入项目目录
                if text.startswith(("[文件]", "[图片]", "[视频]", "[语音]")):
                    _cur_proj = ""
                    if hasattr(bot, 'current_projects'):
                        _cur_proj = bot.current_projects.get(_proj_uid, "")
                    if _cur_proj:
                        # 从 text 中提取文件名（在 [文件] xxx.后缀 或 [文件] 已归档路径中）
                        import re as _re
                        _fpath = ""
                        _m = _re.search(r"已归档:?\s*(/shared/[^\s]+)", text)
                        if _m:
                            _fpath = _m.group(1)
                        if _fpath and _fpath.startswith("/"):
                            _dir = f"/shared/workspace/projects/{_cur_proj}"
                            import os, shutil
                            os.makedirs(_dir, exist_ok=True)
                            _dest = f"{_dir}/{os.path.basename(_fpath)}"
                            try:
                                shutil.copy2(_fpath, _dest)
                                root_log.info("[项目] 文件自动入库 %s → %s", _fpath, _dest)
                            except Exception as _ce:
                                root_log.warning("[项目] 文件入库失败: %s", _ce)
                    # 不 continue，让消息继续走后面流程
'''

content = content.replace(marker, project_code + '\n' + marker)

with open('/home/ubuntu/weclaw-keepalive/keepalive_service.py', 'w') as f:
    f.write(content)

# 验证语法
try:
    compile(content, 'keepalive_service.py', 'exec')
    print('✅ SYNTAX OK')
except SyntaxError as e:
    print(f'❌ SYNTAX ERROR: {e}')
    exit(1)
