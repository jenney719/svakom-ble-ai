import asyncio
import math
import os
import time
import requests
from bleak import BleakScanner, BleakClient

WRITE_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

BRIDGE_URL = os.environ.get("BRIDGE_URL", "").rstrip("/")
BRIDGE_SECRET = os.environ.get("BRIDGE_SECRET", "")
PREFERRED_DEVICE_NAME = os.environ.get("BLE_DEVICE_NAME", "SL278H")

POLL_SEC = 0.3
KEEPALIVE_SEC = 1.5
FRAME_GAP_SEC = 0.3

client_ref = None
write_lock = asyncio.Lock()
channel_tasks = {}
active = {
    "vibrate": None,
    "auto": None,
    "patting": None,
    "suction": None,
    "thrust": None,
    "heat": None,
}

def log(s):
    print(s, flush=True)


def clamp(v, lo, hi):
    return max(lo, min(hi, int(v)))


def frame_03(mode, level):
    return bytes([0x55, 0x03, 0x00, 0x00, clamp(mode, 1, 10), clamp(level, 1, 10), 0x00])


def frame_03_stop():
    return bytes([0x55, 0x03, 0x00, 0x00, 0x00, 0x00, 0x00])


def frame_04(level):
    return bytes([0x55, 0x04, 0x00, 0x00, 0x01, clamp(level, 0, 255), 0xAA])


def frame_04_stop():
    return bytes([0x55, 0x04, 0x00, 0x00, 0x00, 0x00, 0xAA])


def frame_07(level):
    return bytes([0x55, 0x07, 0x00, 0x00, clamp(level, 1, 7), 0x00, 0x00])


def frame_07_stop():
    return bytes([0x55, 0x07, 0x00, 0x00, 0x00, 0x00, 0x00])


def frame_08(level):
    return bytes([0x55, 0x08, 0x00, 0x00, clamp(level, 1, 7), 0x00, 0x00])


def frame_08_stop():
    return bytes([0x55, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00])


def frame_09(mode, level):
    return bytes([0x55, 0x09, 0x00, 0x00, clamp(mode, 1, 5), clamp(level, 1, 10), 0x00])


def frame_09_stop():
    return bytes([0x55, 0x09, 0x00, 0x00, 0x00, 0x00, 0x00])


def frame_05(temp):
    return bytes([0x55, 0x05, 0x01, clamp(temp, 1, 55), 0x00, 0x00, 0x00])


def frame_05_stop():
    return bytes([0x55, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00])


async def write(buf, active_channel=None):
    async with write_lock:
        if active_channel and not active[active_channel]:
            return
        if client_ref and client_ref.is_connected:
            try:
                await client_ref.write_gatt_char(WRITE_UUID, buf, response=False)
                await asyncio.sleep(FRAME_GAP_SEC)
            except Exception as e:
                log(f"写入失败: {e}")


async def cancel_channel(channel, send_stop=True):
    task = channel_tasks.get(channel)
    if task:
        # 到时停止任务不能取消自己，否则停止帧可能在等待写锁时被取消。
        if task is not asyncio.current_task():
            task.cancel()
        channel_tasks[channel] = None

    active[channel] = None

    if send_stop:
        if channel == "vibrate":
            await write(frame_03_stop())
        elif channel == "auto":
            await write(frame_04_stop())
        elif channel == "patting":
            await write(frame_07_stop())
        elif channel == "suction":
            await write(frame_09_stop())
        elif channel == "thrust":
            await write(frame_08_stop())
        elif channel == "heat":
            await write(frame_05_stop())


async def timed_stop(channel, sec):
    try:
        await asyncio.sleep(float(sec))
        await cancel_channel(channel, send_stop=True)
        log(f"⏱ {channel} 到时自动停")
    except asyncio.CancelledError:
        pass


def set_timer(channel, sec):
    task = channel_tasks.get(channel)
    if task:
        task.cancel()

    channel_tasks[channel] = None

    if sec is not None:
        sec = float(sec)
        if sec > 0:
            channel_tasks[channel] = asyncio.create_task(timed_stop(channel, sec))


async def start_vibrate(mode, level, sec=None):
    # 0x03 与 0x04 共用一个马达，切换前显式停另一条
    await cancel_channel("auto", send_stop=True)

    active["vibrate"] = {
        "mode": mode,
        "level": level,
    }

    await write(frame_03(mode, level))
    set_timer("vibrate", sec)
    log(f"🌀 震动：模式 {mode} / 强度 {level}/10")


async def start_auto(level, sec=None):
    await cancel_channel("vibrate", send_stop=True)

    active["auto"] = {"level": level}

    await write(frame_04(level))
    set_timer("auto", sec)
    log(f"📳 自动模式：{round(level / 255 * 100)}%")


async def start_patting(level, sec=None):
    active["patting"] = {"level": level}
    await write(frame_07(level))
    set_timer("patting", sec)
    log(f"👏 拍打：{level}/7")


async def start_thrust(level, sec=None):
    active["thrust"] = {"level": level}
    await write(frame_08(level))
    set_timer("thrust", sec)
    log(f"↕ 伸缩：{level}/7")


async def start_suction(mode, level, sec=None):
    if not 1 <= mode <= 5 or not 0 <= level <= 10:
        raise ValueError("吮吸 mode 必须为 1–5，level 必须为 0–10")
    if sec is not None:
        sec = float(sec)
        if not math.isfinite(sec) or sec <= 0:
            raise ValueError("吮吸 sec 必须是正数")
    await cancel_channel("suction", send_stop=False)
    if level == 0:
        await write(frame_09_stop())
        log("⏹ 停止：suction")
        return
    active["suction"] = {"mode": mode, "level": level}
    await write(frame_09(mode, level))
    set_timer("suction", sec)
    log(f"💨 吮吸：模式 {mode} / 强度 {level}/10")


async def start_heat(temp, sec=None):
    # 加热必须有封顶计时
    if sec is None or float(sec) <= 0:
        log("⚠️ 加热必须提供 sec，已拒绝")
        return

    temp = clamp(temp, 1, 55)
    active["heat"] = {"temp": temp}

    await write(frame_05(temp))
    set_timer("heat", sec)
    log(f"🔥 温度：{temp}°C / {sec}秒")


async def stop_all():
    for ch in list(active.keys()):
        task = channel_tasks.get(ch)
        if task:
            task.cancel()
            channel_tasks[ch] = None
        active[ch] = None

    # 联动转发需要留出间隔，每个通道发送各自的停止帧。
    await write(frame_09_stop())
    await write(frame_03_stop())
    await write(frame_04_stop())
    await write(frame_07_stop())
    await write(frame_08_stop())
    await write(frame_05_stop())

    log("⏹ 全部停止")


async def exec_cmd(c):
    command = c.get("type")

    try:
        if command == "stop":
            channel = c.get("channel", "all")

            if channel == "all":
                await stop_all()
            elif channel in active:
                await cancel_channel(channel, send_stop=True)
                log(f"⏹ 停止：{channel}")
            return

        if command == "vibrate":
            await start_vibrate(
                int(c.get("mode", 1)),
                int(c.get("level", 1)),
                c.get("sec"),
            )
            return

        if command == "auto":
            await start_auto(
                int(c.get("level", 128)),
                c.get("sec"),
            )
            return

        if command == "patting":
            await start_patting(
                int(c.get("level", 1)),
                c.get("sec"),
            )
            return

        if command == "thrust":
            await start_thrust(
                int(c.get("level", 1)),
                c.get("sec"),
            )
            return

        if command == "suction":
            await start_suction(
                int(c.get("mode", 1)),
                int(c.get("level", 1)),
                c.get("sec"),
            )
            return

        if command == "heat":
            await start_heat(
                int(c.get("temp", 44)),
                c.get("sec"),
            )
            return

        log(f"⚠️ 未知指令：{c}")

    except Exception as e:
        log(f"执行失败：{e}")


def handle_notify(sender, data):
    raw = bytes(data)

    # 设备状态上报：55 FE CMD MODE LEVEL 00 00
    if len(raw) == 7 and raw[0] == 0x55 and raw[1] == 0xFE:
        log(f"📡 设备上报：{' '.join(f'{x:02X}' for x in raw)}")
    elif len(raw) == 7 and raw[:2] == b"\x55\x09":
        log(f"📡 吮吸指令回显：{raw.hex(' ')}")


async def bridge_loop():
    if not BRIDGE_URL:
        log("⚠️ 未设置 BRIDGE_URL")
        return

    headers = {"x-bridge-secret": BRIDGE_SECRET} if BRIDGE_SECRET else {}

    while True:
        if not client_ref or not client_ref.is_connected:
            await asyncio.sleep(POLL_SEC)
            continue
        try:
            # HTTP 轮询不能阻塞蓝牙通知、保活和定时停止。
            r = await asyncio.to_thread(
                requests.get,
                f"{BRIDGE_URL}/toy-next",
                headers=headers,
                timeout=4,
            )

            if r.ok:
                c = r.json()

                if c and c.get("type") != "hello" and len(c):
                    log(f"📨 收到：{c}")
                    await exec_cmd(c)

        except Exception:
            pass

        await asyncio.sleep(POLL_SEC)


async def keepalive_loop():
    while True:
        await asyncio.sleep(KEEPALIVE_SEC)

        if active["auto"]:
            a = active["auto"]
            await write(frame_04(a["level"]), active_channel="auto")
        if active["suction"]:
            a = active["suction"]
            await write(frame_09(a["mode"], a["level"]), active_channel="suction")


async def ble_loop():
    global client_ref

    while True:
        log(f"🔍 扫描 SL278 系列，优先连接 {PREFERRED_DEVICE_NAME} ...")

        try:
            devs = await BleakScanner.discover(timeout=8.0, return_adv=True)
        except Exception as e:
            log(f"扫描失败：{e}")
            await asyncio.sleep(5)
            continue

        # Windows 上名称可能只出现在广播里，不能只使用 d.name。
        candidates = [
            (d, a.local_name or d.name or "") for d, a in devs.values()
            if "SL278" in (a.local_name or d.name or "")
        ]
        candidates.sort(key=lambda item: (
            item[1] != PREFERRED_DEVICE_NAME,
            item[1] != "SL278H",
            item[1] != "SL278J",
        ))
        dev, device_name = candidates[0] if candidates else (None, "")

        if not dev:
            log("⚠️ 没找到设备，5秒后重试")
            await asyncio.sleep(5)
            continue

        log(f"🔗 连接 {device_name} ({dev.address}) ...")
        if device_name == "SL278J":
            log("ℹ️ 当前直连吮吸端；两端联动请先开机身，让桥接连接 SL278H")

        try:
            async with BleakClient(dev) as c:
                client_ref = c

                try:
                    await c.start_notify(NOTIFY_UUID, handle_notify)
                except Exception as e:
                    log(f"⚠️ 无法订阅 FFE2：{e}")

                log("🎉 就绪！等待指令中...")

                while c.is_connected:
                    await asyncio.sleep(1)

        except Exception as e:
            log(f"断开：{e}")

        client_ref = None
        for channel in active:
            await cancel_channel(channel, send_stop=False)
        await asyncio.sleep(2)


async def main():
    await asyncio.gather(
        bridge_loop(),
        ble_loop(),
        keepalive_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
