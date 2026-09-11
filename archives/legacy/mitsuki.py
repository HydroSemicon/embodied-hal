#motor_flask_hex
from flask import Flask, request  # type: ignore
import RPi.GPIO as GPIO  # type: ignore
import time

app = Flask(__name__)

AIN1 = 20
AIN2 = 21

GPIO.setmode(GPIO.BCM)
GPIO.setup(AIN1, GPIO.OUT)
GPIO.setup(AIN2, GPIO.OUT)

pwm1 = GPIO.PWM(AIN1, 100)
pwm2 = GPIO.PWM(AIN2, 100)
pwm1.start(0)
pwm2.start(0)


def forward(speed=70):
    pwm1.ChangeDutyCycle(speed)
    pwm2.ChangeDutyCycle(0)


def stop():
    pwm1.ChangeDutyCycle(0)
    pwm2.ChangeDutyCycle(0)

def parse_mt_command(cmd: str):

    if not cmd.startswith("MT") or not cmd.endswith(";"):
        return None

    hexpart = cmd[2:-1]

    if len(hexpart) != 6:
        return None

    try:
        duty_raw = int(hexpart[0:2], 16)
        time_raw = int(hexpart[2:4], 16)
    except ValueError:
        return None

    # duty: 40–100%
    duty = 40 + (duty_raw / 255.0) * 60

    # time: 0–255秒
    duration = time_raw

    return duty, duration


@app.route("/command", methods=["POST"])
def handle_command():

    # raw body を優先して取得
    command = request.get_data(as_text=True).strip()

    # form送信にも対応
    if not command:
        command = request.form.get("command", "").strip()

    print(f"受信: {command}")

    parsed = parse_mt_command(command)

    if parsed:
        duty, duration = parsed

        print(f"モーター回転 duty={duty:.1f}% time={duration}s")

        forward(duty)
        time.sleep(duration)
        stop()

        return f"OK: duty={duty:.1f} time={duration}"

    else:
        print(f"不明コマンド: {command}")
        stop()
        return f"Unknown command: {command}", 400

@app.route("/")
def home():
    return "Motor control API running"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)