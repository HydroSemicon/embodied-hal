# nanoka.py - Raspberry Piでモーターを制御するサンプルコード（無限）
import RPi.GPIO as GPIO  # type: ignore
import time

# ピン番号の設定（BCM方式）
AIN1 = 20
AIN2 = 21

GPIO.setmode(GPIO.BCM)
GPIO.setup(AIN1, GPIO.OUT)
GPIO.setup(AIN2, GPIO.OUT)

# PWM設定（例：100Hz）
pwm1 = GPIO.PWM(AIN1, 100)
pwm2 = GPIO.PWM(AIN2, 100)

pwm1.start(0)
pwm2.start(0)


def forward(speed=50):
    """前進"""
    pwm1.ChangeDutyCycle(speed)
    pwm2.ChangeDutyCycle(0)


def backward(speed=50):
    """逆転"""
    pwm1.ChangeDutyCycle(0)
    pwm2.ChangeDutyCycle(speed)


def stop():
    """停止（ブレーキではなく惰性停止）"""
    pwm1.ChangeDutyCycle(0)
    pwm2.ChangeDutyCycle(0)


try:
    while True:
        print("Forward")
        forward(70)   # デューティ比70%で正転
        time.sleep(2)

        print("Stop")
        stop()
        time.sleep(1)

        print("Backward")
        backward(70)  # デューティ比70%で逆転
        time.sleep(2)

        print("Stop")
        stop()
        time.sleep(1)

except KeyboardInterrupt:
    pass

finally:
    pwm1.stop()
    pwm2.stop()
    GPIO.cleanup()
