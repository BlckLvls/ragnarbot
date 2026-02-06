<div align="center">
  <img src="ragnarbot_logo.png" alt="ragnarbot" width="500">
  <h1>ragnarbot: Ultra-Lightweight Personal AI Assistant</h1>
  <p>
    <img src="https://img.shields.io/badge/python-≥3.11-blue" alt="Python">
    <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  </p>
</div>

🤖 **ragnarbot** is an **ultra-lightweight** personal AI assistant inspired by [Clawdbot](https://github.com/openclaw/openclaw)

⚡️ Delivers core agent functionality in just **~4,000** lines of code — **99% smaller** than Clawdbot's 430k+ lines.

## Key Features of ragnarbot:

🪶 **Ultra-Lightweight**: Just ~4,000 lines of code — 99% smaller than Clawdbot - core functionality.

🔬 **Research-Ready**: Clean, readable code that's easy to understand, modify, and extend for research.

⚡️ **Lightning Fast**: Minimal footprint means faster startup, lower resource usage, and quicker iterations.

💎 **Easy-to-Use**: One-click to depoly and you're ready to go.

## ✨ Features

<table align="center">
  <tr align="center">
    <th><p align="center">📈 24/7 Real-Time Market Analysis</p></th>
    <th><p align="center">🚀 Full-Stack Software Engineer</p></th>
    <th><p align="center">📅 Smart Daily Routine Manager</p></th>
    <th><p align="center">📚 Personal Knowledge Assistant</p></th>
  </tr>
  <tr>
    <td align="center"><p align="center"><img src="case/search.gif" width="180" height="400"></p></td>
    <td align="center"><p align="center"><img src="case/code.gif" width="180" height="400"></p></td>
    <td align="center"><p align="center"><img src="case/scedule.gif" width="180" height="400"></p></td>
    <td align="center"><p align="center"><img src="case/memory.gif" width="180" height="400"></p></td>
  </tr>
  <tr>
    <td align="center">Discovery • Insights • Trends</td>
    <td align="center">Develop • Deploy • Scale</td>
    <td align="center">Schedule • Automate • Organize</td>
    <td align="center">Learn • Memory • Reasoning</td>
  </tr>
</table>

## 📦 Install

**Install from source** (latest features, recommended for development)

```bash
git clone https://github.com/BlckLvls/ragnarbot.git
cd ragnarbot
pip install -e .
```

**Install with [uv](https://github.com/astral-sh/uv)** (stable, fast)

```bash
uv tool install ragnarbot-ai
```

**Install from PyPI** (stable)

```bash
pip install ragnarbot-ai
```

## 🚀 Quick Start

> [!TIP]
> Set your API key in `~/.ragnarbot/config.json`.
> Get API keys: [OpenRouter](https://openrouter.ai/keys) (LLM) · [Brave Search](https://brave.com/search/api/) (optional, for web search)
> You can also change the model to `minimax/minimax-m2` for lower cost.

**1. Initialize**

```bash
ragnarbot onboard
```

**2. Configure** (`~/.ragnarbot/config.json`)

```json
{
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    }
  },
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  },
  "tools": {
    "web": {
      "search": {
        "apiKey": "BSA-xxx"
      }
    }
  }
}
```


**3. Chat**

```bash
ragnarbot agent -m "What is 2+2?"
```

That's it! You have a working AI assistant in 2 minutes.

## 🖥️ Local Models (vLLM)

Run ragnarbot with your own local models using vLLM or any OpenAI-compatible server.

**1. Start your vLLM server**

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --port 8000
```

**2. Configure** (`~/.ragnarbot/config.json`)

```json
{
  "providers": {
    "vllm": {
      "apiKey": "dummy",
      "apiBase": "http://localhost:8000/v1"
    }
  },
  "agents": {
    "defaults": {
      "model": "meta-llama/Llama-3.1-8B-Instruct"
    }
  }
}
```

**3. Chat**

```bash
ragnarbot agent -m "Hello from my local LLM!"
```

> [!TIP]
> The `apiKey` can be any non-empty string for local servers that don't require authentication.

## 💬 Chat Apps

Talk to your ragnarbot through Telegram, WhatsApp, or Feishu — anytime, anywhere.

| Channel | Setup |
|---------|-------|
| **Telegram** | Easy (just a token) |
| **WhatsApp** | Medium (scan QR) |
| **Feishu** | Medium (app credentials) |

<details>
<summary><b>Telegram</b> (Recommended)</summary>

**1. Create a bot**
- Open Telegram, search `@BotFather`
- Send `/newbot`, follow prompts
- Copy the token

**2. Configure**

```json
{
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "YOUR_BOT_TOKEN",
      "allowFrom": ["YOUR_USER_ID"]
    }
  }
}
```

> Get your user ID from `@userinfobot` on Telegram.

**3. Run**

```bash
ragnarbot gateway
```

</details>

<details>
<summary><b>WhatsApp</b></summary>

Requires **Node.js ≥18**.

**1. Link device**

```bash
ragnarbot channels login
# Scan QR with WhatsApp → Settings → Linked Devices
```

**2. Configure**

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "allowFrom": ["+1234567890"]
    }
  }
}
```

**3. Run** (two terminals)

```bash
# Terminal 1
ragnarbot channels login

# Terminal 2
ragnarbot gateway
```

</details>

<details>
<summary><b>Feishu (飞书)</b></summary>

Uses **WebSocket** long connection — no public IP required.

```bash
pip install ragnarbot-ai[feishu]
```

**1. Create a Feishu bot**
- Visit [Feishu Open Platform](https://open.feishu.cn/app)
- Create a new app → Enable **Bot** capability
- **Permissions**: Add `im:message` (send messages)
- **Events**: Add `im.message.receive_v1` (receive messages)
  - Select **Long Connection** mode (requires running ragnarbot first to establish connection)
- Get **App ID** and **App Secret** from "Credentials & Basic Info"
- Publish the app

**2. Configure**

```json
{
  "channels": {
    "feishu": {
      "enabled": true,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": "",
      "allowFrom": []
    }
  }
}
```

> `encryptKey` and `verificationToken` are optional for Long Connection mode.
> `allowFrom`: Leave empty to allow all users, or add `["ou_xxx"]` to restrict access.

**3. Run**

```bash
ragnarbot gateway
```

> [!TIP]
> Feishu uses WebSocket to receive messages — no webhook or public IP needed!

</details>

## ⚙️ Configuration

Config file: `~/.ragnarbot/config.json`

### Providers

> [!NOTE]
> Groq provides free voice transcription via Whisper. If configured, Telegram voice messages will be automatically transcribed.

| Provider | Purpose | Get API Key |
|----------|---------|-------------|
| `openrouter` | LLM (recommended, access to all models) | [openrouter.ai](https://openrouter.ai) |
| `anthropic` | LLM (Claude direct) | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | LLM (GPT direct) | [platform.openai.com](https://platform.openai.com) |
| `deepseek` | LLM (DeepSeek direct) | [platform.deepseek.com](https://platform.deepseek.com) |
| `groq` | LLM + **Voice transcription** (Whisper) | [console.groq.com](https://console.groq.com) |
| `gemini` | LLM (Gemini direct) | [aistudio.google.com](https://aistudio.google.com) |


<details>
<summary><b>Full config example</b></summary>

```json
{
  "agents": {
    "defaults": {
      "model": "anthropic/claude-opus-4-5"
    }
  },
  "providers": {
    "openrouter": {
      "apiKey": "sk-or-v1-xxx"
    },
    "groq": {
      "apiKey": "gsk_xxx"
    }
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456:ABC...",
      "allowFrom": ["123456789"]
    },
    "whatsapp": {
      "enabled": false
    },
    "feishu": {
      "enabled": false,
      "appId": "cli_xxx",
      "appSecret": "xxx",
      "encryptKey": "",
      "verificationToken": "",
      "allowFrom": []
    }
  },
  "tools": {
    "web": {
      "search": {
        "apiKey": "BSA..."
      }
    }
  }
}
```

</details>

## CLI Reference

| Command | Description |
|---------|-------------|
| `ragnarbot onboard` | Initialize config & workspace |
| `ragnarbot agent -m "..."` | Chat with the agent |
| `ragnarbot agent` | Interactive chat mode |
| `ragnarbot gateway` | Start the gateway |
| `ragnarbot status` | Show status |
| `ragnarbot channels login` | Link WhatsApp (scan QR) |
| `ragnarbot channels status` | Show channel status |

<details>
<summary><b>Scheduled Tasks (Cron)</b></summary>

```bash
# Add a job
ragnarbot cron add --name "daily" --message "Good morning!" --cron "0 9 * * *"
ragnarbot cron add --name "hourly" --message "Check status" --every 3600

# List jobs
ragnarbot cron list

# Remove a job
ragnarbot cron remove <job_id>
```

</details>

## 🐳 Docker

> [!TIP]
> The `-v ~/.ragnarbot:/root/.ragnarbot` flag mounts your local config directory into the container, so your config and workspace persist across container restarts.

Build and run ragnarbot in a container:

```bash
# Build the image
docker build -t ragnarbot .

# Initialize config (first time only)
docker run -v ~/.ragnarbot:/root/.ragnarbot --rm ragnarbot onboard

# Edit config on host to add API keys
vim ~/.ragnarbot/config.json

# Run gateway (connects to Telegram/WhatsApp)
docker run -v ~/.ragnarbot:/root/.ragnarbot -p 18790:18790 ragnarbot gateway

# Or run a single command
docker run -v ~/.ragnarbot:/root/.ragnarbot --rm ragnarbot agent -m "Hello!"
docker run -v ~/.ragnarbot:/root/.ragnarbot --rm ragnarbot status
```

## 📁 Project Structure

```
ragnarbot/
├── agent/          # 🧠 Core agent logic
│   ├── loop.py     #    Agent loop (LLM ↔ tool execution)
│   ├── context.py  #    Prompt builder
│   ├── memory.py   #    Persistent memory
│   ├── skills.py   #    Skills loader
│   ├── subagent.py #    Background task execution
│   └── tools/      #    Built-in tools (incl. spawn)
├── skills/         # 🎯 Bundled skills (github, weather, tmux...)
├── channels/       # 📱 WhatsApp integration
├── bus/            # 🚌 Message routing
├── cron/           # ⏰ Scheduled tasks
├── heartbeat/      # 💓 Proactive wake-up
├── providers/      # 🤖 LLM providers (OpenRouter, etc.)
├── session/        # 💬 Conversation sessions
├── config/         # ⚙️ Configuration
└── cli/            # 🖥️ Commands
```


<p align="center">
  <sub>ragnarbot is for educational, research, and technical exchange purposes only</sub>
</p>
