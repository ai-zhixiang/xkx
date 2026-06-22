"""
图片代理层：在 OpenClaw → Hermes 之间拦截图片请求
1. 接收 OpenClaw 的 vision 格式请求
2. 下载图片 base64，调 OpenRouter 识别
3. 替换 image_url 为文字描述
4. 转发给 Hermes (:8089)
"""

import os, base64, json
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

OPENROUTER_KEY = os.environ.get(
    OPENROUTER_API_KEY,
    os.getenv("DEEPSEEK_API_KEY")
)
VISION_MODEL = qwen/qwen2.5-vl-72b-instruct
HERMES_URL = http://127.0.0.1:8089/v1/chat/completions


async def _describe_image(b64_data: str) -> str:
    """调 OpenRouter 视觉模型识别图片"""
    try:
        payload = {
            model: VISION_MODEL,
            messages: [{
                role: user,
                content: [
                    {type: text, text: 用一句话简洁描述这张图片的内容，包括文字和关键视觉元素。如果含文字截图，提取关键信息。},
                    {type: image_url, image_url: {url: fdata:image/jpeg
