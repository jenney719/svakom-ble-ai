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
synced_generation = -1
batch_applying = False
server_instance = None
last_command_id = 0
connection_fault = None
connected_device_name = None
last_notification_at = 0.0
notification_count = 0
write_lock = asyncio.Lock()
flush_lock = asyncio.Lock()
channel_tasks = {}
active = {
    "vibrate": None,
    "auto": None,
    "patting": None,
    "suction": None,
    "thrust": None,
    "heat": None,
}
channel_revisions = {channel: 0 for channel in active}
dirty_channels = set()

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
    for channel in active:
        remember_channel(channel, None)


def invalidate_connection(expected_client):
    global client_ref, connection_generation, synced_generation
    if client_ref is expected_client and expected_client is not None:
        client_ref = None
        connection_generation += 1
        synced_generation = -1
        # 保留目标状态和原截止时间；重连并同步云端后再恢复。
        dirty_channels.update(active)
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


def valid_duration(sec):
    if sec is None:
        return None
    sec = float(sec)
    if not math.isfinite(sec) or sec <= 0:
        raise ValueError("sec 必须是有限正数")
    return sec


def remember_channel(channel, parameters, sec=None):
    duration = valid_duration(sec)
    previous = channel_tasks.get(channel)
    if previous and previous is not asyncio.current_task():
        previous.cancel()
    channel_tasks[channel] = None
    channel_revisions[channel] += 1
    dirty_channels.add(channel)
    if parameters is None:
        active[channel] = None
        return
    deadline = time.monotonic() + duration if duration is not None else None
    active[channel] = {**parameters, "deadline": deadline}
    if deadline is not None:
        channel_tasks[channel] = asyncio.create_task(
            timed_stop(channel, deadline, channel_revisions[channel]))


def expire_due_channels():
    now = time.monotonic()
    for channel, state in active.items():
        if state and state.get("deadline") is not None and state["deadline"] <= now:
            remember_channel(channel, None)
            log(f"⏱ {channel} 已到原截止时间，取消恢复")


def can_start_output():
    return bool(client_ref and client_ref.is_connected and not batch_applying
                and synced_generation == connection_generation)


def channel_frame(channel, state):
    if state is None:
        return {"vibrate": frame_03_stop, "auto": frame_04_stop,
                "patting": frame_07_stop, "thrust": frame_08_stop,
                "suction": frame_09_stop, "heat": frame_05_stop}[channel]()
    if channel == "vibrate":
        return frame_03(state["mode"], state["level"])
    if channel == "auto":
        return frame_04(state["level"])
    if channel == "patting":
        return frame_07(state["level"])
    if channel == "thrust":
        return frame_08(state["level"])
    if channel == "suction":
        return frame_09(state["mode"], state["level"])
    return frame_05(state["temp"])


async def write(buf, active_channel=None, expected_revision=None):
    async with write_lock:
        if active_channel:
            expire_due_channels()
            if (not can_start_output() or not active[active_channel]
                    or (expected_revision is not None
                        and channel_revisions[active_channel] != expected_revision)):
                return False
        transport = client_ref
        if not transport or not transport.is_connected:
            raise ConnectionError("BLE 未连接，指令状态已保留")
        try:
            await asyncio.wait_for(
                transport.write_gatt_char(WRITE_UUID, buf, response=False), WRITE_TIMEOUT_SEC)
            await asyncio.sleep(FRAME_GAP_SEC)
            return True
        except Exception as error:
            log(f"❌ BLE 写入失败 CMD=0x{buf[1]:02X}：{type(error).__name__}: {error!r}")
            invalidate_connection(transport)
            raise ConnectionError("BLE 写入失败，保留有效状态并请求重连") from error


async def flush_dirty_channels():
    if batch_applying:
        return
    async with flush_lock:
        while client_ref and client_ref.is_connected and not batch_applying:
            expire_due_channels()
            # 先发停止，再恢复动作；同步完成前绝不发启动帧。
            stopped = [ch for ch in active if ch in dirty_channels and active[ch] is None]
            running = ([ch for ch in active if ch in dirty_channels and active[ch] is not None]
                       if can_start_output() else [])
            pending = stopped + running
            if not pending:
                break
            for channel in pending:
                state = active[channel]
                revision = channel_revisions[channel]
                generation = connection_generation
                try:
                    sent = await write(channel_frame(channel, state),
                                       active_channel=channel if state else None,
                                       expected_revision=revision)
                except ConnectionError:
                    return
                if (sent and revision == channel_revisions[channel]
                        and generation == connection_generation):
                    dirty_channels.discard(channel)


async def cancel_channel(channel, send_stop=True):
    remember_channel(channel, None)
    if send_stop:
        await flush_dirty_channels()


async def timed_stop(channel, deadline, revision):
    try:
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        if channel_revisions[channel] != revision:
            return
        await cancel_channel(channel)
        log(f"⏱ {channel} 到时停止；离线时保留停止状态，重连后也不恢复")
    except asyncio.CancelledError:
        pass


async def start_vibrate(mode, level, sec=None):
    valid_duration(sec)
    remember_channel("auto", None)
    remember_channel("vibrate", {"mode": mode, "level": level}, sec)
    await flush_dirty_channels()
    log(f"🌀 震动目标：模式 {mode} / 强度 {level}/10")


async def start_auto(level, sec=None):
    valid_duration(sec)
    remember_channel("vibrate", None)
    remember_channel("auto", {"level": level}, sec) if level > 0 else remember_channel("auto", None)
    await flush_dirty_channels()
    log(f"📳 自动模式目标：{round(level / 255 * 100)}%")


async def start_patting(level, sec=None):
    remember_channel("patting", {"level": level}, sec)
    await flush_dirty_channels()
    log(f"👏 拍打目标：{level}/7")


async def start_thrust(level, sec=None):
    remember_channel("thrust", {"level": level}, sec)
    await flush_dirty_channels()
    log(f"↕ 伸缩目标：{level}/7")


async def start_suction(mode, level, sec=None):
    if not 1 <= mode <= 5 or not 0 <= level <= 10:
        raise ValueError("吮吸 mode 必须为 1–5，level 必须为 0–10")
    valid_duration(sec)
    remember_channel("suction", {"mode": mode, "level": level}, sec) if level else remember_channel("suction", None)
    await flush_dirty_channels()
    log(f"💨 吮吸目标：模式 {mode} / 强度 {level}/10")


async def start_heat(temp, sec=None):
    if sec is None:
        raise ValueError("加热必须提供 sec")
    remember_channel("heat", {"temp": clamp(temp, 1, 55)}, sec)
    await flush_dirty_channels()
    log(f"🔥 加热目标：{temp}°C / {sec}秒")


async def stop_all():
    clear_active_channels()
    await flush_dirty_channels()
    log("⏹ 全部停止；已取消所有待恢复动作")


async def exec_cmd(c):
    command = c.get("type")
    try:
        if command == "stop":
            channel = c.get("channel", "all")
            if channel == "all":
                await stop_all()
            elif channel in active:
                await cancel_channel(channel)
                log(f"⏹ 停止目标：{channel}")
            return
        if command == "vibrate":
            await start_vibrate(int(c.get("mode", 1)), int(c.get("level", 1)), c.get("sec"))
        elif command == "auto":
            await start_auto(int(c.get("level", 128)), c.get("sec"))
        elif command == "patting":
            await start_patting(int(c.get("level", 1)), c.get("sec"))
        elif command == "thrust":
            await start_thrust(int(c.get("level", 1)), c.get("sec"))
        elif command == "suction":
            await start_suction(int(c.get("mode", 1)), int(c.get("level", 1)), c.get("sec"))
        elif command == "heat":
            await start_heat(int(c.get("temp", 44)), c.get("sec"))
        else:
            log(f"⚠️ 未知指令：{c}")
    except Exception as error:
        log(f"执行失败：{type(error).__name__}: {error!r}")


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


async def apply_command_batch(batch, request_generation, request_started):
    global batch_applying, server_instance, last_command_id, synced_generation
    if (batch.get("type") != "batch" or not isinstance(batch.get("instanceId"), str)
            or not isinstance(batch.get("commands"), list)):
        raise ValueError("服务器尚不支持恢复同步；等待新版服务，不恢复旧动作")
    if server_instance != batch["instanceId"]:
        server_instance = batch["instanceId"]
        last_command_id = 0
    batch_applying = True
    try:
        for original in batch["commands"]:
            command_id = original.get("id", 0)
            if command_id <= last_command_id:
                continue
            command = dict(original)
            try:
                if command.get("sec") is not None and command.get("type") != "stop":
                    duration = valid_duration(command["sec"])
                    queue_age = max(0, (batch["serverTime"] - command.get("createdAt", batch["serverTime"])) / 1000)
                    remaining = duration - queue_age - max(0, time.monotonic() - request_started)
                    if remaining <= 0:
                        command = {"type": "stop", "channel": command["type"]}
                        log(f"⏱ 云端指令 {command_id} 已到期，只保留停止目标")
                    else:
                        command["sec"] = remaining
            except (TypeError, ValueError, OverflowError) as error:
                log(f"⚠️ 跳过无效指令 {command_id}：{error}；继续处理后续停止指令")
                last_command_id = command_id
                continue
            log(f"📨 更新目标：{command}")
            await exec_cmd(command)
            last_command_id = command_id
    finally:
        batch_applying = False
    if (client_ref and client_ref.is_connected
            and request_generation == connection_generation):
        if synced_generation != connection_generation:
            log("✅ 已同步云端最新停止/修改指令，恢复仍有效的动作；沿用原截止时间")
        synced_generation = connection_generation
    await flush_dirty_channels()


async def bridge_loop():
    if not BRIDGE_URL:
        log("⚠️ 未设置 BRIDGE_URL，无法同步和自动恢复动作")
        return
    headers = {"x-bridge-secret": BRIDGE_SECRET} if BRIDGE_SECRET else {}
    last_failure_log = 0.0
    cloud_failed = False
    while True:
        try:
            request_generation = connection_generation
            request_started = time.monotonic()
            params = {"batch": "1"}
            if server_instance is not None:
                params.update({"instance": server_instance, "ack": str(last_command_id)})
            # 断线时仍读取最新目标和停止；不发送动作帧。未确认的批次可重取。
            response = await asyncio.to_thread(requests.get,
                f"{BRIDGE_URL}/toy-next", headers=headers, params=params, timeout=4)
            response.raise_for_status()
            batch = response.json()
            if cloud_failed:
                log("✅ 云端轮询已恢复")
                cloud_failed = False
            await apply_command_batch(batch, request_generation, request_started)
        except Exception as error:
            now = time.monotonic()
            if not cloud_failed or now - last_failure_log >= 30:
                log(f"🌐 云端轮询/同步失败：{type(error).__name__}: {error!r}")
                last_failure_log = now
            cloud_failed = True
        await asyncio.sleep(POLL_SEC)


async def keepalive_loop():
    while True:
        await asyncio.sleep(KEEPALIVE_SEC)
        expire_due_channels()
        await flush_dirty_channels()
        if not can_start_output():
            continue
        try:
            for channel in ("auto", "suction"):
                state = active[channel]
                if state:
                    await write(channel_frame(channel, state), active_channel=channel,
                                expected_revision=channel_revisions[channel])
        except ConnectionError:
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
