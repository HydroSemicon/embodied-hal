import RPi.GPIO as GPIO # type: ignore
import time

R = 17
G = 27
B = 22

GPIO.setmode(GPIO.BCM)

GPIO.setup(R, GPIO.OUT)
GPIO.setup(G, GPIO.OUT)
GPIO.setup(B, GPIO.OUT)

r = GPIO.PWM(R, 1000)
g = GPIO.PWM(G, 1000)
b = GPIO.PWM(B, 1000)

r.start(0)
g.start(0)
b.start(0)

def set_color(red, green, blue):
    r.ChangeDutyCycle(red)
    g.ChangeDutyCycle(green)
    b.ChangeDutyCycle(blue)

try:
    while True:
        set_color(100,0,0)   # 赤
        time.sleep(1)

        set_color(0,100,0)   # 緑
        time.sleep(1)

        set_color(0,0,100)   # 青
        time.sleep(1)

        set_color(100,100,0) # 黄
        time.sleep(1)

        set_color(0,100,100) # シアン
        time.sleep(1)

        set_color(100,0,100) # 紫
        time.sleep(1)

        set_color(100,100,100) # 白っぽい
        time.sleep(1)

        set_color(0,0,0)     # 消灯
        time.sleep(1)

except KeyboardInterrupt:
    pass

r.stop()
g.stop()
b.stop()
GPIO.cleanup()