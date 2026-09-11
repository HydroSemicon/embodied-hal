import importlib.util
import sys
import types
from pathlib import Path

import pytest


class FakePwm:
    def __init__(self, pin, frequency):
        self.pin = pin
        self.frequency = frequency
        self.duty = None
        self.stopped = False

    def start(self, duty):
        self.duty = duty

    def ChangeDutyCycle(self, duty):
        self.duty = duty

    def stop(self):
        self.stopped = True


class FakeSpiDev:
    def __init__(self):
        self.max_speed_hz = None

    def open(self, bus, device):
        self.bus = bus
        self.device = device

    def close(self):
        pass


class FakeSmbus:
    def __init__(self, bus):
        self.bus = bus

    def close(self):
        pass


def install_hardware_stubs(monkeypatch):
    def no_op(*args, **kwargs):
        return None

    gpio = types.ModuleType("RPi.GPIO")

    def read_low(pin):
        return gpio.LOW

    gpio.BCM = 11
    gpio.OUT = 0
    gpio.PUD_DOWN = 21
    gpio.HIGH = 1
    gpio.LOW = 0
    gpio.setwarnings = no_op
    gpio.setmode = no_op
    gpio.setup = no_op
    gpio.input = read_low
    gpio.cleanup = no_op
    gpio.PWM = FakePwm

    rpi = types.ModuleType("RPi")
    rpi.GPIO = gpio

    spidev = types.ModuleType("spidev")
    spidev.SpiDev = FakeSpiDev

    smbus2 = types.ModuleType("smbus2")
    smbus2.SMBus = FakeSmbus

    monkeypatch.setitem(sys.modules, "RPi", rpi)
    monkeypatch.setitem(sys.modules, "RPi.GPIO", gpio)
    monkeypatch.setitem(sys.modules, "spidev", spidev)
    monkeypatch.setitem(sys.modules, "smbus2", smbus2)


@pytest.fixture(scope="session")
def hal_module():
    monkeypatch = pytest.MonkeyPatch()
    install_hardware_stubs(monkeypatch)

    module_path = Path(__file__).resolve().parents[1] / "kokomi_raspi.py"
    spec = importlib.util.spec_from_file_location("kokomi_raspi_ci", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    yield module

    module.cleanup()
    monkeypatch.undo()


@pytest.fixture
def client(hal_module):
    hal_module.app.config.update(TESTING=True)
    return hal_module.app.test_client()
