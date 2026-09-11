#!/usr/bin/env python3

import argparse
import json
import math
import sys
import time
from pathlib import Path

import board # type: ignore
import adafruit_bno055 # type: ignore


# ============================================================
# Quaternion utilities
# Convention:
#   q = (w, x, y, z)
#   Hamilton product
# ============================================================

def quat_normalize(q):
    if q is None or len(q) != 4 or any(v is None for v in q):
        return None

    n = math.sqrt(sum(v * v for v in q))
    if n < 1e-12:
        return None

    return tuple(v / n for v in q)


def quat_conjugate(q):
    w, x, y, z = q
    return (w, -x, -y, -z)


def quat_multiply(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b

    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def quat_dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def quat_make_continuous(q, q_previous):
    """
    q and -q represent the same rotation.

    Choose the sign closest to the previous quaternion so that
    the numerical quaternion trajectory remains continuous.
    """
    if q_previous is None:
        return q

    if quat_dot(q, q_previous) < 0.0:
        return tuple(-v for v in q)

    return q


def relative_quaternion(q_reference, q_current):
    """
    Rotation relative to the reference orientation.

        q_rel = inverse(q_ref) * q_current

    Unit quaternions satisfy inverse(q) = conjugate(q).
    """
    q_rel = quat_multiply(
        quat_conjugate(q_reference),
        q_current,
    )

    return quat_normalize(q_rel)


def quat_to_rpy_zyx_deg(q):
    """
    Conventional ZYX yaw-pitch-roll decomposition.

    Returns:
        roll, pitch, yaw [deg]

    This is mainly for human-readable presentation.
    Internally, keep using the quaternion.
    """
    w, x, y, z = q

    # Roll: rotation about X
    sinr_cosp = 2.0 * (w * x + y * z)
    cosr_cosp = 1.0 - 2.0 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # Pitch: rotation about Y
    sinp = 2.0 * (w * y - z * x)
    sinp = max(-1.0, min(1.0, sinp))
    pitch = math.asin(sinp)

    # Yaw: rotation about Z
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return tuple(
        math.degrees(v)
        for v in (roll, pitch, yaw)
    )


def quat_to_rotation_matrix(q):
    """
    3x3 rotation matrix corresponding to q.
    """
    w, x, y, z = q

    return (
        (
            1.0 - 2.0 * (y*y + z*z),
            2.0 * (x*y - w*z),
            2.0 * (x*z + w*y),
        ),
        (
            2.0 * (x*y + w*z),
            1.0 - 2.0 * (x*x + z*z),
            2.0 * (y*z - w*x),
        ),
        (
            2.0 * (x*z - w*y),
            2.0 * (y*z + w*x),
            1.0 - 2.0 * (x*x + y*y),
        ),
    )


# ============================================================
# Calibration storage
# ============================================================

def save_calibration(sensor, filename):
    data = {
        "offsets_accelerometer":
            list(sensor.offsets_accelerometer),

        "offsets_magnetometer":
            list(sensor.offsets_magnetometer),

        "offsets_gyroscope":
            list(sensor.offsets_gyroscope),

        "radius_accelerometer":
            sensor.radius_accelerometer,

        "radius_magnetometer":
            sensor.radius_magnetometer,
    }

    Path(filename).write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


def load_calibration(sensor, filename):
    path = Path(filename)

    if not path.exists():
        return False

    data = json.loads(
        path.read_text(encoding="utf-8")
    )

    sensor.offsets_accelerometer = tuple(
        data["offsets_accelerometer"]
    )

    sensor.offsets_magnetometer = tuple(
        data["offsets_magnetometer"]
    )

    sensor.offsets_gyroscope = tuple(
        data["offsets_gyroscope"]
    )

    sensor.radius_accelerometer = int(
        data["radius_accelerometer"]
    )

    sensor.radius_magnetometer = int(
        data["radius_magnetometer"]
    )

    return True


# ============================================================
# Helpers
# ============================================================

def list_or_none(v):
    if v is None:
        return None

    if any(x is None for x in v):
        return None

    return [float(x) for x in v]


# ============================================================
# Main
# ============================================================

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--rate",
        type=float,
        default=50.0,
        help="Quaternion sampling rate [Hz]"
    )

    parser.add_argument(
        "--diag-rate",
        type=float,
        default=1.0,
        help="Full diagnostic read rate [Hz]"
    )

    parser.add_argument(
        "--calibration-file",
        default="bno055_calibration.json",
    )

    parser.add_argument(
        "--no-load-calibration",
        action="store_true",
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # I2C / sensor initialization
    # --------------------------------------------------------

    i2c = board.I2C()

    sensor = adafruit_bno055.BNO055_I2C(
        i2c,
        address=0x28,
    )

    # Highest-quality absolute 9-axis fusion mode.
    sensor.mode = adafruit_bno055.NDOF_MODE

    # AE-BNO055-BO has an external 32.768-kHz crystal.
    sensor.use_external_crystal = True

    print(
        "BNO055 initialized in NDOF mode.",
        file=sys.stderr,
    )

    print(
        f"External crystal: {sensor.external_crystal}",
        file=sys.stderr,
    )

    # --------------------------------------------------------
    # Restore calibration
    # --------------------------------------------------------

    if not args.no_load_calibration:
        try:
            if load_calibration(
                sensor,
                args.calibration_file,
            ):
                print(
                    "Calibration data loaded.",
                    file=sys.stderr,
                )
            else:
                print(
                    "No calibration file found.",
                    file=sys.stderr,
                )

        except Exception as e:
            print(
                f"Calibration load failed: {e}",
                file=sys.stderr,
            )

    # --------------------------------------------------------
    # Sampling state
    # --------------------------------------------------------

    period = 1.0 / args.rate
    diag_period = 1.0 / args.diag_rate

    start_time = time.monotonic()
    next_sample = start_time
    next_diag = start_time

    q_reference = None
    q_previous = None

    calibration_saved = False

    while True:

        now = time.monotonic()

        try:
            # ------------------------------------------------
            # High-rate attitude acquisition
            # ------------------------------------------------

            q_raw = sensor.quaternion
            q = quat_normalize(q_raw)

            if q is None:
                raise RuntimeError(
                    "Invalid quaternion from BNO055"
                )

            # Avoid q -> -q numerical discontinuity.
            q = quat_make_continuous(
                q,
                q_previous,
            )

            q_previous = q

            # The first valid orientation becomes zero/reference.
            if q_reference is None:
                q_reference = q

            q_rel = relative_quaternion(
                q_reference,
                q,
            )

            roll, pitch, yaw = quat_to_rpy_zyx_deg(
                q_rel
            )

            output = {
                "t": now - start_time,

                # BNO055 absolute orientation
                "quaternion_abs_wxyz": [
                    float(x) for x in q
                ],

                # Boot-relative orientation
                "quaternion_rel_wxyz": [
                    float(x) for x in q_rel
                ],

                # Human-readable relative angles
                "rpy_rel_deg": {
                    "roll": roll,
                    "pitch": pitch,
                    "yaw": yaw,
                },
            }

            # ------------------------------------------------
            # Slow diagnostics
            #
            # These require additional I2C transactions.
            # Do not read all registers at 100 Hz unnecessarily.
            # ------------------------------------------------

            if now >= next_diag:

                euler = sensor.euler
                gyro = sensor.gyro
                linear_accel = sensor.linear_acceleration
                gravity = sensor.gravity
                magnetic = sensor.magnetic
                temperature = sensor.temperature

                sys_cal, gyro_cal, accel_cal, mag_cal = (
                    sensor.calibration_status
                )

                diag = {
                    # Native BNO055 Euler output:
                    # heading, roll, pitch
                    "bno_euler_deg": (
                        None if euler is None else {
                            "heading": euler[0],
                            "roll": euler[1],
                            "pitch": euler[2],
                        }
                    ),

                    "gyro_rad_s":
                        list_or_none(gyro),

                    "linear_acceleration_m_s2":
                        list_or_none(linear_accel),

                    "gravity_m_s2":
                        list_or_none(gravity),

                    "magnetic_uT":
                        list_or_none(magnetic),

                    "temperature_C":
                        temperature,

                    "calibration": {
                        "system": sys_cal,
                        "gyro": gyro_cal,
                        "accel": accel_cal,
                        "mag": mag_cal,
                    },
                }

                output["diagnostics"] = diag

                # Save calibration once all four states become 3.
                if (
                    not calibration_saved
                    and
                    (sys_cal, gyro_cal, accel_cal, mag_cal)
                    == (3, 3, 3, 3)
                ):
                    try:
                        save_calibration(
                            sensor,
                            args.calibration_file,
                        )

                        calibration_saved = True

                        print(
                            "Fully calibrated. "
                            "Calibration data saved.",
                            file=sys.stderr,
                        )

                    except Exception as e:
                        print(
                            f"Calibration save failed: {e}",
                            file=sys.stderr,
                        )

                next_diag = now + diag_period

            print(
                json.dumps(
                    output,
                    separators=(",", ":"),
                ),
                flush=True,
            )

        except (OSError, RuntimeError) as e:
            print(
                f"BNO055 read error: {e}",
                file=sys.stderr,
            )

            time.sleep(0.05)

        # ----------------------------------------------------
        # Fixed-rate scheduling
        # ----------------------------------------------------

        next_sample += period

        remaining = (
            next_sample - time.monotonic()
        )

        if remaining > 0:
            time.sleep(remaining)

        elif remaining < -5.0 * period:
            # Resynchronize after a long delay.
            next_sample = time.monotonic()


if __name__ == "__main__":
    main()