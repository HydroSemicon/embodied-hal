# led_flask_hex
from flask import Flask, request  # type: ignore
import RPi.GPIO as GPIO  # type: ignore
import threading
import time

app = Flask(__name__)

R_PIN = 17
G_PIN = 27
B_PIN = 22

GPIO.setmode(GPIO.BCM)
GPIO.setup(R_PIN, GPIO.OUT)
GPIO.setup(G_PIN, GPIO.OUT)
GPIO.setup(B_PIN, GPIO.OUT)

r = GPIO.PWM(R_PIN, 1000)
g = GPIO.PWM(G_PIN, 1000)
b = GPIO.PWM(B_PIN, 1000)

r.start(0)
g.start(0)
b.start(0)

# 現在の色と目標の色
current_color = [0.0, 0.0, 0.0]
target_color = [0.0, 0.0, 0.0]

# 排他制御
color_lock = threading.Lock()

# 変化の速さ
STEP = 2.0          # 1回ごとにどれだけ近づくか
UPDATE_INTERVAL = 0.03  # 秒

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


def parse_lt_command(cmd: str):

    if not cmd.startswith("LT") or not cmd.endswith(";"):
        return None

    hexpart = cmd[2:-1]

    if len(hexpart) != 6:
        return None

    try:
        code = int(hexpart[0:2], 16)
    except ValueError:
        return None

    return code


def apply_emotion(code):

    if code == 0x01:       # 青：落ち着き
        set_target_color(0, 0, 100)
    elif code == 0x02:     # 赤：怒り
        set_target_color(100, 0, 0)
    elif code == 0x03:     # ピンク：喜び
        set_target_color(100, 0, 40)
    elif code == 0x04:     # 緑：正常
        set_target_color(0, 100, 0)
    elif code == 0x05:     # 紫：困惑
        set_target_color(60, 0, 100)
    else:
        set_target_color(0, 0, 0)


@app.route("/command", methods=["POST"])
def handle_command():
    command = request.get_data(as_text=True).strip()

    if not command:
        command = request.form.get("command", "").strip()

    print(f"受信: {command}")
    code = parse_lt_command(command)

    if code is not None:
        print(f"LED code=0x{code:02X}")
        apply_emotion(code)
        return f"OK: code={code}"
    else:
        print(f"不明コマンド: {command}")
        return f"Unknown command: {command}", 400

if __name__ == "__main__":
    threading.Thread(target=fade_worker, daemon=True).start()
    try:
        app.run(host="0.0.0.0", port=5002)
    finally:
        r.stop()
        g.stop()
        b.stop()
        GPIO.cleanup()