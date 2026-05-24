"""Entry point: python -m palmtop"""

import asyncio
import logging
import sys
from pathlib import Path

from palmtop.config.settings import Config, CloudTierConfig
from palmtop.core.loop import AgentLoop
from palmtop.persona import build_system_prompt, build_web_system_prompt
from palmtop.core.goal_aligner import GoalAligner
from palmtop.core.goals_paths import resolve_goals_path
from palmtop.inference.local import LocalBackend
from palmtop.memory.conversation import ConversationMemory
from palmtop.memory.structured import StructuredMemory
from palmtop.memory.plans import PlanMemory
from palmtop.tools.base import ToolRegistry
from palmtop.tools.web_search import WebSearchTool
from palmtop.tools.calendar import GoogleCalendarTool
from palmtop.tools.reminders import ReminderTool
from palmtop.tools.knowledge import KnowledgeTool
from palmtop.knowledge.store import KnowledgeBase


def _make_cloud(tier: CloudTierConfig, label: str, log):
    if not tier.api_key:
        return None
    from palmtop.inference.cloud import create_cloud_backend

    backend = create_cloud_backend(tier.provider, tier.api_key, tier.model or None)
    log.info("Cloud %s: %s / %s", label, tier.provider, backend._model)
    return backend


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("palmtop")

    config_path = Path("config.toml")
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])

    cfg = Config.load(config_path)
    log.info("Runtime: %s | Channel: %s", cfg.runtime, cfg.channel)

    local_backend = LocalBackend(cfg.inference)

    # ── Create stores (sync constructors only — .init() deferred) ────
    conv_memory = ConversationMemory(cfg.data_dir / "conversations.db")
    structured_memory = StructuredMemory(cfg.data_dir / "memories.db")
    plan_memory = PlanMemory(cfg.data_dir / "plans.db")
    calendar = GoogleCalendarTool(cfg.data_dir, timezone=cfg.timezone)
    reminders = ReminderTool(cfg.data_dir / "reminders.db", timezone=cfg.timezone)
    kb = KnowledgeBase(cfg.data_dir / "knowledge.db")

    light = _make_cloud(cfg.cloud_light, "light", log)
    heavy = _make_cloud(cfg.cloud_heavy, "heavy", log)

    if not light and not heavy:
        log.info("No cloud API keys — running local-only")

    tools = ToolRegistry()
    tools.register(WebSearchTool(
        brave_keys=cfg.search.brave_keys,
        serper_keys=cfg.search.serper_keys,
        preferred_order=cfg.search.preferred_order or None,
    ))
    tools.register(calendar)
    tools.register(reminders)
    tools.register(KnowledgeTool(kb))

    # --- Local file persistence ---
    from palmtop.tools.files import FileTool
    tools.register(FileTool(cfg.data_dir))
    log.info("File tool enabled: %s", cfg.data_dir / "docs")

    # --- Email (AgentMail) ---
    email_tool = None
    if cfg.email.api_key:
        from palmtop.tools.email import EmailTool
        email_tool = EmailTool(cfg.email.api_key, cfg.email.inbox_id)
        tools.register(email_tool)
        log.info("Email tool registered (init deferred to event loop)")
    else:
        log.info("No AgentMail API key — email disabled")

    # --- Atlassian: MCP first, REST fallback ---
    # Build REST tools (cheap to create, used as fallback if MCP fails)
    jira_rest = None
    confluence_rest = None
    if cfg.atlassian.domain and cfg.atlassian.api_token:
        from palmtop.tools.jira import JiraTool, ConfluenceTool
        jira_rest = JiraTool(cfg.atlassian.domain, cfg.atlassian.email, cfg.atlassian.api_token)
        confluence_rest = ConfluenceTool(cfg.atlassian.domain, cfg.atlassian.email, cfg.atlassian.api_token)
    elif cfg.atlassian.domain and not cfg.atlassian.api_token:
        log.warning(
            "Atlassian domain set (%s) but no API token — "
            "set ATLASSIAN_API_TOKEN env var",
            cfg.atlassian.domain,
        )

    # Check if an MCP server covers Atlassian
    atlassian_mcp_entry = None
    for entry in cfg.mcp_servers:
        if entry.name.lower() == "atlassian" and entry.command:
            atlassian_mcp_entry = entry
            break

    if atlassian_mcp_entry:
        from palmtop.mcp.client import MCPServerConfig, check_mcp_prerequisites
        from palmtop.mcp.gateway import MCPGatewayTool
        prereq_error = check_mcp_prerequisites(atlassian_mcp_entry.command)
        if prereq_error:
            log.warning("MCP 'atlassian' unavailable (%s) — falling back to REST", prereq_error)
            if jira_rest:
                tools.register(jira_rest)
                tools.register(confluence_rest)
                log.info("Atlassian REST fallback enabled: %s", cfg.atlassian.domain)
        else:
            # Auto-map [atlassian] config → env vars the MCP server expects
            mcp_env = dict(atlassian_mcp_entry.env)
            if cfg.atlassian.domain:
                jira_url = f"https://{cfg.atlassian.domain}"
                confluence_url = f"https://{cfg.atlassian.domain}/wiki"
                mcp_env.setdefault("JIRA_URL", jira_url)
                mcp_env.setdefault("CONFLUENCE_URL", confluence_url)
            if cfg.atlassian.email:
                mcp_env.setdefault("JIRA_USERNAME", cfg.atlassian.email)
                mcp_env.setdefault("CONFLUENCE_USERNAME", cfg.atlassian.email)
            if cfg.atlassian.api_token:
                mcp_env.setdefault("JIRA_API_TOKEN", cfg.atlassian.api_token)
                mcp_env.setdefault("CONFLUENCE_API_TOKEN", cfg.atlassian.api_token)

            mcp_cfg = MCPServerConfig(
                name=atlassian_mcp_entry.name,
                command=atlassian_mcp_entry.command,
                env=mcp_env,
            )
            gateway = MCPGatewayTool(
                mcp_cfg,
                tool_description=atlassian_mcp_entry.description,
                fallback_tools=[jira_rest, confluence_rest] if jira_rest else None,
            )
            tools.register(gateway)
            log.info("Atlassian MCP gateway registered (REST fallback ready)")
    elif jira_rest:
        tools.register(jira_rest)
        tools.register(confluence_rest)
        log.info("Atlassian REST tools enabled: %s", cfg.atlassian.domain)
    else:
        log.info("No Atlassian config — Jira/Confluence disabled")

    # --- 12 Week Year MCP (Railway remote mode) ---
    from palmtop.mcp.twelvewy import register_twelvewy, is_twelvewy_server

    twelvewy_prompt = register_twelvewy(cfg, tools)

    # Register remaining MCP servers (non-Atlassian; 12wy uses REST gateway above)
    if cfg.mcp_servers:
        from palmtop.mcp.client import MCPServerConfig, check_mcp_prerequisites
        from palmtop.mcp.gateway import MCPGatewayTool
        for entry in cfg.mcp_servers:
            if not entry.command or entry.name.lower() == "atlassian":
                continue  # already handled above
            if is_twelvewy_server(entry.name):
                if not twelvewy_prompt:
                    log.warning(
                        "MCP server '%s' in config.toml ignored — set [twelvewy] "
                        "api_base_url and TWELVEWY_API_KEY",
                        entry.name,
                    )
                continue
            prereq_error = check_mcp_prerequisites(entry.command)
            if prereq_error:
                log.warning("MCP '%s' skipped: %s", entry.name, prereq_error)
                continue
            cwd = entry.cwd or None
            mcp_cfg = MCPServerConfig(
                name=entry.name,
                command=entry.command,
                env=entry.env,
                cwd=cwd,
            )
            gateway = MCPGatewayTool(mcp_cfg, tool_description=entry.description)
            tools.register(gateway)
            log.info("MCP gateway '%s' registered (connects on first use)", entry.name)

    # --- Observability ---
    from palmtop.core.tracing import Tracer
    tracer = Tracer(
        enabled=cfg.observability.enabled,
        backend=cfg.observability.backend,
        data_dir=cfg.data_dir,
    )

    project_root = Path(".").resolve()
    if cfg.alignment.goals_path:
        goals_path = Path(cfg.alignment.goals_path)
    else:
        goals_path = resolve_goals_path(cfg.data_dir, project_root)

    # Build the engine LLM from cloud backends (heavy preferred, light fallback)
    from palmtop.inference.engine_llm import CloudLLMAdapter

    engine_llm = None
    if heavy:
        engine_llm = CloudLLMAdapter(heavy, fallback=light)
    elif light:
        engine_llm = CloudLLMAdapter(light)

    # Semantic judge uses the cloud LLM (if available)
    from palmtop.core.alignment_judge import SemanticAlignmentJudge
    semantic_judge = (
        SemanticAlignmentJudge(engine_llm)
        if cfg.alignment.use_semantic and engine_llm
        else None
    )

    goal_aligner = GoalAligner(
        goals_path,
        semantic_judge=semantic_judge,
        use_semantic=cfg.alignment.use_semantic,
    )
    if goals_path.is_file():
        log.info("12WY goal alignment enabled: %s (mode=%s)", goals_path, cfg.alignment.mode)
    else:
        log.info(
            "12WY goal alignment ready — copy docs/plans/twy_goals.example.json to %s",
            goals_path,
        )
    sovereign = None
    if cfg.engine.enabled and engine_llm:
        from palmtop.core.engine import PalmtopAgent
        from palmtop.core.context import ContextManager

        engine_context = ContextManager(
            structured=structured_memory,
            plans=plan_memory,
            kb=kb,
        )

        try:
            sovereign = PalmtopAgent(
                goals_path=goals_path,
                llm=engine_llm,
                aligner=goal_aligner,
                context=engine_context,
                data_dir=cfg.data_dir,
                project_root=project_root,
                autonomous=True,
            )
            log.info(
                "Sovereign engine wired to %s (with context) — "
                "trigger with /engine or engine: <task>",
                cfg.persona.name,
            )
        except (ValueError, ConnectionError) as e:
            log.warning("Sovereign engine disabled: %s", e)
            sovereign = None
    elif cfg.engine.enabled:
        log.info("Sovereign engine skipped — no cloud API keys configured")

    # --- Blessing gate (engine + cursor + deploy tools share human approval) ---
    from palmtop.core.blessing import BlessingGate
    deploy_enabled = (
        (cfg.vercel.enabled and cfg.vercel.api_token)
        or (cfg.railway.enabled and cfg.railway.api_token)
    )
    blessing_gate = BlessingGate() if (sovereign or cfg.cursor.enabled or deploy_enabled) else None

    # --- Cursor Cloud Agents bridge ---
    cursor_manager = None
    if cfg.cursor.enabled and cfg.cursor.api_key:
        from palmtop.cursor.client import CursorAgentsClient
        from palmtop.cursor.runner import CursorJobManager
        from palmtop.tools.cursor_delegate import DelegateCursorTool

        cursor_client = CursorAgentsClient(cfg.cursor.api_key)
        cursor_manager = CursorJobManager(
            cursor_client,
            cfg.cursor,
            cfg.data_dir,
            blessing_gate=blessing_gate if cfg.cursor.require_blessing else None,
        )
        tools.register(DelegateCursorTool(cursor_manager))
        log.info(
            "Cursor bridge enabled (%d allowed repos, max_concurrent=%d)",
            len(cfg.cursor.allowed_repos),
            cfg.cursor.max_concurrent,
        )
    elif cfg.cursor.enabled:
        log.info("Cursor bridge enabled in config but CURSOR_API_KEY not set — disabled")

    # --- Vercel deploy ---
    vercel_tool = None
    if cfg.vercel.enabled and cfg.vercel.api_token:
        from palmtop.tools.vercel import VercelDeployTool

        vercel_tool = VercelDeployTool(
            cfg.vercel,
            blessing_gate=blessing_gate if cfg.vercel.require_blessing else None,
        )
        tools.register(vercel_tool)
        log.info("Vercel deploy tool registered")
    elif cfg.vercel.enabled:
        log.info("Vercel enabled in config but VERCEL_TOKEN not set — disabled")

    # --- Railway deploy ---
    railway_tool = None
    if cfg.railway.enabled and cfg.railway.api_token:
        from palmtop.tools.railway import RailwayDeployTool

        railway_tool = RailwayDeployTool(
            cfg.railway,
            blessing_gate=blessing_gate if cfg.railway.require_blessing else None,
        )
        tools.register(railway_tool)
        log.info("Railway deploy tool registered")
    elif cfg.railway.enabled:
        log.info("Railway enabled in config but RAILWAY_TOKEN not set — disabled")

    agent = AgentLoop(
        local_backend,
        memory=conv_memory,
        structured_memory=structured_memory,
        plan_memory=plan_memory,
        tools=tools,
        light_backend=light,
        heavy_backend=heavy,
        timezone=cfg.timezone,
        tracer=tracer,
        extra_system_prompt=twelvewy_prompt,
        goal_aligner=goal_aligner,
        alignment_mode=cfg.alignment.mode,
        sovereign_engine=sovereign,
        data_dir=cfg.data_dir,
        blessing_gate=blessing_gate,
        cursor_manager=cursor_manager,
        system_prompt=build_system_prompt(cfg.persona),
    )

    # ── Async startup ────────────────────────────────────────────────
    # All async initialization runs ONCE inside the channel's event loop.
    # This avoids the fragmentation from multiple asyncio.run() calls
    # which create and destroy loops, orphaning DB connections and
    # httpx clients on dead loops.

    async def _async_startup():
        """Initialize all async resources inside the real event loop.

        Stores init in parallel (no dependencies between them).
        API auth checks also run in parallel after stores are ready.
        """
        # Phase 1: all DB stores + calendar creds in parallel
        results = await asyncio.gather(
            conv_memory.init(),
            structured_memory.init(),
            plan_memory.init(),
            calendar.init(),
            reminders.init(),
            kb.init(),
            return_exceptions=True,
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                names = ["conversations", "structured", "plans", "calendar", "reminders", "knowledge"]
                log.warning("Store init failed (%s): %s", names[i], r)

        # Phase 2: API auth checks in parallel (all independent)
        api_tasks = []
        if email_tool:
            api_tasks.append(("email", email_tool.init()))
        if jira_rest:
            api_tasks.append(("jira", jira_rest.verify_auth()))
        if confluence_rest:
            api_tasks.append(("confluence", confluence_rest.verify_auth()))
        if vercel_tool:
            api_tasks.append(("vercel", vercel_tool.verify_auth()))
        if railway_tool:
            api_tasks.append(("railway", railway_tool.verify_auth()))

        if api_tasks:
            api_results = await asyncio.gather(
                *[t for _, t in api_tasks],
                return_exceptions=True,
            )
            for (label, _), result in zip(api_tasks, api_results):
                if isinstance(result, Exception):
                    log.warning("%s init/auth failed (will retry on use): %s", label, result)
                elif isinstance(result, str):
                    # verify_auth returns error string or None
                    log.warning("%s auth issue: %s", label, result)
                elif label == "email":
                    log.info("Email tool ready: %s", email_tool._email_address or "(resolves on first use)")

        log.info("Async startup complete — all stores initialized")

    if cfg.channel == "sms":
        from palmtop.channels.sms import SmsChannel

        SmsChannel(agent).run(async_init=_async_startup)
    else:
        try:
            from palmtop.channels.telegram import TelegramChannel
        except ImportError:
            log.error(
                "Telegram channel requires python-telegram-bot. "
                "On the S21 run: uv sync --extra telegram"
            )
            raise SystemExit(1) from None

        # --- Voice (STT + TTS) ---
        stt, tts = None, None
        if cfg.voice.enabled:
            from palmtop.voice.stt import create_stt
            stt = create_stt(cfg.voice)
            if stt:
                log.info("Voice STT enabled: %s", type(stt).__name__)
            else:
                log.warning("Voice enabled in config but STT provider failed to load")

            if cfg.voice.tts_enabled:
                from palmtop.voice.tts import create_tts
                tts = create_tts(cfg.voice)
                if tts:
                    log.info("Voice TTS enabled: %s", type(tts).__name__)
                else:
                    log.warning("TTS enabled in config but provider failed to load")

        channel = TelegramChannel(
            cfg.telegram.bot_token,
            agent,
            allowed_users=cfg.telegram.allowed_users or None,
            stt=stt,
            tts=tts,
            data_dir=cfg.data_dir,
            blessing_gate=blessing_gate,
        )
        reminders.set_notify(channel.send_message)
        # Wire send_fn for blessing gate + Cursor completion notifications
        agent._send_fn = channel.send_message
        if cursor_manager:
            cursor_manager.set_notify(channel.send_message)
        if vercel_tool:
            vercel_tool.set_notify(channel.send_message)
        if railway_tool:
            railway_tool.set_notify(channel.send_message)

        def _on_start():
            # Give the ContextManager a ref to the running loop so
            # gather_sync() (called from engine worker threads) schedules
            # coroutines on this loop where the DB connections live.
            if sovereign:
                try:
                    ctx = sovereign.context
                    if ctx is not None:
                        ctx._loop = asyncio.get_running_loop()
                except Exception:
                    pass
            reminders.start_background_check()
            if cfg.digest.enabled and cfg.telegram.allowed_users:
                from palmtop.core.digest import DigestService
                digest = DigestService(
                    send_fn=channel.send_message,
                    user_ids=[str(uid) for uid in cfg.telegram.allowed_users],
                    calendar=calendar,
                    reminders=reminders,
                    plans=plan_memory,
                    hour=cfg.digest.hour,
                    minute=cfg.digest.minute,
                    timezone=cfg.timezone,
                )
                digest.start()
                log.info("Daily digest enabled at %02d:%02d", cfg.digest.hour, cfg.digest.minute)

            # --- Proactive monitor ---
            if cfg.monitor.enabled and cfg.telegram.allowed_users:
                from palmtop.core.monitor import MonitorService
                # Find jira/atlassian tool if registered
                jira_tool = tools.get("atlassian") or tools.get("jira")
                email_tool_ref = tools.get("email")

                # Jira→Cursor auto-delegation bridge
                jira_cursor_bridge = None
                if cursor_manager and jira_tool:
                    from palmtop.cursor.jira_bridge import JiraCursorBridge
                    jira_cursor_bridge = JiraCursorBridge(
                        jira_tool,
                        cursor_manager,
                        send_fn=channel.send_message,
                        user_id=str(cfg.telegram.allowed_users[0]),
                    )
                    log.info("Jira→Cursor bridge enabled — coding tickets auto-delegate")

                monitor = MonitorService(
                    send_fn=channel.send_message,
                    user_ids=[str(uid) for uid in cfg.telegram.allowed_users],
                    config=cfg.monitor,
                    timezone=cfg.timezone,
                    calendar=calendar,
                    plans=plan_memory,
                    email_tool=email_tool_ref,
                    jira_tool=jira_tool,
                    data_dir=cfg.data_dir,
                    llm_backend=light or heavy,
                    jira_cursor_bridge=jira_cursor_bridge,
                )
                monitor.start()
                log.info("Proactive monitor enabled")

            # --- SMS listener (dual-channel, runs alongside Telegram) ---
            if cfg.sms.enabled and (
                cfg.sms.allowed_numbers or cfg.sms.allowed_sender_names
            ):
                from palmtop.channels.sms_listener import SmsListener
                sms_listener = SmsListener(
                    agent,
                    allowed_numbers=cfg.sms.allowed_numbers,
                    allowed_sender_names=cfg.sms.allowed_sender_names,
                    telegram_send_fn=channel.send_message,
                )
                sms_listener.start()
                log.info(
                    "SMS listener enabled (numbers: %s, RCS names: %s)",
                    ", ".join(cfg.sms.allowed_numbers) or "—",
                    ", ".join(cfg.sms.allowed_sender_names) or "—",
                )

            # --- Web channel (sandboxed — no internal access) ---
            if cfg.web.enabled:
                web_llm = light or heavy
                if web_llm:
                    from palmtop.web.agent import WebAgent
                    from palmtop.web.ratelimit import RateLimiter
                    from palmtop.web.app import create_app

                    web_agent = WebAgent(
                        web_llm,
                        system_prompt=build_web_system_prompt(cfg.persona),
                    )
                    web_limiter = RateLimiter(
                        chat_rpm=cfg.web.chat_rpm,
                        chat_rpd=cfg.web.chat_rpd,
                        form_rpm=cfg.web.form_rpm,
                        max_concurrent=cfg.web.max_concurrent_chats,
                    )
                    notify_uid = str(cfg.telegram.allowed_users[0]) if cfg.telegram.allowed_users else ""

                    # Lead outreach: qualify + auto-email
                    lead_outreach = None
                    if email_tool:
                        from palmtop.web.outreach import LeadOutreach
                        lead_outreach = LeadOutreach(
                            llm=web_llm,
                            email_tool=email_tool,
                            notify_fn=channel.send_message,
                            notify_user_id=notify_uid,
                            persona=cfg.persona,
                        )
                        log.info("Lead outreach enabled (auto-qualify + email)")

                    web_app = create_app(
                        agent=web_agent,
                        rate_limiter=web_limiter,
                        notify_fn=channel.send_message,
                        notify_user_id=notify_uid,
                        allowed_origin=cfg.web.allowed_origin,
                        outreach=lead_outreach,
                        persona=cfg.persona,
                    )

                    import uvicorn
                    uvi_config = uvicorn.Config(
                        web_app,
                        host=cfg.web.host,
                        port=cfg.web.port,
                        log_level="warning",
                        access_log=False,
                    )
                    uvi_server = uvicorn.Server(uvi_config)
                    asyncio.create_task(uvi_server.serve())
                    log.info(
                        "Web channel started on %s:%d (sandboxed — no internal access)",
                        cfg.web.host, cfg.web.port,
                    )
                else:
                    log.warning("Web channel enabled but no cloud LLM available — disabled")

        async def _shutdown(_app):
            """Close all resources cleanly on shutdown."""
            log.info("Shutting down — closing resources...")
            close_tasks = []
            if conv_memory:
                close_tasks.append(conv_memory.close())
            if structured_memory:
                close_tasks.append(structured_memory.close())
            if plan_memory:
                close_tasks.append(plan_memory.close())
            if kb:
                close_tasks.append(kb.close())
            close_tasks.append(tools.close())
            if light:
                close_tasks.append(light.close())
            if heavy:
                close_tasks.append(heavy.close())
            if cursor_manager:
                close_tasks.append(cursor_manager.close())
            # Voice clients
            if stt and hasattr(stt, "close"):
                close_tasks.append(stt.close())
            if tts and hasattr(tts, "close"):
                close_tasks.append(tts.close())
            for task in close_tasks:
                try:
                    await task
                except Exception:
                    log.debug("Cleanup error (non-fatal)", exc_info=True)
            log.info("Shutdown complete")

        channel._app.post_shutdown = _shutdown
        channel.run(on_start=_on_start, async_init=_async_startup)


if __name__ == "__main__":
    main()
