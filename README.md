# Palmtop

A self-hosted AI agent platform that runs on a phone — or anywhere.

Built and battle-tested on a Galaxy S21 running Termux, deployed via Cloudflare Tunnel, handling real business operations 24/7 with zero cloud compute costs.

## What is this?

Palmtop is a production-grade personal AI agent with:

- **Cascading inference** — local llama.cpp model for routing, cloud LLMs (Anthropic, Google, OpenAI) for heavy lifting
- **Multi-channel communication** — Telegram, SMS (via Termux:API), and web chat
- **Tool integrations** — Jira, Google Calendar, email (AgentMail), web search, Vercel/Railway deploy
- **Memory system** — conversation history, structured memory extraction, plans, knowledge base (all SQLite)
- **MCP client/server** — connect to any Model Context Protocol server
- **Voice** — speech-to-text (Gemini, Whisper, OpenAI) and text-to-speech
- **Web presence** — landing page with chat widget, intake forms, blog engine, lead qualification
- **Git-push deploy** — push to your device, auto-restart via post-receive hook
- **Configurable persona** — name, personality, services, brand colors — all driven by `config.toml`

## Architecture

```
                    ┌─────────────┐
                    │  Telegram   │
                    │    Bot      │
                    └──────┬──────┘
                           │
┌──────────┐        ┌──────┴──────┐        ┌──────────────┐
│   SMS    ├────────┤  AgentLoop  ├────────┤  Tool        │
│ (Termux) │        │  (core)     │        │  Registry    │
└──────────┘        └──────┬──────┘        └──────┬───────┘
                           │                      │
┌──────────┐        ┌──────┴──────┐        ┌──────┴───────┐
│ Web Chat ├────────┤  Inference  │        │ Calendar     │
│ (public) │        │  Cascade    │        │ Email        │
└──────────┘        └─────────────┘        │ Jira         │
                    local → light → heavy  │ Search       │
                                           │ Deploy       │
                    Security boundary:     │ Files        │
                    Web visitors get a     │ Knowledge    │
                    sandboxed WebAgent     │ MCP Gateway  │
                    with NO tool access    └──────────────┘
```

**Security**: Web visitors interact with a sandboxed `WebAgent` that has access to a cloud LLM for conversation only. It has zero access to tools, memory, or any internal systems.

## Quickstart

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- At least one API key: `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY`
- A Telegram bot token (from [@BotFather](https://t.me/botfather))

### macOS / Linux (development)

```bash
git clone https://github.com/jbxter/palmtop.git
cd palmtop
bash bootstrap_macos.sh

# Configure
cp config.example.toml config.toml
# Edit config.toml — set [persona], API keys, Telegram token

# Set secrets
export ANTHROPIC_API_KEY="sk-ant-..."
export TELEGRAM_BOT_TOKEN="123456:ABC..."

# Run
uv run python -m palmtop
```

### Galaxy S21 / Android (Termux)

```bash
# Install Termux from F-Droid (not Play Store)
pkg update && pkg install git
git clone https://github.com/jbxter/palmtop.git
cd palmtop
bash bootstrap_termux.sh
# Follow the printed instructions
```

The bootstrap script installs Python, uv, llama.cpp (with Vulkan GPU), and all dependencies. It takes about 10 minutes on a fresh Termux install.

### Git-push deploy (S21 as a server)

Once the agent is running on your phone, set it up as a git remote for zero-downtime deploys:

```bash
# On the phone (Termux)
bash scripts/setup-git-server.sh

# On your laptop
git remote add phone ssh://phone-ip:8022/path/to/palmtop.git
git push phone main
# → auto-pulls, restarts agent, restarts tunnel
```

See `scripts/git-post-receive.sh` for the deploy hook.

## Configuration

Copy `config.example.toml` to `config.toml` and customize:

### Persona (your agent's identity)

```toml
[persona]
name = "My Agent"
tagline = "AI-powered executive assistant"
owner_name = "Your Name"
domain = "yourdomain.com"
linkedin_url = "https://linkedin.com/in/you"
location = "San Francisco"

personality = """\
Sharp analytical thinking with genuine warmth. Casual but competent."""

capabilities = [
    "Scheduling and time management",
    "Business decisions and tradeoff analysis",
    "Research and recommendations",
]

services = [
    "Full-stack web development",
    "AI/ML integrations",
    "Technical consulting",
]
```

### Inference

```toml
[inference]
model_path = "models/phi-3.5-mini-instruct-q4_k_m.gguf"
n_ctx = 4096
n_gpu_layers = -1   # -1 = all layers on GPU

[cloud.light]
# provider = "google"       # cheap, fast
[cloud.heavy]
# provider = "anthropic"    # capable, expensive
```

### Channels

```toml
[telegram]
# Set TELEGRAM_BOT_TOKEN env var

[sms]
enabled = false
allowed_numbers = ["+15551234567"]
```

See `config.example.toml` for the full reference with all options documented.

## Features

### Inference cascade

Messages are routed through a cascade: local model (fast, free) → cloud light tier (Gemini Flash) → cloud heavy tier (Claude Sonnet). The local model handles simple messages and routes complex ones to the appropriate cloud tier.

### Memory system

- **Conversation memory**: Full chat history per channel, stored in SQLite
- **Structured memory**: The agent extracts facts, preferences, and relationships from conversations
- **Plans**: Track multi-step plans with status updates
- **Knowledge base**: Persistent key-value store for reference information

### Tool integrations

All tools are optional — configure only what you need:

| Tool | Config key | What it does |
|------|-----------|--------------|
| Web search | `[search]` | Brave → Serper → DuckDuckGo fallback chain |
| Google Calendar | automatic | Read/write events (OAuth) |
| Email | `[email]` | Send/receive via AgentMail |
| Jira + Confluence | `[atlassian]` | Search, create, update issues and wiki pages |
| Vercel | `[vercel]` | Deploy projects |
| Railway | `[railway]` | Redeploy services |
| Cursor | `[cursor]` | Delegate coding tasks to Cursor Cloud Agents |
| MCP servers | `[[mcp.servers]]` | Connect to any MCP-compatible server |

### Web presence

The built-in web server provides:
- **Landing page** with configurable persona
- **Chat widget** — visitors talk to a sandboxed WebAgent
- **Intake form** — collects leads, notifies you on Telegram
- **Blog engine** — zero-dependency markdown-to-HTML
- **Lead outreach** — auto-qualifies leads and sends branded follow-up emails

### Voice

Telegram voice messages are automatically transcribed (Gemini, Whisper, or OpenAI) and the agent can reply with synthesized speech.

## Project structure

```
src/palmtop/
├── __main__.py          # Entry point — wires everything
├── persona.py           # Persona config → system prompts
├── brand.py             # HTML email template (persona-driven)
├── config/settings.py   # Config loader
├── core/
│   ├── loop.py          # AgentLoop — main conversation engine
│   ├── engine.py        # Sovereign engine (autonomous tasks)
│   ├── blessing.py      # Human-in-the-loop approval gate
│   ├── goal_aligner.py  # 12-Week-Year goal alignment
│   ├── monitor.py       # Proactive monitoring
│   └── tracing.py       # Observability (SQLite/Langfuse)
├── inference/
│   ├── local.py         # llama.cpp backend
│   └── cloud.py         # Anthropic/Google/OpenAI backends
├── channels/
│   ├── telegram.py      # Telegram bot
│   ├── sms.py           # Termux SMS
│   └── sms_listener.py  # Dual-channel SMS listener
├── tools/               # Calendar, email, Jira, search, deploy...
├── memory/              # Conversation, structured, plans
├── knowledge/           # SQLite knowledge base
├── mcp/                 # MCP client, server, gateway
├── voice/               # STT + TTS
├── cursor/              # Cursor Cloud Agents bridge
└── web/
    ├── app.py           # Starlette ASGI server
    ├── agent.py         # Sandboxed WebAgent
    ├── blog.py          # Blog engine
    ├── outreach.py      # Lead qualification + auto-email
    └── static/          # Landing page, CSS, JS, blog posts
```

## Running on a Galaxy S21

This project was built on and for a Galaxy S21. The full stack — Python, llama.cpp with Vulkan GPU acceleration, Cloudflare Tunnel, SQLite databases, git server — runs on a single phone with no cloud compute.

Key details:
- **Termux** from F-Droid (not Play Store — the Play Store version is outdated)
- **Vulkan** for GPU inference (the S21's Mali GPU handles 4-bit quantized models well)
- **Cloudflare Tunnel** exposes the web server to the internet
- **Tailscale** for SSH access from anywhere
- **Termux:Boot** for auto-start on reboot

See `bootstrap_termux.sh` for the complete setup script.

## Contributing

Contributions welcome. Please open an issue first for significant changes.

## License

MIT
