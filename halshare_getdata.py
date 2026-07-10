"""
Halshare TM2101-SR 体表温センサー 診断・データ取得スクリプト
対応OS: macOS / Linux (Raspberry Pi)

依存: pip install bleak
"""

import asyncio
import csv
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

# ── UUID ──────────────────────────────────────────────────────────
SERVICE_UUID    = "61830845-385d-41e8-9ee5-a30b150b49e9"
WRITE_CHAR_UUID = "804cdb50-bac9-448b-8ae2-41e9750ef93a"
READ_CHAR_UUID  = "169bb1bb-ae80-4650-bf4b-afb79f38422a"

# ── 温度変換定数 ──────────────────────────────────────────────────
BASE_TEMP = 25.0
LSB       = 0.0625

# ── 設定 ──────────────────────────────────────────────────────────
DEVICE_NAME_PREFIX = "TM2101-SR"
SCAN_TIMEOUT       = 10.0
DATA_TIMEOUT       = 60.0


# ═══════════════════════════════════════════════════════════════════
# コマンド定義
# ═══════════════════════════════════════════════════════════════════

def cmd_getdata() -> bytes:
    return b"GETDATA\n"

def cmd_clrdata() -> bytes:
    return b"CLRDATA\n"

def cmd_setting(interval_min: int = 1) -> bytes:
    """
    SETTINGコマンド（ネイティブアプリの実装を再現）
    SETTING + interval + 0x00 + interval + 0xFF + 0xFF + 0xFF + 0xFF + \n
    """
    b = bytearray(b"SETTING")
    b.append(interval_min & 0xFF)   # measurementInterval
    b.append(0x00)
    b.append(interval_min & 0xFF)   # 同じ値を再度
    b.append(0xFF)                  # SETTING_TEMPERATURE (65535) LSB
    b.append(0xFF)                  # SETTING_TEMPERATURE (65535) MSB
    b.append(0xFF)                  # SETTING_TEMPERATURE_DIFFERENCE LSB
    b.append(0xFF)                  # SETTING_TEMPERATURE_DIFFERENCE MSB
    b.append(0x0A)                  # \n
    return bytes(b)


# ═══════════════════════════════════════════════════════════════════
# ユーティリティ
# ═══════════════════════════════════════════════════════════════════

def to_hex(data: bytes | bytearray) -> str:
    return " ".join(f"{b:02x}" for b in data)

def calc_temp(byte_val: int) -> float:
    return (byte_val & 0xFF) * LSB + BASE_TEMP

def triple_le(b0: int, b1: int, b2: int) -> int:
    return b0 | (b1 << 8) | (b2 << 16)

def is_last_frame(data: bytes | bytearray) -> bool:
    return (len(data) >= 9
            and data[0] == 0x45
            and data[1] == 0x4E
            and data[-1] == 0x0A)

def parse_last_frame(data: bytes | bytearray) -> tuple[int, int]:
    """byte[2..4]=secondsFromLastMeasurement, byte[5..7]=lastCounter"""
    seconds      = triple_le(data[2], data[3], data[4])
    last_counter = triple_le(data[5], data[6], data[7])
    return seconds, last_counter


# ═══════════════════════════════════════════════════════════════════
# スキャン
# ═══════════════════════════════════════════════════════════════════

def parse_manufacturer_data(adv_data) -> dict:
    """ManufacturerDataからバッテリー情報等をパース"""
    result = {"serial": None, "system_time": None, "battery_warning": None, "battery_str": "不明"}
    mfr = adv_data.manufacturer_data.get(2585, b'') if hasattr(adv_data, 'manufacturer_data') else b''
    if len(mfr) >= 22:
        result["serial"] = ''.join(f'{mfr[i]:02X}' for i in range(5, -1, -1))
        result["system_time"] = mfr[18] | (mfr[19] << 8) | (mfr[20] << 16)
        bw = (mfr[21] & 0x30) >> 4
        result["battery_warning"] = bw
        if bw == 0:
            result["battery_str"] = "✅ 正常"
        elif bw == 2:
            result["battery_str"] = "⚠️  低下"
        elif bw == 3:
            result["battery_str"] = "🔴 ほぼ切れ"
        else:
            result["battery_str"] = f"不明({bw})"
    return result


async def scan_for_device(name_prefix: str, timeout: float):
    print(f"[SCAN] {name_prefix} を探しています... ({timeout}秒)")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    matched = [(d, adv) for d, adv in devices.values()
               if d.name and d.name.startswith(name_prefix)]
    if not matched:
        print(f"[ERROR] '{name_prefix}' で始まるデバイスが見つかりませんでした")
        return None
    if len(matched) == 1:
        d, adv = matched[0]
        info = parse_manufacturer_data(adv)
        print(f"[SCAN] 発見: {d.name}  ({d.address})  battery={info['battery_str']}")
        if info['battery_warning'] and info['battery_warning'] >= 2:
            print(f"[WARN] バッテリー低下！データ取得できない可能性があります")
        return d
    print("[SCAN] 複数のデバイスが見つかりました:")
    for i, (d, adv) in enumerate(matched):
        info = parse_manufacturer_data(adv)
        print(f"  [{i}] {d.name}  ({d.address})  battery={info['battery_str']}")
    idx = int(input("番号を選んでください: "))
    return matched[idx][0]


# ═══════════════════════════════════════════════════════════════════
# 汎用コマンド送受信
# ═══════════════════════════════════════════════════════════════════

async def send_command_and_wait(
    client: BleakClient,
    command: bytes,
    label: str,
    timeout: float = DATA_TIMEOUT,
    stop_on_last_frame: bool = True,
    stop_on_ok: bool = False,
) -> list[bytes]:
    """
    コマンドを送信して受信フレームをすべて返す。
    stop_on_last_frame: ENラストフレームで終了
    stop_on_ok:        "OK\n" レスポンスで終了
    """
    frames: list[bytes] = []
    done_event = asyncio.Event()
    last_frame_found = [False]

    def on_notify(sender, data: bytearray):
        raw = bytes(data)
        hex_str = to_hex(raw)

        if len(raw) == 0:
            print(f"    [RX] 空フレーム")
            return

        print(f"    [RX] ({len(raw)}B): [{hex_str}]", end="")

        # OK\n
        if raw == b"OK\n":
            print("  ← OK")
            frames.append(raw)
            if stop_on_ok:
                done_event.set()
            return

        # ラストフレーム
        if is_last_frame(raw):
            sec, last_counter = parse_last_frame(raw)
            print(f"  ← ENラストフレーム 経過秒={sec}  lastCounter={last_counter}")
            if last_counter == 0:
                print(f"    [WARN] lastCounter=0 → センサにデータなし可能性あり")
            frames.append(raw)
            last_frame_found[0] = True
            if stop_on_last_frame:
                done_event.set()
            return

        # 偶数バイト → 温度データフレーム候補
        if len(raw) % 2 == 0:
            n = len(raw) // 2
            temps = [f"{calc_temp(raw[i*2+1]):.2f}°C" for i in range(min(n, 3))]
            suffix = "..." if n > 3 else ""
            print(f"  ← データフレーム ({n}件: {', '.join(temps)}{suffix})")
            frames.append(raw)
            # ラストフレームがすでに来ていればデータ完了
            if last_frame_found[0]:
                done_event.set()
            return

        # その他
        try:
            text = raw.decode("utf-8").strip()
            print(f"  ← テキスト: {text!r}")
        except Exception:
            print(f"  ← 不明")
        frames.append(raw)

    await client.start_notify(READ_CHAR_UUID, on_notify)

    hex_cmd = to_hex(command)
    try:
        text_repr = command.decode("utf-8").replace("\n", "\\n")
    except Exception:
        text_repr = hex_cmd
    print(f"  [TX] {label}: {text_repr}  [{hex_cmd}]")

    await client.write_gatt_char(WRITE_CHAR_UUID, command, response=True)

    try:
        await asyncio.wait_for(done_event.wait(), timeout=timeout)
        print(f"    [INFO] 受信完了")
    except asyncio.TimeoutError:
        print(f"    [WARN] タイムアウト ({timeout}秒)")

    try:
        await client.stop_notify(READ_CHAR_UUID)
    except Exception:
        pass
    return frames


# ═══════════════════════════════════════════════════════════════════
# データパース
# ═══════════════════════════════════════════════════════════════════

def parse_measurements(frames: list[bytes]) -> list[dict]:
    last = None
    data_frames = []
    for f in frames:
        if is_last_frame(f):
            last = f
        elif f != b"OK\n" and len(f) % 2 == 0:
            data_frames.append(f)

    if last is not None:
        seconds_from_last, _ = parse_last_frame(last)
        base_time = datetime.now(timezone.utc) - timedelta(seconds=seconds_from_last)
    else:
        base_time = datetime.now(timezone.utc)

    # 全フレームのデータを1つのリストに結合してから処理
    # （HTML版と同じロジック: 全体を後ろから1件ずつ時刻を逆算）
    all_pairs = []
    for frame in data_frames:
        n = len(frame) // 2
        for i in range(n):
            all_pairs.append((frame[i * 2], frame[i * 2 + 1]))  # (interval, tempByte)

    # 最新から逆算して古い順に並べる
    current_time = base_time
    result = []
    for interval_min, temp_byte in reversed(all_pairs):
        result.append({"datetime": current_time, "temperature": calc_temp(temp_byte), "interval": interval_min})
        current_time -= timedelta(minutes=interval_min)

    result.reverse()  # 古い順（昇順）に並べ直す
    return result


# ═══════════════════════════════════════════════════════════════════
# CSV保存
# ═══════════════════════════════════════════════════════════════════

def save_csv(measurements: list[dict], wearer_name: str = "test") -> Path:
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = Path(f"halshare_{ts}.csv")
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["halshareWearerName", "datetime", "temperature", "interval_min"])
        for m in measurements:
            dt_str = m["datetime"].astimezone().strftime("%Y/%m/%d %H:%M:%S")
            writer.writerow([wearer_name, dt_str, f"{m['temperature']:.4f}", m.get("interval", "")])
    return filename


# ═══════════════════════════════════════════════════════════════════
# インタラクティブメニュー
# ═══════════════════════════════════════════════════════════════════

MENU = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Halshare TM2101-SR データ取得
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  [1] GETDATA
  [2] SETTING(1分)
  [3] SETTING(5分)
  [q] 終了
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""

async def run_menu(client: BleakClient):
    all_measurements = []

    while True:
        print(MENU)
        choice = input("選択: ").strip().lower()

        if choice == "q":
            break

        elif choice == "1":
            print("\n── GETDATA ──")
            frames = await send_command_and_wait(
                client, cmd_getdata(), "GETDATA",
                stop_on_last_frame=True, stop_on_ok=False
            )
            measurements = parse_measurements(frames)
            if measurements:
                all_measurements = measurements
                _print_summary(measurements)
            else:
                print("  → データ0件")



        elif choice == "2":
            print("\n── SETTING(1分) ──")
            frames = await send_command_and_wait(
                client, cmd_setting(1), "SETTING",
                timeout=10, stop_on_last_frame=False, stop_on_ok=True
            )
            print(f"  受信: {[to_hex(f) for f in frames]}")

        elif choice == "3":
            print("\n── SETTING(5分) ──")
            frames = await send_command_and_wait(
                client, cmd_setting(5), "SETTING",
                timeout=10, stop_on_last_frame=False, stop_on_ok=True
            )
            print(f"  受信: {[to_hex(f) for f in frames]}")

        # elif choice == "4":  # CLRDATA
        #     print("\n── CLRDATA ──")
        #     frames = await send_command_and_wait(
        #         client, cmd_clrdata(), "CLRDATA",
        #         timeout=10, stop_on_last_frame=False, stop_on_ok=True
        #     )
        #     print(f"  受信フレーム: {[to_hex(f) for f in frames]}")



        else:
            print("  無効な選択です")

        # データがあればCSV保存を提案
        if all_measurements:
            save = input(f"\n{len(all_measurements)}件のデータをCSV保存しますか? [y/N]: ").strip().lower()
            if save == "y":
                path = save_csv(all_measurements)
                print(f"  → 保存: {path}")
            all_measurements = []


def _print_summary(measurements: list[dict]):
    temps = [m["temperature"] for m in measurements]
    print(f"\n  ✅ {len(measurements)}件取得")
    print(f"     最古: {measurements[0]['datetime'].astimezone().strftime('%Y/%m/%d %H:%M')}  {measurements[0]['temperature']:.2f}°C")
    print(f"     最新: {measurements[-1]['datetime'].astimezone().strftime('%Y/%m/%d %H:%M')}  {measurements[-1]['temperature']:.2f}°C")
    print(f"     平均: {sum(temps)/len(temps):.2f}°C  最高: {max(temps):.2f}°C  最低: {min(temps):.2f}°C")


# ═══════════════════════════════════════════════════════════════════
# メイン
# ═══════════════════════════════════════════════════════════════════

async def main():
    device = await scan_for_device(DEVICE_NAME_PREFIX, SCAN_TIMEOUT)
    if device is None:
        sys.exit(1)

    # 接続リトライ（macOSはスキャン直後に接続できないことがある）
    client = None
    for attempt in range(3):
        try:
            print(f"[BLE] 接続中: {device.name} ({device.address})  (試行 {attempt+1}/3)")
            client = BleakClient(device, timeout=20.0)
            await client.connect()
            print(f"[BLE] 接続成功")
            break
        except Exception as e:
            print(f"[BLE] 接続失敗: {e}")
            if attempt < 2:
                print(f"[BLE] 3秒後にリトライ...")
                await asyncio.sleep(3)
            else:
                print(f"[BLE] 接続できませんでした")
                sys.exit(1)

    try:
        # MTU交渉
        try:
            if hasattr(client._backend, '_acquire_mtu'):
                # Linux (BlueZ) の場合
                await client._backend._acquire_mtu()
                print(f"[BLE] MTU交渉完了: {client.mtu_size}")
            elif hasattr(client, 'request_mtu'):
                mtu = await client.request_mtu(512)
                print(f"[BLE] MTU交渉完了: {mtu}")
            else:
                print(f"[BLE] MTU: 自動 (現在={client.mtu_size})")
        except Exception as e:
            print(f"[BLE] MTU交渉スキップ: {e}")

        await run_menu(client)
    finally:
        if client and client.is_connected:
            await client.disconnect()
        print("[BLE] 切断しました")


if __name__ == "__main__":
    asyncio.run(main())