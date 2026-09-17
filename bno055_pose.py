#!/usr/bin/env python3
"""Stream BNO055 pose data from a Raspberry Pi to a web browser.

Sensor acquisition runs in its own fixed-rate thread. Flask transports poses
from a bounded buffer as newline-delimited JSON, so a slow browser or network
never blocks I2C reads or creates unbounded latency. The browser UI is served
as a static file from this process.
"""

import argparse
import json
import math
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, send_file, stream_with_context  # type: ignore


I2C_ADDRESS = 0x28
IMUPLUS_MODE = 0x08
NDOF_FMC_OFF_MODE = 0x0B
NDOF_MODE = 0x0C
FUSION_MODES = {
    "imuplus": IMUPLUS_MODE,
    "ndof-fmc-off": NDOF_FMC_OFF_MODE,
    "ndof": NDOF_MODE,
}
RASPBERRY_PI_LAN_IP = "192.168.0.118"
DEFAULT_SAMPLE_HZ = 100.0
DEFAULT_DIAGNOSTIC_HZ = 1.0
DEFAULT_CALIBRATION_FILE = "bno055_calibration.json"
DEFAULT_INVALID_QUATERNION_TIMEOUT_S = 3.0
DEFAULT_SPIKE_THRESHOLD_DEG = 8.0


class FusionStarting(Exception):
    """Internal signal used while the BNO055 fusion output is warming up."""


def quaternion_distance_deg(a, b):
    """Shortest angular distance between two unit quaternions."""
    dot = abs(sum(x * y for x, y in zip(a, b)))
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, dot))))


class QuaternionSpikeFilter:
    """Suppress an isolated large jump while preserving sustained motion."""

    def __init__(self, threshold_deg=DEFAULT_SPIKE_THRESHOLD_DEG):
        if threshold_deg < 0:
            raise ValueError("threshold_deg must not be negative")
        self.threshold_deg = float(threshold_deg)
        self.accepted = None
        self.candidate = None
        self.suppressed_count = 0

    def update(self, quaternion):
        if self.accepted is None or self.threshold_deg == 0:
            self.accepted = quaternion
            self.candidate = None
            return quaternion, False

        if quaternion_distance_deg(self.accepted, quaternion) <= self.threshold_deg:
            self.accepted = quaternion
            self.candidate = None
            return quaternion, False

        if self.candidate is not None and quaternion_distance_deg(self.candidate, quaternion) <= self.threshold_deg:
            # The large change persisted for a second sample: it is real motion.
            self.accepted = quaternion
            self.candidate = None
            return quaternion, False

        self.candidate = quaternion
        self.suppressed_count += 1
        return self.accepted, True


def quat_normalize(q):
    """Normalize a BNO055 quaternion in register order (w, x, y, z)."""
    if q is None or len(q) != 4 or any(value is None for value in q):
        return None
    norm = math.sqrt(sum(float(value) * float(value) for value in q))
    if norm < 1e-12:
        return None
    return tuple(float(value) / norm for value in q)


def quat_conjugate(q):
    w, x, y, z = q
    return (w, -x, -y, -z)


def quat_multiply(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def quat_make_continuous(q, previous):
    """Choose q or -q so consecutive numeric values remain continuous."""
    if previous is not None and sum(a * b for a, b in zip(q, previous)) < 0.0:
        return tuple(-value for value in q)
    return q


def relative_quaternion(reference, current):
    return quat_normalize(quat_multiply(quat_conjugate(reference), current))


def quat_to_rpy_zyx_deg(q):
    """Return conventional roll, pitch and yaw for display only."""
    w, x, y, z = q
    roll = math.atan2(2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y))
    sin_pitch = max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
    pitch = math.asin(sin_pitch)
    yaw = math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return tuple(math.degrees(value) for value in (roll, pitch, yaw))


def save_calibration(sensor, filename):
    data = {
        "offsets_accelerometer": list(sensor.offsets_accelerometer),
        "offsets_magnetometer": list(sensor.offsets_magnetometer),
        "offsets_gyroscope": list(sensor.offsets_gyroscope),
        "radius_accelerometer": sensor.radius_accelerometer,
        "radius_magnetometer": sensor.radius_magnetometer,
    }
    Path(filename).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_calibration(sensor, filename):
    path = Path(filename)
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    sensor.offsets_accelerometer = tuple(data["offsets_accelerometer"])
    sensor.offsets_magnetometer = tuple(data["offsets_magnetometer"])
    sensor.offsets_gyroscope = tuple(data["offsets_gyroscope"])
    sensor.radius_accelerometer = int(data["radius_accelerometer"])
    sensor.radius_magnetometer = int(data["radius_magnetometer"])
    return True


def list_or_none(value):
    if value is None or any(item is None for item in value):
        return None
    return [float(item) for item in value]


def create_sensor(address=I2C_ADDRESS, use_external_crystal=False, fusion_mode=IMUPLUS_MODE):
    """Create the hardware object lazily so tests do not require Blinka."""
    import adafruit_bno055  # type: ignore
    import board  # type: ignore

    sensor = adafruit_bno055.BNO055_I2C(board.I2C(), address=address)
    sensor.mode = fusion_mode
    sensor.use_external_crystal = use_external_crystal
    # Switching the clock source temporarily enters CONFIG mode. Explicitly
    # restore NDOF and allow the fusion engine to begin producing output.
    sensor.mode = fusion_mode
    time.sleep(0.1)
    return sensor


class PoseSampler:
    def __init__(
        self,
        sensor,
        target_hz=DEFAULT_SAMPLE_HZ,
        diagnostic_hz=DEFAULT_DIAGNOSTIC_HZ,
        calibration_file=DEFAULT_CALIBRATION_FILE,
        invalid_quaternion_timeout_s=DEFAULT_INVALID_QUATERNION_TIMEOUT_S,
        expected_mode=IMUPLUS_MODE,
        spike_threshold_deg=DEFAULT_SPIKE_THRESHOLD_DEG,
    ):
        if target_hz <= 0:
            raise ValueError("target_hz must be positive")
        if diagnostic_hz <= 0:
            raise ValueError("diagnostic_hz must be positive")
        if invalid_quaternion_timeout_s < 0:
            raise ValueError("invalid_quaternion_timeout_s must not be negative")
        self.sensor = sensor
        self.target_hz = float(target_hz)
        self.diagnostic_hz = float(diagnostic_hz)
        self.period_s = 1.0 / self.target_hz
        self.diagnostic_period_s = 1.0 / self.diagnostic_hz
        self.calibration_file = calibration_file
        self.invalid_quaternion_timeout_s = float(invalid_quaternion_timeout_s)
        self.expected_mode = expected_mode
        self.spike_filter = QuaternionSpikeFilter(spike_threshold_deg)
        self.invalid_started_monotonic = None
        self.invalid_quaternion_count = 0
        self.last_invalid_quaternion = None
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.latest = None
        self.samples = deque(maxlen=max(128, math.ceil(self.target_hz * 2)))
        self.last_error = None
        self.diagnostics = None
        self.reference = None
        self.previous = None
        self.recenter_requested = False
        self.calibration_saved = False
        self.thread = threading.Thread(target=self._sample_loop, name="bno055-pose-sampler", daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=2.0)

    def request_recenter(self):
        with self.condition:
            self.recenter_requested = True

    def wait_next(self, after_seq, timeout=1.0):
        with self.condition:
            self.condition.wait_for(
                lambda: self.stop_event.is_set()
                or (self.latest is not None and self.latest["seq"] != after_seq)
                or self.last_error is not None,
                timeout=timeout,
            )
            return self.latest, self.last_error

    def wait_batch(self, after_seq, timeout=1.0):
        """Return buffered samples newer than after_seq without blocking acquisition."""
        with self.condition:
            self.condition.wait_for(
                lambda: self.stop_event.is_set()
                or (self.latest is not None and (after_seq is None or self.latest["seq"] > after_seq))
                or self.last_error is not None,
                timeout=timeout,
            )
            if self.latest is None:
                samples = []
            elif after_seq is None:
                # A new browser starts live instead of replaying old data.
                samples = [self.latest]
            else:
                samples = [sample for sample in self.samples if sample["seq"] > after_seq]
            return samples, self.last_error

    def snapshot(self):
        with self.condition:
            sample = None if self.latest is None else dict(self.latest)
            error = self.last_error
        return sample, error

    def _read_diagnostics(self):
        euler = self.sensor.euler
        sys_cal, gyro_cal, accel_cal, mag_cal = self.sensor.calibration_status
        diagnostics = {
            "bno_euler_deg": None
            if euler is None or any(value is None for value in euler)
            else {"heading": euler[0], "roll": euler[1], "pitch": euler[2]},
            "gyro_rad_s": list_or_none(self.sensor.gyro),
            "linear_acceleration_m_s2": list_or_none(self.sensor.linear_acceleration),
            "gravity_m_s2": list_or_none(self.sensor.gravity),
            "magnetic_uT": list_or_none(self.sensor.magnetic),
            "temperature_C": self.sensor.temperature,
            "calibration": {
                "system": int(sys_cal),
                "gyro": int(gyro_cal),
                "accel": int(accel_cal),
                "mag": int(mag_cal),
            },
        }
        fully_calibrated = (
            (sys_cal, gyro_cal, accel_cal) == (3, 3, 3)
            if self.expected_mode == IMUPLUS_MODE
            else (sys_cal, gyro_cal, accel_cal, mag_cal) == (3, 3, 3, 3)
        )
        if not self.calibration_saved and fully_calibrated:
            save_calibration(self.sensor, self.calibration_file)
            self.calibration_saved = True
        return diagnostics

    def _sample_loop(self):
        seq = 0
        next_deadline = time.monotonic()
        next_diagnostic = next_deadline
        rate_started = next_deadline
        rate_count = 0
        measured_hz = 0.0

        while not self.stop_event.is_set():
            try:
                raw_quaternion = self.sensor.quaternion
                quaternion = quat_normalize(raw_quaternion)
                if quaternion is None:
                    now = time.monotonic()
                    if self.invalid_started_monotonic is None:
                        self.invalid_started_monotonic = now
                    self.invalid_quaternion_count += 1
                    self.last_invalid_quaternion = raw_quaternion
                    invalid_for_s = now - self.invalid_started_monotonic
                    if invalid_for_s < self.invalid_quaternion_timeout_s:
                        raise FusionStarting
                    try:
                        mode = self.sensor.mode
                        mode_text = f"0x{mode:02x}"
                    except Exception as exc:
                        mode_text = f"unreadable ({exc})"
                    raise RuntimeError(
                        "BNO055 fusion output remained invalid "
                        f"for {invalid_for_s:.1f}s: raw={raw_quaternion!r}, "
                        f"mode={mode_text}, expected fusion mode=0x{self.expected_mode:02x}. "
                        "Check the clock setting; use --external-crystal only "
                        "when the board actually has a 32.768-kHz crystal."
                    )
                self.invalid_started_monotonic = None
                self.invalid_quaternion_count = 0
                quaternion = quat_make_continuous(quaternion, self.previous)
                self.previous = quaternion
                sensor_quaternion = quaternion
                quaternion, spike_suppressed = self.spike_filter.update(quaternion)

                with self.condition:
                    if self.reference is None or self.recenter_requested:
                        self.reference = quaternion
                        self.recenter_requested = False
                    reference = self.reference

                relative = relative_quaternion(reference, quaternion)
                if relative is None:
                    raise RuntimeError("could not compute relative quaternion")
                roll, pitch, yaw = quat_to_rpy_zyx_deg(relative)
                captured = time.monotonic()
                seq += 1
                rate_count += 1
                rate_elapsed = captured - rate_started
                if rate_elapsed >= 1.0:
                    measured_hz = rate_count / rate_elapsed
                    rate_started = captured
                    rate_count = 0

                if captured >= next_diagnostic:
                    try:
                        self.diagnostics = self._read_diagnostics()
                    except Exception as exc:
                        # Diagnostics and calibration persistence are secondary;
                        # a failure here must never stop the high-rate pose path.
                        diagnostics = {} if self.diagnostics is None else dict(self.diagnostics)
                        diagnostics["error"] = str(exc)
                        self.diagnostics = diagnostics
                    finally:
                        next_diagnostic = captured + self.diagnostic_period_s

                sample = {
                    "type": "pose",
                    "seq": seq,
                    "monotonic_s": captured,
                    "unix_s": time.time(),
                    "quaternion_sensor_wxyz": list(sensor_quaternion),
                    "quaternion_abs_wxyz": list(quaternion),
                    "quaternion_rel_wxyz": list(relative),
                    "rpy_rel_deg": {"roll": roll, "pitch": pitch, "yaw": yaw},
                    "sample_hz": measured_hz,
                    "spike_suppressed": spike_suppressed,
                    "spikes_suppressed_total": self.spike_filter.suppressed_count,
                    "diagnostics": self.diagnostics,
                }
                with self.condition:
                    self.latest = sample
                    self.samples.append(sample)
                    self.last_error = None
                    self.condition.notify_all()
            except FusionStarting:
                # Zero/None output is normal briefly after reset or a mode
                # change. Keep the HTTP status at "starting" during grace.
                with self.condition:
                    self.last_error = None
                    self.condition.notify_all()
                self.stop_event.wait(0.02)
            except Exception as exc:
                with self.condition:
                    self.last_error = str(exc)
                    self.condition.notify_all()
                self.stop_event.wait(0.05)

            next_deadline += self.period_s
            delay = next_deadline - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)
            else:
                next_deadline = time.monotonic()


def create_app(sampler, viewer_path=None):
    app = Flask(__name__)
    viewer = Path(viewer_path) if viewer_path else Path(__file__).with_name("bno055_pose_viewer.html")
    worker = viewer.with_name("bno055_stream_worker.js")

    @app.get("/")
    def index():
        response = send_file(viewer)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/bno055_stream_worker.js")
    def stream_worker():
        response = send_file(worker, mimetype="text/javascript")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api")
    def api_index():
        return jsonify(
            service="bno055-pose-server",
            endpoints=["/api/status", "/api/stream", "/api/recenter"],
            target_hz=sampler.target_hz,
        )

    @app.get("/api/status")
    def status():
        sample, error = sampler.snapshot()
        age_s = None
        if sample is not None:
            age_s = max(0.0, time.monotonic() - sample["monotonic_s"])
        return jsonify(
            status="error" if error else ("ok" if sample else "starting"),
            error=error,
            age_s=age_s,
            latest=sample,
            target_hz=sampler.target_hz,
        )

    @app.post("/api/recenter")
    def recenter():
        sampler.request_recenter()
        return jsonify(status="queued")

    @app.get("/api/stream")
    def stream():
        @stream_with_context
        def generate():
            last_seq = None
            last_error = None
            while not sampler.stop_event.is_set():
                samples, error = sampler.wait_batch(last_seq, timeout=1.0)
                if error is not None and error != last_error:
                    yield json.dumps({"type": "error", "message": error}, separators=(",", ":")) + "\n"
                    last_error = error
                elif error is not None:
                    sampler.stop_event.wait(0.05)
                if samples:
                    yield "".join(json.dumps(sample, separators=(",", ":")) + "\n" for sample in samples)
                    last_seq = samples[-1]["seq"]
                    last_error = None
                elif error is None:
                    yield '{"type":"heartbeat"}\n'

        return Response(
            generate(),
            content_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return app


def parse_i2c_address(value):
    address = int(value, 0)
    if not 0 <= address <= 0x7F:
        raise argparse.ArgumentTypeError("I2C address must be between 0x00 and 0x7f")
    return address


def main():
    parser = argparse.ArgumentParser(description="Stream BNO055 pose data and serve a browser visualizer.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--sample-hz", type=float, default=DEFAULT_SAMPLE_HZ)
    parser.add_argument("--diagnostic-hz", type=float, default=DEFAULT_DIAGNOSTIC_HZ)
    parser.add_argument("--address", type=parse_i2c_address, default=I2C_ADDRESS)
    parser.add_argument(
        "--fusion-mode",
        choices=tuple(FUSION_MODES),
        default="imuplus",
        help="imuplus is most stable for boot-relative pose; ndof modes preserve magnetic heading.",
    )
    parser.add_argument(
        "--spike-threshold-deg",
        type=float,
        default=DEFAULT_SPIKE_THRESHOLD_DEG,
        help="Confirm one-sample jumps larger than this angle; 0 disables filtering.",
    )
    parser.add_argument("--calibration-file", default=DEFAULT_CALIBRATION_FILE)
    parser.add_argument("--no-load-calibration", action="store_true")
    clock_group = parser.add_mutually_exclusive_group()
    clock_group.add_argument(
        "--external-crystal",
        action="store_true",
        help="Use a board-mounted 32.768-kHz crystal (internal oscillator is the default).",
    )
    clock_group.add_argument(
        "--no-external-crystal",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()

    # Internal oscillator is the safe default for this board. The old
    # --no-external-crystal spelling remains accepted as a compatibility no-op.
    use_external_crystal = args.external_crystal
    fusion_mode = FUSION_MODES[args.fusion_mode]
    sensor = create_sensor(
        args.address,
        use_external_crystal=use_external_crystal,
        fusion_mode=fusion_mode,
    )
    if not args.no_load_calibration:
        try:
            loaded = load_calibration(sensor, args.calibration_file)
            print("Calibration loaded." if loaded else "No calibration file found.")
        except Exception as exc:
            print(f"Calibration load failed: {exc}")

    # Loading offsets temporarily switches modes in the driver. Restore NDOF
    # once more and give fusion a full second before starting the 100-Hz loop.
    sensor.mode = fusion_mode
    time.sleep(1.0)

    sampler = PoseSampler(
        sensor,
        args.sample_hz,
        args.diagnostic_hz,
        args.calibration_file,
        expected_mode=fusion_mode,
        spike_threshold_deg=args.spike_threshold_deg,
    )
    sampler.start()
    app = create_app(sampler)

    print("BNO055 pose server")
    print(f"I2C address : 0x{args.address:02X}")
    print(f"Clock       : {'external crystal' if use_external_crystal else 'internal oscillator'}")
    print(f"Fusion      : {args.fusion_mode} (0x{fusion_mode:02X})")
    print(f"Spike filter: {args.spike_threshold_deg:.1f} deg")
    print(f"Target rate : {args.sample_hz:.1f} Hz")
    print(f"Open on Windows: http://{RASPBERRY_PI_LAN_IP}:{args.port}/")
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        sampler.close()


if __name__ == "__main__":
    main()
