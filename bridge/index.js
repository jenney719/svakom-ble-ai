import express from "express";

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const SECRET = process.env.BRIDGE_SECRET || "";

const queue = [];
let lastPoll = 0;

function authorized(req) {
  const headerSecret = req.get("x-bridge-secret");
  const querySecret = req.query.secret;

  if (!SECRET) return true;
  return headerSecret === SECRET || querySecret === SECRET;
}

function enqueue(command) {
  queue.push(command);
  if (queue.length > 50) queue.shift();
}

app.get("/", (req, res) => {
  res.json({
    ok: true,
    service: "svakom-bridge",
    mcp: "/mcp",
    poll: "/toy-next"
  });
});

app.get("/health", (req, res) => {
  res.json({ ok: true });
});

app.get("/toy-next", (req, res) => {
  if (!authorized(req)) {
    return res.status(401).json({ error: "unauthorized" });
  }

  lastPoll = Date.now();

  const command = queue.shift();

  if (!command) {
    return res.json({ type: "hello", online: true });
  }

  res.json(command);
});

/*
 * Minimal MCP HTTP endpoint.
 * Supports:
 *   initialize
 *   tools/list
 *   tools/call
 */
app.post("/mcp", (req, res) => {
  if (!authorized(req)) {
    return res.status(401).json({
      jsonrpc: "2.0",
      error: {
        code: -32001,
        message: "Unauthorized"
      },
      id: req.body?.id ?? null
    });
  }

  const msg = req.body || {};
  const id = msg.id ?? null;

  if (msg.method === "initialize") {
    return res.json({
      jsonrpc: "2.0",
      id,
      result: {
        protocolVersion: "2024-11-05",
        capabilities: {
          tools: {}
        },
        serverInfo: {
          name: "svakom-bridge",
          version: "1.0.0"
        }
      }
    });
  }

  if (msg.method === "notifications/initialized") {
    return res.status(202).end();
  }

  if (msg.method === "tools/list") {
    return res.json({
      jsonrpc: "2.0",
      id,
      result: {
        tools: [
          {
            name: "toy_set_speed",
            description: "Set device intensity from 0.0 to 1.0.",
            inputSchema: {
              type: "object",
              properties: {
                speed: {
                  type: "number",
                  minimum: 0,
                  maximum: 1
                },
                sec: {
                  type: "number",
                  minimum: 0
                }
              },
              required: ["speed"]
            }
          },
          {
            name: "toy_set_pattern",
            description: "Set vibration pattern.",
            inputSchema: {
              type: "object",
              properties: {
                pattern: {
                  type: "integer",
                  minimum: 1,
                  maximum: 8
                },
                level: {
                  type: "number",
                  minimum: 0,
                  maximum: 1
                },
                sec: {
                  type: "number",
                  minimum: 0
                }
              },
              required: ["pattern"]
            }
          },
          {
            name: "toy_stop",
            description: "Stop the device immediately.",
            inputSchema: {
              type: "object",
              properties: {}
            }
          },
          {
            name: "toy_status",
            description: "Check whether the BLE bridge is polling this server.",
            inputSchema: {
              type: "object",
              properties: {}
            }
          }
        ]
      }
    });
  }

  if (msg.method === "tools/call") {
    const name = msg.params?.name;
    const args = msg.params?.arguments || {};

    if (name === "toy_set_speed") {
      const speed = Math.max(0, Math.min(1, Number(args.speed)));

      if (!Number.isFinite(speed)) {
        return res.json({
          jsonrpc: "2.0",
          id,
          error: {
            code: -32602,
            message: "speed must be a number between 0 and 1"
          }
        });
      }

      enqueue({
        speed,
        ...(args.sec ? { sec: Number(args.sec) } : {})
      });

      return res.json({
        jsonrpc: "2.0",
        id,
        result: {
          content: [
            {
              type: "text",
              text: `Queued speed ${Math.round(speed * 100)}%.`
            }
          ]
        }
      });
    }

    if (name === "toy_set_pattern") {
      const pattern = Number(args.pattern);
      const level =
        args.level === undefined
          ? 0.6
          : Math.max(0, Math.min(1, Number(args.level)));

      if (!Number.isInteger(pattern) || pattern < 1 || pattern > 8) {
        return res.json({
          jsonrpc: "2.0",
          id,
          error: {
            code: -32602,
            message: "pattern must be an integer from 1 to 8"
          }
        });
      }

      enqueue({
        pattern,
        level,
        ...(args.sec ? { sec: Number(args.sec) } : {})
      });

      return res.json({
        jsonrpc: "2.0",
        id,
        result: {
          content: [
            {
              type: "text",
              text: `Queued pattern ${pattern}.`
            }
          ]
        }
      });
    }

    if (name === "toy_stop") {
      enqueue({ stop: true });

      return res.json({
        jsonrpc: "2.0",
        id,
        result: {
          content: [
            {
              type: "text",
              text: "Stop command queued."
            }
          ]
        }
      });
    }

    if (name === "toy_status") {
      const online =
        lastPoll > 0 && Date.now() - lastPoll < 5000;

      return res.json({
        jsonrpc: "2.0",
        id,
        result: {
          content: [
            {
              type: "text",
              text: online
                ? "BLE bridge is online and polling."
                : "BLE bridge is offline or not polling."
            }
          ]
        }
      });
    }

    return res.json({
      jsonrpc: "2.0",
      id,
      error: {
        code: -32601,
        message: `Unknown tool: ${name}`
      }
    });
  }

  return res.json({
    jsonrpc: "2.0",
    id,
    error: {
      code: -32601,
      message: `Unknown method: ${msg.method}`
    }
  });
});

app.listen(PORT, "0.0.0.0", () => {
  console.log(`svakom-bridge listening on ${PORT}`);
});
