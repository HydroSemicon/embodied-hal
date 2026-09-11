import pytest


def test_root_advertises_operational_routes(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.get_json()["endpoints"] == [
        "/bme280/sensor_data",
        "/bme280/diagnostics",
        "/cds/sensor_data",
        "/touch/status",
        "/motor/command",
        "/led/command",
    ]


def test_sensor_resources_expose_initial_state(client):
    bme280 = client.get("/bme280/sensor_data").get_json()
    brightness = client.get("/cds/sensor_data").get_json()
    touch = client.get("/touch/status").get_json()

    assert bme280["status"] == "starting"
    assert bme280["temp"] is None
    assert brightness == {"cds": None, "timestamp": None}
    assert touch["sensors"] == {}
    assert touch["queued_events"] == 0


def test_tear_command_returns_mapped_duty_and_stops_motor(client, hal_module):
    response = client.post(
        "/motor/command",
        json={"type": "tear", "params": {"speed": 255, "duration": 0}},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "OK",
        "type": "tear",
        "speed": 255,
        "duty": 100.0,
        "duration": 0,
    }
    assert hal_module.pwm1.duty == 0
    assert hal_module.pwm2.duty == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "tear", "params": {"speed": True, "duration": 0}},
        {"type": "tear", "params": {"speed": 0, "duration": 0, "extra": 1}},
        {"type": "unknown", "params": {"speed": 0, "duration": 0}},
    ],
)
def test_tear_command_rejects_invalid_contract(client, payload):
    response = client.post("/motor/command", json=payload)

    assert response.status_code == 400
    assert set(response.get_json()) == {"error"}


def test_actuator_routes_require_json(client):
    motor = client.post("/motor/command", data="tear")
    led = client.post("/led/command", data="#00FF00")

    assert motor.status_code == 400
    assert led.status_code == 400
    assert motor.get_json() == {"error": "Request body must be JSON"}
    assert led.get_json() == {"error": "Request body must be JSON"}


def test_led_command_normalizes_color_and_maps_pwm(client):
    response = client.post(
        "/led/command",
        json={"type": "led_change", "params": {"color": "#00ff80"}},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "OK",
        "type": "led_change",
        "color": "#00FF80",
        "pwm": {"red": 0, "green": 100, "blue": 50.2},
    }


@pytest.mark.parametrize("color", ["00FF00", "#GG0000", "#00000000", 123])
def test_led_command_rejects_invalid_color(client, color):
    response = client.post(
        "/led/command",
        json={"type": "led_change", "params": {"color": color}},
    )

    assert response.status_code == 400
    assert set(response.get_json()) == {"error"}
