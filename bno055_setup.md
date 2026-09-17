# BNO055姿勢モニターのセットアップ

Raspberry PiがBNO055を読み取り、FlaskのNDJSONストリームで姿勢データを配信します。Windows側にはPythonや追加アプリは不要で、同じLAN上のブラウザから板状モデルを表示できます。Raspberry PiのLAN内IPアドレスは`192.168.0.118`として説明します。

## 1. Raspberry Piの準備

`raspi-config`でI2Cを有効にし、BNO055を接続します。既定のI2Cアドレスは`0x28`です。

```bash
sudo raspi-config
i2cdetect -y 1
```

既存の仮想環境を有効にして、このリポジトリで依存パッケージを入れます。

```bash
source ~/venvs/eflesh/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements-bno055-server.txt
```

すでに次の2コマンドを実行済みでも問題ありません。requirementsファイルには、再現用に2026-09-17時点のバージョンを固定しています。

```bash
pip install --upgrade adafruit-blinka
pip install --upgrade adafruit-circuitpython-bno055
```

## 2. サーバーの起動

```bash
python3 bno055_pose.py --sample-hz 100
```

既定値は`0.0.0.0:5002`、センサ取得は100 Hzです。別アドレスの基板では、たとえば`--address 0x29`を追加します。

## 3. Windowsのブラウザで開く

Raspberry Piと同じLANに接続したWindowsで、ChromeまたはEdgeから次を開きます。

```text
http://192.168.0.118:5002/
```

Windows側のPython環境は不要です。`bno055_pose_viewer.html`と`bno055_stream_worker.js`は`bno055_pose.py`と同じディレクトリに置いてください。FlaskがHTMLとJavaScriptをPiからブラウザへ配信します。

画面の「現在の姿勢をゼロにする」を押すと、その瞬間の姿勢を基準にできます。`http://192.168.0.118:5002/api/status`では、ブラウザ表示なしで取得状態を確認できます。

サーバーは既定で`0.0.0.0:5002`へバインドします。`0.0.0.0`はPi上の全ネットワークインターフェースで接続を受け付けるためのサーバー側設定です。Windowsからアクセスするときは実際のLANアドレスである`192.168.0.118`を使用します。

## 更新速度について

- BNO055の融合姿勢出力は最大100 Hzのため、サーバーの既定値も100 Hzです。
- ブラウザ描画は`requestAnimationFrame`を使い、ディスプレイが60 Hzなら約60 FPS、120 Hzなら約120 FPSで動きます。
- 120 Hz画面でも新しいセンサ値は最大100回/秒です。フレーム間はクォータニオン補間で滑らかに表示します。
- 診断レジスタはI2C負荷を増やさないよう1 Hzで読みます。

## キャリブレーション

起動時の姿勢が表示上のゼロになります。既定のIMUPLUSではSystem、Gyro、Accelがすべて`3`になると、校正値を`bno055_calibration.json`へ保存し、次回起動時に読み戻します。NDOFではMagも`3`になる必要があります。このファイルを別のセンサ個体へ流用しないでください。

## 主なオプション

```text
--host 0.0.0.0
--port 5002
--sample-hz 100
--diagnostic-hz 1
--address 0x28
--fusion-mode imuplus
--spike-threshold-deg 8
--calibration-file bno055_calibration.json
--no-load-calibration
--external-crystal
```

この構成では内部オシレータが既定です。`--external-crystal`は、32.768 kHz外部水晶が実際に載っている基板だけで指定してください。旧版との互換性のため`--no-external-crystal`も受け付けますが、現在は指定しなくても同じ動作です。

このFlaskサーバーには認証とTLSがありません。信頼できるローカルネットワーク内だけで使用し、インターネットへ直接公開しないでください。

## `BNO055 fusion output remained invalid`と表示される場合

起動直後のゼロ値は3秒間まで融合処理の準備中として待機します。それを超えても無効な場合、画面には生のクォータニオンと実際の動作モードが表示されます。

まず通常どおり起動します。

```bash
python3 bno055_pose.py --sample-hz 100
```

内部オシレータが既定なので、外部水晶のない基板ではクロック用オプションを付けません。`--external-crystal`を誤って指定すると、画面が一時的に表示できても融合出力が不安定になったり、クォータニオンが全ゼロになったりする可能性があります。通常起動でも無効な場合は、サーバーを停止して次を確認します。

```bash
i2cdetect -y 1
python3 -c "import time,board,adafruit_bno055 as b; s=b.BNO055_I2C(board.I2C(),address=0x28); time.sleep(2); print('mode=',hex(s.mode),'quaternion=',s.quaternion,'euler=',s.euler,'calibration=',s.calibration_status)"
```

`i2cdetect`で`28`が見えない場合は配線、電源、I2C有効化を確認します。`28`が見えても`mode`が`0xc`でない場合はモード設定、`mode=0xc`なのにクォータニオンが全ゼロのままならクロック設定またはセンサ初期化を疑います。

Flaskログの`Bad request version`とバイナリ文字列は、ブラウザがHTTP専用ポートへHTTPSを試したときに出ます。姿勢センサのエラーとは無関係です。Windowsでは必ず`http://192.168.0.118:5002/`を開いてください（`https://`ではありません）。

## 静置時に一瞬だけ姿勢が飛ぶ場合

この表示は起動時基準の相対姿勢なので、既定の融合モードは磁気センサを使わない`imuplus`です。ジャイロと加速度だけを使うため、周囲の金属、磁石、モーター、電源配線や磁気キャリブレーションによる急な方位補正を避けられます。代わりに、長時間ではYawが少しずつドリフトする可能性があります。

さらに、既定では直前の姿勢から8度を超える変化を1サンプルだけ保留します。次のサンプルでも同じ方向へ動いていれば実際の動きとして通し、すぐ元へ戻れば単発スパイクとして捨てます。100 Hz時の追加遅延は、急な動きが発生した場合だけ約10 msです。画面の「スパイク除去」で累計を確認できます。

磁北を基準にした絶対Yawが必要な場合はNDOFを選択できます。

```bash
python3 bno055_pose.py --sample-hz 100 --fusion-mode ndof
```

NDOFの高速磁気キャリブレーションだけを無効にする場合は次を使います。

```bash
python3 bno055_pose.py --sample-hz 100 --fusion-mode ndof-fmc-off
```

スパイク除去が実際の非常に速い動きを抑えてしまう場合は閾値を上げます。無効化は`--spike-threshold-deg 0`です。
