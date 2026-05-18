"""
小龙虾AI平台 — 二维码生成
使用 qrcode 库生成带场景值的二维码
"""
import os
import qrcode
from io import BytesIO


QR_CODE_DIR = 'data/qrcodes'


def generate_tenant_qrcode(tenant_id: int, scene_id: int, base_url: str) -> tuple:
    """
    生成租户专属二维码
    
    当微信服务号就绪后，这里应调用微信API：
    POST https://api.weixin.qq.com/cgi-bin/qrcode/create?access_token=TOKEN
    获取 ticket → 组装二维码URL
    
    当前使用模拟方案：生成一个带场景值说明的本地二维码图片
    
    返回: (本地路径, 访问URL)
    """
    os.makedirs(QR_CODE_DIR, exist_ok=True)
    
    filename = f'tenant_{tenant_id}_scene_{scene_id}.png'
    filepath = os.path.join(QR_CODE_DIR, filename)
    file_url = f'{base_url}/api/admin/qrcode/{tenant_id}'
    
    # 二维码内容：模拟场景值（真实对接后改为微信二维码URL）
    qr_content = f'wechat://scan?scene={scene_id}&tenant={tenant_id}'
    
    qr = qrcode.QRCode(
        version=2,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=2,
    )
    qr.add_data(qr_content)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="#2D7F9A", back_color="white")
    img.save(filepath)
    
    print(f"[二维码] 已生成: {filepath} (场景值={scene_id})")
    
    return filepath, file_url
