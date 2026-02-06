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
> Get API keys: [Anthropic](https://console.anthropic.com/keys) (LLM) · [Brave Search](https://brave.com/search/api/) (optional, for web search)

**1. Initialize**

```bash
ragnarbot onboard
```

**2. Configure** (`~/.ragnarbot/config.json`)

```json
{
  "providers": {
    "anthropic": {
      "apiKey": "sk-ant-xxx"
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

## 💬 Chat Apps

Talk to your ragnarbot through Telegram — anytime, anywhere.

| Channel | Setup |
|---------|-------|
| **Telegram** | Easy (just a token) |

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

## ⚙️ Configuration

Config file: `~/.ragnarbot/config.json`

### Providers

> [!NOTE]
> Groq provides free voice transcription via Whisper. If configured under `transcription`, Telegram voice messages will be automatically transcribed.

| Provider | Purpose | Get API Key |
|----------|---------|-------------|
| `anthropic` | LLM (Claude) | [console.anthropic.com](https://console.anthropic.com) |
| `openai` | LLM (GPT) | [platform.openai.com](https://platform.openai.com) |
| `gemini` | LLM (Gemini) | [aistudio.google.com](https://aistudio.google.com) |


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
    "anthropic": {
      "apiKey": "sk-ant-xxx"
    }
  },
  "transcription": {
    "apiKey": "gsk_xxx"
  },
  "channels": {
    "telegram": {
      "enabled": true,
      "token": "123456:ABC...",
      "allowFrom": ["123456789"]
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

# Run gateway (connects to Telegram)
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
├── channels/       # 📱 Telegram integration
├── bus/            # 🚌 Message routing
├── cron/           # ⏰ Scheduled tasks
├── heartbeat/      # 💓 Proactive wake-up
├── providers/      # 🤖 LLM providers (Anthropic, OpenAI, Gemini)
├── session/        # 💬 Conversation sessions
├── config/         # ⚙️ Configuration
└── cli/            # 🖥️ Commands
```


<p align="center">
  <sub>ragnarbot is for educational, research, and technical exchange purposes only</sub>
</p>
