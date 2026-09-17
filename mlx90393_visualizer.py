#!/usr/bin/env python3

import argparse
import json
import os
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np # type: ignore
import requests # type: ignore
from scipy.optimize import least_squares # type: ignore

import matplotlib # type: ignore
matplotlib.use("TkAgg")

import matplotlib.pyplot as plt # type: ignore
from matplotlib.animation import FuncAnimation # type: ignore
from matplotlib.widgets import Button # type: ignore

# ============================================================
# User configuration
# ============================================================

I2C_ADDRESS = 0x18

# 無荷重・中央位置における
# 「MLX90393の感磁点 ～ 磁石中心」の距離 [mm]
#
# ここは実物に合わせて変更する。
REST_Z_MM = 10.0

# 位置推定で許容する範囲 [mm]
XY_LIMIT_MM = 15.0
Z_MIN_MM = 2.0
Z_MAX_MM = 30.0

# GUI更新レート（センサー取得レートとは独立）
GUI_HZ = 30.0

# Raspberry Pi上のmlx90393_sensor_server.py。
# 環境変数または --url で上書きできる。
DEFAULT_SERVER_URL = os.environ.get(
    "MLX90393_SERVER_URL",
    "http://192.168.0.118:5001",
)

# 指数移動平均
# 大きいほど追従が速く、小さいほど滑らか
FILTER_ALPHA = 0.30

# キャリブレーション時の平均回数
CALIBRATION_SAMPLES = 120

# キャリブレーション保存先
CALIBRATION_FILE = Path.home() / ".mlx90393_single_calibration.json"


# ============================================================
# Remote sensor stream
# ============================================================

class RemoteFieldSource:
    """Receive a newline-delimited JSON sample stream without blocking the GUI."""

    def __init__(self, server_url):
        self.stream_url = server_url.rstrip("/") + "/api/stream"
        self.samples = deque(maxlen=4096)
        self.recent_raw = deque(maxlen=max(CALIBRATION_SAMPLES * 2, 512))
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.connected = False
        self.error = None
        self.response = None
        self.thread = threading.Thread(
            target=self._receive_loop,
            name="mlx90393-stream",
            daemon=True,
        )
        self.thread.start()

    def _receive_loop(self):
        while not self.stop_event.is_set():
            try:
                with requests.get(
                    self.stream_url,
                    stream=True,
                    timeout=(3.0, 5.0),
                    headers={"Accept": "application/x-ndjson"},
                ) as response:
                    self.response = response
                    response.raise_for_status()
                    self.connected = True
                    self.error = None

                    # A small chunk prevents long batching delays without
                    # causing a Python read call for every single byte.
                    for line in response.iter_lines(chunk_size=128, decode_unicode=True):
                        if self.stop_event.is_set():
                            return
                        if not line:
                            continue

                        packet = json.loads(line)
                        if packet.get("type") == "error":
                            self.error = packet.get("message", "sensor error")
                            continue
                        if packet.get("type") != "sample":
                            continue

                        field = np.array(packet["field_uT"], dtype=float)
                        if field.shape != (3,) or not np.all(np.isfinite(field)):
                            raise ValueError("invalid field_uT in sensor stream")

                        sample = {
                            "seq": int(packet["seq"]),
                            "t": float(packet["monotonic_s"]),
                            "field": field,
                            "sample_hz": float(packet.get("sample_hz", 0.0)),
                        }
                        with self.lock:
                            self.samples.append(sample)
                            self.recent_raw.append(field)

            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
                self.error = str(exc)
            finally:
                self.connected = False
                self.response = None

            self.stop_event.wait(1.0)

    def drain(self):
        with self.lock:
            items = list(self.samples)
            self.samples.clear()
        return items

    def average_recent(self, samples=CALIBRATION_SAMPLES):
        with self.lock:
            values = list(self.recent_raw)[-samples:]
        if len(values) < samples:
            raise RuntimeError(f"need {samples} samples; only {len(values)} received")
        return np.mean(values, axis=0)

    def close(self):
        self.stop_event.set()
        response = self.response
        if response is not None:
            response.close()
        self.thread.join(timeout=2.0)


# ============================================================
# Magnetic dipole model
# ============================================================

def dipole_field(position_mm, k):
    """
    z方向に磁化された点磁気双極子による磁場を計算する。

    Parameters
    ----------
    position_mm : array-like
        センサ原点から見た磁石中心位置 [x, y, z] [mm]

    k : float
        磁気双極子強度に相当する係数
        単位は、このプログラムでは実質的に [uT * mm^3]

    Returns
    -------
    np.ndarray
        [Bx, By, Bz] [uT]

    Notes
    -----
    Bx = k * 3*x*z / r^5
    By = k * 3*y*z / r^5
    Bz = k * (3*z^2 - r^2) / r^5

    k は実測によってキャリブレーションするため、
    磁石の磁気モーメントを事前に知る必要はない。
    """

    x, y, z = position_mm

    r2 = x*x + y*y + z*z
    r = np.sqrt(r2)

    if r < 1e-9:
        return np.zeros(3)

    r5 = r2 * r2 * r

    bx = k * (3.0 * x * z) / r5
    by = k * (3.0 * y * z) / r5
    bz = k * (3.0 * z * z - r2) / r5

    return np.array([bx, by, bz], dtype=float)


# ============================================================
# Main application
# ============================================================

class MLX90393Visualizer:

    def __init__(self, source, estimate_mode="xy"):
        self.source = source
        self.estimate_mode = estimate_mode

        # 磁石以外の背景磁場・オフセット
        self.offset_uT = np.zeros(3)

        # 真の「磁石なし」背景を測定したか
        self.full_background_calibrated = False

        # 双極子モデル係数
        self.k = None

        # フィルタ後磁場
        self.filtered_B = None

        # 最後に得られた位置
        self.last_position = np.array(
            [0.0, 0.0, REST_Z_MM],
            dtype=float
        )

        # 直近データ
        # GUIは30 FPSでも、Piから届いた全サンプルを6秒分程度保持する。
        # 4096点なら200 Hzで20秒以上あり、通常運用には十分。
        n_history = 4096

        self.history_t = deque(maxlen=n_history)
        self.history_bx = deque(maxlen=n_history)
        self.history_by = deque(maxlen=n_history)
        self.history_bz = deque(maxlen=n_history)
        self.history_norm = deque(maxlen=n_history)

        self.start_time = time.monotonic()
        self.last_seq = None
        self.dropped_samples = 0
        self.sample_hz = 0.0

        self.status_message = "Starting"

        self.load_calibration()

        self.create_gui()

    # --------------------------------------------------------
    # Sensor
    # --------------------------------------------------------

    def read_field(self):
        """
        互換用。リモートストリームの最新サンプルを取得。
        """
        samples = self.source.drain()
        if not samples:
            raise RuntimeError("no sensor samples received")
        return samples[-1]["field"]

    def average_field(self, samples=CALIBRATION_SAMPLES):
        """
        複数回平均によるキャリブレーション用取得。
        """

        return self.source.average_recent(samples)

    # --------------------------------------------------------
    # Calibration
    # --------------------------------------------------------

    def calibrate_background(self, event=None):
        """
        磁石をセンサから十分離した状態で実行する。

        地磁気、基板オフセット、周囲の磁性体等を含めた
        背景磁場を測定する。
        """

        print()
        print("Background calibration")
        print("Keep the magnet away from the sensor.")

        self.status_message = "Measuring background..."
        self.fig.canvas.draw_idle()

        try:
            b = self.average_field()
        except RuntimeError as exc:
            self.status_message = f"Calibration failed: {exc}"
            return

        self.offset_uT = b
        self.full_background_calibrated = True

        print("Background:")
        print(f"  Bx = {b[0]:.2f} uT")
        print(f"  By = {b[1]:.2f} uT")
        print(f"  Bz = {b[2]:.2f} uT")

        self.status_message = "Background calibrated"

        self.filtered_B = None
        self.clear_history()

        self.save_calibration()

    def calibrate_rest(self, event=None):
        """
        磁石を

            x = 0
            y = 0
            z = REST_Z_MM

        に置いて実行する。

        ここから双極子モデル係数 k を求める。
        """

        print()
        print("Rest-position calibration")
        print(
            f"Place magnet at x=0, y=0, z={REST_Z_MM:.2f} mm."
        )

        self.status_message = "Measuring rest position..."
        self.fig.canvas.draw_idle()

        try:
            raw = self.average_field()
        except RuntimeError as exc:
            self.status_message = f"Calibration failed: {exc}"
            return

        # 磁石なし背景を測っていない場合でも、
        # 中央位置では理想的には磁石由来 Bx=By=0 なので、
        # この時点のBx,Byをゼロ点として利用できる。
        if not self.full_background_calibrated:
            self.offset_uT[0] = raw[0]
            self.offset_uT[1] = raw[1]

            # Bzについては磁石の信号と背景磁場を分離できないため
            # 0のままとする。
            self.offset_uT[2] = 0.0

        corrected = raw - self.offset_uT

        bz_rest = corrected[2]

        if abs(bz_rest) < 100.0:
            self.status_message = (
                "Calibration failed: Bz too small"
            )

            print("ERROR:")
            print("Bz is too small.")
            print("Check magnet orientation and distance.")

            return

        # 中心軸上では
        #
        # Bz = 2 k / z^3
        #
        # より、
        #
        # k = Bz z^3 / 2
        #
        self.k = bz_rest * REST_Z_MM**3 / 2.0

        self.last_position = np.array(
            [0.0, 0.0, REST_Z_MM],
            dtype=float
        )

        print("Rest field:")
        print(f"  Bx = {corrected[0]:.2f} uT")
        print(f"  By = {corrected[1]:.2f} uT")
        print(f"  Bz = {corrected[2]:.2f} uT")
        print()
        print(f"k = {self.k:.3f} uT mm^3")

        self.status_message = "Position calibration completed"

        self.filtered_B = None
        self.clear_history()

        self.save_calibration()

    def clear_history(self):
        self.history_t.clear()
        self.history_bx.clear()
        self.history_by.clear()
        self.history_bz.clear()
        self.history_norm.clear()

    # --------------------------------------------------------
    # Position estimation
    # --------------------------------------------------------

    def estimate_position(self, measured_B):
        """
        磁場から磁石位置を逆推定する。

        直前の位置を初期値とすることで、
        時系列として連続な解を優先する。

        xyモードではBzを残差に使用せず、zをREST_Z_MMに固定する。
        """

        if self.k is None:
            return None, None

        if self.estimate_mode == "xy":
            return self.estimate_xy_position(measured_B)

        measured_norm = np.linalg.norm(measured_B)

        if measured_norm < 1.0:
            return None, None

        initial = self.last_position.copy()

        lower = np.array([
            -XY_LIMIT_MM,
            -XY_LIMIT_MM,
            Z_MIN_MM
        ])

        upper = np.array([
            XY_LIMIT_MM,
            XY_LIMIT_MM,
            Z_MAX_MM
        ])

        # 前回値がbounds外へ行かないようにする
        initial = np.clip(
            initial,
            lower + 1e-6,
            upper - 1e-6
        )

        # 数値安定化用
        scale = max(measured_norm, 100.0)

        def residual(p):
            predicted = dipole_field(p, self.k)

            return (predicted - measured_B) / scale

        result = least_squares(
            residual,
            initial,
            bounds=(lower, upper),
            method="trf",
            loss="soft_l1",
            max_nfev=30,
        )

        position = result.x

        predicted = dipole_field(position, self.k)

        error_vector = predicted - measured_B

        relative_error = (
            np.linalg.norm(error_vector)
            / max(measured_norm, 1.0)
        )

        self.last_position = position

        return position, relative_error

    def estimate_xy_position(self, measured_B):
        """Estimate x/y from Bx/By while holding z at the rest height."""
        measured_xy = measured_B[:2]
        measured_norm = np.linalg.norm(measured_xy)

        initial = np.clip(
            self.last_position[:2],
            -XY_LIMIT_MM + 1e-6,
            XY_LIMIT_MM - 1e-6,
        )
        scale = max(measured_norm, 100.0)

        def residual(xy):
            position = np.array([xy[0], xy[1], REST_Z_MM])
            predicted_xy = dipole_field(position, self.k)[:2]
            return (predicted_xy - measured_xy) / scale

        result = least_squares(
            residual,
            initial,
            bounds=(-XY_LIMIT_MM, XY_LIMIT_MM),
            method="trf",
            loss="soft_l1",
            max_nfev=30,
        )

        position = np.array([result.x[0], result.x[1], REST_Z_MM])
        predicted_xy = dipole_field(position, self.k)[:2]
        relative_error = (
            np.linalg.norm(predicted_xy - measured_xy)
            / max(measured_norm, 1.0)
        )
        self.last_position = position
        return position, relative_error

    # --------------------------------------------------------
    # Calibration file
    # --------------------------------------------------------

    def save_calibration(self):

        data = {
            "rest_z_mm": REST_Z_MM,
            "offset_uT": self.offset_uT.tolist(),
            "full_background_calibrated":
                self.full_background_calibrated,
            "k": None if self.k is None else float(self.k),
        }

        with open(CALIBRATION_FILE, "w") as f:
            json.dump(data, f, indent=2)

    def load_calibration(self):

        if not CALIBRATION_FILE.exists():
            return

        try:
            with open(CALIBRATION_FILE, "r") as f:
                data = json.load(f)

            saved_z = float(
                data.get("rest_z_mm", REST_Z_MM)
            )

            self.offset_uT = np.array(
                data.get("offset_uT", [0.0, 0.0, 0.0]),
                dtype=float
            )

            self.full_background_calibrated = bool(
                data.get(
                    "full_background_calibrated",
                    False
                )
            )

            # REST_Z_MMが変更されていた場合、
            # kのキャリブレーションをそのまま使わない。
            if abs(saved_z - REST_Z_MM) < 1e-6:
                k_value = data.get("k", None)

                if k_value is not None:
                    self.k = float(k_value)

            print(
                f"Loaded calibration: {CALIBRATION_FILE}"
            )

        except Exception as e:
            print(
                f"Could not load calibration: {e}"
            )

    # --------------------------------------------------------
    # GUI
    # --------------------------------------------------------

    def create_gui(self):

        self.fig = plt.figure(
            figsize=(13, 7)
        )

        self.fig.canvas.manager.set_window_title(
            "MLX90393 eFlesh Visualizer"
        )

        # ------------------------------
        # Left: position
        # ------------------------------

        self.ax_pos = self.fig.add_subplot(
            1,
            2,
            1,
            projection="3d"
        )

        position_title = "Estimated magnet position"
        if self.estimate_mode == "xy":
            position_title += " (Bx/By only)"
        self.ax_pos.set_title(position_title)

        self.ax_pos.set_xlabel("X [mm]")
        self.ax_pos.set_ylabel("Y [mm]")
        self.ax_pos.set_zlabel("Z [mm]")

        self.ax_pos.set_xlim(
            -XY_LIMIT_MM,
            XY_LIMIT_MM
        )

        self.ax_pos.set_ylim(
            -XY_LIMIT_MM,
            XY_LIMIT_MM
        )

        self.ax_pos.set_zlim(
            0.0,
            Z_MAX_MM
        )

        self.ax_pos.set_box_aspect(
            (
                2 * XY_LIMIT_MM,
                2 * XY_LIMIT_MM,
                Z_MAX_MM
            )
        )

        # センサ
        self.ax_pos.scatter(
            [0],
            [0],
            [0],
            marker="s",
            s=100,
            label="MLX90393"
        )

        # 無荷重位置
        self.ax_pos.scatter(
            [0],
            [0],
            [REST_Z_MM],
            marker="x",
            s=90,
            label="Rest position"
        )

        # 推定された磁石
        self.magnet_point = self.ax_pos.scatter(
            [0],
            [0],
            [REST_Z_MM],
            marker="o",
            s=150,
            label="Magnet"
        )

        # センサ→磁石の線
        self.position_line, = self.ax_pos.plot(
            [0, 0],
            [0, 0],
            [0, REST_Z_MM],
            linewidth=2
        )

        self.ax_pos.legend(
            loc="lower left"
        )

        # 数値表示
        self.info_text = self.ax_pos.text2D(
            0.02,
            0.98,
            "",
            transform=self.ax_pos.transAxes,
            verticalalignment="top",
            family="monospace"
        )

        # ------------------------------
        # Right: magnetic field
        # ------------------------------

        self.ax_field = self.fig.add_subplot(
            1,
            2,
            2
        )

        self.ax_field.set_title(
            "Magnetic field"
        )

        self.ax_field.set_xlabel(
            "Time [s]"
        )

        self.ax_field.set_ylabel(
            "Magnetic flux density [mT]"
        )

        self.ax_field.set_xlim(
            -6.0,
            0.0
        )

        # ±50mT近辺まで見えるようにする
        self.ax_field.set_ylim(
            -55.0,
            55.0
        )

        self.ax_field.grid(True)

        self.line_bx, = self.ax_field.plot(
            [],
            [],
            label="Bx"
        )

        self.line_by, = self.ax_field.plot(
            [],
            [],
            label="By"
        )

        self.line_bz, = self.ax_field.plot(
            [],
            [],
            label="Bz"
        )

        norm_label = "|Bxy|" if self.estimate_mode == "xy" else "|B|"
        self.line_norm, = self.ax_field.plot(
            [],
            [],
            linewidth=2,
            label=norm_label
        )

        self.ax_field.legend(
            loc="upper left"
        )

        # ボタン用余白
        self.fig.subplots_adjust(
            bottom=0.17
        )

        # Background calibration button
        ax_bg = self.fig.add_axes(
            [0.27, 0.04, 0.16, 0.06]
        )

        self.button_bg = Button(
            ax_bg,
            "Background (B)"
        )

        self.button_bg.on_clicked(
            self.calibrate_background
        )

        # Rest calibration button
        ax_rest = self.fig.add_axes(
            [0.45, 0.04, 0.16, 0.06]
        )

        self.button_rest = Button(
            ax_rest,
            "Set Rest (R)"
        )

        self.button_rest.on_clicked(
            self.calibrate_rest
        )

        self.fig.text(
            0.5,
            0.125,
            (
                "B: background calibration   "
                "R: centered rest calibration   "
                "Q: quit"
            ),
            horizontalalignment="center"
        )

        self.fig.canvas.mpl_connect(
            "key_press_event",
            self.on_key
        )

    # --------------------------------------------------------
    # Keyboard
    # --------------------------------------------------------

    def on_key(self, event):

        if event.key in ("b", "B"):
            self.calibrate_background()

        elif event.key in ("r", "R"):
            self.calibrate_rest()

        elif event.key in ("q", "Q"):
            plt.close(self.fig)

    # --------------------------------------------------------
    # Animation
    # --------------------------------------------------------

    def update(self, frame):
        samples = self.source.drain()

        if not samples:
            if self.source.error:
                self.status_message = f"Stream error: {self.source.error}"
            elif not self.source.connected:
                self.status_message = "Connecting to Raspberry Pi..."
            self.info_text.set_text(self.status_message)
            return (self.info_text,)

        # 受信した全サンプルをフィルタとグラフへ反映する。位置推定だけは
        # 描画フレームごとの最新値に対して行い、重い最小二乗計算を抑える。
        raw_B = None
        B = None
        B_norm = None
        for sample in samples:
            seq = sample["seq"]
            if self.last_seq is not None and seq > self.last_seq + 1:
                self.dropped_samples += seq - self.last_seq - 1
            self.last_seq = seq
            self.sample_hz = sample["sample_hz"]
            raw_B = sample["field"]

            if self.filtered_B is None:
                self.filtered_B = raw_B.copy()
            else:
                self.filtered_B = (
                    FILTER_ALPHA * raw_B
                    + (1.0 - FILTER_ALPHA) * self.filtered_B
                )

            B = self.filtered_B - self.offset_uT
            if self.estimate_mode == "xy":
                B_norm = np.linalg.norm(B[:2])
            else:
                B_norm = np.linalg.norm(B)
            self.history_t.append(sample["t"])
            self.history_bx.append(B[0] / 1000.0)
            self.history_by.append(B[1] / 1000.0)
            self.history_bz.append(B[2] / 1000.0)
            self.history_norm.append(B_norm / 1000.0)

        position, relative_error = self.estimate_position(B)

        t = np.array(self.history_t)

        if len(t) > 0:
            t = t - t[-1]

        self.line_bx.set_data(
            t,
            self.history_bx
        )

        self.line_by.set_data(
            t,
            self.history_by
        )

        self.line_bz.set_data(
            t,
            self.history_bz
        )

        self.line_norm.set_data(
            t,
            self.history_norm
        )

        # ------------------------------
        # Saturation warning
        # ------------------------------

        saturation_warning = ""

        if np.max(np.abs(raw_B)) > 45000.0:
            saturation_warning = (
                "\nWARNING: approaching sensor range"
            )

        stream_status = (
            f"\nstream = {self.sample_hz:6.1f} Hz"
            f"  dropped = {self.dropped_samples}"
        )
        estimate_status = (
            "\nmode   = XY only (Bz ignored, z fixed)"
            if self.estimate_mode == "xy"
            else "\nmode   = XYZ"
        )
        norm_name = "|Bxy|" if self.estimate_mode == "xy" else "|B|  "

        # ------------------------------
        # Position display
        # ------------------------------

        if position is not None:

            x, y, z = position

            self.magnet_point._offsets3d = (
                [x],
                [y],
                [z]
            )

            self.position_line.set_data_3d(
                [0, x],
                [0, y],
                [0, z]
            )

            compression = REST_Z_MM - z
            compression_text = (
                "compression=     n/a (Bz ignored)\n"
                if self.estimate_mode == "xy"
                else f"compression= {compression:7.3f} mm\n"
            )

            lateral = np.sqrt(
                x*x + y*y
            )

            error_percent = (
                100.0 * relative_error
                if relative_error is not None
                else np.nan
            )

            quality = "OK"

            if error_percent > 15.0:
                quality = "MODEL MISMATCH"

            text = (
                f"Bx   = {B[0] / 1000:8.3f} mT\n"
                f"By   = {B[1] / 1000:8.3f} mT\n"
                f"Bz   = {B[2] / 1000:8.3f} mT\n"
                f"{norm_name}= {B_norm / 1000:8.3f} mT\n"
                "\n"
                f"x    = {x:8.3f} mm\n"
                f"y    = {y:8.3f} mm\n"
                f"z    = {z:8.3f} mm"
                f"{' (fixed)' if self.estimate_mode == 'xy' else ''}\n"
                "\n"
                f"lateral    = {lateral:7.3f} mm\n"
                f"{compression_text}"
                "\n"
                f"fit error  = {error_percent:6.2f} %\n"
                f"fit quality= {quality}"
                f"{estimate_status}"
                f"{stream_status}"
                f"{saturation_warning}"
            )

        else:

            self.magnet_point._offsets3d = (
                [],
                [],
                []
            )

            self.position_line.set_data_3d(
                [],
                [],
                []
            )

            text = (
                f"Bx   = {B[0] / 1000:8.3f} mT\n"
                f"By   = {B[1] / 1000:8.3f} mT\n"
                f"Bz   = {B[2] / 1000:8.3f} mT\n"
                f"{norm_name}= {B_norm / 1000:8.3f} mT\n"
                "\n"
                "Position not calibrated.\n"
                "\n"
                "Set REST_Z_MM,\n"
                "then press R."
                f"{estimate_status}"
                f"{stream_status}"
                f"{saturation_warning}"
            )

        self.info_text.set_text(text)

        return (
            self.line_bx,
            self.line_by,
            self.line_bz,
            self.line_norm,
            self.position_line,
            self.info_text,
        )

    # --------------------------------------------------------
    # Run
    # --------------------------------------------------------

    def run(self):

        interval_ms = 1000.0 / GUI_HZ

        self.animation = FuncAnimation(
            self.fig,
            self.update,
            interval=interval_ms,
            blit=False,
            cache_frame_data=False,
        )

        try:
            plt.show()
        finally:
            self.source.close()


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Display an MLX90393 stream from a Raspberry Pi.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_SERVER_URL,
        help=f"Raspberry Pi server URL (default: {DEFAULT_SERVER_URL})",
    )
    parser.add_argument(
        "--estimate-mode",
        choices=("xy", "xyz"),
        default="xy",
        help="Position estimator: xy ignores Bz and fixes z; xyz uses all axes (default: xy)",
    )
    args = parser.parse_args()

    print("MLX90393 eFlesh Visualizer")
    print("--------------------------")
    print(f"Server      : {args.url}")
    print(f"Rest Z      : {REST_Z_MM:.2f} mm")
    print(f"Estimate    : {args.estimate_mode.upper()}")
    print()
    print("Controls:")
    print("  B : background calibration")
    print("  R : rest-position calibration")
    print("  Q : quit")
    print()

    source = RemoteFieldSource(args.url)
    app = MLX90393Visualizer(source, estimate_mode=args.estimate_mode)
    app.run()


if __name__ == "__main__":
    main()
