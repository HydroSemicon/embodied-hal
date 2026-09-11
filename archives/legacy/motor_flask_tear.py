from flask import Flask, request # type: ignore
import RPi.GPIO as GPIO # type: ignore
import time

app = Flask(__name__)

AIN1 = 17
AIN2 = 18

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

@app.route("/command", methods=["POST"])
def handle_command():
    # JSONやform、バイナリなど全部に対応
    command = (
        request.data.decode("utf-8").strip()
        or request.form.get("command", "").strip()
    )
    print(f"受信: {command}")

    if "tear" in command.lower():   # ← 部分一致でもOKに
        print("涙ポンプ作動中...")
        forward(70)
        time.sleep(2)
        stop()
        return "OK: tear"
    else:
        print(f"不明なコマンド: {command}")
        stop()
        return f"Unknown command: {command}", 400

@app.route("/")
def home():
    return "Tear pump control API running"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
