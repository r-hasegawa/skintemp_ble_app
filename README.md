# Halshare TM2101-SR データ取得ツール

Halshare TM2101-SR 体表温センサーからBLE経由でデータを取得するツール群です。

## ファイル構成

| ファイル | 説明 |
|---|---|
| `halshare_gui.py` | GUIアプリ（メイン） |
| `halshare_getdata.py` | CLIスクリプト |
| `halshare_web_postscan.html` | Webアプリ（接続後にシリアル番号取得） |
| `halshare_web_prescan.html` | Webアプリ（接続前にシリアル番号取得、Chrome限定） |
| `requirements.txt` | 必要パッケージ |

## セットアップ

### 共通手順（macOS / Windows）

**1. リポジトリをクローン**

```bash
git clone <リポジトリURL>
cd <フォルダ名>
```

**2. 仮想環境を作成・有効化**

macOS:
```bash
python3 -m venv build_env
source build_env/bin/activate
```

Windows:
```cmd
python -m venv build_env
build_env\Scripts\activate
```

**3. パッケージをインストール**

```bash
pip install -r requirements.txt
```

## 使い方

### GUIアプリ（推奨）

macOS:
```bash
python3 halshare_gui.py
```

Windows:
```cmd
python halshare_gui.py
```

1. 「📡 スキャン開始」を押してセンサを探す
2. 一覧からセンサを選択して「🔗 ペアリング」
3. 「📊 GETDATA」でデータ取得
4. 「💾 CSV保存」で保存

CSVは実行ファイルと同じ階層の `exportdata/` フォルダに保存されます。

### CLIスクリプト

```bash
python3 halshare_getdata.py
```

## アプリ化（pyinstaller）

仮想環境を有効化した状態で実行してください。
それぞれ使いたいOSでビルドする必要があります（クロスコンパイル不可）。

**macOS** → `dist/HalshareGUI.app`（ダブルクリックで起動）:
```bash
pyinstaller halshare_gui.py --onefile --windowed --strip --name HalshareGUI
```

**Windows** → `dist/HalshareGUI.exe`（ダブルクリックで起動）:
```cmd
pyinstaller halshare_gui.py --onefile --windowed --name HalshareGUI
```

ビルド成果物は `dist/` フォルダに生成されます。

## 動作要件

- Python 3.10 以上
- Bluetooth LE 対応PC
- macOS 10.15 以上 / Windows 10 以上
- Chrome / Edge（Webアプリ使用時）
