import asyncio
from bleak import BleakScanner

async def scan_devices():
    print("BLEデバイスをスキャン中...")
    devices = await BleakScanner.discover(timeout=10.0, return_adv=True)
    
    # デバイス名が"TM2101-SR"のものだけをフィルタリング
    filtered_devices = {
        address: (device, adv_data)
        for address, (device, adv_data) in devices.items()
        if device.name and device.name.startswith("TM2101-SR")
    }
    
    # 該当デバイスが見つからなかった場合
    if not filtered_devices:
        print("\n⚠️ TM2101-SR デバイスが見つかりませんでした")
        print("デバイスの電源が入っているか、Bluetooth範囲内にあるか確認してください")
        return
    
    # RSSIでソート（降順：0に近い順）
    sorted_devices = sorted(
        filtered_devices.items(),
        key=lambda x: x[1][1].rssi,
        reverse=True
    )
    
    print(f"\n検出されたデバイス数: {len(sorted_devices)}\n")
    
    for address, (device, advertisement_data) in sorted_devices:
        print(f"デバイス名: {device.name}")
        print(f"アドレス: {device.address}")
        print(f"RSSI: {advertisement_data.rssi} dBm")
        print("-" * 50)

if __name__ == "__main__":
    asyncio.run(scan_devices())