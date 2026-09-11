# Archived material

This directory contains files that are not part of the operational Kokomi Embodied HAL service. They remain available for hardware investigation and historical reference, but they do not define the current runtime contract.

## Experiments

| File | Historical purpose |
| --- | --- |
| `experiments/bme280-plot.py` | Realtime BME280 pressure plotting |
| `experiments/bno055_pose.py` | Standalone BNO055 quaternion, pose, and calibration experiment |
| `experiments/cds_cell_basic.py` | Standalone MCP3002/CdS console reader |
| `experiments/motor_basic.py` | Continuous forward/stop/reverse motor exercise |
| `experiments/rgb_led_basic.py` | Standalone RGB color-cycle exercise |
| `experiments/test_touch_endpoint.js` | Synthetic touch-event sender |
| `experiments/touch_endpoint_server.js` | Mock touch-event receiver used before Kernel integration |
| `experiments/touch_sensor_post.py` | Standalone three-channel touch-event sender |

## Superseded adapters

| File | Historical purpose |
| --- | --- |
| `legacy/bme280-flask.py` | BME280-only Flask service |
| `legacy/cds_cell_flask.py` | CdS-only Flask service |
| `legacy/mitsuki.py` | Legacy hexadecimal motor command service |
| `legacy/motor_flask_tear.py` | Plain-text tear command service |
| `legacy/motor_stdio_tear.py` | Standard-input tear command adapter |
| `legacy/rgb_led_flask.py` | Legacy hexadecimal RGB command service |

## Debug and prompt artifacts

| File | Historical purpose |
| --- | --- |
| `debug/flask_debug.py` | Raw HTTP POST body inspection server |
| `prompts/prompt.md` | Implementation prompt used for the JSON actuator migration |

Archived programs may bind the same GPIO pins, buses, or TCP ports as the operational service. They must not be run concurrently with `kokomi_raspi.py` unless their hardware bindings are deliberately changed.
