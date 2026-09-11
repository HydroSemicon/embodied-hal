#標準入力からコマンドを受け取ってモーターを制御するサンプルコード（サーバは立てていない）
import RPi.GPIO as GPIO # type: ignore
import sys
import time

# ピン番号の設定（BCM方式）
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


def backward(speed=70):
    pwm1.ChangeDutyCycle(0)
    pwm2.ChangeDutyCycle(speed)


def stop():
    pwm1.ChangeDutyCycle(0)
    pwm2.ChangeDutyCycle(0)


try:
    # 標準入力から受け取る（例: echo tear | python3 motor.py）
    command = sys.stdin.read().strip()

    if command == "tear":
        print("Forward (tear)")
        forward(70)
        time.sleep(2)
        stop()
    else:
        print(f"Unknown command: {command}")
        stop()

except KeyboardInterrupt:
    pass

finally:
    pwm1.stop()
    pwm2.stop()
    GPIO.cleanup()
