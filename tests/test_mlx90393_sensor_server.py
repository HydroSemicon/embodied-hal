import json

from mlx90393_sensor_server import SensorSampler, create_app


class FakeSensor:
    def __init__(self):
        self.value = 0

    @property
    def magnetic(self):
        self.value += 1
        return self.value, self.value + 1, self.value + 2


def test_sampler_and_http_stream_publish_sensor_values():
    sampler = SensorSampler(FakeSensor(), target_hz=100.0)
    sampler.start()
    try:
        sample, error = sampler.wait_next(after_seq=-1, timeout=1.0)
        assert error is None
        assert sample["seq"] >= 1
        assert len(sample["field_uT"]) == 3

        client = create_app(sampler).test_client()
        status = client.get("/api/status").get_json()
        assert status["status"] == "ok"
        assert status["latest"]["type"] == "sample"

        response = client.get("/api/stream", buffered=False)
        packet = json.loads(next(response.response))
        assert packet["type"] == "sample"
        assert packet["seq"] >= sample["seq"]
        response.close()
    finally:
        sampler.close()


def test_sampler_rejects_non_positive_rate():
    try:
        SensorSampler(FakeSensor(), target_hz=0)
    except ValueError as exc:
        assert str(exc) == "target_hz must be positive"
    else:
        raise AssertionError("non-positive sample rate was accepted")
