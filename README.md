# Palmtop

[![CI](https://github.com/jbxter/palmtop/actions/workflows/ci.yml/badge.svg)](https://github.com/jbxter/palmtop/actions/workflows/ci.yml)
[![Palmtop - Self-hosted AI agent platform that runs on a phone | Product Hunt](https://api.producthunt.com/widgets/embed-image/v1/featured.svg?post_id=1154398&theme=dark&t=1779595840074)](https://www.producthunt.com/products/pocket-agent-2?embed=true&utm_source=badge-featured&utm_medium=badge&utm_campaign=badge-pocket-agent-2)

A self-hosted AI agent. One process on a machine you control: a laptop, a server, or a phone.

## Start

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)
- At least one API key: `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY`
- A Telegram bot token from [@BotFather](https://t.me/botfather)

On macOS or Linux:

```bash
git clone https://github.com/jbxter/palmtop.git
cd palmtop
bash bootstrap_macos.sh

cp config.example.toml config.toml
# Edit config.toml — set [persona], API keys, Telegram token

export ANTHROPIC_API_KEY="sk-ant-..."
export TELEGRAM_BOT_TOKEN="123456:ABC..."

uv run python -m palmtop
```

On a phone, follow [On the phone](#on-the-phone).

## What runs

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

Private channels share one process and one `data_dir`: the same memory, the same tools, and the same inference cascade.

A channel starts only when it is listed in `channels`. Telegram, SMS (Termux), email, Discord, Slack, Matrix, IRC, WhatsApp, XMPP, and ScuttleBot are the names. A single `channel = "telegram"` or `channel = "sms"` still works; any other lone `channel` value falls through to Telegram.

Each private channel except ScuttleBot refuses everyone until its allow-list is set (`allowed_users`, `allowed_numbers`, `allowed_jids`, or `allowed_senders`). `allow_anyone = true` is the explicit public switch, and it is logged at startup. ScuttleBot has no nick allow-list, so the IRC server itself has to be private. A mention or DM (`palmtop: …`, `@palmtop`, or a private message) runs the full agent as `scuttlebot:<nick>`. `!pause`, `!resume`, and `!status` work. Tool calls stay out of the room.

Tools run after the matching config is set: web search, Google Calendar, reminders, files under `data_dir/docs`, the knowledge base, AgentMail, Jira and Confluence, Vercel, Railway, Cursor, and any MCP server in `[[mcp.servers]]`.

Owner commands need your id in `[agent] owners`. An empty list refuses them on every channel. `/engine` on Telegram, and `engine:` on any channel, run a sovereign task on the configured cloud tier. `/cursor` and `cursor:` send repo work to a Cursor Cloud Agent on an allowlisted repo. Deploy tools ask before they ship. Reply `/approve` or `/deny`. The operator steps are in [docs/delegation-workflow.md](docs/delegation-workflow.md).

The public site is a landing page, a blog, and an intake form. Its chat widget talks to a sandboxed WebAgent: a cloud conversation with no tools and no memory. That visitor cannot reach the agent you message from Telegram.

On Telegram, a voice message is transcribed (Gemini, Whisper, or OpenAI) and the agent can reply with speech.

## Files an operator moves

Conversation history, plans, reminders, and the knowledge base are SQLite files in `data_dir`. Extracted facts are a signed log. `memories.db` is the view that recall and search read.

`python -m palmtop` accepts `backup`, `restore`, `export`, `import`, `init`, `facts export`, `facts import`, `facts epoch`, `vault-push`, and `vault-pull`. `[facts]` in `config.example.toml` is where the cap and the peer allow-list live.

With `[admin]` enabled, `GET /health` listens on loopback.

## Configure

Copy `config.example.toml` to `config.toml`. That file is the full reference.

```toml
channels = ["telegram", "sms"]

[telegram]
# Set TELEGRAM_BOT_TOKEN env var
# allowed_users = [123456789]   # empty = refuse everyone

[sms]
enabled = false
allowed_numbers = ["+15551234567"]
```

An empty allow-list refuses everyone. Discord, Slack, Matrix, IRC, WhatsApp, XMPP, email, and ScuttleBot belong in that `channels` list, each with its own allow-list.

The local model routes the turn, then cloud light, then cloud heavy. `config.example.toml` lists the accepted providers.

## On the phone

Android is one place it runs. A Galaxy S21 in Termux has been running it. Install Termux from F-Droid. The Play Store build is outdated.

```bash
pkg update && pkg install git
git clone https://github.com/jbxter/palmtop.git
cd palmtop
bash bootstrap_termux.sh
```

The bootstrap script installs Python, uv, llama.cpp with Vulkan, and the dependencies. On a fresh Termux install it takes about 10 minutes. The S21's Mali GPU runs 4-bit quantized models. Cloudflare Tunnel exposes the web server. Tailscale is SSH from anywhere. Termux:Boot starts the stack on reboot.

Once the agent is running, the phone can be a git remote:

```bash
# On the phone (Termux)
bash scripts/setup-git-server.sh

# On your laptop
git remote add phone ssh://phone-ip:8022/path/to/palmtop.git
git push phone main
# → auto-pulls, restarts agent, restarts tunnel
```

The deploy hook is `scripts/git-post-receive.sh`.

## Layout

```
src/palmtop/
├── core/         # AgentLoop, sovereign engine, approval gate
├── channels/     # One module per private channel
├── memory/       # Conversation, structured facts, plans
├── knowledge/    # Knowledge base and vault sync
├── tools/        # Calendar, email, Jira, search, deploy
└── web/          # Public site and sandboxed WebAgent
```

## Contributing

Contributions welcome. Please open an issue first for significant changes.

## License

MIT
