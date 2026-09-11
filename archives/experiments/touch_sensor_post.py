import time
import sys

import requests  # type: ignore
import RPi.GPIO as GPIO  # type: ignore


# BCM GPIO pin assignments.
TOUCH_SENSORS = {
    "touch_01": 5,
    "touch_02": 6,
    "touch_03": 13,
}

ENDPOINT_URL = "http://192.168.0.42:3000/touch_sensor_input"

# Most capacitive touch modules, such as TTP223, output HIGH while touched.
ACTIVE_HIGH = True
PULL_UP_DOWN = GPIO.PUD_DOWN

DEBOUNCE_SECONDS = 0.05
LOOP_SLEEP_SECONDS = 0.01  # 100 Hz
REQUEST_TIMEOUT_SECONDS = 2.0


def read_sensor_state(pin):
    value = GPIO.input(pin)
    return value == GPIO.HIGH if ACTIVE_HIGH else value == GPIO.LOW


def send_touch_event(sensor_id, event_type):
    payload = {
        "event": {
            "source": "touch",
            "type": event_type,
            "sensor_id": sensor_id,
        }
    }

    try:
        response = requests.post(
            ENDPOINT_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        print(f"{sensor_id}: {event_type} POST ok ({response.status_code})")
    except requests.RequestException as exc:
        print(f"{sensor_id}: {event_type} POST failed: {exc}")


def main():
    GPIO.setwarnings(False)
    GPIO.cleanup()
    GPIO.setmode(GPIO.BCM)

    try:
        for sensor_id, pin in TOUCH_SENSORS.items():
            try:
                GPIO.setup(pin, GPIO.IN, pull_up_down=PULL_UP_DOWN)
            except Exception as exc:
                print(f"{sensor_id}: failed to setup BCM GPIO {pin}: {exc}")
                print("Check whether another process is using this GPIO pin.")
                GPIO.cleanup()
                sys.exit(1)

        now = time.monotonic()
        sensor_states = {}

        for sensor_id, pin in TOUCH_SENSORS.items():
            initial_state = read_sensor_state(pin)
            sensor_states[sensor_id] = {
                "pin": pin,
                "stable_state": initial_state,
                "last_raw_state": initial_state,
                "last_raw_change": now,
            }
            print(f"{sensor_id}: initial state {'ON' if initial_state else 'OFF'}")

        while True:
            now = time.monotonic()

            for sensor_id, state in sensor_states.items():
                raw_state = read_sensor_state(state["pin"])

                if raw_state != state["last_raw_state"]:
                    state["last_raw_state"] = raw_state
                    state["last_raw_change"] = now
                    continue

                if raw_state == state["stable_state"]:
                    continue

                if now - state["last_raw_change"] < DEBOUNCE_SECONDS:
                    continue

                state["stable_state"] = raw_state
                event_type = "touch_started" if raw_state else "touch_ended"
                send_touch_event(sensor_id, event_type)

            time.sleep(LOOP_SLEEP_SECONDS)

    except KeyboardInterrupt:
        print("Stopping touch sensor reader")
    finally:
        GPIO.cleanup()
        print("GPIO cleaned up")


if __name__ == "__main__":
    main()
