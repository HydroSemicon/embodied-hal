# coding: utf-8
# BME280, MCP3002(CdS), Touch, Motor, RGB LED を1つの Flask アプリに統合

import atexit
import os
import queue
import threading
import time
from flask import Flask, request  # type: ignore
from werkzeug.exceptions import BadRequest  # type: ignore
import RPi.GPIO as GPIO  # type: ignore
import requests  # type: ignore
import spidev  # type: ignore
from smbus2 import SMBus  # type: ignore

app = Flask(__name__)
shutdown_event = threading.Event()


def is_plain_object(value):
    return isinstance(value, dict)


def has_exactly_keys(obj, keys):
    return is_plain_object(obj) and set(obj.keys()) == set(keys)


def error_response(message, status=400):
    return {"error": message}, status


def parse_json_request():
    if not request.is_json:
        raise ValueError("Request body must be JSON")

    try:
        payload = request.get_json()
    except BadRequest:
        raise ValueError("Request body must be valid JSON")

    if not is_plain_object(payload):
        raise ValueError("JSON body must be an object")

    return payload


def validate_uint8(value, name):
    if type(value) is not int:
        raise ValueError(f"{name} must be an integer")
    if value < 0 or value > 255:
        raise ValueError(f"{name} must be from 0 to 255")
    return value


def validate_tear_command(payload):
    if not has_exactly_keys(payload, ("type", "params")):
        raise ValueError("Motor command must contain exactly type and params")
    if payload["type"] != "tear":
        raise ValueError('Motor command type must be "tear"')

    params = payload["params"]
    if not has_exactly_keys(params, ("speed", "duration")):
        raise ValueError("Motor params must contain exactly speed and duration")

    speed = validate_uint8(params["speed"], "speed")
    duration = validate_uint8(params["duration"], "duration")
    return speed, duration


def validate_led_command(payload):
    if not has_exactly_keys(payload, ("type", "params")):
        raise ValueError("LED command must contain exactly type and params")
    if payload["type"] != "led_change":
        raise ValueError('LED command type must be "led_change"')

    params = payload["params"]
    if not has_exactly_keys(params, ("color",)):
        raise ValueError("LED params must contain exactly color")

    color = params["color"]
    if not isinstance(color, str):
        raise ValueError("color must be a string")

    hex_color_to_pwm(color)
    return color.upper()


def channel_to_pwm(value):
    duty = round((value / 255.0) * 100, 1)
    if duty.is_integer():
        return int(duty)
    return duty


def hex_color_to_pwm(color):
    if not isinstance(color, str):
        raise ValueError("color must be a string")
    if len(color) != 7 or color[0] != "#":
        raise ValueError("color must be in #RRGGBB format")

    try:
        red_raw = int(color[1:3], 16)
        green_raw = int(color[3:5], 16)
        blue_raw = int(color[5:7], 16)
    except ValueError:
        raise ValueError("color must be in #RRGGBB format")

    return {
        "red": channel_to_pwm(red_raw),
        "green": channel_to_pwm(green_raw),
        "blue": channel_to_pwm(blue_raw),
    }

# ================================
# BME280
# ================================

bme_latest_sensor = {
    "temp": None,
    "pressure": None,
    "humidity": None,
    "timestamp": None,
}
bme_lock = threading.Lock()
bme_diagnostics = {
    "chip_id": None,
    "sample_count": 0,
    "last_raw": None,
    "unchanged_samples": 0,
    "last_sample_monotonic": None,
    "last_error": None,
    "last_error_timestamp": None,
}

bus_number = 1
i2c_address = 0x76
bus = SMBus(bus_number)

BME280_EXPECTED_CHIP_ID = 0x60
BME280_SAMPLE_INTERVAL_SECONDS = 1.0
BME280_RETRY_INTERVAL_SECONDS = 2.0
BME280_MEASUREMENT_TIMEOUT_SECONDS = 0.2
BME280_STALE_AFTER_SECONDS = 3.0
BME280_UNCHANGED_WARNING_SAMPLES = 10

digT = []
digP = []
digH = []
t_fine = 0.0


def bme_write_reg(reg_address, data):
    bus.write_byte_data(i2c_address, reg_address, data)


def signed_value(value, bits):
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value


def bme_get_calib_param():
    calib_tp = bus.read_i2c_block_data(i2c_address, 0x88, 24)
    calib_h1 = bus.read_byte_data(i2c_address, 0xA1)
    calib_h = bus.read_i2c_block_data(i2c_address, 0xE1, 7)

    digT[:] = [
        (calib_tp[1] << 8) | calib_tp[0],
        signed_value((calib_tp[3] << 8) | calib_tp[2], 16),
        signed_value((calib_tp[5] << 8) | calib_tp[4], 16),
    ]
    digP[:] = [
        (calib_tp[7] << 8) | calib_tp[6],
        *[
            signed_value((calib_tp[i + 1] << 8) | calib_tp[i], 16)
            for i in range(8, 24, 2)
        ],
    ]
    digH[:] = [
        calib_h1,
        signed_value((calib_h[1] << 8) | calib_h[0], 16),
        calib_h[2],
        signed_value((calib_h[3] << 4) | (calib_h[4] & 0x0F), 12),
        signed_value((calib_h[5] << 4) | (calib_h[4] >> 4), 12),
        signed_value(calib_h[6], 8),
    ]

    if digT[0] in (0, 0xFFFF) or digP[0] in (0, 0xFFFF):
        raise RuntimeError("BME280 calibration data is invalid")


def bme_compensate_p(adc_p):
    global t_fine

    v1 = (t_fine / 2.0) - 64000.0
    v2 = (((v1 / 4.0) * (v1 / 4.0)) / 2048) * digP[5]
    v2 = v2 + ((v1 * digP[4]) * 2.0)
    v2 = (v2 / 4.0) + (digP[3] * 65536.0)
    v1 = (((digP[2] * (((v1 / 4.0) * (v1 / 4.0)) / 8192)) / 8) + ((digP[1] * v1) / 2.0)) / 262144
    v1 = ((32768 + v1) * digP[0]) / 32768

    if v1 == 0:
        raise RuntimeError("BME280 pressure calibration caused division by zero")

    pressure = ((1048576 - adc_p) - (v2 / 4096)) * 3125
    if pressure < 0x80000000:
        pressure = (pressure * 2.0) / v1
    else:
        pressure = (pressure / v1) * 2

    v1 = (digP[8] * (((pressure / 8.0) * (pressure / 8.0)) / 8192.0)) / 4096
    v2 = ((pressure / 4.0) * digP[7]) / 8192.0
    pressure = pressure + ((v1 + v2 + digP[6]) / 16.0)

    return pressure / 100


def bme_compensate_t(adc_t):
    global t_fine

    v1 = (adc_t / 16384.0 - digT[0] / 1024.0) * digT[1]
    v2 = (adc_t / 131072.0 - digT[0] / 8192.0) * (adc_t / 131072.0 - digT[0] / 8192.0) * digT[2]
    t_fine = v1 + v2

    return t_fine / 5120.0


def bme_compensate_h(adc_h):
    global t_fine

    var_h = t_fine - 76800.0

    if var_h == 0:
        raise RuntimeError("BME280 humidity calibration is invalid")

    var_h = (adc_h - (digH[3] * 64.0 + digH[4] / 16384.0 * var_h)) * (
        digH[1]
        / 65536.0
        * (1.0 + digH[5] / 67108864.0 * var_h * (1.0 + digH[2] / 67108864.0 * var_h))
    )

    var_h = var_h * (1.0 - digH[0] * var_h / 524288.0)

    if var_h > 100.0:
        var_h = 100.0
    elif var_h < 0.0:
        var_h = 0.0

    return var_h


def bme_read_data():
    # Forced mode creates a fresh conversion for every API sample.  A single
    # block read then keeps pressure, temperature and humidity from one frame.
    ctrl_meas_forced = (1 << 5) | (1 << 2) | 1
    bme_write_reg(0xF4, ctrl_meas_forced)
    time.sleep(0.015)

    deadline = time.monotonic() + BME280_MEASUREMENT_TIMEOUT_SECONDS
    while bus.read_byte_data(i2c_address, 0xF3) & 0x08:
        if time.monotonic() >= deadline:
            raise TimeoutError("BME280 measurement did not finish")
        time.sleep(0.002)

    data = bus.read_i2c_block_data(i2c_address, 0xF7, 8)

    pres_raw = (data[0] << 12) | (data[1] << 4) | (data[2] >> 4)
    temp_raw = (data[3] << 12) | (data[4] << 4) | (data[5] >> 4)
    hum_raw = (data[6] << 8) | data[7]

    temperature = bme_compensate_t(temp_raw)
    pressure = bme_compensate_p(pres_raw)
    humidity = bme_compensate_h(hum_raw)

    if not (-40.0 <= temperature <= 85.0):
        raise RuntimeError(f"BME280 temperature out of range: {temperature:.2f} C")
    if not (300.0 <= pressure <= 1100.0):
        raise RuntimeError(f"BME280 pressure out of range: {pressure:.2f} hPa")

    return {
        "temp": temperature,
        "pressure": pressure,
        "humidity": humidity,
        "timestamp": time.time(),
    }, (pres_raw, temp_raw, hum_raw)


def bme_setup():
    osrs_t = 1
    osrs_p = 1
    osrs_h = 1
    mode = 0
    t_sb = 5
    bme_filter = 0
    spi3w_en = 0

    ctrl_meas_reg = (osrs_t << 5) | (osrs_p << 2) | mode
    config_reg = (t_sb << 5) | (bme_filter << 2) | spi3w_en
    ctrl_hum_reg = osrs_h

    # Configuration can only be changed reliably while the device sleeps.
    bme_write_reg(0xF4, 0)
    bme_write_reg(0xF2, ctrl_hum_reg)
    bme_write_reg(0xF5, config_reg)
    bme_write_reg(0xF4, ctrl_meas_reg)


def bme_initialize():
    chip_id = bus.read_byte_data(i2c_address, 0xD0)
    with bme_lock:
        bme_diagnostics["chip_id"] = chip_id

    if chip_id != BME280_EXPECTED_CHIP_ID:
        raise RuntimeError(
            f"Unexpected chip ID 0x{chip_id:02X}; expected BME280 0x60"
        )

    bme_write_reg(0xE0, 0xB6)
    time.sleep(0.005)

    deadline = time.monotonic() + BME280_MEASUREMENT_TIMEOUT_SECONDS
    while bus.read_byte_data(i2c_address, 0xF3) & 0x01:
        if time.monotonic() >= deadline:
            raise TimeoutError("BME280 calibration copy did not finish")
        time.sleep(0.002)

    bme_get_calib_param()
    bme_setup()


def bme_worker():
    initialized = False

    while not shutdown_event.is_set():
        try:
            if not initialized:
                bme_initialize()
                initialized = True

            sample, raw = bme_read_data()
            now_monotonic = time.monotonic()

            with bme_lock:
                if raw == bme_diagnostics["last_raw"]:
                    bme_diagnostics["unchanged_samples"] += 1
                else:
                    bme_diagnostics["unchanged_samples"] = 0

                bme_latest_sensor.update(sample)
                bme_diagnostics["sample_count"] += 1
                bme_diagnostics["last_raw"] = raw
                bme_diagnostics["last_sample_monotonic"] = now_monotonic
                bme_diagnostics["last_error"] = None

        except Exception as exc:
            initialized = False
            with bme_lock:
                bme_diagnostics["last_error"] = f"{type(exc).__name__}: {exc}"
                bme_diagnostics["last_error_timestamp"] = time.time()
            print(f"BME280 read failed; retrying: {exc}")
            shutdown_event.wait(BME280_RETRY_INTERVAL_SECONDS)
            continue

        shutdown_event.wait(BME280_SAMPLE_INTERVAL_SECONDS)


@app.route("/bme280/sensor_data")
def bme280_sensor_data():
    with bme_lock:
        result = dict(bme_latest_sensor)
        last_sample = bme_diagnostics["last_sample_monotonic"]
        error = bme_diagnostics["last_error"]
        result["sample_count"] = bme_diagnostics["sample_count"]
        result["unchanged_samples"] = bme_diagnostics["unchanged_samples"]

    age = None if last_sample is None else max(0.0, time.monotonic() - last_sample)
    result["age_seconds"] = None if age is None else round(age, 3)
    result["error"] = error

    if error is not None:
        result["status"] = "error"
    elif age is None:
        result["status"] = "starting"
    elif age > BME280_STALE_AFTER_SECONDS:
        result["status"] = "stale"
    elif result["unchanged_samples"] >= BME280_UNCHANGED_WARNING_SAMPLES:
        result["status"] = "unchanged"
    else:
        result["status"] = "ok"

    return result


@app.route("/bme280/diagnostics")
def bme280_diagnostics():
    with bme_lock:
        result = dict(bme_diagnostics)

    result.pop("last_sample_monotonic", None)
    chip_id = result["chip_id"]
    result["chip_id"] = None if chip_id is None else f"0x{chip_id:02X}"
    raw = result["last_raw"]
    if raw is not None:
        result["last_raw"] = {
            "pressure": raw[0],
            "temperature": raw[1],
            "humidity": raw[2],
        }
    result["i2c_bus"] = bus_number
    result["i2c_address"] = f"0x{i2c_address:02X}"
    return result


# ================================
# MCP3002 + CdS
# ================================

cds_latest_sensor = {
    "cds": None,
    "timestamp": None,
}

spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 100000


def read_adc(channel: int) -> int:
    if channel not in (0, 1):
        raise ValueError("channel must be 0 or 1")

    if channel == 0:
        cmd = [0b01101000, 0x00]
    else:
        cmd = [0b01111000, 0x00]

    resp = spi.xfer2(cmd)
    return ((resp[0] & 0x03) << 8) | resp[1]


def read_cds():
    value = read_adc(0)
    cds_latest_sensor["cds"] = value
    cds_latest_sensor["timestamp"] = time.time()


def cds_worker():
    while True:
        read_cds()
        time.sleep(1)


@app.route("/cds/sensor_data")
def cds_sensor_data():
    return cds_latest_sensor


# ================================
# Capacitive touch sensors
# ================================

TOUCH_SENSORS = {
    "touch_01": 5,
    "touch_02": 6,
    "touch_03": 13,
}
TOUCH_ENDPOINT_URL = os.environ.get(
    "TOUCH_ENDPOINT_URL",
    "http://192.168.0.42:3000/touch_sensor_input",
)
TOUCH_ACTIVE_HIGH = True
TOUCH_PULL_UP_DOWN = GPIO.PUD_DOWN
TOUCH_DEBOUNCE_SECONDS = 0.05
TOUCH_LOOP_SLEEP_SECONDS = 0.01
TOUCH_REQUEST_TIMEOUT_SECONDS = 2.0
TOUCH_REQUEST_ATTEMPTS = 3

touch_event_queue = queue.Queue(maxsize=100)
touch_lock = threading.Lock()
touch_status = {
    "endpoint_url": TOUCH_ENDPOINT_URL,
    "sensors": {},
    "last_event": None,
    "last_error": None,
    "last_error_timestamp": None,
}


def read_touch_sensor_state(pin):
    value = GPIO.input(pin)
    return value == GPIO.HIGH if TOUCH_ACTIVE_HIGH else value == GPIO.LOW


def set_touch_error(message):
    with touch_lock:
        touch_status["last_error"] = message
        touch_status["last_error_timestamp"] = time.time()


def enqueue_touch_event(sensor_id, event_type):
    event = {
        "source": "touch",
        "type": event_type,
        "sensor_id": sensor_id,
    }

    try:
        touch_event_queue.put_nowait(event)
    except queue.Full:
        set_touch_error("Touch event queue is full; event was dropped")
        print(f"{sensor_id}: {event_type} dropped because the queue is full")


def touch_reader_worker():
    sensor_states = {}

    try:
        for sensor_id, pin in TOUCH_SENSORS.items():
            GPIO.setup(pin, GPIO.IN, pull_up_down=TOUCH_PULL_UP_DOWN)

        now = time.monotonic()
        for sensor_id, pin in TOUCH_SENSORS.items():
            initial_state = read_touch_sensor_state(pin)
            sensor_states[sensor_id] = {
                "pin": pin,
                "stable_state": initial_state,
                "last_raw_state": initial_state,
                "last_raw_change": now,
            }
            print(f"{sensor_id}: initial state {'ON' if initial_state else 'OFF'}")

        with touch_lock:
            touch_status["sensors"] = {
                sensor_id: {"pin": state["pin"], "touched": state["stable_state"]}
                for sensor_id, state in sensor_states.items()
            }
            touch_status["last_error"] = None

        while not shutdown_event.is_set():
            now = time.monotonic()

            for sensor_id, state in sensor_states.items():
                raw_state = read_touch_sensor_state(state["pin"])

                if raw_state != state["last_raw_state"]:
                    state["last_raw_state"] = raw_state
                    state["last_raw_change"] = now
                    continue

                if raw_state == state["stable_state"]:
                    continue

                if now - state["last_raw_change"] < TOUCH_DEBOUNCE_SECONDS:
                    continue

                state["stable_state"] = raw_state
                event_type = "touch_started" if raw_state else "touch_ended"
                with touch_lock:
                    touch_status["sensors"][sensor_id]["touched"] = raw_state
                enqueue_touch_event(sensor_id, event_type)

            shutdown_event.wait(TOUCH_LOOP_SLEEP_SECONDS)

    except Exception as exc:
        set_touch_error(f"{type(exc).__name__}: {exc}")
        print(f"Touch sensor reader stopped: {exc}")


def touch_sender_worker():
    while not shutdown_event.is_set():
        try:
            event = touch_event_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        last_error = None
        try:
            for attempt in range(1, TOUCH_REQUEST_ATTEMPTS + 1):
                try:
                    response = requests.post(
                        TOUCH_ENDPOINT_URL,
                        json={"event": event},
                        timeout=TOUCH_REQUEST_TIMEOUT_SECONDS,
                    )
                    response.raise_for_status()
                    with touch_lock:
                        touch_status["last_event"] = {
                            **event,
                            "sent_at": time.time(),
                            "http_status": response.status_code,
                        }
                        touch_status["last_error"] = None
                    print(
                        f"{event['sensor_id']}: {event['type']} POST ok "
                        f"({response.status_code})"
                    )
                    last_error = None
                    break
                except requests.RequestException as exc:
                    last_error = (
                        f"POST attempt {attempt}/{TOUCH_REQUEST_ATTEMPTS} failed: {exc}"
                    )
                    if attempt < TOUCH_REQUEST_ATTEMPTS:
                        shutdown_event.wait(0.25 * attempt)

            if last_error is not None:
                set_touch_error(last_error)
                print(f"{event['sensor_id']}: {event['type']} {last_error}")
        finally:
            touch_event_queue.task_done()


@app.route("/touch/status")
def touch_sensor_status():
    with touch_lock:
        result = {
            **touch_status,
            "sensors": {
                sensor_id: dict(sensor_state)
                for sensor_id, sensor_state in touch_status["sensors"].items()
            },
        }
    result["queued_events"] = touch_event_queue.qsize()
    return result


# ================================
# Motor (旧 mitsuki.py)
# ================================

AIN1 = 20
AIN2 = 21

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(AIN1, GPIO.OUT)
GPIO.setup(AIN2, GPIO.OUT)

pwm1 = GPIO.PWM(AIN1, 100)
pwm2 = GPIO.PWM(AIN2, 100)
pwm1.start(0)
pwm2.start(0)


def motor_forward(speed=70):
    pwm1.ChangeDutyCycle(speed)
    pwm2.ChangeDutyCycle(0)


def motor_stop():
    pwm1.ChangeDutyCycle(0)
    pwm2.ChangeDutyCycle(0)


@app.route("/motor/command", methods=["POST"])
def handle_motor_command():
    try:
        payload = parse_json_request()
        speed, duration = validate_tear_command(payload)
    except ValueError as exc:
        motor_stop()
        return error_response(str(exc))

    duty = 40 + (speed / 255.0) * 60
    motor_forward(duty)
    time.sleep(duration)
    motor_stop()

    return {
        "status": "OK",
        "type": "tear",
        "speed": speed,
        "duty": round(duty, 1),
        "duration": duration,
    }


# ================================
# RGB LED
# ================================

R_PIN = 17
G_PIN = 27
B_PIN = 22

GPIO.setup(R_PIN, GPIO.OUT)
GPIO.setup(G_PIN, GPIO.OUT)
GPIO.setup(B_PIN, GPIO.OUT)

r = GPIO.PWM(R_PIN, 1000)
g = GPIO.PWM(G_PIN, 1000)
b = GPIO.PWM(B_PIN, 1000)

r.start(0)
g.start(0)
b.start(0)

current_color = [0.0, 0.0, 0.0]
target_color = [0.0, 0.0, 0.0]
color_lock = threading.Lock()

STEP = 2.0
UPDATE_INTERVAL = 0.03


def apply_pwm(red, green, blue):
    r.ChangeDutyCycle(red)
    g.ChangeDutyCycle(green)
    b.ChangeDutyCycle(blue)


def set_target_color(red, green, blue):
    with color_lock:
        target_color[0] = float(red)
        target_color[1] = float(green)
        target_color[2] = float(blue)


def fade_worker():
    global current_color

    while True:
        with color_lock:
            for i in range(3):
                diff = target_color[i] - current_color[i]
                if abs(diff) <= STEP:
                    current_color[i] = target_color[i]
                elif diff > 0:
                    current_color[i] += STEP
                else:
                    current_color[i] -= STEP

            red, green, blue = current_color

        apply_pwm(red, green, blue)
        time.sleep(UPDATE_INTERVAL)


@app.route("/led/command", methods=["POST"])
def handle_led_command():
    try:
        payload = parse_json_request()
        color = validate_led_command(payload)
        pwm = hex_color_to_pwm(color)
    except ValueError as exc:
        return error_response(str(exc))

    set_target_color(pwm["red"], pwm["green"], pwm["blue"])
    return {
        "status": "OK",
        "type": "led_change",
        "color": color,
        "pwm": pwm,
    }


@app.route("/")
def home():
    return {
        "service": "kokomi_raspi",
        "endpoints": [
            "/bme280/sensor_data",
            "/bme280/diagnostics",
            "/cds/sensor_data",
            "/touch/status",
            "/motor/command",
            "/led/command",
        ],
        "touch_endpoint_url": TOUCH_ENDPOINT_URL,
    }


def cleanup():
    shutdown_event.set()

    try:
        spi.close()
    except Exception:
        pass

    try:
        bus.close()
    except Exception:
        pass

    try:
        pwm1.stop()
        pwm2.stop()
    except Exception:
        pass

    try:
        r.stop()
        g.stop()
        b.stop()
    except Exception:
        pass

    GPIO.cleanup()


atexit.register(cleanup)


if __name__ == "__main__":
    threading.Thread(target=bme_worker, daemon=True).start()
    threading.Thread(target=cds_worker, daemon=True).start()
    threading.Thread(target=touch_reader_worker, daemon=True).start()
    threading.Thread(target=touch_sender_worker, daemon=True).start()
    threading.Thread(target=fade_worker, daemon=True).start()

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
