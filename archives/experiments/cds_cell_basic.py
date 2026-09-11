import spidev # type: ignore
import time

spi = spidev.SpiDev()
spi.open(0,0)  # bus0 CE0
spi.max_speed_hz = 100000


def read_adc(channel):

    if channel == 0:
        cmd = [0b01101000, 0]
    else:
        cmd = [0b01111000, 0]

    resp = spi.xfer2(cmd)

    value = ((resp[0] & 0x03) << 8) | resp[1]

    return value


try:
    while True:

        val = read_adc(0)

        print("CdS:", val)

        time.sleep(0.5)

except KeyboardInterrupt:
    spi.close()