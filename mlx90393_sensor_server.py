#!/usr/bin/env python3
"""High-rate MLX90393 acquisition server for Raspberry Pi.

The sensor is sampled in a dedicated thread.  Flask only transports the newest
sample as newline-delimited JSON, so a slow client never blocks I2C acquisition.
"""

import argparse
import json
import threading
import time

from flask import Flask, Response, jsonify, stream_with_context  # type: ignore


I2C_ADDRESS = 0x18
DEFAULT_SAMPLE_HZ = 200.0


def create_sensor():
    import adafruit_mlx90393  # type: ignore
    import board  # type: ignore

    class MLX90393Fixed(adafruit_mlx90393.MLX90393):
        def _transceive(self, payload, rxlen=0):
            data = bytearray(rxlen + 1)
            with self.i2c_device as i2c:
                i2c.write_then_readinto(payload, data)
            self._status_last = data[0]
            return data

    i2c = board.I2C()
    return MLX90393Fixed(
        i2c,
        address=I2C_ADDRESS,
        gain=adafruit_mlx90393.GAIN_1X,
        resolution=adafruit_mlx90393.RESOLUTION_16,
        filt=adafruit_mlx90393.FILTER_2,
        oversampling=adafruit_mlx90393.OSR_0,
    )


class SensorSampler:
    def __init__(self, sensor, target_hz):
        if target_hz <= 0:
            raise ValueError("target_hz must be positive")
        self.sensor = sensor
        self.target_hz = target_hz
        self.period_s = 1.0 / target_hz
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.latest = None
        self.last_error = None
        self.thread = threading.Thread(
            target=self._sample_loop,
            name="mlx90393-sampler",
            daemon=True,
        )

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=2.0)

    def wait_next(self, after_seq, timeout=1.0):
        with self.condition:
            self.condition.wait_for(
                lambda: self.stop_event.is_set()
                or (self.latest is not None and self.latest["seq"] != after_seq)
                or self.last_error is not None,
                timeout=timeout,
            )
            return self.latest, self.last_error

    def snapshot(self):
        with self.condition:
            sample = None if self.latest is None else dict(self.latest)
            error = self.last_error
        return sample, error

    def _sample_loop(self):
        seq = 0
        next_deadline = time.monotonic()
        rate_started = next_deadline
        rate_count = 0
        measured_hz = 0.0

        while not self.stop_event.is_set():
            try:
                bx, by, bz = self.sensor.magnetic
                captured = time.monotonic()
                seq += 1
                rate_count += 1
                rate_elapsed = captured - rate_started
                if rate_elapsed >= 1.0:
                    measured_hz = rate_count / rate_elapsed
                    rate_started = captured
                    rate_count = 0

                sample = {
                    "type": "sample",
                    "seq": seq,
                    "monotonic_s": captured,
                    "unix_s": time.time(),
                    "field_uT": [float(bx), float(by), float(bz)],
                    "sample_hz": measured_hz,
                }
                with self.condition:
                    self.latest = sample
                    self.last_error = None
                    self.condition.notify_all()
            except Exception as exc:
                with self.condition:
                    self.last_error = str(exc)
                    self.condition.notify_all()
                self.stop_event.wait(0.1)

            next_deadline += self.period_s
            delay = next_deadline - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)
            else:
                # Do not accumulate an ever-growing timing debt when an I2C
                # conversion takes longer than the requested period.
                next_deadline = time.monotonic()


def create_app(sampler):
    app = Flask(__name__)

    @app.get("/")
    def index():
        return jsonify(
            service="mlx90393-sensor-server",
            endpoints=["/api/status", "/api/stream"],
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

    @app.get("/api/stream")
    def stream():
        @stream_with_context
        def generate():
            last_seq = -1
            last_error = None
            while not sampler.stop_event.is_set():
                sample, error = sampler.wait_next(last_seq, timeout=1.0)
                if error is not None and error != last_error:
                    yield json.dumps(
                        {"type": "error", "message": error},
                        separators=(",", ":"),
                    ) + "\n"
                    last_error = error
                elif error is not None:
                    sampler.stop_event.wait(0.1)
                if sample is not None and sample["seq"] != last_seq:
                    yield json.dumps(sample, separators=(",", ":")) + "\n"
                    last_seq = sample["seq"]
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


def main():
    parser = argparse.ArgumentParser(description="Stream MLX90393 samples over HTTP.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--sample-hz", type=float, default=DEFAULT_SAMPLE_HZ)
    args = parser.parse_args()

    sensor = create_sensor()
    sampler = SensorSampler(sensor, args.sample_hz)
    sampler.start()
    app = create_app(sampler)

    print("MLX90393 sensor server")
    print(f"I2C address : 0x{I2C_ADDRESS:02X}")
    print(f"Target rate : {args.sample_hz:.1f} Hz")
    print(f"Listen      : http://{args.host}:{args.port}")
    try:
        app.run(
            host=args.host,
            port=args.port,
            threaded=True,
            use_reloader=False,
        )
    finally:
        sampler.close()


if __name__ == "__main__":
    main()
