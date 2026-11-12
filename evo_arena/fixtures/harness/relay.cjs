const http = require("node:http");
const fs = require("node:fs/promises");
const { randomUUID } = require("node:crypto");
const { setTimeout: delay } = require("node:timers/promises");

async function handle(req, res) {
  const routes = { "GET /v1/models": "models", "POST /v1/responses": "model",
    "POST /order": "submit", "GET /order": "status", "POST /business": "business" };
  const route = routes[req.method + " " + req.url];
  if (!route) {
    res.writeHead(404);
    return res.end();
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 2_000_000) throw new Error("Request too large");
    chunks.push(chunk);
  }
  const base = "/arena-relay/" + randomUUID();
  await fs.writeFile(base + ".tmp", JSON.stringify({ route, body: Buffer.concat(chunks).toString() }));
  await fs.rename(base + ".tmp", base + ".request");
  // Harbor owns the task deadline. A model request can take more than 90s.
  // Stop polling when the caller disconnects; never impose a second deadline.
  while (!res.destroyed) {
    try {
      const reply = JSON.parse(await fs.readFile(base + ".reply", "utf8"));
      await fs.unlink(base + ".reply");
      res.writeHead(reply.status, { "content-type": reply.type });
      return res.end(reply.body);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      await delay(100);
    }
  }
}

http.createServer((req, res) => {
  handle(req, res).catch(() => {
    if (!res.headersSent) res.writeHead(502, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: { message: "Model relay failed" } }));
  });
}).listen(18765, "127.0.0.1");
