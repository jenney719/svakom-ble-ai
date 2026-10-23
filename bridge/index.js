import express from "express";

const app = express();
app.use(express.json());

const PORT = process.env.PORT || 3000;
const BRIDGE_SECRET = process.env.BRIDGE_SECRET || "";

const queue = [];
let nextId = 1;

const toolResult = (text) => ({
  content: [{ type: "text", text }],
});

function checkAuth(req) {
  const secret = req.headers["x-bridge-secret"] || req.query.secret || "";
  return !BRIDGE_SECRET || secret === BRIDGE_SECRET;
}

function enqueue(command) {
  queue.push({
    id: nextId++,
    ...command,
    createdAt: Date.now(),
  });

  // 防止极端情况下队列无限增长
  while (queue.length > 100) queue.shift();

  return queue[queue.length - 1];
}

app.get("/", (req, res) => {
  res.json({
    name: "svakom-ble-ai bridge",
    status: "online",
    mcp: "/mcp",
    next: "/toy-next",
    health: "/health",
  });
});

app.get("/health", (req, res) => {
  res.json({
    ok: true,
    service: "svakom-ble-ai",
  });
});

app.get("/toy-next", (req, res) => {
  if (!checkAuth(req)) {
    return res.status(401).json({ error: "Unauthorized" });
  }

  const command = queue.shift();

  if (!command) {
    return res.json({ type: "hello" });
  }

  res.json(command);
});

function listTools() {
  return [
    {
      name: "toy_status",
      description:
        "检查 Windows BLE bridge 是否在线并正在轮询 Railway。",
      inputSchema: {
        type: "object",
        properties: {},
        additionalProperties: false,
      },
    },

    {
      name: "toy_set_speed",
      description:
        "兼容旧版：设置设备总强度。speed 使用 0 到 1，sec 为持续秒数。",
      inputSchema: {
        type: "object",
        properties: {
          speed: {
            type: "number",
            description: "总强度，0 到 1",
          },
          sec: {
            type: "number",
            description: "持续秒数；建议填写",
          },
        },
        required: ["speed"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_set_pattern",
      description:
        "兼容旧版：设置震动模式。现在支持模式 1 到 10、强度 1 到 10。",
      inputSchema: {
        type: "object",
        properties: {
          pattern: {
            type: "integer",
            description: "震动模式 1 到 10",
          },
          level: {
            type: "integer",
            description: "震动强度 1 到 10",
          },
          sec: {
            type: "number",
            description: "持续秒数",
          },
        },
        required: ["pattern", "level"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_vibrate",
      description:
        "设置震动：模式 1 到 10，强度 1 到 10，可指定持续秒数。",
      inputSchema: {
        type: "object",
        properties: {
          mode: {
            type: "integer",
            description: "模式 1 到 10",
          },
          level: {
            type: "integer",
            description: "强度 1 到 10",
          },
          sec: {
            type: "number",
            description: "持续秒数",
          },
        },
        required: ["mode", "level"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_auto",
      description:
        "设置自动模式总强度。强度 0 到 255，可指定持续秒数。",
      inputSchema: {
        type: "object",
        properties: {
          level: {
            type: "integer",
            description: "总强度 0 到 255",
          },
          sec: {
            type: "number",
            description: "持续秒数",
          },
        },
        required: ["level"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_patting",
      description:
        "设置拍打档位 1 到 7，可指定持续秒数。",
      inputSchema: {
        type: "object",
        properties: {
          level: {
            type: "integer",
            description: "拍打档位 1 到 7",
          },
          sec: {
            type: "number",
            description: "持续秒数",
          },
        },
        required: ["level"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_thrust",
      description:
        "设置伸缩档位 1 到 7，可指定持续秒数。只有档位，没有独立强度。",
      inputSchema: {
        type: "object",
        properties: {
          level: {
            type: "integer",
            description: "伸缩档位 1 到 7",
          },
          sec: {
            type: "number",
            description: "持续秒数",
          },
        },
        required: ["level"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_suction",
      description:
        "控制吮吸头，使用独立 0x09 通道。mode 为 1 到 5（默认 1），level 为 0 到 10，0 只停止吮吸；建议指定 sec。机身与吮吸头先开机联动，一个 BLE bridge 连接 SL278H 即可。",
      inputSchema: {
        type: "object",
        properties: {
          mode: { type: "integer", minimum: 1, maximum: 5, default: 1, description: "吮吸模式；已实测模式 1" },
          level: { type: "integer", minimum: 0, maximum: 10, description: "吮吸强度；0 只停止吮吸，首次从 1 开始" },
          sec: { type: "number", exclusiveMinimum: 0, description: "持续秒数；建议填写，蓝牙桥到时单独停止吮吸" },
        },
        required: ["level"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_heat",
      description:
        "设置加热温度（摄氏度）并强制要求持续秒数。必须由客户端负责自动停止。",
      inputSchema: {
        type: "object",
        properties: {
          temp: {
            type: "integer",
            description: "温度，单位摄氏度",
          },
          sec: {
            type: "number",
            description: "必须填写，持续秒数",
          },
        },
        required: ["temp", "sec"],
        additionalProperties: false,
      },
    },

    {
      name: "toy_stop",
      description:
        "停止设备。channel 可选 all、vibrate、auto、patting、thrust、suction、heat；suction 只停止吮吸，all 停止所有通道。",
      inputSchema: {
        type: "object",
        properties: {
          channel: {
            type: "string",
            description:
              "停止目标：all / vibrate / auto / patting / thrust / suction / heat",
          },
        },
        additionalProperties: false,
      },
    },
  ];
}

async function callTool(name, args = {}) {
  switch (name) {
    case "toy_status":
      return toolResult("BLE bridge is online and polling.");

    case "toy_set_speed": {
      let speed = Number(args.speed ?? 0);
      let sec = args.sec;

      if (!Number.isFinite(speed)) speed = 0;
      speed = Math.max(0, Math.min(1, speed));

      // 兼容旧工具：映射到 0x04 自动模式
      const level = Math.round(speed * 255);

      enqueue({
        type: level <= 0 ? "stop" : "auto",
        level,
        sec,
      });

      return toolResult(
        `Auto mode queued: ${Math.round(speed * 100)}%` +
          (sec != null ? ` for ${sec}s.` : ".")
      );
    }

    case "toy_set_pattern": {
      const mode = Math.max(1, Math.min(10, Number(args.pattern)));
      const level = Math.max(1, Math.min(10, Number(args.level)));
      const sec = args.sec;

      enqueue({
        type: "vibrate",
        mode,
        level,
        sec,
      });

      return toolResult(
        `Vibration queued: mode ${mode}, level ${level}/10` +
          (sec != null ? ` for ${sec}s.` : ".")
      );
    }

    case "toy_vibrate": {
      const mode = Math.max(1, Math.min(10, Number(args.mode)));
      const level = Math.max(1, Math.min(10, Number(args.level)));
      const sec = args.sec;

      enqueue({
        type: "vibrate",
        mode,
        level,
        sec,
      });

      return toolResult(
        `Vibration queued: mode ${mode}, level ${level}/10` +
          (sec != null ? ` for ${sec}s.` : ".")
      );
    }

    case "toy_auto": {
      const level = Math.max(0, Math.min(255, Number(args.level)));
      const sec = args.sec;

      if (level <= 0) {
        enqueue({
          type: "stop",
          channel: "auto",
        });

        return toolResult("Auto mode stop queued.");
      }

      enqueue({
        type: "auto",
        level,
        sec,
      });

      return toolResult(
        `Auto mode queued: ${Math.round((level / 255) * 100)}%` +
          (sec != null ? ` for ${sec}s.` : ".")
      );
    }

    case "toy_patting": {
      const level = Math.max(1, Math.min(7, Number(args.level)));
      const sec = args.sec;

      enqueue({
        type: "patting",
        level,
        sec,
      });

      return toolResult(
        `Patting queued: level ${level}/7` +
          (sec != null ? ` for ${sec}s.` : ".")
      );
    }

    case "toy_thrust": {
      const level = Math.max(1, Math.min(7, Number(args.level)));
      const sec = args.sec;

      enqueue({
        type: "thrust",
        level,
        sec,
      });

      return toolResult(
        `Thrust queued: level ${level}/7` +
          (sec != null ? ` for ${sec}s.` : ".")
      );
    }

    case "toy_suction": {
      const mode = args.mode ?? 1;
      const level = args.level;
      const sec = args.sec;
      if (!Number.isInteger(mode) || mode < 1 || mode > 5 ||
          !Number.isInteger(level) || level < 0 || level > 10) {
        throw new Error("toy_suction requires mode 1–5 and level 0–10 (integers).");
      }
      if (sec != null && (typeof sec !== "number" || !Number.isFinite(sec) || sec <= 0)) {
        throw new Error("toy_suction requires a positive finite sec value when provided.");
      }
      if (level === 0) {
        enqueue({ type: "stop", channel: "suction" });
        return toolResult("Suction stop queued.");
      }
      enqueue({ type: "suction", mode, level, sec });
      return toolResult(`Suction queued: mode ${mode}, level ${level}/10` +
        (sec != null ? ` for ${sec}s.` : "."));
    }

    case "toy_heat": {
      const temp = Math.max(1, Math.min(55, Number(args.temp)));
      const sec = Number(args.sec);

      if (!Number.isFinite(sec) || sec <= 0) {
        throw new Error("toy_heat requires a positive sec value.");
      }

      enqueue({
        type: "heat",
        temp,
        sec,
      });

      return toolResult(
        `Heating queued: ${temp}°C for ${sec}s.`
      );
    }

    case "toy_stop": {
      const channel = args.channel || "all";

      const valid = [
        "all",
        "vibrate",
        "auto",
        "patting",
        "thrust",
        "suction",
        "heat",
      ];

      if (!valid.includes(channel)) {
        throw new Error(`Invalid channel: ${channel}`);
      }

      enqueue({
        type: "stop",
        channel,
      });

      return toolResult(`Stop command queued: ${channel}.`);
    }

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

app.post("/mcp", async (req, res) => {
  if (!checkAuth(req)) {
    return res.status(401).json({
      jsonrpc: "2.0",
      id: req.body?.id ?? null,
      error: {
        code: -32001,
        message: "Unauthorized",
      },
    });
  }

  const body = req.body || {};
  const id = body.id ?? null;
  const method = body.method;
  const params = body.params || {};

  try {
    if (method === "initialize") {
      return res.json({
        jsonrpc: "2.0",
        id,
        result: {
          protocolVersion: "2025-03-26",
          capabilities: {
            tools: {},
          },
          serverInfo: {
            name: "svakom-ble-ai",
            version: "2.1.0",
          },
        },
      });
    }

    if (method === "notifications/initialized") {
      return res.status(204).end();
    }

    if (method === "tools/list") {
      return res.json({
        jsonrpc: "2.0",
        id,
        result: {
          tools: listTools(),
        },
      });
    }

    if (method === "tools/call") {
      const name = params.name;
      const args = params.arguments || {};

      const result = await callTool(name, args);

      return res.json({
        jsonrpc: "2.0",
        id,
        result,
      });
    }

    if (method === "ping") {
      return res.json({
        jsonrpc: "2.0",
        id,
        result: {},
      });
    }

    return res.status(400).json({
      jsonrpc: "2.0",
      id,
      error: {
        code: -32601,
        message: `Method not found: ${method}`,
      },
    });
  } catch (err) {
    return res.status(200).json({
      jsonrpc: "2.0",
      id,
      result: {
        isError: true,
        content: [
          {
            type: "text",
            text: err?.message || String(err),
          },
        ],
      },
    });
  }
});

app.listen(PORT, () => {
  console.log(`svakom-ble-ai MCP server listening on ${PORT}`);
});
