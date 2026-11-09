#!/usr/bin/env python3
"""Control the dual-wavelength ethanol detector and serve its browser UI.

The Raspberry Pi owns GPIO and I2C.  A dedicated acquisition thread samples
the MCP3425, while Flask exposes a small command API and an NDJSON stream to a
browser on the Windows controller.  Safety handling is deliberately local to
the Pi: both LEDs are forced off at startup, shutdown, acquisition failure,
and control-client timeout.
"""

import argparse
import atexit
import json
import logging
import math
import threading
import time
from collections import deque
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, stream_with_context  # type: ignore


LED_1450_PIN = 17
LED_940_PIN = 18
DEFAULT_I2C_ADDRESS = 0x68
DEFAULT_I2C_BUS = 1
DEFAULT_SAMPLE_HZ = 15.0
MAX_SAMPLE_HZ = 15.0
DEFAULT_CLIENT_TIMEOUT_S = 5.0
DEFAULT_PORT = 5003
RASPBERRY_PI_LAN_IP = "192.168.0.118"
LOGGER = logging.getLogger(__name__)


class ConversionNotReady(Exception):
    """Raised while the MCP3425 is still completing its first conversion."""


class ADCConfigurationError(Exception):
    """Raised before conversion when the MCP3425 is not in the required mode."""


class InvalidControlRequest(ValueError):
    """Raised when a control request has invalid fields or values."""


class MCP3425ADC:
    """Minimal MCP3425 driver for continuous 16-bit, 15-SPS, PGA x1 mode."""

    CONFIG_16_BIT_15_SPS_CONTINUOUS_X1 = 0x18
    RESOLUTION_MASK = 0x0C
    PGA_MASK = 0x03
    CONTINUOUS_MASK = 0x10
    READY_MASK = 0x80
    RESOLUTIONS = {0x00: 12, 0x04: 14, 0x08: 16}
    PGA_GAINS = {0x00: 1, 0x01: 2, 0x02: 4, 0x03: 8}
    LSB_16_BIT_VOLTS = 62.5e-6
    # Compatibility alias used by existing callers and tests.
    LSB_VOLTS = LSB_16_BIT_VOLTS

    def __init__(self, bus, address=DEFAULT_I2C_ADDRESS, message_factory=None):
        if not 0 <= address <= 0x7F:
            raise ValueError("I2C address must be between 0x00 and 0x7f")
        if message_factory is None:
            from smbus2 import i2c_msg  # type: ignore

            message_factory = i2c_msg
        self.bus = bus
        self.address = address
        self.message_factory = message_factory
        self.config = self.CONFIG_16_BIT_15_SPS_CONTINUOUS_X1
        self.last_config = None
        self.configuration_verified = False
        self.configure()
        self.verify_configuration(retry=True)

    def configure(self):
        message = self.message_factory.write(self.address, [self.config])
        self.bus.i2c_rdwr(message)
        self.configuration_verified = False
        LOGGER.info(
            "MCP3425 configuration 0x%02X written: continuous, 16 bit, 15 SPS, PGA x1",
            self.config,
        )

    def _read_bytes(self):
        message = self.message_factory.read(self.address, 3)
        self.bus.i2c_rdwr(message)
        data = list(message)
        if len(data) != 3:
            raise ValueError("MCP3425 conversion must contain three bytes")
        return data

    @classmethod
    def config_info(cls, config):
        config = int(config) & 0xFF
        resolution_bits = cls.RESOLUTIONS.get(config & cls.RESOLUTION_MASK)
        pga = cls.PGA_GAINS[config & cls.PGA_MASK]
        continuous = bool(config & cls.CONTINUOUS_MASK)
        ready = not bool(config & cls.READY_MASK)
        expected = resolution_bits == 16 and pga == 1 and continuous
        return {
            "config_byte": config,
            "resolution_bits": resolution_bits,
            "pga": pga,
            "continuous": continuous,
            "ready": ready,
            "expected": expected,
        }

    def verify_configuration(self, retry=False):
        config = self._read_bytes()[2]
        info = self.config_info(config)
        self.last_config = config
        if not info["expected"] and retry:
            LOGGER.warning(
                "MCP3425 configuration mismatch: read 0x%02X; rewriting required 0x%02X",
                config,
                self.config,
            )
            self.configure()
            config = self._read_bytes()[2]
            info = self.config_info(config)
            self.last_config = config
        if not info["expected"]:
            raise ADCConfigurationError(
                "MCP3425 configuration mismatch: "
                f"read 0x{config:02X}, expected continuous 16 bit / 15 SPS / PGA x1 (0x18)"
            )
        if not self.configuration_verified:
            LOGGER.info("MCP3425 configuration verified from device: 0x%02X", config)
        self.configuration_verified = True
        return info

    @classmethod
    def decode(cls, data):
        if len(data) != 3:
            raise ValueError("MCP3425 conversion must contain three bytes")
        config = int(data[2])
        info = cls.config_info(config)
        if config & cls.READY_MASK:
            raise ConversionNotReady("MCP3425 conversion is not ready")
        resolution_bits = info["resolution_bits"]
        if resolution_bits is None:
            raise ADCConfigurationError(f"unsupported MCP3425 resolution bits in config 0x{config:02X}")
        mask = (1 << resolution_bits) - 1
        sign_bit = 1 << (resolution_bits - 1)
        raw = ((int(data[0]) << 8) | int(data[1])) & mask
        if raw & sign_bit:
            raw -= 1 << resolution_bits
        lsb_volts = 2.048 / (1 << (resolution_bits - 1))
        differential_v = raw * lsb_volts / info["pga"]
        return raw, differential_v, config

    @classmethod
    def saturation_flags(cls, raw, resolution_bits=16):
        maximum = (1 << (resolution_bits - 1)) - 1
        minimum = -(1 << (resolution_bits - 1))
        saturated_high = raw >= maximum
        saturated_low = raw <= minimum
        near_threshold = int(maximum * 0.98)
        return {
            "adc_saturated_high": saturated_high,
            "adc_saturated_low": saturated_low,
            "adc_saturated": saturated_high or saturated_low,
            "adc_near_saturation": abs(raw) >= near_threshold,
        }

    def read_conversion(self):
        try:
            data = self._read_bytes()
        except Exception:
            # A transient I2C failure can coincide with an ADC brownout.  If
            # the bus is usable again, restore 0x18 before the next read.
            try:
                self.configure()
            except Exception:
                LOGGER.exception("MCP3425 reconfiguration after I2C error failed")
            raise
        config = data[2]
        if not self.config_info(config)["expected"]:
            LOGGER.warning(
                "MCP3425 configuration changed to 0x%02X; discarding conversion and restoring 0x18",
                config,
            )
            self.configure()
            raise ADCConfigurationError(
                f"MCP3425 returned unexpected configuration 0x{config:02X}; conversion discarded"
            )
        self.last_config = config
        self.configuration_verified = True
        return self.decode(data)

    def close(self):
        self.bus.close()


class LedController:
    """Active-high LED outputs with a hard mutual-exclusion invariant."""

    PINS = {"1450": LED_1450_PIN, "940": LED_940_PIN}

    def __init__(self, gpio):
        self.gpio = gpio
        self.lock = threading.RLock()
        self.states = {"1450": False, "940": False}
        self.closed = False
        self.gpio.setwarnings(False)
        self.gpio.setmode(self.gpio.BCM)
        try:
            for pin in self.PINS.values():
                self.gpio.setup(pin, self.gpio.OUT)
                self.gpio.output(pin, self.gpio.LOW)
        except Exception:
            self._best_effort_low()
            raise

    def _best_effort_low(self):
        for pin in self.PINS.values():
            try:
                self.gpio.output(pin, self.gpio.LOW)
            except Exception:
                pass
        self.states = {"1450": False, "940": False}

    def set_led(self, wavelength, enabled):
        if not isinstance(wavelength, str) or wavelength not in self.PINS:
            raise InvalidControlRequest("wavelength must be '1450' or '940'")
        if type(enabled) is not bool:
            raise InvalidControlRequest("enabled must be a boolean")
        with self.lock:
            if self.closed:
                raise RuntimeError("LED controller is closed")
            try:
                if enabled:
                    # Turn everything off before enabling one channel.  There
                    # is no instant in which the two LEDs are commanded high.
                    for name, pin in self.PINS.items():
                        self.gpio.output(pin, self.gpio.LOW)
                        self.states[name] = False
                    self.gpio.output(self.PINS[wavelength], self.gpio.HIGH)
                    self.states[wavelength] = True
                else:
                    self.gpio.output(self.PINS[wavelength], self.gpio.LOW)
                    self.states[wavelength] = False
            except Exception:
                self._best_effort_low()
                raise
            return dict(self.states)

    def all_off(self):
        with self.lock:
            self._best_effort_low()
            return dict(self.states)

    def snapshot(self):
        with self.lock:
            return dict(self.states)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self._best_effort_low()
            try:
                self.gpio.cleanup(list(self.PINS.values()))
            finally:
                self.closed = True


class EthanolDetectorService:
    """Own acquisition, stream buffering, and the client safety watchdog."""

    def __init__(self, adc, leds, sample_hz=DEFAULT_SAMPLE_HZ, client_timeout_s=DEFAULT_CLIENT_TIMEOUT_S):
        if not 0 < sample_hz <= MAX_SAMPLE_HZ:
            raise ValueError(f"sample_hz must be greater than 0 and no more than {MAX_SAMPLE_HZ:g}")
        if client_timeout_s <= 0:
            raise ValueError("client_timeout_s must be positive")
        self.adc = adc
        self.leds = leds
        self.sample_hz = float(sample_hz)
        self.period_s = 1.0 / self.sample_hz
        self.client_timeout_s = float(client_timeout_s)
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.samples = deque(maxlen=max(128, math.ceil(self.sample_hz * 65)))
        self.latest = None
        self.adc_error = None
        self.safety_message = None
        self.controller_client_id = None
        self.controller_last_seen = None
        self.started = False
        self.closed = False
        self.close_lock = threading.Lock()
        self.sample_thread = threading.Thread(target=self._sample_loop, name="mcp3425-sampler", daemon=True)
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, name="led-safety-watchdog", daemon=True)

    def start(self):
        if self.started:
            return
        self.leds.all_off()
        self.started = True
        self.sample_thread.start()
        self.watchdog_thread.start()

    def _validate_client_id(self, client_id):
        if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 128:
            raise InvalidControlRequest("client_id must be a non-empty string of at most 128 characters")
        return client_id.strip()

    def heartbeat(self, client_id):
        client_id = self._validate_client_id(client_id)
        with self.condition:
            is_controller = client_id == self.controller_client_id
            if is_controller:
                self.controller_last_seen = time.monotonic()
            return is_controller

    def command_led(self, client_id, wavelength, enabled):
        client_id = self._validate_client_id(client_id)
        with self.condition:
            self.controller_client_id = client_id
            self.controller_last_seen = time.monotonic()
            self.safety_message = None
        try:
            states = self.leds.set_led(wavelength, enabled)
        except InvalidControlRequest:
            self._fail_safe("Invalid LED command; both LEDs were commanded off.")
            raise
        except Exception:
            self._fail_safe("LED GPIO operation failed; both LEDs were commanded off.")
            raise
        with self.condition:
            self.condition.notify_all()
        return states

    def release_client(self, client_id):
        client_id = self._validate_client_id(client_id)
        with self.condition:
            is_controller = client_id == self.controller_client_id
        if is_controller:
            self.emergency_off("Control client disconnected; both LEDs were turned off.")
        return is_controller

    def emergency_off(self, message=None):
        states = self.leds.all_off()
        with self.condition:
            self.controller_client_id = None
            self.controller_last_seen = None
            if message:
                self.safety_message = message
            self.condition.notify_all()
        return states

    def _fail_safe(self, message):
        try:
            self.leds.all_off()
        finally:
            with self.condition:
                self.safety_message = message
                self.controller_client_id = None
                self.controller_last_seen = None
                self.condition.notify_all()

    def _watchdog_loop(self):
        while not self.stop_event.wait(0.1):
            with self.condition:
                client_id = self.controller_client_id
                last_seen = self.controller_last_seen
            if client_id is None or last_seen is None or not any(self.leds.snapshot().values()):
                continue
            if time.monotonic() - last_seen > self.client_timeout_s:
                self._fail_safe(
                    f"Control client timed out after {self.client_timeout_s:g} s; both LEDs were turned off."
                )

    def _sample_loop(self):
        seq = 0
        next_deadline = time.monotonic()
        rate_started = next_deadline
        rate_count = 0
        measured_hz = 0.0
        while not self.stop_event.is_set():
            try:
                raw, voltage, config = self.adc.read_conversion()
                config_info = MCP3425ADC.config_info(config)
                if not config_info["expected"]:
                    # Hardware drivers must never pass an unverified sample
                    # into the fixed 16-bit measurement contract.
                    raise ADCConfigurationError(f"unverified MCP3425 configuration 0x{config:02X}")
                saturation = MCP3425ADC.saturation_flags(raw, config_info["resolution_bits"])
                captured_monotonic = time.monotonic()
                captured_unix = time.time()
                seq += 1
                rate_count += 1
                rate_elapsed = captured_monotonic - rate_started
                if rate_elapsed >= 1.0:
                    measured_hz = rate_count / rate_elapsed
                    rate_started = captured_monotonic
                    rate_count = 0
                with self.condition:
                    safety_message = self.safety_message
                sample = {
                    "type": "sample",
                    "seq": seq,
                    "monotonic_s": captured_monotonic,
                    "unix_s": captured_unix,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(captured_unix))
                    + f".{int(captured_unix % 1 * 1000):03d}",
                    "adc_raw": raw,
                    "raw_code": raw,
                    "differential_v": voltage,
                    "adc_config_byte": config,
                    "adc_resolution_bits": config_info["resolution_bits"],
                    "adc_pga": config_info["pga"],
                    "adc_continuous": config_info["continuous"],
                    "sample_hz": measured_hz,
                    "leds": self.leds.snapshot(),
                    "adc_status": "ok",
                    **saturation,
                    # Retained for clients written against the first API.
                    "saturated": saturation["adc_saturated"],
                    "warnings": [] if safety_message is None else [safety_message],
                }
                with self.condition:
                    self.latest = sample
                    self.samples.append(sample)
                    self.adc_error = None
                    self.condition.notify_all()
            except ConversionNotReady:
                pass
            except Exception:
                self._fail_safe("ADC measurement failed; both LEDs were commanded off.")
                with self.condition:
                    first_failure = self.adc_error is None
                    self.adc_error = "ADC measurement failed."
                    self.condition.notify_all()
                if first_failure:
                    LOGGER.exception("ADC measurement failed")

            next_deadline += self.period_s
            delay = next_deadline - time.monotonic()
            if delay > 0:
                self.stop_event.wait(delay)
            else:
                next_deadline = time.monotonic()

    def wait_batch(self, after_seq, timeout=1.0):
        with self.condition:
            self.condition.wait_for(
                lambda: self.stop_event.is_set()
                or (self.latest is not None and (after_seq is None or self.latest["seq"] > after_seq))
                or self.adc_error is not None,
                timeout=timeout,
            )
            if self.latest is None:
                samples = []
            elif after_seq is None:
                samples = [self.latest]
            else:
                samples = [sample for sample in self.samples if sample["seq"] > after_seq]
            return samples, self.adc_error

    def status_snapshot(self):
        with self.condition:
            latest = None if self.latest is None else dict(self.latest)
            adc_error = self.adc_error
            safety_message = self.safety_message
            controller_connected = self.controller_client_id is not None
            last_seen = self.controller_last_seen
        age_s = None if latest is None else max(0.0, time.monotonic() - latest["monotonic_s"])
        controller_age_s = None if last_seen is None else max(0.0, time.monotonic() - last_seen)
        warnings = []
        if adc_error:
            warnings.append(f"ADC error: {adc_error}")
        if safety_message:
            warnings.append(safety_message)
        if latest and latest["adc_saturated"]:
            direction = "upper" if latest["adc_saturated_high"] else "lower"
            warnings.append(f"ADC {direction} limit reached; this sample is clipped and is not a valid measurement.")
        elif latest and latest["adc_near_saturation"]:
            warnings.append("ADC input is within 2% of the 16-bit full-scale limit.")
        return {
            "status": "error" if adc_error else ("ok" if latest else "starting"),
            "adc_status": "error" if adc_error else ("ok" if latest else "starting"),
            "adc_error": adc_error,
            "age_s": age_s,
            "latest": latest,
            "leds": self.leds.snapshot(),
            "target_hz": self.sample_hz,
            "controller_connected": controller_connected,
            "controller_age_s": controller_age_s,
            "client_timeout_s": self.client_timeout_s,
            "warnings": warnings,
        }

    def close(self):
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        try:
            self.leds.all_off()
        finally:
            if self.started:
                self.sample_thread.join(timeout=2.0)
                self.watchdog_thread.join(timeout=2.0)
            try:
                self.adc.close()
            finally:
                self.leds.close()


def _json_object():
    if not request.is_json:
        raise InvalidControlRequest
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise InvalidControlRequest
    return payload


def create_app(service, viewer_path=None):
    app = Flask(__name__)
    viewer = Path(viewer_path) if viewer_path else Path(__file__).with_name("ethanol_detector_viewer.html")
    worker = viewer.with_name("ethanol_detector_stream_worker.js")

    @app.get("/")
    def index():
        response = send_file(viewer)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/ethanol_detector_stream_worker.js")
    def stream_worker():
        response = send_file(worker, mimetype="text/javascript")
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api")
    def api_index():
        return jsonify(
            service="ethanol-detector-server",
            endpoints=[
                "/api/status",
                "/api/stream",
                "/api/led",
                "/api/leds/off",
                "/api/client/heartbeat",
                "/api/client/release",
            ],
            target_hz=service.sample_hz,
        )

    @app.get("/api/status")
    def status():
        return jsonify(service.status_snapshot())

    @app.post("/api/led")
    def led_command():
        try:
            payload = _json_object()
            if set(payload) != {"client_id", "wavelength", "enabled"}:
                raise InvalidControlRequest
            states = service.command_led(payload["client_id"], payload["wavelength"], payload["enabled"])
            return jsonify(status="ok", leds=states)
        except InvalidControlRequest:
            return jsonify(error="Invalid LED command. Expected a client ID, wavelength, and boolean enabled."), 400
        except Exception:
            LOGGER.exception("LED command failed")
            return jsonify(error="LED command failed."), 500

    @app.post("/api/leds/off")
    def leds_off():
        try:
            return jsonify(status="ok", leds=service.emergency_off())
        except Exception:
            LOGGER.exception("Could not turn both LEDs off")
            return jsonify(error="Could not turn both LEDs off."), 500

    @app.post("/api/client/heartbeat")
    def client_heartbeat():
        try:
            payload = _json_object()
            if set(payload) != {"client_id"}:
                raise InvalidControlRequest
            is_controller = service.heartbeat(payload["client_id"])
            return jsonify(status="ok", is_controller=is_controller, leds=service.leds.snapshot())
        except InvalidControlRequest:
            return jsonify(error="Invalid heartbeat request. Expected a client ID."), 400

    @app.post("/api/client/release")
    def client_release():
        try:
            payload = _json_object()
            if set(payload) != {"client_id"}:
                raise InvalidControlRequest
            released = service.release_client(payload["client_id"])
            return jsonify(status="ok", released=released, leds=service.leds.snapshot())
        except InvalidControlRequest:
            return jsonify(error="Invalid release request. Expected a client ID."), 400

    @app.get("/api/stream")
    def stream():
        @stream_with_context
        def generate():
            last_seq = None
            last_error = None
            while not service.stop_event.is_set():
                samples, error = service.wait_batch(last_seq, timeout=1.0)
                if error is not None and error != last_error:
                    packet = {"type": "error", "message": error, "status": service.status_snapshot()}
                    yield json.dumps(packet, separators=(",", ":")) + "\n"
                    last_error = error
                if samples:
                    yield "".join(json.dumps(sample, separators=(",", ":")) + "\n" for sample in samples)
                    last_seq = samples[-1]["seq"]
                    last_error = None
                elif error is None:
                    packet = {"type": "heartbeat", "status": service.status_snapshot()}
                    yield json.dumps(packet, separators=(",", ":")) + "\n"
                else:
                    # adc_error makes wait_batch immediately ready.  Avoid a
                    # tight loop after the first error packet has been sent.
                    service.stop_event.wait(0.05)

        return Response(
            generate(),
            content_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    return app


def parse_i2c_address(value):
    address = int(value, 0)
    if not 0 <= address <= 0x7F:
        raise argparse.ArgumentTypeError("I2C address must be between 0x00 and 0x7f")
    return address


def create_hardware(address, bus_number):
    import RPi.GPIO as GPIO  # type: ignore
    from smbus2 import SMBus  # type: ignore

    leds = LedController(GPIO)
    try:
        adc = MCP3425ADC(SMBus(bus_number), address)
    except Exception:
        leds.close()
        raise
    return adc, leds


def main():
    parser = argparse.ArgumentParser(description="Serve the MCP3425 ethanol detector control and monitor UI.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--address", type=parse_i2c_address, default=DEFAULT_I2C_ADDRESS)
    parser.add_argument("--i2c-bus", type=int, default=DEFAULT_I2C_BUS)
    parser.add_argument("--sample-hz", type=float, default=DEFAULT_SAMPLE_HZ)
    parser.add_argument("--client-timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_S)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    adc, leds = create_hardware(args.address, args.i2c_bus)
    try:
        service = EthanolDetectorService(adc, leds, args.sample_hz, args.client_timeout)
    except Exception:
        try:
            adc.close()
        finally:
            leds.close()
        raise
    atexit.register(service.close)
    service.start()
    app = create_app(service)

    print("Ethanol detector server")
    print(f"MCP3425     : I2C-{args.i2c_bus}, 0x{args.address:02X}, 16 bit, 15 SPS, PGA x1")
    print(f"LED 1450 nm : BCM GPIO{LED_1450_PIN} (physical pin 11, active high)")
    print(f"LED 940 nm  : BCM GPIO{LED_940_PIN} (physical pin 12, active high)")
    print(f"Target rate : {args.sample_hz:.1f} Hz")
    print(f"Safety timer: {args.client_timeout:.1f} s")
    print(f"Open on Windows: http://{RASPBERRY_PI_LAN_IP}:{args.port}/")
    try:
        app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    finally:
        service.close()


if __name__ == "__main__":
    main()
