# embodied-hal

Raspberry Pi に接続したセンサーとアクチュエーターを、HTTP/JSON や標準出力から扱うための実験用ハードウェア層です。温湿度・気圧・明るさ・姿勢・タッチの取得と、涙ポンプ用モーター・RGB LED の制御をまとめています。

## 主な機能

- BME280 から温度・気圧・湿度を取得
- MCP3002 経由で CdS セルの明るさを取得
- DC モーター（涙ポンプ）を JSON API から制御
- RGB LED を JSON API から滑らかに色変更
- BNO055 の絶対姿勢と起動時基準の相対姿勢を NDJSON で出力
- 3つのタッチセンサーのイベントを別ホストへ HTTP POST

統合利用では [`kokomi_raspi.py`](kokomi_raspi.py) を起動します。BME280、CdS、3つのタッチセンサー、モーター、RGB LED を1つのプロセスで扱えます。Flask サーバーはポート `5000` で待ち受け、タッチイベントは受信側PCへ HTTP POST します。

```text
                         Raspberry Pi
  BME280 ── I2C ──┐   ┌──────────────────┐
  CdS ─ MCP3002 ──┼──▶│ kokomi_raspi.py │◀── HTTP/JSON client
  Touch sensors ──┤   │     :5000        │──HTTP POST──▶ receiver :3000
  Motor driver ◀──┤   └──────────────────┘
  RGB LED      ◀──┘

  BNO055 ── I2C ─────▶ bno055_pose.py ─────▶ NDJSON
```

## 対象環境

- Raspberry Pi（GPIO、I2C、SPI を使用）
- Python 3
- Node.js（タッチイベント受信サーバー／テストを使う場合のみ）

主に使用するハードウェアは次のとおりです。

| デバイス | 接続・設定 | 用途 |
| --- | --- | --- |
| BME280 | I2C bus 1、アドレス `0x76` | 温度・気圧・湿度 |
| MCP3002 + CdS セル | SPI bus 0 / CE0、CH0 | 明るさ（10 bit ADC値） |
| モータードライバー | BCM GPIO 20 / 21 | 涙ポンプ |
| RGB LED／ドライバー | BCM GPIO 17 / 27 / 22 | R / G / B PWM出力 |
| BNO055 | I2C、アドレス `0x28` | 9軸姿勢推定 |
| タッチセンサー × 3 | BCM GPIO 5 / 6 / 13 | タッチ開始・終了イベント |

> [!CAUTION]
> Raspberry Pi の GPIO にモーターを直接接続しないでください。適切なモータードライバー、外部電源、逆起電力対策を使用し、GPIO と各デバイスの電圧・電流仕様を確認してください。

## セットアップ

Raspberry Pi の I2C と SPI を有効にします。

```bash
sudo raspi-config
```

`Interface Options` から I2C と SPI を有効にした後、再起動してください。接続確認には次のコマンドを利用できます。

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip i2c-tools
i2cdetect -y 1
```

リポジトリを取得し、Python 環境を作成します。

```bash
git clone https://github.com/HydroSemicon/embodied-hal.git
cd embodied-hal
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install Flask smbus2 spidev RPi.GPIO requests matplotlib \
  adafruit-blinka adafruit-circuitpython-bno055
```

すべての依存パッケージが全スクリプトに必要なわけではありません。統合サーバーだけを使う場合は `Flask`、`smbus2`、`spidev`、`RPi.GPIO`、`requests` が必要です。

## 統合サーバーを起動する

配線と I2C／SPI の有効化を確認してから、Raspberry Pi 上で実行します。

```bash
source .venv/bin/activate
export TOUCH_ENDPOINT_URL="http://192.168.0.42:3000/touch_sensor_input"
python kokomi_raspi.py
```

`TOUCH_ENDPOINT_URL` は受信側PCの実際のIPアドレスへ変更してください。省略時は上記と同じURLを使います。サーバーは全インターフェースの `5000` 番ポートで待ち受けます。

```bash
curl http://localhost:5000/
```

### センサー API

#### `GET /bme280/sensor_data`

```bash
curl http://localhost:5000/bme280/sensor_data
```

レスポンス例:

```json
{
  "temp": 24.8,
  "pressure": 1012.6,
  "humidity": 48.3,
  "timestamp": 1770000000.0,
  "sample_count": 42,
  "unchanged_samples": 0,
  "age_seconds": 0.31,
  "status": "ok",
  "error": null
}
```

`sample_count` が増え、`age_seconds` が概ね3秒未満なら更新中です。未補正値が10回連続で同じ場合は `status` が `unchanged` になります。`status` が `ok` 以外なら次の診断APIを確認します。

#### `GET /bme280/diagnostics`

```bash
curl http://localhost:5000/bme280/diagnostics
```

BME280のチップID（正常値は `0x60`）、I²Cバス／アドレス、直近の未補正値、連続して同じ未補正値だった回数、最後のエラーを返します。

#### `GET /touch/status`

```bash
curl http://localhost:5000/touch/status
```

3センサーの現在状態、送信先URL、直近の送信成功イベント、送信エラー、キュー内イベント数を返します。

#### `GET /cds/sensor_data`

```bash
curl http://localhost:5000/cds/sensor_data
```

レスポンス例:

```json
{
  "cds": 512,
  "timestamp": 1770000000.0
}
```

`cds` は MCP3002 の ADC 値（`0`〜`1023`）です。明暗との対応は CdS セルの分圧回路によって変わります。

### アクチュエーター API

どちらのエンドポイントも `Content-Type: application/json` が必須です。未定義フィールドを含むリクエストや、型・範囲が不正な値は HTTP `400` になります。

#### `POST /motor/command`

```bash
curl -X POST http://localhost:5000/motor/command \
  -H 'Content-Type: application/json' \
  -d '{"type":"tear","params":{"speed":10,"duration":5}}'
```

- `speed`: `0`〜`255` の整数。PWM デューティ比 `40`〜`100%` に変換されます。
- `duration`: `0`〜`255` の整数（秒）。

レスポンス例:

```json
{
  "status": "OK",
  "type": "tear",
  "speed": 10,
  "duty": 42.4,
  "duration": 5
}
```

このリクエストは、指定時間のモーター動作が完了してから応答します。

#### `POST /led/command`

```bash
curl -X POST http://localhost:5000/led/command \
  -H 'Content-Type: application/json' \
  -d '{"type":"led_change","params":{"color":"#00FF00"}}'
```

`color` は `#RRGGBB` 形式で指定します。各チャンネルは `0`〜`100%` の PWM デューティ比に変換され、現在色から目標色へ徐々に遷移します。

レスポンス例:

```json
{
  "status": "OK",
  "type": "led_change",
  "color": "#00FF00",
  "pwm": {
    "red": 0,
    "green": 100,
    "blue": 0
  }
}
```

## BNO055 の姿勢を取得する

[`bno055_pose.py`](bno055_pose.py) は BNO055 を NDOF モードで動かし、1行1 JSON の NDJSON を標準出力へ送ります。最初に取得できた姿勢が相対姿勢の基準（ゼロ点）になります。

```bash
python bno055_pose.py --rate 50 --diag-rate 1
```

主なオプション:

| オプション | 既定値 | 説明 |
| --- | ---: | --- |
| `--rate` | `50` | 姿勢の出力レート（Hz） |
| `--diag-rate` | `1` | 診断情報の取得レート（Hz） |
| `--calibration-file` | `bno055_calibration.json` | キャリブレーションの保存先 |
| `--no-load-calibration` | 無効 | 保存済みキャリブレーションを読み込まない |

標準出力には絶対／相対クォータニオンと相対 roll・pitch・yaw が含まれます。診断タイミングではジャイロ、加速度、重力、磁気、温度、キャリブレーション状態も追加されます。全キャリブレーション値が `3` になるとデータを自動保存します。

## タッチイベントを送受信する

統合サーバーは BCM GPIO 5 / 6 / 13 を監視し、タッチ開始・終了を `TOUCH_ENDPOINT_URL` へ自動送信します。[`touch_sensor_post.py`](touch_sensor_post.py) は単体動作を確認する場合だけ使用します。統合サーバーと同時には起動しないでください。

受信側PCで、外部パッケージ不要の Node.js サーバーを起動します。

```bash
node touch_endpoint_server.js
```

別のターミナルから擬似イベントで疎通確認できます。

```bash
node test_touch_endpoint.js
```

単体テストの場合だけ、Raspberry Pi で次を実行します。このときは [`touch_sensor_post.py`](touch_sensor_post.py) 内の `ENDPOINT_URL` も受信側PCのIPへ変更してください。

```bash
python touch_sensor_post.py
```

送信されるイベントの例:

```json
{
  "event": {
    "source": "touch",
    "type": "touch_started",
    "sensor_id": "touch_01"
  }
}
```

`type` は `touch_started` または `touch_ended`、`sensor_id` は `touch_01`〜`touch_03` です。

## スクリプト一覧

| ファイル | 説明 | ポート |
| --- | --- | ---: |
| `kokomi_raspi.py` | BME280、CdS、タッチ送信、モーター、RGB LED の統合サーバー | 5000 |
| `bno055_pose.py` | BNO055 の姿勢を NDJSON で出力 | — |
| `touch_sensor_post.py` | タッチイベントを HTTP POST | — |
| `touch_endpoint_server.js` | タッチイベントの受信・検証サーバー | 3000 |
| `test_touch_endpoint.js` | タッチ受信サーバーの疎通テスト | — |
| `bme280-flask.py` | BME280 単体の Flask サーバー | 5001 |
| `bme280-plot.py` | BME280 の気圧をリアルタイム表示 | — |
| `cds_cell_basic.py` | CdS の ADC 値を標準出力へ表示 | — |
| `cds_cell_flask.py` | CdS 単体の Flask サーバー | 5003 |
| `motor_basic.py` | モーターの正転・停止・逆転デモ | — |
| `motor_stdio_tear.py` | 標準入力の `tear` でモーターを駆動 | — |
| `motor_flask_tear.py` | `tear` コマンド用の簡易 Flask API | 5000 |
| `rgb_led_basic.py` | RGB LED の色切り替えデモ | — |
| `rgb_led_flask.py` | 旧形式コマンド用の RGB LED Flask API | 5002 |
| `mitsuki.py` | 旧形式コマンド用のモーター Flask API | 5000 |
| `flask_debug.py` | POSTされた生データを確認するデバッグサーバー | 5000 |

`mitsuki.py` と `rgb_led_flask.py` は旧HEXコマンド形式の検証用です。新しい実装では JSON API の `kokomi_raspi.py` を使用してください。

## 注意事項

- 複数のスクリプトが同じ GPIO、I2C、SPI、またはポートを使用します。同じデバイスを扱うスクリプトは同時に起動しないでください。
- HTTP サーバーに認証や TLS はありません。信頼できるローカルネットワーク内で使用してください。
- GPIO 番号は物理ピン番号ではなく BCM 番号です。
- RGB LED の極性やドライバー回路によっては、PWM 値の反転が必要です。
- 実機を動かす前に低い出力・短い時間から試し、非常停止できる状態で確認してください。

## License

[MIT License](LICENSE)
