#!/usr/bin/env python3
"""生成二维码图片并等待扫码登录，结果写回 .credential.json"""
import asyncio, sys, os, json

CREDENTIAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".credential.json")
QR_IMAGE_FILE = "/tmp/bilibili_qr.png"

async def main():
    from bilibili_api.login_v2 import QrCodeLogin, QrCodeLoginEvents

    qr = QrCodeLogin()
    await qr.generate_qrcode()
    
    # 保存二维码图片
    pic = qr.get_qrcode_picture()
    pic.to_file(QR_IMAGE_FILE)
    
    # 输出信息给调用方
    info = {
        "status": "ready",
        "qr_image": QR_IMAGE_FILE,
        "qr_key": qr._QrCodeLogin__qr_key,
        "qr_link": qr._QrCodeLogin__qr_link,
    }
    print(json.dumps(info), flush=True)
    
    # 轮询扫码状态
    while True:
        event = await qr.check_state()
        if event == QrCodeLoginEvents.SCAN:
            print(json.dumps({"status": "scanned"}), flush=True)
        elif event == QrCodeLoginEvents.TIMEOUT:
            print(json.dumps({"status": "timeout", "error": "二维码已过期"}), flush=True)
            return 1
        elif event == QrCodeLoginEvents.DONE:
            cred_obj = qr.get_credential()
            cred = {
                "sessdata": cred_obj.sessdata,
                "bili_jct": cred_obj.bili_jct,
                "dedeuserid": cred_obj.dedeuserid,
                "ac_time_value": cred_obj.ac_time_value,
                "buvid3": cred_obj.buvid3,
                "buvid4": cred_obj.buvid4,
            }
            with open(CREDENTIAL_FILE, "w") as f:
                json.dump(cred, f)
            os.chmod(CREDENTIAL_FILE, 0o600)
            print(json.dumps({"status": "done", "credential_file": CREDENTIAL_FILE}), flush=True)
            return 0
        await asyncio.sleep(1.5)

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
