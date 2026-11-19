import json
import time

import pytest

from ethanol_detector_server import (
    ADCConfigurationError,
    MCP3425ADC,
    EthanolDetectorService,
    LedController,
    create_app,
    parse_i2c_address,
)


class FakeGPIO:
    BCM = 11
    OUT = 1
    LOW = 0
    HIGH = 1

    def __init__(self):
        self.outputs = {}
        self.history = []
        self.cleaned = []

    def setwarnings(self, _enabled):
        pass

    def setmode(self, mode):
        assert mode == self.BCM

    def setup(self, pin, mode):
        assert mode == self.OUT
        self.outputs.setdefault(pin, None)

    def output(self, pin, value):
        self.outputs[pin] = value
        self.history.append((pin, value, dict(self.outputs)))

    def cleanup(self, pins):
        self.cleaned.extend(pins)


class FakeADC:
    def __init__(self):
        self.value = 99
        self.error = None
        self.closed = False

    def read_conversion(self):
        if self.error:
            raise RuntimeError(self.error)
        self.value += 1
        return self.value, self.value * MCP3425ADC.LSB_VOLTS, 0x18

    def close(self):
        self.closed = True


def wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def make_service(sample_hz=15.0, client_timeout_s=0.2):
    gpio = FakeGPIO()
    leds = LedController(gpio)
    adc = FakeADC()
    service = EthanolDetectorService(adc, leds, sample_hz=sample_hz, client_timeout_s=client_timeout_s)
    return service, adc, gpio


def test_mcp3425_decodes_signed_16_bit_codes_and_voltage():
    raw, voltage, config = MCP3425ADC.decode([0x7F, 0xFF, 0x18])
    assert raw == 32767
    assert voltage == pytest.approx(2.0479375)
    assert config == 0x18

    raw, voltage, _ = MCP3425ADC.decode([0xFF, 0xFF, 0x18])
    assert raw == -1
    assert voltage == pytest.approx(-0.0000625)


def test_mcp3425_uses_resolution_from_config_when_decoding():
    raw, voltage, config = MCP3425ADC.decode([0x07, 0xFF, 0x10])
    assert raw == 2047
    assert voltage == pytest.approx(2.047)
    assert config == 0x10

    raw, voltage, _ = MCP3425ADC.decode([0x07, 0xFF, 0x18])
    assert raw == 2047
    assert voltage == pytest.approx(0.1279375)

    raw, voltage, _ = MCP3425ADC.decode([0x0F, 0xFF, 0x10])
    assert raw == -1
    assert voltage == pytest.approx(-0.001)


def test_config_0x18_means_16_bit_continuous_pga_x1():
    info = MCP3425ADC.config_info(0x18)
    assert info == {
        "config_byte": 0x18,
        "resolution_bits": 16,
        "pga": 1,
        "continuous": True,
        "ready": True,
        "expected": True,
    }
    # RDY is allowed to change without invalidating the configured mode.
    assert MCP3425ADC.config_info(0x98)["expected"] is True


def test_16_bit_full_scale_sets_saturation_flags():
    high = MCP3425ADC.saturation_flags(32767)
    assert high["adc_saturated"] is True
    assert high["adc_saturated_high"] is True
    assert high["adc_saturated_low"] is False
    assert high["adc_near_saturation"] is True

    low = MCP3425ADC.saturation_flags(-32768)
    assert low["adc_saturated"] is True
    assert low["adc_saturated_low"] is True

    normal = MCP3425ADC.saturation_flags(12000)
    assert normal["adc_saturated"] is False
    assert normal["adc_near_saturation"] is False


class FakeI2CMessage(list):
    def __init__(self, kind, address, values):
        super().__init__(values)
        self.kind = kind
        self.address = address


class FakeMessageFactory:
    @staticmethod
    def write(address, values):
        return FakeI2CMessage("write", address, values)

    @staticmethod
    def read(address, length):
        return FakeI2CMessage("read", address, [0] * length)


class FakeI2CBus:
    def __init__(self, reads):
        self.reads = list(reads)
        self.writes = []
        self.closed = False

    def i2c_rdwr(self, message):
        if message.kind == "write":
            self.writes.append(list(message))
        else:
            message[:] = self.reads.pop(0)

    def close(self):
        self.closed = True


def test_adc_writes_and_verifies_0x18_at_startup():
    bus = FakeI2CBus([[0x00, 0x00, 0x98]])
    adc = MCP3425ADC(bus, message_factory=FakeMessageFactory)
    assert bus.writes == [[0x18]]
    assert adc.configuration_verified is True
    assert adc.last_config == 0x98


def test_adc_rewrites_mismatched_startup_configuration():
    bus = FakeI2CBus([[0x07, 0xFF, 0x10], [0x00, 0x00, 0x98]])
    adc = MCP3425ADC(bus, message_factory=FakeMessageFactory)
    assert bus.writes == [[0x18], [0x18]]
    assert adc.configuration_verified is True


def test_adc_discards_runtime_sample_if_configuration_changed():
    bus = FakeI2CBus([[0x00, 0x00, 0x98], [0x07, 0xFF, 0x10]])
    adc = MCP3425ADC(bus, message_factory=FakeMessageFactory)
    with pytest.raises(ADCConfigurationError, match="conversion discarded"):
        adc.read_conversion()
    assert bus.writes == [[0x18], [0x18]]


def test_adc_returns_only_verified_16_bit_conversion():
    bus = FakeI2CBus([[0x00, 0x00, 0x98], [0x7F, 0xFF, 0x18]])
    adc = MCP3425ADC(bus, message_factory=FakeMessageFactory)
    raw, voltage, config = adc.read_conversion()
    assert raw == 32767
    assert voltage == pytest.approx(2.0479375)
    assert config == 0x18


def test_led_controller_starts_off_and_never_commands_both_high():
    gpio = FakeGPIO()
    leds = LedController(gpio)
    assert leds.snapshot() == {"1450": False, "940": False}
    assert gpio.outputs == {17: 0, 18: 0}

    leds.set_led("1450", True)
    assert leds.snapshot() == {"1450": True, "940": False}
    leds.set_led("940", True)
    assert leds.snapshot() == {"1450": False, "940": True}
    assert all(not (state.get(17) == 1 and state.get(18) == 1) for _, _, state in gpio.history)

    leds.close()
    assert gpio.outputs == {17: 0, 18: 0}
    assert gpio.cleaned == [17, 18]


def test_sampler_produces_16_bit_measurement_contract():
    service, adc, _ = make_service()
    service.start()
    try:
        assert wait_until(lambda: service.status_snapshot()["latest"] is not None)
        status = service.status_snapshot()
        sample = status["latest"]
        assert status["adc_status"] == "ok"
        assert sample["type"] == "sample"
        assert sample["adc_raw"] >= 100
        assert sample["differential_v"] == pytest.approx(sample["adc_raw"] * MCP3425ADC.LSB_VOLTS)
        assert sample["raw_code"] == sample["adc_raw"]
        assert sample["adc_config_byte"] == 0x18
        assert sample["adc_resolution_bits"] == 16
        assert sample["adc_pga"] == 1
        assert sample["adc_saturated"] is False
        assert sample["adc_near_saturation"] is False
        assert sample["leds"] == {"1450": False, "940": False}
    finally:
        service.close()
    assert adc.closed


def test_sampler_marks_full_scale_sample_as_invalid_saturation():
    service, _, _ = make_service()
    service.adc.value = 32766
    service.start()
    try:
        assert wait_until(lambda: service.status_snapshot()["latest"] is not None)
        status = service.status_snapshot()
        sample = status["latest"]
        assert sample["raw_code"] == 32767
        assert sample["adc_saturated"] is True
        assert sample["adc_saturated_high"] is True
        assert sample["adc_near_saturation"] is True
        assert any("not a valid measurement" in warning for warning in status["warnings"])
    finally:
        service.close()


def test_client_timeout_forces_both_leds_off():
    service, _, gpio = make_service(client_timeout_s=0.1)
    service.start()
    try:
        service.command_led("browser-a", "1450", True)
        assert gpio.outputs[17] == 1
        assert wait_until(lambda: service.leds.snapshot() == {"1450": False, "940": False}, timeout=1.0)
        assert "timed out" in service.status_snapshot()["warnings"][0]
    finally:
        service.close()


def test_adc_exception_forces_both_leds_off_and_reports_error(caplog):
    service, adc, _ = make_service()
    client = create_app(service).test_client()
    service.start()
    try:
        service.command_led("browser-a", "940", True)
        adc.error = "private I2C diagnostic"
        assert wait_until(lambda: service.status_snapshot()["adc_status"] == "error")
        status = client.get("/api/status").get_json()
        assert status["leds"] == {"1450": False, "940": False}
        assert status["adc_error"] == "ADC measurement failed."
        assert any("both LEDs were commanded off" in warning for warning in status["warnings"])
        assert adc.error not in json.dumps(status)
        with client.get("/api/stream", buffered=False) as stream:
            packet = json.loads(next(iter(stream.response)))
            assert packet["type"] == "error"
            assert packet["message"] == status["adc_error"]
            assert adc.error not in json.dumps(packet)
        assert wait_until(lambda: adc.error in caplog.text)
    finally:
        service.close()


def test_http_commands_are_strict_and_exclusive():
    service, _, _ = make_service()
    app = create_app(service)
    client = app.test_client()

    page = client.get("/")
    assert page.status_code == 200
    assert "エタノール検出モニター".encode() in page.data
    worker = client.get("/ethanol_detector_stream_worker.js")
    assert worker.status_code == 200
    assert worker.mimetype == "text/javascript"

    response = client.post(
        "/api/led",
        json={"client_id": "browser-a", "wavelength": "1450", "enabled": True},
    )
    assert response.status_code == 200
    assert response.get_json()["leds"] == {"1450": True, "940": False}

    response = client.post(
        "/api/led",
        json={"client_id": "browser-a", "wavelength": "940", "enabled": True},
    )
    assert response.get_json()["leds"] == {"1450": False, "940": True}

    bad = client.post(
        "/api/led",
        json={"client_id": "browser-a", "wavelength": "940", "enabled": 1},
    )
    assert bad.status_code == 400
    assert set(bad.get_json()) == {"error"}

    off = client.post("/api/leds/off", json={})
    assert off.status_code == 200
    assert off.get_json()["leds"] == {"1450": False, "940": False}
    service.close()


def test_gpio_exception_is_logged_without_leaking_to_clients(monkeypatch, caplog):
    service, _, gpio = make_service()
    client = create_app(service).test_client()
    output = gpio.output

    def fail_to_turn_on(pin, value):
        if value == gpio.HIGH:
            raise ValueError("private GPIO diagnostic")
        output(pin, value)

    monkeypatch.setattr(gpio, "output", fail_to_turn_on)
    try:
        response = client.post("/api/led", json={"client_id": "browser-a", "wavelength": "1450", "enabled": True})
        assert response.status_code == 500
        assert response.get_json() == {"error": "LED command failed."}
        status = client.get("/api/status").get_json()
        assert status["leds"] == {"1450": False, "940": False}
        assert "private GPIO diagnostic" not in json.dumps(status)
        assert "private GPIO diagnostic" in caplog.text
    finally:
        service.close()


@pytest.mark.parametrize("value, expected", [("0x68", 0x68), ("104", 0x68), ("0x00", 0)])
def test_parse_i2c_address(value, expected):
    assert parse_i2c_address(value) == expected


def test_rejects_sampling_above_mcp3425_conversion_rate():
    service, _, _ = make_service()
    service.close()
    with pytest.raises(ValueError, match="no more than 15"):
        EthanolDetectorService(FakeADC(), LedController(FakeGPIO()), sample_hz=15.1)
