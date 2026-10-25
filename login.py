"""
用 B站 App 扫码登录【小号】。

登录态保存在 cred.json —— 它等于这个账号的钥匙：别发给任何人，别传到网上。
想退出登录，直接删掉 cred.json 即可。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from bilibili_api import Credential, login_v2, request_log, user

from bili import CRED_FILE, save_credential

HERE = Path(__file__).resolve().parent
LOGIN_COOKIES = ("SESSDATA", "bili_jct", "DedeUserID")


async def main() -> int:
    # bilibili-api 17.4 只从扫码轮询结果的 url 参数里解析登录 cookie，B站 现在只用 Set-Cookie 下发，库解析出来是空的。
    # 这里监听库公开的 request_log 响应事件（同步回调，check_state 返回前就已执行），记下轮询响应里的 cookie 作补充。
    polled: dict[str, str] = {}

    def grab(_: str, data: dict) -> None:
        if "/qrcode/poll" in str(data.get("url", "")):
            polled.update({k: v for k, v in (data.get("cookies") or {}).items() if k in LOGIN_COOKIES and v})

    request_log.add_event_listener("RESPONSE", grab)

    qr = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
    await qr.generate_qrcode()
    png = HERE / "login_qr.png"
    qr.get_qrcode_picture().to_file(str(png))
    print(qr.get_qrcode_terminal())
    print("请用【小号】的 B站 App 扫上面的二维码。")
    print(f"终端里的二维码显示不全的话，打开这张图片扫：{png}\n")

    events = login_v2.QrCodeLoginEvents
    msg = {events.SCAN: "等待扫码……", events.CONF: "已扫码，请在手机上点确认……",
           events.TIMEOUT: "二维码过期了，重新运行 python login.py", events.DONE: "确认成功"}
    last = None
    while True:
        await asyncio.sleep(2)
        state = await qr.check_state()
        if state != last:
            print(msg.get(state, str(state)))
            last = state
        if state == events.TIMEOUT:
            return 1
        if state == events.DONE:
            break

    try:
        png.unlink()
    except OSError:
        pass
    got = qr.get_credential()
    cred = Credential(sessdata=got.sessdata or polled.get("SESSDATA"), bili_jct=got.bili_jct or polled.get("bili_jct"),
                      dedeuserid=got.dedeuserid or polled.get("DedeUserID"), ac_time_value=got.ac_time_value)
    missing = [k for k, v in zip(LOGIN_COOKIES, (cred.sessdata, cred.bili_jct, cred.dedeuserid)) if not v]
    if missing:  # 不齐就不保存，免得留下一个看着像登录了、其实用不了的 cred.json
        print(f"\n手机上确认成功了，但没拿到登录 cookie：{'、'.join(missing)}。{CRED_FILE.name} 没有改动。")
        print("多半是 B站 又改了登录接口，需要更新 login.py。")
        return 1
    save_credential(cred)
    try:
        me = await user.get_self_info(cred)
    except Exception as e:
        print(f"\n登录态已保存到 {CRED_FILE.name}，但查询账号信息失败：{str(e)[:200]}")
        print("过一会儿运行 python surf.py --check 再确认一次。")
        return 1
    print(f"\n已登录：{me.get('name')}（UID {me.get('mid')}）")
    print(f"登录态保存在 {CRED_FILE.name}，别外传。确认这是你的小号；不是的话删掉 {CRED_FILE.name} 重新登录。")
    return 0


if __name__ == "__main__":
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.exit(asyncio.run(main()))
