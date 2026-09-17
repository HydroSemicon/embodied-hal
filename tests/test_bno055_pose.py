import json
import math
import time

import pytest

from bno055_pose import QuaternionSpikeFilter, PoseSampler, create_app, quat_to_rpy_zyx_deg, relative_quaternion


class FakeSensor:
    def __init__(self):
        self.angle = 0.0
        self.mode = 0x0C
        self.offsets_accelerometer = (0, 0, 0)
        self.offsets_magnetometer = (0, 0, 0)
        self.offsets_gyroscope = (0, 0, 0)
        self.radius_accelerometer = 1
        self.radius_magnetometer = 1

    @property
    def quaternion(self):
        self.angle += math.radians(1)
        return (math.cos(self.angle / 2), 0.0, 0.0, math.sin(self.angle / 2))

    euler = (0.0, 0.0, 0.0)
    calibration_status = (0, 1, 2, 3)
    gyro = (0.0, 0.0, 0.1)
    linear_acceleration = (0.0, 0.0, 0.0)
    gravity = (0.0, 0.0, 9.81)
    magnetic = (20.0, 1.0, -40.0)
    temperature = 25


class WarmupSensor(FakeSensor):
    def __init__(self, invalid_reads):
        super().__init__()
        self.invalid_reads = invalid_reads

    @property
    def quaternion(self):
        if self.invalid_reads > 0:
            self.invalid_reads -= 1
            return (0.0, 0.0, 0.0, 0.0)
        return super().quaternion


class InvalidSensor(FakeSensor):
    @property
    def quaternion(self):
        return (0.0, 0.0, 0.0, 0.0)


def test_relative_quaternion_and_angles_use_wxyz_order():
    identity = (1.0, 0.0, 0.0, 0.0)
    yaw_90 = (math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5))
    relative = relative_quaternion(identity, yaw_90)
    roll, pitch, yaw = quat_to_rpy_zyx_deg(relative)
    assert roll == 0.0
    assert pitch == 0.0
    assert yaw == pytest.approx(90.0)


def test_sampler_stream_status_and_recenter(tmp_path):
    sampler = PoseSampler(
        FakeSensor(),
        target_hz=100.0,
        diagnostic_hz=10.0,
        calibration_file=tmp_path / "calibration.json",
    )
    sampler.start()
    try:
        sample, error = sampler.wait_next(after_seq=-1, timeout=1.0)
        assert error is None
        assert sample["type"] == "pose"
        assert len(sample["quaternion_rel_wxyz"]) == 4
        assert sample["diagnostics"]["calibration"]["mag"] == 3

        app = create_app(sampler)
        client = app.test_client()
        status = client.get("/api/status").get_json()
        assert status["status"] == "ok"
        assert status["target_hz"] == 100.0
        page = client.get("/")
        assert page.status_code == 200
        assert b"BNO055 Pose Monitor" in page.data
        worker = client.get("/bno055_stream_worker.js")
        assert worker.status_code == 200
        assert worker.mimetype == "text/javascript"

        response = client.get("/api/stream", buffered=False)
        packet = json.loads(next(response.response))
        assert packet["type"] == "pose"
        assert packet["seq"] >= sample["seq"]
        response.close()

        reference_before = sampler.reference
        assert client.post("/api/recenter").get_json() == {"status": "queued"}
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with sampler.condition:
                if not sampler.recenter_requested and sampler.reference != reference_before:
                    break
            time.sleep(0.001)
        assert sampler.reference != reference_before
    finally:
        sampler.close()


def test_sampler_rejects_invalid_rates():
    sensor = FakeSensor()
    try:
        PoseSampler(sensor, target_hz=0)
    except ValueError as exc:
        assert str(exc) == "target_hz must be positive"
    else:
        raise AssertionError("non-positive sample rate was accepted")


def test_sampler_treats_initial_zero_quaternion_as_warmup(tmp_path):
    sampler = PoseSampler(
        WarmupSensor(invalid_reads=3),
        target_hz=100.0,
        diagnostic_hz=10.0,
        calibration_file=tmp_path / "calibration.json",
        invalid_quaternion_timeout_s=1.0,
    )
    sampler.start()
    try:
        sample, error = sampler.wait_next(after_seq=-1, timeout=1.0)
        assert error is None
        assert sample["type"] == "pose"
    finally:
        sampler.close()


def test_persistent_zero_quaternion_reports_raw_value_and_mode(tmp_path):
    sampler = PoseSampler(
        InvalidSensor(),
        target_hz=100.0,
        diagnostic_hz=10.0,
        calibration_file=tmp_path / "calibration.json",
        invalid_quaternion_timeout_s=0.02,
    )
    sampler.start()
    try:
        _, error = sampler.wait_next(after_seq=-1, timeout=1.0)
        assert "raw=(0.0, 0.0, 0.0, 0.0)" in error
        assert "mode=0x0c" in error
        assert "--external-crystal" in error
    finally:
        sampler.close()


def test_quaternion_spike_filter_rejects_single_sample_jump():
    pose_filter = QuaternionSpikeFilter(threshold_deg=8.0)
    identity = (1.0, 0.0, 0.0, 0.0)
    yaw_90 = (math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5))

    assert pose_filter.update(identity) == (identity, False)
    assert pose_filter.update(yaw_90) == (identity, True)
    assert pose_filter.update(identity) == (identity, False)
    assert pose_filter.suppressed_count == 1


def test_quaternion_spike_filter_accepts_sustained_large_motion():
    pose_filter = QuaternionSpikeFilter(threshold_deg=8.0)
    identity = (1.0, 0.0, 0.0, 0.0)
    yaw_90 = (math.sqrt(0.5), 0.0, 0.0, math.sqrt(0.5))

    pose_filter.update(identity)
    assert pose_filter.update(yaw_90) == (identity, True)
    assert pose_filter.update(yaw_90) == (yaw_90, False)
