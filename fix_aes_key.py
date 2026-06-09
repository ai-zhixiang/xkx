with open("/home/ubuntu/weclaw-1/app/bot/unified_connector.py", "r") as f:
    content = f.read()

old_key_decode = """        aes_key = base64.b64decode(aes_key_b64)"""

new_key_decode = """        # 上传端编码: aeskey_hex = aes_key.hex() → base64.b64encode(aeskey_hex)
        # 所以下载端解码: base64 → ASCII hex string → bytes.fromhex
        aes_key_hex_b64 = base64.b64decode(aes_key_b64).decode("ascii")
        aes_key = bytes.fromhex(aes_key_hex_b64)
        log.info("密钥解码: len(b64)=%d hex=%s... key=%dB", len(aes_key_b64), aes_key_hex_b64[:16], len(aes_key))"""

content = content.replace(old_key_decode, new_key_decode)

with open("/home/ubuntu/weclaw-1/app/bot/unified_connector.py", "w") as f:
    f.write(content)

print("DONE")
