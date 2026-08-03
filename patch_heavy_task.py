#!/usr/bin/env python3
"""Patch bot_gateway.py: add heavy task detection with 5s probe"""
import os, re

path = "/home/ubuntu/weclaw-1/app/routes/bot_gateway.py"
backup = path + ".bak-" + str(int(__import__('time').time()))

with open(path) as f:
    code = f.read()

# Backup
with open(backup, "w") as f:
    f.write(code)
print(f"Backup: {backup}")

# ── Step 1: Replace the Hermes API call block with 5s probe + background ──

old_hermes_block = '''        # 在熔断器保护下调用 Hermes API
        try:
            resp = await _hermes_cb.call(
                client.post(
                    hermes_url,
                    json={
                        "model": "hermes-agent",
                        "messages": messages,
                        "max_tokens": use_max_tokens,
                        "stream": False,
                    },
                    headers=headers,
                ),
                timeout=cb_timeout,
            )
        except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException, CircuitBreakerOpen) as cb_e:
            is_cb_open = isinstance(cb_e, CircuitBreakerOpen)
            logger.warning(f"[CB] Hermes 调用失败({type(cb_e).__name__}): {cb_e}")
            # Fallback chain: Kimi -> Doubao
            reply = await _try_kimi_fallback(messages, use_max_tokens)
            if not reply:
                reply = await _try_doubao_fallback(messages, use_max_tokens)
            sem.release()
            if reply:
                await _save_conversation_pair(session_key, user_account_id, openid, content, reply)
                return reply
            err_msg = "服务暂时不可用（AI 引擎忙），请稍后重试。"
            await _save_conversation_pair(session_key, user_account_id, openid, content, err_msg)
            return err_msg
        if resp.status_code == 200:
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            
            # ── DeepSeek 拒绝检测：拦截 "无法回答" 等降级到 Doubao ──
            if _is_refusal(reply):
                logger.warning(f"[拒绝检测] DeepSeek 拒绝，降级: {reply[:60]}")
                sem.release()
                fb = await _try_kimi_fallback(messages, use_max_tokens)
                if not fb:
                    fb = await _try_doubao_fallback(messages, use_max_tokens)
                if fb:
                    await _save_conversation_pair(session_key, user_account_id, openid, content, fb)
                    return fb
                # 后备也失败，重新获取 sem 走正常流程
                await sem.acquire()
            
            # 保全对话上下文（确保重启/降级后上下文不断）
            await _save_conversation_pair(session_key, user_account_id, openid, content, reply)
            
            # 无论是否有 session_key 都正常返回回复
            sem.release()
            return reply
        else:
            logger.warning(f"Hermes API 非 200 状态: {resp.status_code}")
            # Fallback chain: Kimi -> Doubao
            reply = await _try_kimi_fallback(messages, use_max_tokens)'''

new_hermes_block = '''        # ── 重型任务检测：先试 5s 超时（不经过熔断器）──
        HEAVY_TIMEOUT = 5
        is_heavy = False
        reply = None
        
        try:
            async with httpx.AsyncClient(timeout=HEAVY_TIMEOUT) as _probe:
                _pr = await _probe.post(
                    hermes_url,
                    json={
                        "model": "hermes-agent",
                        "messages": messages,
                        "max_tokens": use_max_tokens,
                        "stream": False,
                    },
                    headers=headers,
                )
            if _pr.status_code == 200:
                _pd = _pr.json()
                reply = _pd["choices"][0]["message"]["content"]
                logger.info(f"[Probe] 快速响应({len(reply)}字符): {content[:30]}")
        except Exception:
            is_heavy = True
            logger.info(f"[HeavyTask] 5s超时 → 重型任务: {content[:40]}")
        
        if is_heavy:
            # 重型任务：秒回确认，后台继续跑
            ack = f"📋 收到「{content[:30]}…」，开始处理，完成后自动通知你。"
            await _save_conversation_pair(session_key, user_account_id, openid, content, ack)
            sem.release()
            # 后台跑完整超时的 Hermes 调用
            asyncio.create_task(_background_hermes_result(
                content=content, messages=messages,
                use_max_tokens=use_max_tokens,
                session_key=session_key, user_account_id=user_account_id,
                openid=openid, bot_id=bot_id, user_id=user_id,
            ))
            return ack
        
        # 5s 内有结果，走正常流程
        if reply is not None:
            # ── DeepSeek 拒绝检测 ──
            if _is_refusal(reply):
                logger.warning(f"[拒绝检测] DeepSeek 拒绝，降级: {reply[:60]}")
                sem.release()
                fb = await _try_kimi_fallback(messages, use_max_tokens)
                if not fb:
                    fb = await _try_doubao_fallback(messages, use_max_tokens)
                if fb:
                    await _save_conversation_pair(session_key, user_account_id, openid, content, fb)
                    return fb
                await sem.acquire()
            
            await _save_conversation_pair(session_key, user_account_id, openid, content, reply)
            sem.release()
            return reply
        else:
            # Probe got non-200, fall through to circuit breaker path
            pass
        
        # ── 熔断器保护下调用 Hermes API（完整超时）──
        try:
            resp = await _hermes_cb.call(
                client.post(
                    hermes_url,
                    json={
                        "model": "hermes-agent",
                        "messages": messages,
                        "max_tokens": use_max_tokens,
                        "stream": False,
                    },
                    headers=headers,
                ),
                timeout=cb_timeout,
            )
        except (asyncio.TimeoutError, httpx.ReadTimeout, httpx.TimeoutException, CircuitBreakerOpen) as cb_e:
            is_cb_open = isinstance(cb_e, CircuitBreakerOpen)
            logger.warning(f"[CB] Hermes 调用失败({type(cb_e).__name__}): {cb_e}")
            # Fallback chain: Kimi -> Doubao
            reply = await _try_kimi_fallback(messages, use_max_tokens)
            if not reply:
                reply = await _try_doubao_fallback(messages, use_max_tokens)
            sem.release()
            if reply:
                await _save_conversation_pair(session_key, user_account_id, openid, content, reply)
                return reply
            err_msg = "服务暂时不可用（AI 引擎忙），请稍后重试。"
            await _save_conversation_pair(session_key, user_account_id, openid, content, err_msg)
            return err_msg
        if resp.status_code == 200:
            data = resp.json()
            reply = data["choices"][0]["message"]["content"]
            
            # ── DeepSeek 拒绝检测 ──
            if _is_refusal(reply):
                logger.warning(f"[拒绝检测] DeepSeek 拒绝，降级: {reply[:60]}")
                sem.release()
                fb = await _try_kimi_fallback(messages, use_max_tokens)
                if not fb:
                    fb = await _try_doubao_fallback(messages, use_max_tokens)
                if fb:
                    await _save_conversation_pair(session_key, user_account_id, openid, content, fb)
                    return fb
                await sem.acquire()
            
            await _save_conversation_pair(session_key, user_account_id, openid, content, reply)
            sem.release()
            return reply
        else:
            logger.warning(f"Hermes API 非 200 状态: {resp.status_code}")
            # Fallback chain: Kimi -> Doubao
            reply = await _try_kimi_fallback(messages, use_max_tokens)'''

if old_hermes_block in code:
    code = code.replace(old_hermes_block, new_hermes_block, 1)
    print("  ✅ Hermes 调用块已替换（5s探针+后台）")
else:
    print("  ⚠️ 未找到匹配的旧 Hermes 块")
    # Try to find a partial match
    idx = code.find('        # 在熔断器保护下调用 Hermes API')
    if idx >= 0:
        print(f"    在位置 {idx} 找到，尝试精确匹配...")
    else:
        print("    ❌ 完全没找到，中止")
        exit(1)

# ── Step 2: Add background task function before _call_hermes ──

bg_task_func = '''
async def _background_hermes_result(content: str, messages: list, use_max_tokens: int,
                                    session_key: str, user_account_id: int, openid: str,
                                    bot_id: str, user_id: str) -> None:
    """重型任务后台完成 → 推送到用户"""
    import httpx as _hx
    hermes_url = "http://127.0.0.1:8642/v1/chat/completions"
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {os.getenv('HERMES_API_KEY', 'sk-xiakexia-external-server')}"}
    
    try:
        async with _hx.AsyncClient(timeout=300) as _hc:
            _r = await _hc.post(hermes_url, json={
                "model": "hermes-agent",
                "messages": messages,
                "max_tokens": use_max_tokens,
                "stream": False,
            }, headers=headers)
        
        if _r.status_code != 200:
            logger.warning(f"[BG] Hermes 后台返回 {_r.status_code}")
            return
        
        _d = _r.json()
        reply = _d["choices"][0]["message"]["content"]
        
        # 保存对话
        await _save_conversation_pair(session_key, user_account_id, openid, content, reply)
        
        # 通过 keepalive 推送结果到用户
        push_text = f"📋 **任务完成**\\n\\n{reply[:500]}"
        if len(reply) > 500:
            push_text += "\\n\\n（内容较长，可继续向我追问）"
        
        async with _hx.AsyncClient(timeout=10) as _hk:
            await _hk.post("http://127.0.0.1:9100/api/send", json={
                "bot_id": bot_id,
                "to_user": user_id,
                "text": push_text,
                "context_token": "",
            })
        logger.info(f"[BG] ✅ 重型任务完成，已推送到 {user_id[:16]}")
    except Exception as e:
        logger.error(f"[BG] 后台任务失败: {e}")
'''


# Insert before _call_hermes function
func_marker = "async def _call_hermes(content: str, user_id: str, user_nickname: str = \"\", openid: str = \"\", user_account_id: int = None, media_path: str = \"\", bot_id: str = \"\") -> str:"
if func_marker in code:
    code = code.replace(func_marker, bg_task_func + "\n\n" + func_marker, 1)
    print("  ✅ 后台任务函数 _background_hermes_result 已插入")
else:
    print("  ⚠️ 未找到 _call_hermes 函数定义")
    exit(1)

# ── Step 3: Verify syntax ──
with open("/tmp/_bg_verify.py", "w") as f:
    f.write(code)

import py_compile
try:
    py_compile.compile("/tmp/_bg_verify.py", doraise=True)
    print("  ✅ 语法验证通过")
except py_compile.PyCompileError as e:
    print(f"  ❌ 语法错误: {e}")
    print("  正在回滚...")
    with open(backup) as f:
        code = f.read()
    with open(path, "w") as f:
        f.write(code)
    print("  已回滚到备份")
    exit(1)

# ── Step 4: Write ──
with open(path, "w") as f:
    f.write(code)
print(f"  ✅ {path} 已更新")
