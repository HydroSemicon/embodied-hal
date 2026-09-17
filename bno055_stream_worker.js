"use strict";

async function connect() {
  while (true) {
    try {
      postMessage({ type: "state", connected: true, message: "接続中…" });
      const response = await fetch("/api/stream", { cache: "no-store" });
      if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) throw new Error("stream closed");
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop();
        for (const line of lines) {
          if (line.trim()) postMessage({ type: "packet", packet: JSON.parse(line) });
        }
      }
    } catch (error) {
      postMessage({ type: "state", connected: false, message: `再接続待ち · ${error.message}` });
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
}

connect();
