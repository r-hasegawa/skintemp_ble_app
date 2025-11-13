#!/usr/bin/env python3
"""
Halshare (TM2101-SR) 体表温センサー データ取得スクリプト
BLEスキャン + デバイス選択 + データ取得 + CSV出力
"""

import asyncio
from bleak import BleakScanner, BleakClient
from datetime import datetime, timedelta
import struct
import sys
import csv

# 固定値
WEARER_NAME = "test"

# UUID定義（APKから取得）
SERVICE_UUID = "61830845-385d-41e8-9ee5-a30b150b49e9"
WRITE_CHAR_UUID = "804cdb50-bac9-448b-8ae2-41e9750ef93a"
READ_CHAR_UUID = "169bb1bb-ae80-4650-bf4b-afb79f38422a"

# 温度変換定数（APKから）
BASE_TEMPERATURE = 25.0
CELSIUS_PER_LSB = 0.0625


async def scan_and_select_device():
    """
    BLEデバイスをスキャンしてTM2101-SRデバイスを表示、
    ユーザーに選択してもらう
    """
    print("=" * 70)
    print("BLEデバイスをスキャン中...")
    print("=" * 70 + "\n")
    
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    
    # デバイス名が"TM2101-SR"のものだけをフィルタリング
    filtered_devices = {
        address: (device, adv_data)
        for address, (device, adv_data) in devices.items()
        if device.name and device.name.startswith("TM2101-SR")
    }
    
    # 該当デバイスが見つからなかった場合
    if not filtered_devices:
        print("⚠️ TM2101-SR デバイスが見つかりませんでした")
        print("デバイスの電源が入っているか、Bluetooth範囲内にあるか確認してください")
        return None
    
    # RSSIでソート（降順：0に近い順）
    sorted_devices = sorted(
        filtered_devices.items(),
        key=lambda x: x[1][1].rssi,
        reverse=True
    )
    
    print(f"検出されたデバイス数: {len(sorted_devices)}\n")
    
    # デバイス一覧を表示
    device_list = []
    for idx, (address, (device, advertisement_data)) in enumerate(sorted_devices, 1):
        print(f"[{idx}] デバイス名: {device.name}")
        print(f"    アドレス: {device.address}")
        print(f"    RSSI: {advertisement_data.rssi} dBm")
        print("-" * 70)
        device_list.append((device.address, device.name, advertisement_data.rssi))
    
    # ユーザーに選択させる
    while True:
        try:
            print(f"\n接続するデバイスを選択してください (1-{len(device_list)}):")
            choice = input("番号を入力 > ")
            
            choice_num = int(choice)
            if 1 <= choice_num <= len(device_list):
                selected_address = device_list[choice_num - 1][0]
                selected_name = device_list[choice_num - 1][1]
                print(f"\n✓ 選択: [{choice_num}] {selected_name} ({selected_address})\n")
                return selected_address
            else:
                print(f"⚠️ 1から{len(device_list)}の番号を入力してください")
        except ValueError:
            print("⚠️ 有効な数字を入力してください")
        except KeyboardInterrupt:
            print("\n\n操作がキャンセルされました")
            return None


class HalshareReader:
    def __init__(self, address):
        self.address = address
        self.client = None
        self.data_buffer = []
        self.measurement_complete = False
        self.data_acquisition_time = None  # データ取得完了時刻
        
    def calculate_temperature(self, byte_value):
        """
        バイト値から温度を計算
        APKの toTemperature() 実装に基づく
        """
        # バイト値を符号なし整数に変換
        unsigned_value = byte_value if byte_value >= 0 else byte_value + 256
        temperature = (unsigned_value * CELSIUS_PER_LSB) + BASE_TEMPERATURE
        return temperature
    
    async def connect(self):
        """デバイスに接続"""
        print(f"デバイスに接続中: {self.address}")
        self.client = BleakClient(self.address)
        await self.client.connect()
        print("✓ 接続成功\n")
        
    async def setup_notification(self):
        """
        通知ハンドラーを設定してIndicationを有効化
        start_notify()が自動的にCCCDを設定する
        """
        print("Indicationを有効化中...")
        
        def notification_handler(sender, data):
            """データ受信時のコールバック"""
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            
            print(f"[{timestamp}] データ受信:")
            print(f"  Hex: {data.hex()}")
            print(f"  長さ: {len(data)} bytes")
            
            # バイト列で終了フレーム検出
            if data.startswith(b'EN'):
                print("  → 最終フレーム（データ取得完了）")
                self.measurement_complete = True
                self.data_acquisition_time = datetime.now()  # 取得完了時刻を記録
                return
            
            # バイナリデータの場合、温度データをパース
            if len(data) >= 2 and len(data) % 2 == 0:
                print("  温度データ:")
                # 2バイトずつ処理（時間間隔 + 温度）
                for i in range(0, len(data), 2):
                    interval_byte = data[i]
                    temp_byte = data[i + 1]
                    
                    temperature = self.calculate_temperature(temp_byte)
                    print(f"    [{i//2}] 間隔={interval_byte}分, 温度={temperature:.2f}°C")
                    
                    self.data_buffer.append({
                        'interval': interval_byte,
                        'temperature': temperature,
                        'raw_temp_byte': temp_byte
                    })
            
            print("-" * 70)
        
        # start_notify()が自動的にCCCDを設定してIndicationを有効化する
        await self.client.start_notify(READ_CHAR_UUID, notification_handler)
        print("✓ Indication有効化・通知監視開始\n")
    
    async def send_command(self, command_str):
        """
        コマンドを送信
        APKの実装に基づき、文字列 + 改行コード
        """
        # UTF-8エンコード + 改行（0x0A）
        command_bytes = (command_str + "\n").encode('utf-8')
        
        print(f"コマンド送信: {repr(command_str)}")
        print(f"  バイト列: {command_bytes.hex()}")
        
        await self.client.write_gatt_char(WRITE_CHAR_UUID, command_bytes)
        print("✓ コマンド送信完了\n")
    
    async def get_temperature_data(self, timeout=30):
        """
        温度データを取得
        GETDATAコマンドを送信してデータを受信
        """
        print("=" * 70)
        print("温度データ取得開始")
        print("=" * 70 + "\n")
        
        # 通知設定
        await self.setup_notification()
        
        # GETDATAコマンド送信
        await self.send_command("GETDATA")
        
        # データ受信完了まで待機
        print(f"データ受信待機中（最大{timeout}秒）...\n")
        
        start_time = asyncio.get_event_loop().time()
        while not self.measurement_complete:
            await asyncio.sleep(0.1)
            
            # タイムアウトチェック
            if asyncio.get_event_loop().time() - start_time > timeout:
                print("⚠ タイムアウト")
                break
        
        # 通知停止
        await self.client.stop_notify(READ_CHAR_UUID)
        
        print("\n" + "=" * 70)
        print("データ取得完了")
        print("=" * 70)
        
        return self.data_buffer
    
    def generate_csv_data(self):
        """
        取得したデータからCSV用のデータを生成
        最後のデータが最新（データ取得完了時刻）、
        そこから遡って各データの時刻を計算
        """
        if not self.data_buffer or not self.data_acquisition_time:
            return []
        
        csv_rows = []
        
        # 最後のデータの時刻から遡って計算
        current_time = self.data_acquisition_time
        
        # データは古い順に入っているので、逆順で処理して時刻を割り当て
        for i in range(len(self.data_buffer) - 1, -1, -1):
            data = self.data_buffer[i]
            
            csv_rows.append({
                'halshareWearerName': WEARER_NAME,
                'halshareId': self.address,
                'datetime': current_time.strftime("%Y/%m/%d %H:%M:%S"),
                'temperature': data['temperature']
            })
            
            # 次（一つ前）のデータの時刻を計算
            # interval分だけ遡る
            if i > 0:  # まだ前のデータがある場合
                current_time = current_time - timedelta(minutes=data['interval'])
        
        # 時系列順（古い→新しい）に並び替え
        csv_rows.reverse()
        
        return csv_rows
    
    async def disconnect(self):
        """切断"""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("\n✓ デバイスから切断しました")


def save_to_csv(csv_data, filename="output.csv"):
    """
    CSVファイルに保存
    温度以外の列はダブルクォートで囲む
    """
    if not csv_data:
        print("⚠ 保存するデータがありません")
        return
    
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['halshareWearerName', 'halshareId', 'datetime', 'temperature']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames, quoting=csv.QUOTE_NONNUMERIC)
        
        writer.writeheader()
        writer.writerows(csv_data)
    
    print(f"\n✓ CSVファイルに保存しました: {filename}")


async def main():
    """メイン処理"""
    print("Halshare 体表温センサー データ取得ツール")
    print("BLEスキャン + デバイス選択 + CSV出力版")
    print("=" * 70)
    print()
    
    # 1. デバイスをスキャンして選択
    selected_address = await scan_and_select_device()
    
    if selected_address is None:
        print("デバイスが選択されませんでした。終了します。")
        return
    
    # 2. 選択したデバイスでデータ取得
    reader = HalshareReader(selected_address)
    
    try:
        # 接続
        await reader.connect()
        
        # 少し待機
        await asyncio.sleep(1)
        
        # 温度データ取得
        measurements = await reader.get_temperature_data(timeout=60)
        
        # 結果表示
        if measurements:
            print(f"\n取得した測定データ: {len(measurements)}件")
            print("\n測定結果一覧:")
            print("-" * 70)
            for i, m in enumerate(measurements, 1):
                print(f"{i:3d}. 温度: {m['temperature']:6.2f}°C "
                      f"(間隔: {m['interval']:3d}分, "
                      f"生データ: 0x{m['raw_temp_byte']:02x})")
            
            # 統計
            temps = [m['temperature'] for m in measurements]
            print("-" * 70)
            print(f"平均温度: {sum(temps)/len(temps):.2f}°C")
            print(f"最高温度: {max(temps):.2f}°C")
            print(f"最低温度: {min(temps):.2f}°C")
            
            # CSV生成
            csv_data = reader.generate_csv_data()
            
            # CSV保存
            output_filename = f"halshare_data_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            save_to_csv(csv_data, output_filename)
            
            # CSVプレビュー
            print("\nCSV出力プレビュー:")
            print("-" * 70)
            for row in csv_data[:5]:  # 最初の5件を表示
                print(f"{row['halshareWearerName']}, {row['halshareId']}, "
                      f"{row['datetime']}, {row['temperature']}")
            if len(csv_data) > 5:
                print(f"... (残り {len(csv_data) - 5} 件)")
        else:
            print("\n⚠ データが取得できませんでした")
        
    except Exception as e:
        print(f"\n✗ エラー: {e}")
        import traceback
        traceback.print_exc()
    
    finally:
        await reader.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n中断されました")
        sys.exit(0)