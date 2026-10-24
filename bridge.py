import asyncio
import math
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
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
WRITE_TIMEOUT_SEC = 5

client_ref = None
connection_generation = 0
connection_fault = None
connected_device_name = None
last_notification_at = 0.0
notification_count = 0
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

logger = logging.getLogger("svakom.bridge")


def setup_logging():
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("[%(asctime)s.%(msecs)03d] %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)
    try:
        handler = RotatingFileHandler(Path(__file__).with_suffix(".log"),
                                      maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError as error:
        logger.warning("无法保存日志：%s: %s", type(error).__name__, error)


def log(s):
    if logger.handlers:
        logger.info(s)
    else:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {s}", flush=True)


def clear_active_channels():
    current = asyncio.current_task()
    for channel in active:
        task = channel_tasks.get(channel)
        if task and task is not current:
            task.cancel()
        channel_tasks[channel] = None
        active[channel] = None


def invalidate_connection(expected_client):
    global client_ref, connection_generation
    if client_ref is expected_client and expected_client is not None:
        client_ref = None
        connection_generation += 1
        clear_active_channels()
        if connection_fault is not None:
            connection_fault.set()


def make_disconnect_callback(device_name, disconnected, session):
    # 每次连接捕获独立状态，迟到的旧回调不能唤醒或清理下一次连接。
    def callback(transport):
        disconnected.set()
        invalidate_connection(transport)
        if not session["closing"]:
            uptime = time.monotonic() - session["started_at"] if session["started_at"] is not None else 0
            age = (f"{time.monotonic() - last_notification_at:.1f}秒前"
                   if last_notification_at else "尚未收到")
            log(f"🔌 BLE 断开：{device_name}，在线 {uptime:.1f} 秒，最后通知 {age}；"
                "Windows 回调未提供底层断开原因")
    return callback


def choose_device(advertisements, pinned_name=None):
    candidates = [
        (device, adv.local_name or device.name or "", getattr(adv, "rssi", None))
        for device, adv in advertisements.values()
        if "SL278" in (adv.local_name or device.name or "")
    ]
    # 成功连接后锁定功能端名称；机身断线时不自动改连吮吸端。MAC 仍允许变化。
    if pinned_name:
        candidates = [item for item in candidates if item[1] == pinned_name]
    candidates.sort(key=lambda item: (
        item[1] != PREFERRED_DEVICE_NAME,
        item[1] != "SL278H",
        item[1] != "SL278J",
    ))
    return candidates[0] if candidates else (None, "", None)


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
        transport = client_ref
        if not transport or not transport.is_connected:
            raise ConnectionError("BLE 未连接，未发送指令")
        try:
            await asyncio.wait_for(
                transport.write_gatt_char(WRITE_UUID, buf, response=False), WRITE_TIMEOUT_SEC)
            await asyncio.sleep(FRAME_GAP_SEC)
        except Exception as error:
            log(f"❌ BLE 写入失败 CMD=0x{buf[1]:02X}：{type(error).__name__}: {error!r}")
            invalidate_connection(transport)
            raise ConnectionError("BLE 写入失败，已清理运行状态并请求重连") from error


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
    except ConnectionError as error:
        log(f"⚠️ {channel} 到时停止未送达：{error}；连接已转入重连流程")


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
        log(f"执行失败：{type(e).__name__}: {e!r}")


def handle_notify(sender, data):
    global last_notification_at, notification_count
    last_notification_at = time.monotonic()
    notification_count += 1
    raw = bytes(data)

    # 设备状态上报：55 FE CMD MODE LEVEL 00 00
    if len(raw) == 7 and raw[0] == 0x55 and raw[1] == 0xFE:
        log(f"📡 设备上报：{' '.join(f'{x:02X}' for x in raw)}")
    elif len(raw) == 7 and raw[:2] == b"\x55\x09":
        log(f"📡 吮吸指令回显：{raw.hex(' ')}")


async def dispatch_polled_command(command, polled_client, polled_generation):
    if not command or command.get("type") == "hello":
        return
    if (client_ref is not polled_client or connection_generation != polled_generation
            or not client_ref or not client_ref.is_connected):
        log(f"⚠️ 丢弃断线前返回的旧指令：id={command.get('id')} type={command.get('type')}")
        return
    log(f"📨 收到：{command}")
    await exec_cmd(command)


async def bridge_loop():
    if not BRIDGE_URL:
        log("⚠️ 未设置 BRIDGE_URL")
        return

    headers = {"x-bridge-secret": BRIDGE_SECRET} if BRIDGE_SECRET else {}
    last_failure_log = 0.0
    cloud_failed = False

    while True:
        if not client_ref or not client_ref.is_connected:
            await asyncio.sleep(POLL_SEC)
            continue
        try:
            polled_client = client_ref
            polled_generation = connection_generation
            # HTTP 轮询不能阻塞蓝牙通知、保活和定时停止。
            r = await asyncio.to_thread(
                requests.get,
                f"{BRIDGE_URL}/toy-next",
                headers=headers,
                timeout=4,
            )

            r.raise_for_status()
            c = r.json()
            if cloud_failed:
                log("✅ 云端轮询已恢复")
                cloud_failed = False
            await dispatch_polled_command(c, polled_client, polled_generation)

        except Exception as error:
            now = time.monotonic()
            if not cloud_failed or now - last_failure_log >= 30:
                log(f"🌐 云端轮询失败（独立于 BLE 断线）：{type(error).__name__}: {error!r}")
                last_failure_log = now
            cloud_failed = True

        await asyncio.sleep(POLL_SEC)


async def keepalive_loop():
    while True:
        await asyncio.sleep(KEEPALIVE_SEC)

        try:
            if active["auto"]:
                a = active["auto"]
                await write(frame_04(a["level"]), active_channel="auto")
            if active["suction"]:
                a = active["suction"]
                await write(frame_09(a["mode"], a["level"]), active_channel="suction")
        except ConnectionError:
            # write 已记录原因并通知连接循环；保活任务不能因此退出。
            pass


async def ble_loop():
    global client_ref, connection_generation, connection_fault
    global connected_device_name, last_notification_at, notification_count
    pinned_name = None

    while True:
        log(f"🔍 扫描 SL278 系列，目标 {pinned_name or PREFERRED_DEVICE_NAME} ...")

        try:
            devs = await BleakScanner.discover(timeout=8.0, return_adv=True)
        except Exception as e:
            log(f"扫描失败：{type(e).__name__}: {e!r}")
            await asyncio.sleep(5)
            continue

        dev, device_name, rssi = choose_device(devs, pinned_name)

        if not dev:
            log(f"⚠️ 没找到目标 {pinned_name or PREFERRED_DEVICE_NAME}，5秒后重试")
            await asyncio.sleep(5)
            continue

        log(f"🔗 连接 {device_name} ({dev.address})，扫描信号 RSSI={rssi} dBm ...")
        if device_name == "SL278J":
            log("ℹ️ 当前直连吮吸端；两端联动请先开机身，让桥接连接 SL278H")

        disconnected = asyncio.Event()
        started_at = None
        session = {"started_at": None, "closing": False}
        c = None
        on_disconnect = make_disconnect_callback(device_name, disconnected, session)

        try:
            async with BleakClient(dev, disconnected_callback=on_disconnect, timeout=20) as c:
                # 订阅成功后才允许云端指令进入，避免连接初始化阶段写入。
                last_notification_at = 0.0
                notification_count = 0
                await asyncio.wait_for(c.start_notify(NOTIFY_UUID, handle_notify), timeout=10)
                if not c.is_connected or disconnected.is_set():
                    raise ConnectionError("订阅通知期间 BLE 已断开")
                pinned_name = device_name
                connected_device_name = device_name
                started_at = time.monotonic()
                session["started_at"] = started_at
                connection_fault = disconnected
                connection_generation += 1
                client_ref = c
                log(f"🎉 就绪！连接代次 {connection_generation}，等待指令中...")

                try:
                    while c.is_connected and not disconnected.is_set():
                        try:
                            await asyncio.wait_for(disconnected.wait(), timeout=30)
                        except asyncio.TimeoutError:
                            age = (f"{time.monotonic() - last_notification_at:.1f}秒"
                                   if last_notification_at else "尚未收到")
                            log(f"💓 BLE 状态：{device_name}，在线 {time.monotonic()-started_at:.0f} 秒，"
                                f"通知 {notification_count} 条，最后通知间隔 {age}")
                finally:
                    session["closing"] = True
                    invalidate_connection(c)
        except Exception as error:
            log(f"连接结束/失败：{type(error).__name__}: {error!r}")
        finally:
            if c is not None:
                invalidate_connection(c)
            if connection_fault is disconnected:
                connection_fault = None
                connected_device_name = None
        await asyncio.sleep(2)


async def main():
    await asyncio.gather(
        bridge_loop(),
        ble_loop(),
        keepalive_loop(),
    )


def acquire_instance_lock():
    handle = Path(__file__).with_suffix(".lock").open("a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        raise


if __name__ == "__main__":
    try:
        instance_lock = acquire_instance_lock()
    except OSError as error:
        log(f"无法启动第二个桥接或获取锁：{type(error).__name__}: {error!r}；请检查是否已有桥接窗口")
        raise SystemExit(1)
    setup_logging()
    log(f"桥接启动 PID={os.getpid()}；日志位置：{Path(__file__).with_suffix('.log')}")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("用户关闭桥接")
    finally:
        instance_lock.close()
