const http = require("http");

// Use IPv4 explicitly because the receiver listens on 0.0.0.0.  On systems
// where localhost resolves to ::1 first, using localhost can time out.
const ENDPOINT_URL = "http://127.0.0.1:3000/touch_sensor_input";
const SENSOR_IDS = ["touch_01", "touch_02", "touch_03"];
const EVENT_TYPES = ["touch_started", "touch_ended"];
const REQUEST_DELAY_MS = 300;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function postTouchEvent(sensorId, eventType) {
  const payload = JSON.stringify({
    event: {
      source: "touch",
      type: eventType,
      sensor_id: sensorId,
    },
  });

  const url = new URL(ENDPOINT_URL);

  const options = {
    hostname: url.hostname,
    port: url.port || 80,
    path: url.pathname,
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Content-Length": Buffer.byteLength(payload),
    },
    timeout: 5000,
  };

  return new Promise((resolve, reject) => {
    const req = http.request(options, (res) => {
      let body = "";

      res.setEncoding("utf8");
      res.on("data", (chunk) => {
        body += chunk;
      });
      res.on("end", () => {
        resolve({
          statusCode: res.statusCode,
          body,
        });
      });
    });

    req.on("timeout", () => {
      req.destroy(new Error("Request timed out"));
    });

    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

async function main() {
  console.log(`POST test target: ${ENDPOINT_URL}`);

  for (const sensorId of SENSOR_IDS) {
    for (const eventType of EVENT_TYPES) {
      try {
        const response = await postTouchEvent(sensorId, eventType);
        const ok =
          response.statusCode >= 200 && response.statusCode < 300
            ? "ok"
            : "failed";

        console.log(
          `${sensorId}: ${eventType} POST ${ok} (${response.statusCode})`
        );

        if (response.body) {
          console.log(`response: ${response.body}`);
        }
      } catch (error) {
        console.log(`${sensorId}: ${eventType} POST failed: ${error.message}`);
      }

      await sleep(REQUEST_DELAY_MS);
    }
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
