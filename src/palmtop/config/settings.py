from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from palmtop.persona import PersonaConfig, BrandConfig, BookingLink

if sys.version_info >= (3, 12):
    import tomllib
else:
    import tomli as tomllib


Runtime = Literal["dev", "phone"]
Channel = Literal["telegram", "sms"]


def detect_runtime() -> Runtime:
    if "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", ""):
        return "phone"
    return "dev"


@dataclass
class InferenceConfig:
    model_path: str = ""
    n_ctx: int = 4096
    n_gpu_layers: int = -1
    n_threads: int = 4


@dataclass
class CloudTierConfig:
    provider: str = ""
    api_key: str = ""
    model: str = ""


@dataclass
class AtlassianConfig:
    domain: str = ""       # e.g. yourcompany.atlassian.net
    email: str = ""
    api_token: str = ""


@dataclass
class EmailConfig:
    api_key: str = ""          # AgentMail API key
    inbox_id: str = ""         # default inbox (auto-detected if empty)


@dataclass
class ObservabilityConfig:
    enabled: bool = False      # opt-in
    backend: str = "sqlite"    # "sqlite" (default, zero deps) or "langfuse"


@dataclass
class MCPServerEntry:
    name: str = ""
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    cwd: str = ""


@dataclass
class TwelveWyConfig:
    """12 Week Year MCP (remote HTTP to Railway)."""
    api_base_url: str = ""
    api_key: str = ""


@dataclass
class SearchConfig:
    preferred_order: list[str] = field(default_factory=list)  # e.g. ["serper", "brave"]
    brave_keys: list[str] = field(default_factory=list)
    serper_keys: list[str] = field(default_factory=list)


@dataclass
class DigestConfig:
    enabled: bool = True
    hour: int = 7
    minute: int = 0


@dataclass
class AlignmentConfig:
    """12WY goal gate for AgentLoop (telegram/sms)."""
    mode: Literal["soft", "hard"] = "soft"
    goals_path: str = ""  # empty = auto-resolve docs/plans then data/docs/plans
    use_semantic: bool = True


@dataclass
class VoiceConfig:
    enabled: bool = False
    stt_provider: str = "gemini"  # "gemini" (default), "whisper_cpp" (local), "openai"
    stt_model_path: str = "models/ggml-base.en.bin"  # for whisper_cpp fallback
    stt_api_key: str = ""  # auto-filled from GOOGLE_API_KEY for gemini
    tts_enabled: bool = False
    tts_provider: str = "gemini"  # "gemini" (default) or "openai"
    tts_voice: str = "Kore"       # Gemini voice name (Kore, Puck, Charon, etc.)


@dataclass
class MonitorConfig:
    enabled: bool = True
    calendar_interval_minutes: int = 10
    plans_interval_hours: int = 12
    email_interval_minutes: int = 15
    jira_interval_minutes: int = 5
    stale_plan_days: int = 3
    alert_cooldown_hours: int = 2
    quiet_hours_start: int = 22   # 10 PM — hold alerts until morning
    quiet_hours_end: int = 7      # 7 AM
    email_auto_reply: bool = False  # auto-reply to threads the agent is part of


@dataclass
class EngineConfig:
    """Sovereign engine invocable from the agent (uses cloud backends)."""
    enabled: bool = True


@dataclass
class CursorConfig:
    """Cursor Cloud Agents bridge — delegate repo work via API.

    When require_blessing is False, the agent can autonomously launch Cursor
    agents without waiting for /approve.  Safety is maintained by
    allowed_repos (whitelist), max_concurrent (rate limit), and
    auto_create_pr (changes land as PRs, not direct pushes).
    """
    enabled: bool = False
    api_key: str = ""  # prefer CURSOR_API_KEY env var
    allowed_repos: list[str] = field(default_factory=list)
    default_repo: str = ""
    default_branch: str = "main"
    max_concurrent: int = 1
    require_blessing: bool = True
    poll_interval_s: int = 30
    timeout_s: int = 3600
    auto_create_pr: bool = True


@dataclass
class VercelConfig:
    """Vercel deployments — trigger production/preview builds via API."""
    enabled: bool = False
    api_token: str = ""  # prefer VERCEL_TOKEN env var
    default_project_id: str = ""  # prj_xxx from dashboard
    default_project_name: str = ""  # fallback if only name is known
    default_target: str = "production"  # production or preview
    default_branch: str = "main"
    team_id: str = ""  # optional teamId query param
    require_blessing: bool = True


@dataclass
class RailwayConfig:
    """Railway service deploys via GraphQL API."""
    enabled: bool = False
    api_token: str = ""  # prefer RAILWAY_TOKEN env var
    default_project_id: str = ""
    default_service_id: str = ""
    default_environment_id: str = ""
    require_blessing: bool = True


@dataclass
class WebConfig:
    """Public website at your-domain.com (runs on S21 via Cloudflare Tunnel).

    SECURITY: The web channel uses a sandboxed WebAgent that is completely
    isolated from the agent's internal AgentLoop.  Web visitors can chat and
    submit intake forms, but have ZERO access to tools, memory, or internal
    systems.  See web/agent.py for the security boundary.
    """
    enabled: bool = False
    host: str = "127.0.0.1"       # localhost only — cloudflared proxies
    port: int = 8080
    chat_rpm: int = 10            # chat messages per minute per IP
    chat_rpd: int = 100           # chat messages per day per IP
    form_rpm: int = 3             # form submissions per minute per IP
    max_concurrent_chats: int = 5
    max_message_length: int = 1000
    allowed_origin: str = ""


@dataclass
class SmsConfig:
    """Dual-channel SMS listener (runs alongside Telegram on the S21)."""
    enabled: bool = False
    allowed_numbers: list[str] = field(default_factory=list)  # e.g. ["+15551234567"]
    # RCS notifications show contact names, not numbers — match notification title
    allowed_sender_names: list[str] = field(default_factory=list)
    poll_interval: int = 5  # seconds


@dataclass
class TelegramConfig:
    bot_token: str = ""
    allowed_users: list[int] = field(default_factory=list)


def _default_channel() -> Channel:
    return "sms" if detect_runtime() == "phone" else "telegram"


@dataclass
class Config:
    runtime: Runtime = field(default_factory=detect_runtime)
    channel: Channel = field(default_factory=_default_channel)
    persona: PersonaConfig = field(default_factory=PersonaConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    cloud_light: CloudTierConfig = field(default_factory=CloudTierConfig)
    cloud_heavy: CloudTierConfig = field(default_factory=CloudTierConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    sms: SmsConfig = field(default_factory=SmsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    atlassian: AtlassianConfig = field(default_factory=AtlassianConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    mcp_servers: list[MCPServerEntry] = field(default_factory=list)
    twelvewy: TwelveWyConfig = field(default_factory=TwelveWyConfig)
    digest: DigestConfig = field(default_factory=DigestConfig)
    alignment: AlignmentConfig = field(default_factory=AlignmentConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    cursor: CursorConfig = field(default_factory=CursorConfig)
    vercel: VercelConfig = field(default_factory=VercelConfig)
    railway: RailwayConfig = field(default_factory=RailwayConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    timezone: str = "America/Los_Angeles"
    data_dir: Path = field(default_factory=lambda: Path("data"))

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        cfg = cls()

        if path and path.exists():
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            # ── Persona ──
            if "persona" in raw:
                p = raw["persona"]
                for k, v in p.items():
                    if k == "brand":
                        for bk, bv in v.items():
                            if hasattr(cfg.persona.brand, bk):
                                setattr(cfg.persona.brand, bk, bv)
                    elif k == "booking":
                        cfg.persona.booking = [
                            BookingLink(**entry) for entry in v
                        ]
                    elif hasattr(cfg.persona, k):
                        setattr(cfg.persona, k, v)
                # Derive allowed_origin from persona domain if not set
                if cfg.persona.domain and not cfg.web.allowed_origin:
                    cfg.web.allowed_origin = f"https://{cfg.persona.domain}"

            if "inference" in raw:
                for k, v in raw["inference"].items():
                    if hasattr(cfg.inference, k):
                        setattr(cfg.inference, k, v)
            if "cloud" in raw:
                cloud = raw["cloud"]
                if "light" in cloud:
                    for k, v in cloud["light"].items():
                        if hasattr(cfg.cloud_light, k):
                            setattr(cfg.cloud_light, k, v)
                if "heavy" in cloud:
                    for k, v in cloud["heavy"].items():
                        if hasattr(cfg.cloud_heavy, k):
                            setattr(cfg.cloud_heavy, k, v)
            if "sms" in raw:
                for k, v in raw["sms"].items():
                    if hasattr(cfg.sms, k):
                        setattr(cfg.sms, k, v)
            if "web" in raw:
                for k, v in raw["web"].items():
                    if hasattr(cfg.web, k):
                        setattr(cfg.web, k, v)
            if "telegram" in raw:
                for k, v in raw["telegram"].items():
                    if hasattr(cfg.telegram, k):
                        setattr(cfg.telegram, k, v)
            if "atlassian" in raw:
                for k, v in raw["atlassian"].items():
                    if hasattr(cfg.atlassian, k):
                        setattr(cfg.atlassian, k, v)
            if "email" in raw:
                for k, v in raw["email"].items():
                    if hasattr(cfg.email, k):
                        setattr(cfg.email, k, v)
            if "observability" in raw:
                for k, v in raw["observability"].items():
                    if hasattr(cfg.observability, k):
                        setattr(cfg.observability, k, v)
            if "search" in raw:
                for k, v in raw["search"].items():
                    if hasattr(cfg.search, k):
                        setattr(cfg.search, k, v)
            if "twelvewy" in raw:
                for k, v in raw["twelvewy"].items():
                    if hasattr(cfg.twelvewy, k):
                        setattr(cfg.twelvewy, k, v)
            if "mcp" in raw:
                for entry in raw["mcp"].get("servers", []):
                    cfg.mcp_servers.append(MCPServerEntry(
                        name=entry.get("name", ""),
                        command=entry.get("command", []),
                        env=entry.get("env", {}),
                        description=entry.get("description", ""),
                        cwd=entry.get("cwd", ""),
                    ))
            if "digest" in raw:
                for k, v in raw["digest"].items():
                    if hasattr(cfg.digest, k):
                        setattr(cfg.digest, k, v)
            if "alignment" in raw:
                for k, v in raw["alignment"].items():
                    if hasattr(cfg.alignment, k):
                        setattr(cfg.alignment, k, v)
            if "engine" in raw:
                for k, v in raw["engine"].items():
                    if hasattr(cfg.engine, k):
                        setattr(cfg.engine, k, v)
            if "cursor" in raw:
                for k, v in raw["cursor"].items():
                    if hasattr(cfg.cursor, k):
                        setattr(cfg.cursor, k, v)
            if "vercel" in raw:
                for k, v in raw["vercel"].items():
                    if hasattr(cfg.vercel, k):
                        setattr(cfg.vercel, k, v)
            if "railway" in raw:
                for k, v in raw["railway"].items():
                    if hasattr(cfg.railway, k):
                        setattr(cfg.railway, k, v)
            if "voice" in raw:
                for k, v in raw["voice"].items():
                    if hasattr(cfg.voice, k):
                        setattr(cfg.voice, k, v)
            if "monitor" in raw:
                for k, v in raw["monitor"].items():
                    if hasattr(cfg.monitor, k):
                        setattr(cfg.monitor, k, v)
            if "channel" in raw:
                cfg.channel = raw["channel"]
            if "timezone" in raw:
                cfg.timezone = raw["timezone"]
            if "data_dir" in raw:
                cfg.data_dir = Path(raw["data_dir"])

        # Env vars fill in API keys
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if token:
            cfg.telegram.bot_token = token

        model = os.environ.get("MODEL_PATH")
        if model:
            cfg.inference.model_path = model

        atlassian_token = os.environ.get("ATLASSIAN_API_TOKEN", "")
        if atlassian_token:
            cfg.atlassian.api_token = atlassian_token
        atlassian_email = os.environ.get("ATLASSIAN_EMAIL", "")
        if atlassian_email:
            cfg.atlassian.email = atlassian_email
        atlassian_domain = os.environ.get("ATLASSIAN_DOMAIN", "")
        if atlassian_domain:
            cfg.atlassian.domain = atlassian_domain

        agentmail_key = os.environ.get("AGENTMAIL_API_KEY", "")
        if agentmail_key and not cfg.email.api_key:
            cfg.email.api_key = agentmail_key
        agentmail_inbox = os.environ.get("AGENTMAIL_INBOX_ID", "")
        if agentmail_inbox and not cfg.email.inbox_id:
            cfg.email.inbox_id = agentmail_inbox

        # Search provider env vars — comma-separated for multiple keys
        brave_keys_env = os.environ.get("BRAVE_API_KEYS", os.environ.get("BRAVE_API_KEY", ""))
        serper_keys_env = os.environ.get("SERPER_API_KEYS", os.environ.get("SERPER_API_KEY", ""))
        if brave_keys_env and not cfg.search.brave_keys:
            cfg.search.brave_keys = [k.strip() for k in brave_keys_env.split(",") if k.strip()]
        if serper_keys_env and not cfg.search.serper_keys:
            cfg.search.serper_keys = [k.strip() for k in serper_keys_env.split(",") if k.strip()]

        twelvewy_base = os.environ.get("TWELVEWY_API_BASE_URL", "")
        if twelvewy_base and not cfg.twelvewy.api_base_url:
            cfg.twelvewy.api_base_url = twelvewy_base
        twelvewy_key = os.environ.get("TWELVEWY_API_KEY", "")
        if twelvewy_key and not cfg.twelvewy.api_key:
            cfg.twelvewy.api_key = twelvewy_key

        whisper_model = os.environ.get("WHISPER_MODEL_PATH", "")
        if whisper_model:
            cfg.voice.stt_model_path = whisper_model

        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        google_key = os.environ.get("GOOGLE_API_KEY", "")

        # Auto-configure tiers from env vars if not set in TOML
        if not cfg.cloud_light.api_key:
            if google_key:
                cfg.cloud_light.provider = cfg.cloud_light.provider or "google"
                cfg.cloud_light.api_key = google_key
            elif anthropic_key:
                cfg.cloud_light.provider = cfg.cloud_light.provider or "anthropic"
                cfg.cloud_light.api_key = anthropic_key

        # Voice STT key: match provider to available key
        if not cfg.voice.stt_api_key:
            if cfg.voice.stt_provider == "gemini" and google_key:
                cfg.voice.stt_api_key = google_key
            elif cfg.voice.stt_provider == "openai":
                openai_key = os.environ.get("OPENAI_API_KEY", "")
                if openai_key:
                    cfg.voice.stt_api_key = openai_key
            elif google_key:
                # Default fallback: use Google key for Gemini STT
                cfg.voice.stt_api_key = google_key

        if not cfg.cloud_heavy.api_key:
            if anthropic_key:
                cfg.cloud_heavy.provider = cfg.cloud_heavy.provider or "anthropic"
                cfg.cloud_heavy.api_key = anthropic_key
            elif google_key:
                cfg.cloud_heavy.provider = cfg.cloud_heavy.provider or "google"
                cfg.cloud_heavy.api_key = google_key

        cursor_key = os.environ.get("CURSOR_API_KEY", "")
        if cursor_key and not cfg.cursor.api_key:
            cfg.cursor.api_key = cursor_key

        vercel_token = os.environ.get("VERCEL_TOKEN", "")
        if vercel_token and not cfg.vercel.api_token:
            cfg.vercel.api_token = vercel_token

        railway_token = os.environ.get("RAILWAY_TOKEN", "")
        if railway_token and not cfg.railway.api_token:
            cfg.railway.api_token = railway_token

        return cfg
