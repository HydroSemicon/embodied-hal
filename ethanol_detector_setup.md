# 二波長NIRエタノール検出モニターのセットアップ

`ethanol_detector_server.py` はRaspberry Pi上でGPIOとMCP3425を制御し、Windowsのブラウザへ操作画面と測定ストリームを配信する。外部JavaScriptやWindows側Python環境は不要。

## 配線とソフトウェア上の割り当て

| 機能 | 物理ピン | BCM番号 | 論理 |
| --- | ---: | ---: | --- |
| 1450 nm LED | 11 | GPIO17 | Highで点灯、Lowで消灯 |
| 940 nm LED | 12 | GPIO18 | Highで点灯、Lowで消灯 |
| MCP3425 SDA | 3 | GPIO2 / SDA1 | I²Cデータ |
| MCP3425 SCL | 5 | GPIO3 / SCL1 | I²Cクロック |
| 3.3 V | 1または17 | — | 回路電源 |
| GND | 6など | — | 共通GND |

GPIO番号モードはBCMを使用する。GPIO17/18からLEDを直接駆動せず、仕様書どおり2N7000のゲートを駆動すること。両ゲートの10 kΩプルダウンは、OS起動前やプロセス停止後の消灯にも必要。

飽和対策後のアナログ部品値は次のとおり。

| 部品 | 値 | 注意 |
| --- | ---: | --- |
| 1450 nm LED直列抵抗 | 200～220 Ω | 100 Ω抵抗をもう1本直列にして合計200 Ωでもよい |
| 940 nm LED直列抵抗 | 1 kΩ | 100 Ωへ変更しない |
| TIA帰還抵抗 | 100 kΩ | 470 kΩや1 MΩへ増やさない |

このツールはGPIO17を使用するため、同じGPIOをRGB LED赤チャネルに使う`kokomi_raspi.py`とは同時起動しないこと。

## Raspberry Piの準備

I²Cを有効化し、MCP3425の実アドレスを確認する。

```bash
sudo raspi-config
sudo apt install -y i2c-tools
i2cdetect -y 1
```

既定アドレスは一般的な`0x68`。現物が異なる場合は起動時の`--address`で指定する。

```bash
python3 -m venv .venv-ethanol
source .venv-ethanol/bin/activate
python3 -m pip install -r requirements-ethanol-detector-server.txt
python3 ethanol_detector_server.py --address 0x68
```

WindowsのChromeまたはEdgeで`http://<Raspberry-PiのIPアドレス>:5003/`を開く。例:

```text
http://192.168.0.118:5003/
```

主な起動オプション:

```text
--host 0.0.0.0
--port 5003
--i2c-bus 1
--address 0x68
--sample-hz 15
--client-timeout 5
```

`--sample-hz`はMCP3425の16 bit変換速度を超えない`0 < Hz <= 15`に制限される。

## 動作と安全設計

- MCP3425を連続変換、16 bit、15 SPS、PGA×1（設定バイト`0x18`）で使用する。
- 起動時に`0x18`を書き込んで設定バイトを読み返す。不一致時は再設定し、なお不一致なら起動を中止する。
- 運転中に設定不一致を検出した測定点は破棄し、`0x18`を再送する。I²C例外後も再設定を試行する。
- 1コードは62.5 µV、差動フルスケールは概ね±2.048 V。
- `raw_code == 32767`または`-32768`は飽和として扱い、有効な測定値として表示しない。フルスケールの98%以上でも警告する。
- 片方を点灯する際は両GPIOをLowへ落としてから対象だけをHighにする。
- 起動時、正常終了時、GPIO/I²C例外時は両LEDを消灯する。
- LEDを操作したブラウザからのハートビートが既定5秒途絶えると両LEDを消灯する。
- ブラウザを正常に閉じる際も制御解放を通知し、両LEDを消灯する。通知が届かない場合はタイムアウトが代替する。
- ADC取得は専用スレッド、HTTPストリームはNDJSON、グラフ描画は10 FPSで分離されている。
- グラフは直近60秒を表示する。CSVにはブラウザを開いてからクリアするまでの全取得点を保持する。
- CSVには設定バイト、分解能、PGA、飽和、飽和接近フラグも記録する。

起動ログに次の2行が出ることを確認する。

```text
MCP3425 configuration 0x18 written: continuous, 16 bit, 15 SPS, PGA x1
MCP3425 configuration verified from device: 0x18
```

RDYが1の場合、読出し値は`0x98`になることがある。RDY以外が`0x18`と一致していれば正常。

## 飽和対策後の実機確認

1. 暗状態、940 nm点灯、1450 nm点灯の順に生コードと電圧を記録する。
2. テスターでMCP6022 pin 1とpin 7を測定し、画面の差動電圧が`pin 1 − pin 7`と概ね一致することを確認する。
3. 1450 nmの光軸を最適化しても、生コードが`32767`へ張り付かないことを確認する。
4. 最大受光時の差動電圧は、目安として1.2～1.6 V以内へ収める。
5. 蒸留水と既知濃度のエタノール水溶液を交互に測定し、飽和していない範囲で変化が再現することを確認する。

従来の`2047 / 0.1279375 V`という連続記録は、12 bit最大コードへ16 bit用LSBを掛けた可能性が高いため、正式な検量線には使用しない。

このFlaskサービスは認証とTLSを持たないため、信頼できるローカルネットワーク内だけで使用する。
