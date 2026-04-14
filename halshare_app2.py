#!/usr/bin/env python3
"""
Halshare (TM2101-SR) 体表温センサー データ取得スクリプト
BLEスキャン + デバイス選択 + データ取得 + CSV出力
【データ消失チェック版：Notification無効・GETDATA送信のみ】
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
        
    async def connect(self):
        """デバイスに接続"""
        print(f"デバイスに接続中: {self.address}")
        self.client = BleakClient(self.address)
        await self.client.connect()
        print("✓ 接続成功\n")
    
    async def send_command_and_disconnect(self, command_str):
        """
        コマンドを送信して即座に切断
        Notification/Indicationは一切有効化しない
        """
        # UTF-8エンコード + 改行（0x0A）
        command_bytes = (command_str + "\n").encode('utf-8')
        
        print(f"コマンド送信: {repr(command_str)}")
        print(f"  バイト列: {command_bytes.hex()}")
        
        await self.client.write_gatt_char(WRITE_CHAR_UUID, command_bytes)
        print("✓ コマンド送信完了")
        
        # ★ 即座に切断（データ受信しない）
        print("⚠ 即座に切断します（Notification無効・データ受信なし）\n")
        await self.client.disconnect()
        print("✓ 切断完了")
    
    async def test_getdata_command(self):
        """
        温度データ取得テスト
        ★Notification無効・GETDATAコマンド送信のみ
        """
        print("=" * 70)
        print("データ消失テスト（Notification無効・GETDATA送信のみ）")
        print("=" * 70 + "\n")
        
        # ★ Notificationは一切有効化せずにコマンド送信
        await self.send_command_and_disconnect("GETDATA")
        
        print("\n=" * 70)
        print("テスト完了（データ受信せずに切断）")
        print("=" * 70)


async def main():
    """メイン処理"""
    print("Halshare 体表温センサー データ取得ツール")
    print("【データ消失チェック版：Notification無効・GETDATA送信のみ】")
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
        
        # テスト実行
        await reader.test_getdata_command()
        
        print("\n✓ テスト終了")
        print("次回接続時にセンサー内のデータが残っているか確認してください")
        
    except Exception as e:
        print(f"\n✗ エラー: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n中断されました")
        sys.exit(0)