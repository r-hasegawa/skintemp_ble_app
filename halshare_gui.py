"""
Halshare TM2101-SR GUI アプリ
依存: pip install bleak customtkinter
"""

import asyncio
import csv
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import customtkinter as ctk

# ── BLE UUIDs ─────────────────────────────────────────────────────
SERVICE_UUID    = "61830845-385d-41e8-9ee5-a30b150b49e9"
WRITE_CHAR_UUID = "804cdb50-bac9-448b-8ae2-41e9750ef93a"
READ_CHAR_UUID  = "169bb1bb-ae80-4650-bf4b-afb79f38422a"
DEVICE_NAME_PREFIX = "TM2101-SR"

BASE_TEMP = 25.0
LSB       = 0.0625

# ── CSV保存先 ──────────────────────────────────────────────────────
def get_export_dir() -> Path:
    """
    .py実行時 → スクリプトと同じ階層の exportdata/
    .app/.exe時 → 実行ファイルと同じ階層の exportdata/
    """
    if getattr(sys, 'frozen', False):
        # pyinstaller でアプリ化されている場合
        base = Path(sys.executable).parent
        # macOSの .app の場合: Contents/MacOS/ の3つ上
        if sys.platform == 'darwin' and base.name == 'MacOS':
            base = base.parent.parent.parent
    else:
        base = Path(__file__).parent
    export_dir = base / "exportdata"
    export_dir.mkdir(exist_ok=True)
    return export_dir


# ═══════════════════════════════════════════════════════════════════
# BLE ユーティリティ
# ═══════════════════════════════════════════════════════════════════

def calc_temp(byte_val: int) -> float:
    return (byte_val & 0xFF) * LSB + BASE_TEMP

def triple_le(b0, b1, b2):
    return b0 | (b1 << 8) | (b2 << 16)

def is_last_frame(data: bytes) -> bool:
    return len(data) >= 9 and data[0] == 0x45 and data[1] == 0x4E and data[-1] == 0x0A

def parse_last_frame(data: bytes):
    return triple_le(data[2], data[3], data[4]), triple_le(data[5], data[6], data[7])

def to_hex(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)

def parse_manufacturer_data(adv_data) -> dict:
    result = {"serial": None, "battery_warning": None, "battery_str": "不明"}
    mfr = adv_data.manufacturer_data.get(2585, b'') if hasattr(adv_data, 'manufacturer_data') else b''
    if len(mfr) >= 22:
        result["serial"] = ''.join(f'{mfr[i]:02X}' for i in range(5, -1, -1))
        bw = (mfr[21] & 0x30) >> 4
        result["battery_warning"] = bw
        result["battery_str"] = {0: "✅ 正常", 2: "⚠️ 低下", 3: "🔴 ほぼ切れ"}.get(bw, "不明")
    return result

def parse_measurements(frames: list) -> list:
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
    all_pairs = []
    for frame in data_frames:
        n = len(frame) // 2
        for i in range(n):
            all_pairs.append((frame[i*2], frame[i*2+1]))
    current_time = base_time
    result = []
    for interval_min, temp_byte in reversed(all_pairs):
        result.append({"datetime": current_time, "temperature": calc_temp(temp_byte), "interval": interval_min})
        current_time -= timedelta(minutes=interval_min)
    result.reverse()
    return result

def save_csv(measurements: list, serial: str, wearer_name: str = "") -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    serial_str = serial or "unknown"
    filename = get_export_dir() / f"halshare_{serial_str}_{ts}.csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        f.write('"halshareWearerName", "halshareId", "datetime", "temperature"\n')
        for m in measurements:
            dt_str = m["datetime"].astimezone().strftime("%Y/%m/%d %H:%M:%S")
            temp = f"{m['temperature']:.4f}"
            f.write(f'"{wearer_name}", "{serial_str}", "{dt_str}", {temp}\n')
    return filename


# ═══════════════════════════════════════════════════════════════════
# BLE 非同期マネージャ
# ═══════════════════════════════════════════════════════════════════

class BLEManager:
    def __init__(self, log_callback):
        self.log = log_callback
        self.client = None
        self.serial = None
        self._loop = None
        self._thread = None
        self._scan_task = None
        self._scanning = False
        self._start_loop()

    def _start_loop(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    # ── スキャン ──
    def start_scan(self, on_found):
        self._scanning = True
        self.run(self._scan_loop(on_found))

    def stop_scan(self):
        self._scanning = False

    async def _scan_loop(self, on_found):
        import time
        from bleak import BleakScanner
        seen = {}      # serial -> (device, info, last_seen_time)
        KEEP_SEC = 10  # 最後に見えてからこの秒数はリストに残す
        while self._scanning:
            try:
                devices = await BleakScanner.discover(timeout=3.0, return_adv=True)
                now = time.time()
                for d, adv in devices.values():
                    if d.name and d.name.startswith(DEVICE_NAME_PREFIX):
                        info = parse_manufacturer_data(adv)
                        serial = info['serial'] or d.address
                        seen[serial] = (d, info, now)
                # KEEP_SEC秒以内に見えたものだけ残す
                seen = {s: v for s, v in seen.items() if now - v[2] < KEEP_SEC}
                on_found([(d, info) for d, info, _ in seen.values()])
            except Exception as e:
                self.log(f"[SCAN] エラー: {e}")

    # ── 接続 ──
    async def _connect(self, device):
        from bleak import BleakClient
        for attempt in range(3):
            try:
                self.log(f"[BLE] 接続中... (試行 {attempt+1}/3)")
                self.client = BleakClient(device, timeout=20.0)
                await self.client.connect()
                self.log("[BLE] 接続成功")
                # MTU交渉
                try:
                    if hasattr(self.client._backend, '_acquire_mtu'):
                        await self.client._backend._acquire_mtu()
                        self.log(f"[BLE] MTU: {self.client.mtu_size}")
                    else:
                        self.log(f"[BLE] MTU: {self.client.mtu_size} (自動)")
                except Exception:
                    pass
                return True
            except Exception as e:
                self.log(f"[BLE] 接続失敗: {e}")
                if attempt < 2:
                    await asyncio.sleep(3)
        return False

    def connect(self, device, serial, on_success, on_fail):
        async def _do():
            ok = await self._connect(device)
            if ok:
                self.serial = serial
                on_success()
            else:
                self.client = None
                on_fail()
        self.run(_do())

    # ── 切断 ──
    def disconnect(self, on_done):
        async def _do():
            try:
                if self.client and self.client.is_connected:
                    await self.client.disconnect()
            except Exception:
                pass
            self.client = None
            self.serial = None
            self.log("[BLE] 切断しました")
            on_done()
        self.run(_do())

    # ── GETDATA ──
    def getdata(self, on_done):
        async def _do():
            frames = []
            done = asyncio.Event()
            last_found = [False]

            def on_notify(_, data: bytearray):
                raw = bytes(data)
                if len(raw) == 0:
                    self.log("[RX] 空フレーム")
                    return
                if raw == b"OK\n":
                    self.log("[RX] OK")
                    frames.append(raw)
                    return
                if is_last_frame(raw):
                    sec, lc = parse_last_frame(raw)
                    self.log(f"[RX] ENラストフレーム 経過秒={sec} lastCounter={lc}")
                    frames.append(raw)
                    last_found[0] = True
                    done.set()
                    return
                if len(raw) % 2 == 0:
                    n = len(raw) // 2
                    self.log(f"[RX] データフレーム {n}件")
                    frames.append(raw)

            try:
                await self.client.start_notify(READ_CHAR_UUID, on_notify)
                self.log("[TX] GETDATA 送信")
                await self.client.write_gatt_char(WRITE_CHAR_UUID, b"GETDATA\n", response=True)
                await asyncio.wait_for(done.wait(), timeout=60.0)
                await self.client.stop_notify(READ_CHAR_UUID)
                measurements = parse_measurements(frames)
                self.log(f"[完了] {len(measurements)}件取得")
                on_done(measurements)
            except asyncio.TimeoutError:
                self.log("[WARN] タイムアウト")
                on_done([])
            except Exception as e:
                self.log(f"[ERROR] GETDATA失敗: {e}")
                on_done([])
        self.run(_do())

    # ── SETTING ──
    def setting(self, interval_min: int, on_done):
        async def _do():
            b = bytearray(b"SETTING")
            b += bytes([interval_min, 0x00, interval_min, 0xFF, 0xFF, 0xFF, 0xFF, 0x0A])
            done = asyncio.Event()
            def on_notify(_, data: bytearray):
                raw = bytes(data)
                if raw == b"OK\n":
                    self.log(f"[RX] OK → SETTING({interval_min}分) 完了")
                    done.set()
                else:
                    self.log(f"[RX] {to_hex(raw)}")
            try:
                await self.client.start_notify(READ_CHAR_UUID, on_notify)
                self.log(f"[TX] SETTING({interval_min}分) 送信")
                await self.client.write_gatt_char(WRITE_CHAR_UUID, bytes(b), response=True)
                await asyncio.wait_for(done.wait(), timeout=10.0)
                await self.client.stop_notify(READ_CHAR_UUID)
                on_done(True)
            except Exception as e:
                self.log(f"[ERROR] SETTING失敗: {e}")
                on_done(False)
        self.run(_do())


# ═══════════════════════════════════════════════════════════════════
# GUI
# ═══════════════════════════════════════════════════════════════════

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

FONT_TITLE  = ("Helvetica", 15, "bold")
FONT_BODY   = ("Helvetica", 13)
FONT_MONO   = ("Courier", 11)
FONT_SMALL  = ("Helvetica", 11)

COLOR_BG    = "#1a1a2e"
COLOR_PANEL = "#16213e"
COLOR_CARD  = "#0f3460"
COLOR_ACCENT= "#e94560"
COLOR_OK    = "#4caf50"
COLOR_WARN  = "#ff9800"
COLOR_TEXT  = "#e0e0e0"
COLOR_MUTED = "#888888"


class HalshareApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Halshare TM2101-SR")
        self.geometry("700x600")
        self.minsize(600, 500)
        self.configure(fg_color=COLOR_BG)

        self._scan_items = []   # [(device, info), ...]
        self._selected_idx = None
        self._measurements = []
        self._ble = BLEManager(self._append_log)

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI構築 ────────────────────────────────────────────────────

    def _build_ui(self):
        # メインフレーム（上：コンテンツ、下：ログ）
        self._content = ctk.CTkFrame(self, fg_color="transparent")
        self._content.pack(fill="both", expand=True, padx=12, pady=(12, 4))

        self._log_frame = self._make_log_panel()
        self._log_frame.pack(fill="x", padx=12, pady=(0, 8))

        # 各画面
        self._scan_frame    = self._make_scan_screen()
        self._connect_frame = self._make_connect_screen()
        self._setting_frame = self._make_setting_screen()

        self._show_screen("scan")

    def _make_log_panel(self):
        frame = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=8)
        ctk.CTkLabel(frame, text="ログ", font=FONT_SMALL, text_color=COLOR_MUTED).pack(anchor="w", padx=8, pady=(4,0))
        self._log_box = ctk.CTkTextbox(frame, height=100, font=FONT_MONO,
                                        fg_color=COLOR_BG, text_color="#aaffaa",
                                        wrap="word", state="disabled")
        self._log_box.pack(fill="x", padx=8, pady=(0,6))
        return frame

    def _make_scan_screen(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")

        # タイトル
        ctk.CTkLabel(frame, text="Halshare TM2101-SR", font=FONT_TITLE,
                     text_color=COLOR_TEXT).pack(pady=(0,8))

        # スキャンボタン
        self._scan_btn = ctk.CTkButton(frame, text="📡 スキャン開始", font=FONT_BODY,
                                        fg_color=COLOR_ACCENT, hover_color="#c73652",
                                        command=self._toggle_scan)
        self._scan_btn.pack(fill="x", pady=(0,8))

        # センサ一覧
        ctk.CTkLabel(frame, text="センサ一覧", font=FONT_SMALL, text_color=COLOR_MUTED).pack(anchor="w")
        self._sensor_list = ctk.CTkScrollableFrame(frame, fg_color=COLOR_PANEL,
                                                    corner_radius=8, height=200)
        self._sensor_list.pack(fill="both", expand=True, pady=(2,8))

        # ペアリングボタン
        self._pair_btn = ctk.CTkButton(frame, text="🔗 ペアリング", font=FONT_BODY,
                                        state="disabled", command=self._do_pair)
        self._pair_btn.pack(fill="x")

        return frame

    def _make_connect_screen(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")

        # シリアル番号表示
        self._serial_label = ctk.CTkLabel(frame, text="", font=FONT_TITLE, text_color=COLOR_ACCENT)
        self._serial_label.pack(pady=(0,8))

        # GETDATAボタン
        self._getdata_btn = ctk.CTkButton(frame, text="📊 GETDATA", font=FONT_BODY,
                                           fg_color=COLOR_OK, hover_color="#388e3c",
                                           command=self._do_getdata)
        self._getdata_btn.pack(fill="x", pady=(0,8))

        # 結果表示
        self._result_frame = ctk.CTkFrame(frame, fg_color=COLOR_PANEL, corner_radius=8)
        self._result_frame.pack(fill="both", expand=True, pady=(0,8))
        self._result_label = ctk.CTkLabel(self._result_frame, text="データなし",
                                           font=FONT_BODY, text_color=COLOR_MUTED,
                                           justify="left")
        self._result_label.pack(expand=True, padx=12, pady=12, anchor="w")
        self._save_btn = ctk.CTkButton(self._result_frame, text="💾 CSV保存", font=FONT_SMALL,
                                        width=120, state="disabled", command=self._do_save)
        self._save_btn.pack(anchor="e", padx=12, pady=(0,8))

        # 下部ボタン
        bottom = ctk.CTkFrame(frame, fg_color="transparent")
        bottom.pack(fill="x")
        ctk.CTkButton(bottom, text="⚙️ 設定", font=FONT_BODY, width=120,
                      fg_color=COLOR_CARD, command=lambda: self._show_screen("setting")
                      ).pack(side="left")
        ctk.CTkButton(bottom, text="🔌 切断", font=FONT_BODY, width=120,
                      fg_color="#555555", hover_color="#333333",
                      command=self._do_disconnect).pack(side="right")

        return frame

    def _make_setting_screen(self):
        frame = ctk.CTkFrame(self._content, fg_color="transparent")

        ctk.CTkLabel(frame, text="測定間隔設定", font=FONT_TITLE, text_color=COLOR_TEXT).pack(pady=(0,4))
        ctk.CTkLabel(frame, text="⚠️  SETTINGを送信するとセンサ内のデータが消去されます",
                     font=FONT_SMALL, text_color=COLOR_WARN).pack(pady=(0,12))

        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.pack(expand=True)
        ctk.CTkButton(btn_frame, text="1分間隔", font=FONT_BODY, width=160,
                      fg_color=COLOR_ACCENT, hover_color="#c73652",
                      command=lambda: self._do_setting(1)).pack(side="left", padx=8)
        ctk.CTkButton(btn_frame, text="5分間隔", font=FONT_BODY, width=160,
                      fg_color=COLOR_CARD, hover_color="#0a2a50",
                      command=lambda: self._do_setting(5)).pack(side="left", padx=8)

        ctk.CTkButton(frame, text="← 戻る", font=FONT_BODY, width=120,
                      fg_color="#555555", hover_color="#333333",
                      command=lambda: self._show_screen("connect")).pack(side="right", pady=(20,0))

        return frame

    # ── 画面遷移 ──────────────────────────────────────────────────

    def _show_screen(self, name):
        for f in [self._scan_frame, self._connect_frame, self._setting_frame]:
            f.pack_forget()
        if name == "scan":
            self._scan_frame.pack(fill="both", expand=True)
        elif name == "connect":
            self._connect_frame.pack(fill="both", expand=True)
        elif name == "setting":
            self._setting_frame.pack(fill="both", expand=True)

    # ── ログ ──────────────────────────────────────────────────────

    def _append_log(self, msg: str):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            self._log_box.configure(state="normal")
            self._log_box.insert("end", f"[{ts}] {msg}\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        self.after(0, _do)

    # ── スキャン ──────────────────────────────────────────────────

    def _toggle_scan(self):
        if not self._ble._scanning:
            self._scan_btn.configure(text="⏹ スキャン停止", fg_color="#555555")
            self._append_log("スキャン開始...")
            self._ble.start_scan(self._on_scan_result)
        else:
            self._ble.stop_scan()
            self._scan_btn.configure(text="📡 スキャン開始", fg_color=COLOR_ACCENT)
            self._append_log("スキャン停止")

    def _on_scan_result(self, items):
        def _do():
            # 選択中のシリアルを記憶
            selected_serial = None
            if self._selected_idx is not None and self._selected_idx < len(self._scan_items):
                d, info = self._scan_items[self._selected_idx]
                selected_serial = info['serial'] or d.address

            self._scan_items = items

            # 一覧を再描画
            for w in self._sensor_list.winfo_children():
                w.destroy()

            new_selected_idx = None
            for i, (device, info) in enumerate(items):
                serial = info['serial'] or device.address
                label = f"{serial}  {info['battery_str']}"
                # 選択中だったセンサは色を変えて表示
                is_selected = (serial == selected_serial)
                if is_selected:
                    new_selected_idx = i
                fg = "#1a6a30" if is_selected else COLOR_CARD
                btn = ctk.CTkButton(self._sensor_list, text=label, font=FONT_MONO,
                                     fg_color=fg, hover_color="#1a4a80",
                                     anchor="w", command=lambda idx=i: self._select_sensor(idx))
                btn.pack(fill="x", pady=2)

            # 選択状態を復元
            self._selected_idx = new_selected_idx
            if new_selected_idx is not None:
                self._pair_btn.configure(state="normal")
            else:
                self._pair_btn.configure(state="disabled")
        self.after(0, _do)

    def _select_sensor(self, idx):
        self._selected_idx = idx
        self._pair_btn.configure(state="normal")
        device, info = self._scan_items[idx]
        serial = info['serial'] or device.address
        self._append_log(f"選択: {serial}  {info['battery_str']}")

    # ── ペアリング ────────────────────────────────────────────────

    def _do_pair(self):
        if self._selected_idx is None:
            return
        device, info = self._scan_items[self._selected_idx]
        serial = info['serial'] or device.address
        self._pair_btn.configure(state="disabled")
        self._ble.stop_scan()
        self._scan_btn.configure(text="📡 スキャン開始", fg_color=COLOR_ACCENT)
        self._append_log(f"ペアリング中: {serial}")

        def on_success():
            def _do():
                self._serial_label.configure(text=f"接続中: {serial}")
                self._measurements = []
                self._result_label.configure(text="データなし", text_color=COLOR_MUTED)
                self._save_btn.configure(state="disabled")
                self._show_screen("connect")
            self.after(0, _do)

        def on_fail():
            def _do():
                self._append_log("ペアリング失敗")
                self._pair_btn.configure(state="disabled")
                self._selected_idx = None
                # スキャン再開
                self._scan_btn.configure(text="⏹ スキャン停止", fg_color="#555555")
                self._ble._scanning = True
                self._ble.start_scan(self._on_scan_result)
            self.after(0, _do)

        self._ble.connect(device, serial, on_success, on_fail)

    # ── GETDATA ───────────────────────────────────────────────────

    def _do_getdata(self):
        self._getdata_btn.configure(state="disabled", text="取得中...")
        self._append_log("GETDATA 送信中...")

        def on_done(measurements):
            def _do():
                self._getdata_btn.configure(state="normal", text="📊 GETDATA")
                self._measurements = measurements
                if measurements:
                    temps = [m["temperature"] for m in measurements]
                    oldest = measurements[0]["datetime"].astimezone().strftime("%Y/%m/%d %H:%M")
                    newest = measurements[-1]["datetime"].astimezone().strftime("%Y/%m/%d %H:%M")
                    text = (f"取得件数: {len(measurements)} 件\n"
                            f"期間:  {oldest}\n"
                            f"    〜 {newest}\n"
                            f"平均: {sum(temps)/len(temps):.2f}°C\n"
                            f"最高: {max(temps):.2f}°C  最低: {min(temps):.2f}°C")
                    self._result_label.configure(text=text, text_color=COLOR_TEXT)
                    self._save_btn.configure(state="normal")
                else:
                    self._result_label.configure(text="データなし (0件)", text_color=COLOR_MUTED)
                    self._save_btn.configure(state="disabled")
            self.after(0, _do)

        self._ble.getdata(on_done)

    # ── CSV保存 ───────────────────────────────────────────────────

    def _do_save(self):
        if not self._measurements:
            return
        path = save_csv(self._measurements, self._ble.serial)
        self._append_log(f"CSV保存: {path}")

    # ── SETTING ───────────────────────────────────────────────────

    def _do_setting(self, interval_min: int):
        self._append_log(f"SETTING({interval_min}分) 送信中...")

        def on_done(ok):
            if ok:
                self._append_log(f"SETTING({interval_min}分) 完了")
            else:
                self._append_log(f"SETTING({interval_min}分) 失敗")

        self._ble.setting(interval_min, on_done)

    # ── 切断 ──────────────────────────────────────────────────────

    def _do_disconnect(self):
        def on_done():
            def _do():
                # センサ一覧をクリア
                for w in self._sensor_list.winfo_children():
                    w.destroy()
                self._scan_items = []
                self._selected_idx = None
                self._pair_btn.configure(state="disabled")
                # スキャンボタンをリセット
                self._scan_btn.configure(text="📡 スキャン開始", fg_color=COLOR_ACCENT)
                self._ble._scanning = False
                self._show_screen("scan")
            self.after(0, _do)
        self._ble.disconnect(on_done)

    # ── 終了 ──────────────────────────────────────────────────────

    def _on_close(self):
        self._ble.stop_scan()
        self.destroy()


if __name__ == "__main__":
    app = HalshareApp()
    app.mainloop()