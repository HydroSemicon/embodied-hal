const http = require("http");

const HOST = "0.0.0.0";
const PORT = 3000;
const ENDPOINT_PATH = "/touch_sensor_input";

const VALID_EVENT_TYPES = new Set(["touch_started", "touch_ended"]);
const VALID_SENSOR_IDS = new Set(["touch_01", "touch_02", "touch_03"]);

function sendJson(res, statusCode, body) {
  const payload = JSON.stringify(body);

  res.writeHead(statusCode, {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
  });
  res.end(payload);
}

function validateTouchEvent(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return "Request body must be a JSON object";
  }

  const event = payload.event;
  if (!event || typeof event !== "object" || Array.isArray(event)) {
    return "Request body must contain an event object";
  }

  if (event.source !== "touch") {
    return 'event.source must be "touch"';
  }

  if (!VALID_EVENT_TYPES.has(event.type)) {
    return 'event.type must be "touch_started" or "touch_ended"';
  }

  if (!VALID_SENSOR_IDS.has(event.sensor_id)) {
    return 'event.sensor_id must be "touch_01", "touch_02", or "touch_03"';
  }

  return null;
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    let body = "";

    req.setEncoding("utf8");

    req.on("data", (chunk) => {
      body += chunk;

      if (body.length > 1024 * 1024) {
        req.destroy(new Error("Request body is too large"));
      }
    });

    req.on("end", () => {
      try {
        resolve(JSON.parse(body || "{}"));
      } catch {
        reject(new Error("Request body must be valid JSON"));
      }
    });

    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  if (req.method !== "POST" || req.url !== ENDPOINT_PATH) {
    sendJson(res, 404, { error: "Not found" });
    return;
  }

  try {
    const payload = await readJsonBody(req);
    const validationError = validateTouchEvent(payload);

    if (validationError) {
      sendJson(res, 400, { error: validationError });
      return;
    }

    const event = payload.event;
    const timestamp = new Date().toISOString();

    console.log(
      `[${timestamp}] ${event.sensor_id}: ${event.type} source=${event.source}`
    );

    sendJson(res, 200, {
      ok: true,
      received: event,
    });
  } catch (error) {
    sendJson(res, 400, { error: error.message });
  }
});

server.listen(PORT, HOST, () => {
  console.log(`Touch endpoint server listening on http://${HOST}:${PORT}`);
  console.log("Use this machine's LAN IP from Raspberry Pi, for example:");
  console.log(`http://192.168.0.42:${PORT}${ENDPOINT_PATH}`);
  console.log(`POST ${ENDPOINT_PATH}`);
});

process.on("SIGINT", () => {
  console.log("\nStopping touch endpoint server");
  server.close(() => {
    process.exit(0);
  });
});
