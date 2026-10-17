# Palmtop

[![CI](https://github.com/jbxter/palmtop/actions/workflows/ci.yml/badge.svg)](https://github.com/jbxter/palmtop/actions/workflows/ci.yml)
[![Palmtop - Self-hosted AI agent platform that runs on a phone | Product Hunt](https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1154398&theme=dark&t=1779595840074)](https://www.producthunt.com/products/pocket-agent-2?embed=true&utm_source=badge-featured&utm_medium=badge&utm_campaign=badge-pocket-agent-2)

A self-hosted AI agent platform that runs on a phone — or anywhere.

Built and battle-tested on a Galaxy S21 running Termux, deployed via Cloudflare Tunnel, handling real business operations 24/7 with zero cloud compute costs.

## What you can do

One process, one `data_dir`. Every private channel shares that agent: the same memory, the same tools, the same inference cascade.

**You, in a private channel.** Send a message. The agent answers with the local model or a cloud tier, keeps the thread, and can call a tool when that integration is configured. Memory is SQLite: conversation history per channel, facts and preferences it extracts, multi-step plans, and a knowledge base. On Telegram, a voice message is transcribed (Gemini, Whisper, or OpenAI) and the agent can reply with speech.

**Channels that reach that agent.** Telegram, SMS (Termux), email, Discord, Slack, Matrix, IRC, WhatsApp, XMPP, and ScuttleBot. List them in `channels` — that is the only way those names start. A single `channel = "telegram"` or `channel = "sms"` still works; any other lone `channel` value falls through to Telegram. Each private channel except ScuttleBot refuses everyone until its allow-list is set (`allowed_users`, `allowed_numbers`, `allowed_jids`, or `allowed_senders`). `allow_anyone = true` is the explicit public switch, and it is logged at startup.

**Tools you can ask for**, after the matching config is set: web search, Google Calendar, reminders, files under `data_dir/docs`, the knowledge base, AgentMail, Jira and Confluence, Vercel, Railway, Cursor, and any MCP server in `[[mcp.servers]]`.

**Owner-only work.** Put your id in `[agent] owners`. An empty list refuses these commands on every channel. `/engine` on Telegram, and `engine:` on any channel, run a sovereign task on the configured cloud tier. `/cursor` and `cursor:` send repo work to a Cursor Cloud Agent on an allowlisted repo. Deploy tools ask before they ship. Reply `/approve` or `/deny`. The operator steps are in [docs/delegation-workflow.md](docs/delegation-workflow.md).

**A web visitor.** The site is a landing page, a blog, and an intake form. The chat widget talks to a sandboxed WebAgent: a cloud conversation with no tools and no memory. That visitor cannot reach the agent you message from Telegram.

**ScuttleBot.** Add `"scuttlebot"` to `channels` and point `[scuttlebot]` at an IRC server. A mention or DM (`palmtop: …`, `@palmtop`, or a private message) runs the full agent as `scuttlebot:<nick>`. `!pause`, `!resume`, and `!status` work. There is no nick allow-list, so the IRC server itself has to be private. Tool calls are not streamed into the room.

**Operate the process.** `python -m palmtop` accepts `backup`, `restore`, `export`, `import`, `facts` (`export`/`import`/`status`/`keygen` for the signed fact log), and `init`. Push to the phone's git remote to pull, restart the agent, and restart the tunnel. With `[admin]` enabled, `GET /health` listens on loopback.

## Architecture

```
Private channels                 AgentLoop                    Optional tools
telegram, sms, email,            local model                  search, calendar,
discord, slack, matrix,    ───>  then cloud.light        ───> reminders, files,
irc, whatsapp, xmpp,             then cloud.heavy             knowledge, email,
scuttlebot                       one shared data_dir          jira, vercel, railway,
                                                              cursor, MCP

Public web, not on that loop
landing page, blog, intake form
WebAgent: conversation only — no tools, no memory
```

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

The local model routes the turn. Light and heavy run whatever provider those sections name. `config.example.toml` lists the accepted providers (Anthropic, Google, OpenAI, Groq, Together, Fireworks, DeepSeek, Mistral, OpenRouter, Ollama, Cohere).

### Channels

```toml
channels = ["telegram", "sms"]

[telegram]
# Set TELEGRAM_BOT_TOKEN env var
# allowed_users = [123456789]   # empty = refuse everyone

[sms]
enabled = false
allowed_numbers = ["+15551234567"]
```

Discord, Slack, Matrix, IRC, WhatsApp, XMPP, email, and ScuttleBot belong in that `channels` list, each with its own allow-list. ScuttleBot has no nick allow-list.

See `config.example.toml` for the full reference with all options documented.

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
│   └── tracing.py       # Observability (SQLite/Langfuse)
├── inference/
│   ├── local.py         # llama.cpp backend
│   └── cloud.py         # Anthropic/Google/OpenAI backends
├── channels/            # One module per private channel listed above
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
