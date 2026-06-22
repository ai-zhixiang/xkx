"""
享客虾 — 文件处理服务
支持：TXT/MD/PDF/DOCX/CSV/图片
"""
import os
import csv
import io
from pathlib import Path

UPLOAD_DIR = Path(os.getenv('UPLOAD_DIR', '/home/ubuntu/weclaw-1/data/uploads'))
MAX_TEXT_PREVIEW = 500

SUPPORTED_TYPES = {
    'txt':  'text/plain',
    'md':   'text/markdown',
    'pdf':  'application/pdf',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'csv':  'text/csv',
    'jpg':  'image/jpeg',
    'jpeg': 'image/jpeg',
    'png':  'image/png',
    'gif':  'image/gif',
    'webp': 'image/webp',
}

MAX_SIZES = {
    'text': 5 * 1024 * 1024,    # 5MB
    'pdf':  10 * 1024 * 1024,   # 10MB
    'docx': 10 * 1024 * 1024,
    'image': 5 * 1024 * 1024,
    'csv':  5 * 1024 * 1024,
}


def get_ext(filename: str) -> str:
    return Path(filename).suffix.lstrip('.').lower()


def get_category(ext: str) -> str:
    if ext in ('txt', 'md'):
        return 'text'
    if ext == 'pdf':
        return 'pdf'
    if ext == 'docx':
        return 'docx'
    if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
        return 'image'
    if ext == 'csv':
        return 'csv'
    return 'unknown'


def extract_text(file_path: str, ext: str) -> str:
    """提取文件中的文本内容"""
    cat = get_category(ext)

    if cat == 'text':
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read(MAX_TEXT_PREVIEW * 4)

    if cat == 'pdf':
        import fitz
        doc = fitz.open(file_path)
        text = ''
        for page in doc:
            text += page.get_text()
            if len(text) > MAX_TEXT_PREVIEW * 4:
                break
        doc.close()
        return text

    if cat == 'docx':
        import docx
        d = docx.Document(file_path)
        text = '\n'.join(p.text for p in d.paragraphs)
        return text[:MAX_TEXT_PREVIEW * 4]

    if cat == 'csv':
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            reader = csv.reader(f)
            rows = []
            for i, row in enumerate(reader):
                if i >= 100:
                    break
                rows.append(','.join(row))
            return '\n'.join(rows)

    if cat == 'image':
        return '[图片文件，AI 将在对话中读取]'

    return ''


def save_and_extract(openid: str, filename: str, content: bytes) -> dict:
    """保存上传文件并提取文本"""
    ext = get_ext(filename)
    cat = get_category(ext)

    if cat == 'unknown':
        return {'ok': False, 'error': f'不支持的文件格式 .{ext}'}

    # 大小检查
    max_size = MAX_SIZES.get(cat, 5 * 1024 * 1024)
    if len(content) > max_size:
        return {'ok': False, 'error': f'文件过大（最大 {max_size // (1024*1024)}MB）'}

    # 保存
    import time
    dir_path = UPLOAD_DIR / openid
    dir_path.mkdir(parents=True, exist_ok=True)
    safe_name = f"{int(time.time())}_{filename}"
    file_path = dir_path / safe_name
    file_path.write_bytes(content)

    # 提取文本
    raw_text = extract_text(str(file_path), ext)

    preview = raw_text[:MAX_TEXT_PREVIEW].strip()
    if len(raw_text) > MAX_TEXT_PREVIEW:
        preview += f'\n…（共 {len(raw_text)} 字，只显示前 {MAX_TEXT_PREVIEW} 字）'

    return {
        'ok': True,
        'file_name': filename,
        'file_type': ext,
        'file_size': len(content),
        'file_path': str(file_path),
        'extracted_text': raw_text,
        'content_preview': preview,
        'category': cat,
    }
