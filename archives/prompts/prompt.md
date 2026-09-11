You are modifying a Raspberry Pi Flask hardware server.

File:
- kokomi_raspi.py

Goal:
Migrate the actuator command endpoints from legacy HEX command strings to JSON commands.

Current behavior:
- /motor/command receives text like "MTFF0A00;"
- /led/command receives text like "LT010000;"
- The server parses these legacy strings and controls GPIO/PWM.
- BME280 and CdS sensor endpoints already work and should be preserved.

New behavior:
- /motor/command must receive JSON only.
- /led/command must receive JSON only.
- Do not support old MT/LT command strings anymore.
- Keep the same Flask endpoints:
  - POST /motor/command
  - POST /led/command
  - GET /bme280/sensor_data
  - GET /cds/sensor_data
- Keep the same port 5000.
- Keep existing BME280, CdS, GPIO, PWM, fade_worker, cleanup behavior unless necessary.

Motor JSON format:
{
  "type": "tear",
  "params": {
    "speed": 10,
    "duration": 5
  }
}

Validation:
- Request body must be valid JSON.
- Top-level object must contain exactly:
  - type
  - params
- type must be exactly "tear".
- params must contain exactly:
  - speed
  - duration
- speed must be an integer from 0 to 255.
- duration must be an integer from 0 to 255.
- Any missing field, extra field, wrong type, or out-of-range value must return HTTP 400.
- On invalid motor command, call motor_stop() before returning the error.

Motor execution:
- Convert speed to PWM duty cycle using the same logic as before:
  duty = 40 + (speed / 255.0) * 60
- duration is seconds.
- Run motor_forward(duty), sleep(duration), then motor_stop().
- Return JSON:
{
  "status": "OK",
  "type": "tear",
  "speed": 10,
  "duty": 42.4,
  "duration": 5
}

LED JSON format:
{
  "type": "led_change",
  "params": {
    "color": "#00FF00"
  }
}

Validation:
- Request body must be valid JSON.
- Top-level object must contain exactly:
  - type
  - params
- type must be exactly "led_change".
- params must contain exactly:
  - color
- color must be a string in #RRGGBB format.
- Any missing field, extra field, wrong type, or invalid color must return HTTP 400.

LED execution:
- Parse #RRGGBB into red, green, blue values.
- Convert each 0–255 channel into PWM duty cycle 0–100.
  Example:
  "#00FF00" -> red=0, green=100, blue=0
- Use set_target_color(red, green, blue).
- Keep the existing fade_worker behavior.
- Return JSON:
{
  "status": "OK",
  "type": "led_change",
  "color": "#00FF00",
  "pwm": {
    "red": 0,
    "green": 100,
    "blue": 0
  }
}

Remove or stop using:
- parse_mt_command()
- parse_lt_command()
- apply_emotion()
- form/text fallback command parsing

Add helper functions:
- is_plain_object(value)
- has_exactly_keys(obj, keys)
- error_response(message, status=400)
- parse_json_request()
- validate_tear_command(payload)
- validate_led_command(payload)
- hex_color_to_pwm(color)

Important:
- Do not introduce new external dependencies.
- Keep Python code simple and readable.
- Preserve GPIO pin assignments.
- Preserve sensor workers.
- Preserve cleanup().
- Preserve threaded Flask server behavior.
- Do not change endpoint paths.
- Do not change BME280/CdS response format unless necessary.
- Return clear JSON error messages for invalid requests.

Output:
Return the complete updated kokomi_raspi.py.