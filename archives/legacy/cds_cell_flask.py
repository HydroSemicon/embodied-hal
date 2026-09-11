# coding: utf-8
# MCP3002 + CdSセル から明るさを読み取って localhost:5003/sensor_data で JSON 形式で提供する

import time
import threading
import spidev  # type: ignore
from flask import Flask  # type: ignore

# ---------------- Flask サーバ ----------------

app = Flask(__name__)

latest_sensor = {
    "cds": None,
    "timestamp": None
}


@app.route("/sensor_data")
def sensor_data():
    return latest_sensor


def run_server():
    app.run(host="0.0.0.0", port=5003, debug=False, use_reloader=False)


threading.Thread(target=run_server, daemon=True).start()

# ---------------- MCP3002 ----------------

spi = spidev.SpiDev()
spi.open(0, 0)  # bus=0, device=0 (CE0)
spi.max_speed_hz = 100000


def read_adc(channel: int) -> int:
    if channel not in (0, 1):
        raise ValueError("channel must be 0 or 1")

    # MCP3002:
    # start bit=1, SGL/DIFF=1, ODD/SIGN=channel, MSBF=0
    # CH0 -> 0b01101000
    # CH1 -> 0b01111000
    if channel == 0:
        cmd = [0b01101000, 0x00]
    else:
        cmd = [0b01111000, 0x00]

    resp = spi.xfer2(cmd)
    value = ((resp[0] & 0x03) << 8) | resp[1]
    return value


def read_cds():
    value = read_adc(0)
    latest_sensor["cds"] = value
    latest_sensor["timestamp"] = time.time()
    print(f"CdS: {value}")


# ---------------- メインループ ----------------

if __name__ == '__main__':
    try:
        while True:
            read_cds()
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        spi.close()