"""
享客虾 — 文档生成 API
支持：PDF / HTML 输出，返回下载链接
"""
import os
import uuid
import subprocess
from pathlib import Path
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

router = APIRouter()

OUTPUT_DIR = Path(os.getenv('XKX_OUTPUT_DIR', '/home/ubuntu/weclaw-1/app/static/output'))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

BASE_URL = os.getenv('BASE_URL', 'https://hai.pangoozn.com/xkx')


class GenerateRequest(BaseModel):
    title: str = '享客虾·文档'
    subtitle: str = ''        # 副标题
    content: str              # HTML 或 Markdown
    format: str = 'pdf'       # pdf | html
    author: str = '享客虾'


def markdown_to_html(title: str, subtitle: str, content: str, author: str) -> str:
    """简易 Markdown → HTML（不依赖外部库）"""
    lines = content.split('\n')
    html_lines = []
    in_list = False
    
    for line in lines:
        stripped = line.strip()
        
        # 标题
        if stripped.startswith('### '):
            html_lines.append(f'<h3>{stripped[4:]}</h3>')
            continue
        if stripped.startswith('## '):
            html_lines.append(f'<h2>{stripped[3:]}</h2>')
            continue
        if stripped.startswith('# '):
            html_lines.append(f'<h1>{stripped[2:]}</h1>')
            continue
        
        # 列表
        if stripped.startswith('- ') or stripped.startswith('* '):
            if not in_list:
                html_lines.append('<ul>')
                in_list = True
            html_lines.append(f'<li>{stripped[2:]}</li>')
            continue
        elif in_list:
            html_lines.append('</ul>')
            in_list = False
        
        # 加粗
        import re
        stripped = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', stripped)
        
        # 空行
        if not stripped:
            html_lines.append('<br>')
            continue
        
        html_lines.append(f'<p>{stripped}</p>')
    
    if in_list:
        html_lines.append('</ul>')
    
    body = '\n'.join(html_lines)
    
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 2.2cm 2cm 2cm 2cm; }}
  body {{
    font-family: 'WenQuanYi Micro Hei', 'Noto Sans CJK SC', 'SimSun', 'STSong', serif;
    font-size: 13px; line-height: 2.0; color: #2C2C2C;
    max-width: 680px; margin: 0 auto;
  }}
  /* ===== 封面区 ===== */
  .cover {{
    text-align: center; padding: 60px 0 40px; page-break-after: always;
  }}
  .cover h1 {{
    font-size: 28px; font-weight: 900; color: #8B0000; letter-spacing: 4px;
    margin: 0 0 8px;
  }}
  .cover .subtitle {{
    font-size: 14px; color: #666; margin: 0 0 30px;
  }}
  .cover .meta {{
    font-size: 11px; color: #999; letter-spacing: 2px;
  }}
  .cover .deco {{
    margin: 30px 0; color: #C0392B; font-size: 18px; letter-spacing: 8px;
  }}
  /* ===== 章节分隔 ===== */
  .divider {{
    text-align: center; margin: 30px 0; color: #C0392B; font-size: 16px;
    letter-spacing: 8px; page-break-after: avoid;
  }}
  /* ===== 标题 ===== */
  h1 {{ font-size: 22px; text-align: center; margin: 30px 0 20px; color: #8B0000; }}
  h2 {{
    font-size: 16px; margin: 28px 0 12px; color: #8B0000;
    border-left: 3px solid #C0392B; padding-left: 10px;
  }}
  h3 {{ font-size: 14px; margin: 18px 0 8px; color: #333; }}
  /* ===== 正文 ===== */
  p {{ margin: 8px 0; text-indent: 2em; text-align: justify; }}
  ul {{ margin: 8px 0 8px 2em; }}
  li {{ margin: 6px 0; }}
  strong {{ color: #8B0000; font-weight: 700; }}
  /* ===== 引用/金句 ===== */
  blockquote {{
    margin: 16px 0; padding: 12px 20px;
    background: #FFF8F0; border-left: 4px solid #C0392B;
    font-style: italic; color: #555;
  }}
  /* ===== 案例框 ===== */
  .case {{
    margin: 14px 0; padding: 10px 14px;
    background: #FAFAFA; border-radius: 4px;
    border: 1px solid #eee; font-size: 12px; color: #555;
  }}
  /* ===== 页脚 ===== */
  .footer {{
    margin-top: 40px; padding-top: 15px; border-top: 1px solid #ddd;
    text-align: center; font-size: 10px; color: #aaa;
  }}
  .footer .brand {{ color: #C0392B; font-weight: 600; }}
</style>
</head>
<body>

<!-- 封面 -->
<div class="cover">
  <div class="deco">◆ ◇ ◆</div>
  <h1>{title}</h1>
  <div class="subtitle">{subtitle}</div>
  <div class="deco">◆ ◇ ◆</div>
  <div class="meta">Pro 版 | {datetime.now().strftime('%Y年%m月%d日')} | @{author}</div>
</div>

<!-- 正文 -->
{body}

<!-- 分隔 -->
<div class="divider">◆ ◇ ◆</div>

<!-- 页脚 -->
<div class="footer">
  <p><span class="brand">AI x {author}</span> | 商业智慧笔记 | Pro 版 | {datetime.now().strftime('%Y年%m月%d日')}</p>
</div>
</body>
</html>"""


@router.post('/api/generate/pdf')
async def generate_pdf(req: GenerateRequest):
    """生成PDF文档，返回下载链接"""
    filename = f"{uuid.uuid4().hex[:10]}.pdf"
    html_path = OUTPUT_DIR / filename.replace('.pdf', '.html')
    pdf_path = OUTPUT_DIR / filename
    
    try:
        # 生成 HTML
        html = markdown_to_html(req.title, req.subtitle, req.content, req.author)
        html_path.write_text(html, encoding='utf-8')
        
        # HTML → PDF
        result = subprocess.run(
            ['wkhtmltopdf', '--encoding', 'UTF-8',
             '--page-size', 'A4', '--margin-top', '15mm',
             '--margin-bottom', '15mm', '--margin-left', '15mm',
             '--margin-right', '15mm',
             '--no-stop-slow-scripts', '--quiet',
             str(html_path), str(pdf_path)],
            capture_output=True, text=True, timeout=30
        )
        
        if result.returncode != 0 or not pdf_path.exists():
            # Fallback: 返回 HTML
            return {
                'ok': True,
                'format': 'html',
                'url': f'{BASE_URL}/static/output/{html_path.name}',
                'filename': html_path.name,
                'title': req.title,
            }
        
        # 清理 HTML
        html_path.unlink(missing_ok=True)
        
        return {
            'ok': True,
            'format': 'pdf',
            'url': f'{BASE_URL}/static/output/{filename}',
            'filename': filename,
            'title': req.title,
            'size': pdf_path.stat().st_size,
        }
    except Exception as e:
        raise HTTPException(500, f'生成失败: {e}')


@router.get('/api/generate/list')
async def list_documents():
    """列出已生成的文档"""
    docs = []
    for f in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.suffix in ('.pdf', '.html'):
            docs.append({
                'filename': f.name,
                'size': f.stat().st_size,
                'url': f'{BASE_URL}/static/output/{f.name}',
                'created': datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
            })
    return {'count': len(docs), 'docs': docs[:20]}
