<div align="center">
  <img src="ragnarbot_logo.jpg" alt="ragnarbot" width="500">
</div>

<p align="center">
  <em>Your personal AI assistant in Telegram. Nothing else. 🪓</em>
</p>

---

No WhatsApp. No Discord. No Slack. No 47 integrations you'll never use.

Just Telegram. Three LLM providers — **Anthropic**, **OpenAI**, **Gemini**. One config. You're done. ⚔️

## Install

Grab [uv](https://github.com/astral-sh/uv) if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then:

```bash
uv tool install ragnarbot-ai
```

## Setup

### 🪓 Step 1: Create a Telegram bot

1. Open Telegram, find [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, follow the prompts, pick a name
3. Copy the token it gives you — you'll need it in a sec

### 🪓 Step 2: Onboard

```bash
ragnarbot onboard
```

Answer a few questions (provider, API key, paste that bot token) and you're live. No config files to edit by hand, no YAML to debug at 2am.

## Access

Once the bot is running, just message it. If someone unauthorized tries to talk to it, the bot will send them an access code. You run one command in your terminal to approve them. That's it — no manual config editing, no user ID lookups.

## 🏃 Run

```bash
ragnarbot gateway
```

Your bot is alive. Go text it.

To manage the gateway:

```bash
ragnarbot gateway start    # start as a background daemon
ragnarbot gateway stop     # stop the daemon
ragnarbot gateway restart  # restart the daemon
ragnarbot gateway delete   # remove the daemon completely
```

## Multiple profiles

Run several fully isolated bots on one machine with the global `--profile <name>` flag. It's an option on the main command, so it goes **before** the subcommand. Each profile gets its own onboarding and its own data root: `default` lives in `~/.ragnarbot`, any other profile in `~/.ragnarbot-<name>` — separate config, credentials, sessions, media, OAuth, browser profile, cron and hook jobs, and daemon.

```bash
ragnarbot --profile work onboard          # set up a separate "work" bot
ragnarbot --profile work gateway          # run it (or: gateway start for a daemon)
ragnarbot --profile work gateway status   # inspect that profile
```

With no flag everything behaves as `--profile default`. You can also select a profile via the `RAGNARBOT_PROFILE` env var. Profile names must be lowercase letters/digits with `-` or `_` (no dots or slashes). Per-profile daemons run in parallel without conflicting, so `work` and `default` can both be live at once.

---

MIT · Based on [nanobot](https://github.com/HKUDS/nanobot)
