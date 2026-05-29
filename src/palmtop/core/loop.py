from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from palmtop.channels.auth import owner_key
from palmtop.core.evaluator import check_capability_claims, check_date_claims, evaluate_response
from palmtop.core.goal_aligner import GoalAligner
from palmtop.core.router import route_fast
from palmtop.core.sovereign_runner import parse_engine_task, run_sovereign_engine
from palmtop.core.tracing import Tracer
from palmtop.cursor.runner import parse_cursor_task
from palmtop.inference.base import InferenceBackend, Message
from palmtop.memory.conversation import ConversationMemory
from palmtop.memory.plans import (
    PLAN_INSTRUCTIONS,
    PlanMemory,
    extract_plans_from_reply,
    format_plans_for_context,
)
from palmtop.memory.structured import EXTRACT_PROMPT, StructuredMemory, parse_extraction
from palmtop.tools.base import (
    ERROR_GUIDANCE,
    ToolRegistry,
    ToolResult,
    build_retry_query,
    classify_result,
    detect_tool_hints,
    extract_action_chains,
    extract_tool_calls,
    get_default_fallbacks,
    resolve_tool_name,
)

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 30.0  # seconds before a tool call is killed
CURSOR_TOOL_TIMEOUT = 330.0  # blessing gate can block up to 5 minutes


def _safe_ensure_future(coro, *, label: str = "background") -> asyncio.Task:
    """Schedule a coroutine and log any exception instead of dropping it."""
    task = asyncio.ensure_future(coro)

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc:
            log.warning("Background task '%s' failed: %s", label, exc)

    task.add_done_callback(_done)
    return task


class AgentLoop:
    def __init__(
        self,
        local_backend: InferenceBackend,
        memory: ConversationMemory | None = None,
        structured_memory: StructuredMemory | None = None,
        plan_memory: PlanMemory | None = None,
        tools: ToolRegistry | None = None,
        light_backend: InferenceBackend | None = None,
        heavy_backend: InferenceBackend | None = None,
        timezone: str = "America/Los_Angeles",
        tracer: Tracer | None = None,
        extra_system_prompt: str | None = None,
        goal_aligner: GoalAligner | None = None,
        alignment_mode: str = "soft",
        sovereign_engine: object | None = None,
        data_dir: Path | None = None,
        blessing_gate: object | None = None,
        send_fn: object | None = None,
        cursor_manager: object | None = None,
        system_prompt: str = "",
        owner_ids: set[str] | None = None,
    ) -> None:
        self._local = local_backend
        self._light = light_backend
        self._heavy = heavy_backend
        self._memory = memory
        self._structured = structured_memory
        self._plans = plan_memory
        self._tools = tools
        self._has_cloud = bool(light_backend or heavy_backend)
        self._system_prompt = system_prompt
        self._tz = ZoneInfo(timezone)
        self._tracer = tracer or Tracer(enabled=False)
        self._extra_system_prompt = extra_system_prompt or ""
        self._aligner = goal_aligner
        self._alignment_mode = alignment_mode
        self._sovereign = sovereign_engine
        self._data_dir = data_dir or Path("data")
        self._blessing_gate = blessing_gate
        self._send_fn = send_fn
        self._cursor = cursor_manager
        # Owners authorized for privileged engine:/cursor: commands. Channel-
        # qualified IDs (e.g. "telegram:123", "sms:+1555"). Empty = fail closed.
        self._owner_ids = owner_ids or set()

        # Pre-build the static portion of the system prompt — tool instructions,
        # plan instructions, and extra system prompt never change between messages.
        # Only date, memory, and plans context are interpolated per-request.
        self._static_suffix = self._build_static_suffix()

    def _is_owner(self, user_id: str, source: str = "") -> bool:
        """Whether this sender may run privileged engine:/cursor: commands.

        Fails closed: with no owners configured, nobody is authorized. The key
        is channel-qualified — channels that already prefix user_id (slack:U1,
        sms:+1...) pass it through; bare-ID channels (Telegram) pass source so
        we can qualify it the same way owners are listed in config.
        """
        if not self._owner_ids:
            return False
        return owner_key(user_id, source) in self._owner_ids

    async def run_sovereign_engine(self, task: str, user_id: str = "default", source: str = "") -> str:
        """Autonomous align → gate → bless → execute (no stdin)."""
        if not self._is_owner(user_id, source):
            log.warning("Refused engine command from non-owner %s", f"{source}:{user_id}" if source else user_id)
            return "Not authorized — engine commands are restricted to the configured owner(s)."
        if not self._sovereign:
            return (
                "Sovereign engine is disabled. Set [engine] enabled = true in config.toml "
                "and configure ANTHROPIC_API_KEY or GOOGLE_API_KEY."
            )
        task = task.strip()
        if not task:
            return "Usage: /engine <task>  or  engine: <task>  (alias: /claude, claude:)"
        return await run_sovereign_engine(
            self._sovereign,
            task,
            data_dir=self._data_dir,
            user_id=user_id,
            blessing_gate=self._blessing_gate,
            send_fn=self._send_fn,
        )

    async def run_cursor_delegate(self, task: str, user_id: str = "default", source: str = "") -> str:
        """Launch a Cursor Cloud Agent (async completion via Telegram notify)."""
        if not self._is_owner(user_id, source):
            log.warning("Refused cursor command from non-owner %s", f"{source}:{user_id}" if source else user_id)
            return "Not authorized — cursor commands are restricted to the configured owner(s)."
        if not self._cursor:
            return (
                "Cursor bridge is disabled. Set [cursor] enabled = true, "
                "CURSOR_API_KEY, and allowed_repos in config.toml."
            )
        task = task.strip()
        if not task:
            return "Usage: /cursor <prompt>  or  cursor: <prompt>"
        alignment = self._check_goal_alignment(task)
        align_dict = dict(alignment) if alignment else None
        return await self._cursor.launch(task, user_id=user_id, alignment=align_dict)

    def _check_goal_alignment(self, task: str) -> dict[str, object] | None:
        if not self._aligner:
            return None
        alignment = self._aligner.check_alignment(task)
        if not alignment["is_aligned"]:
            log.debug(
                "Goal alignment: %s (score=%s)",
                alignment["note"],
                alignment.get("score", 0),
            )
        return alignment

    def _build_static_suffix(self) -> str:
        """Pre-compute the system prompt suffix that never changes between messages.

        Tool instructions, plan instructions, and extra system prompt are all
        static for the lifetime of the AgentLoop. Caching avoids regenerating
        ~2-4KB of text on every single message.
        """
        parts: list[str] = []
        if self._tools:
            parts.append(self._tools.format_instructions())
        if self._plans:
            parts.append(PLAN_INSTRUCTIONS)
        if self._extra_system_prompt:
            parts.append(self._extra_system_prompt)
        return "".join(parts)

    def _pick_extractor(self) -> InferenceBackend:
        return self._light or self._heavy or self._local

    async def _build_memory_context(self, user_id: str, query: str = "") -> str:
        """Build memory context, prioritizing relevant memories.

        If a query is provided, searches for matching memories in parallel,
        then fills remaining slots with recent memories.
        """
        if not self._structured:
            return ""

        relevant: list = []
        seen_ids: set[int] = set()

        # Search for query-relevant memories — run all terms in parallel
        if query and len(query) > 3:
            terms = [w for w in query.lower().split() if len(w) > 3][:3]
            if terms:
                search_results = await asyncio.gather(
                    *[self._structured.search(user_id, term, limit=5) for term in terms],
                    # Also fetch recent in parallel with the searches
                    self._structured.recall(user_id, limit=30),
                )
                # search_results[-1] is the recall result
                for hits in search_results[:-1]:
                    for m in hits:
                        if m.id not in seen_ids:
                            relevant.append(m)
                            seen_ids.add(m.id)
                for m in search_results[-1]:
                    if m.id not in seen_ids:
                        relevant.append(m)
                        seen_ids.add(m.id)
                    if len(relevant) >= 30:
                        break

                if not relevant:
                    return ""
                return "\n".join(f"[{m.category}] {m.content}" for m in relevant)

        # No query — just fetch recent
        recent = await self._structured.recall(user_id, limit=30)
        if not recent:
            return ""
        return "\n".join(f"[{m.category}] {m.content}" for m in recent)

    async def _extract_and_store(self, user_id: str, user_text: str, reply: str) -> None:
        if not self._structured:
            return
        extractor = self._pick_extractor()
        prompt = EXTRACT_PROMPT.format(user_msg=user_text, assistant_msg=reply)
        try:
            raw = await extractor.complete([Message(role="user", content=prompt)], max_tokens=256)
            parsed = parse_extraction(raw)
            for category, content in parsed:
                await self._structured.store(user_id, category, content)
        except Exception:
            log.exception("Memory extraction failed (non-fatal)")

    async def _process_plan_output(self, user_id: str, reply: str) -> None:
        if not self._plans:
            return
        plan_actions = extract_plans_from_reply(reply)
        for pa in plan_actions:
            try:
                if pa["action"] == "close":
                    await self._plans.close_plan(user_id, pa["title"])
                else:
                    all_steps = pa["steps_pending"] + pa["steps_done"]
                    if all_steps:
                        plan = await self._plans.create_plan(user_id, pa["title"], all_steps)
                        if pa["steps_done"]:
                            await self._plans.mark_steps_done(plan.id, pa["steps_done"])
            except Exception:
                log.exception("Plan processing failed (non-fatal)")

    async def _execute_tool(self, tool_name: str, query: str) -> ToolResult:
        """Execute a single tool call with classification and auto-retry."""
        resolved = resolve_tool_name(self._tools, tool_name) if self._tools else None
        tool = self._tools.get(resolved) if resolved and self._tools else None
        if not tool:
            return ToolResult(
                tool=tool_name,
                query=query,
                result="Unknown tool.",
                success=False,
                error_kind="not_found",
            )

        log.info("Running tool: %s(%s)", resolved, query[:80])
        if hasattr(tool, "set_user_id") and getattr(self, "_current_user_id", None):
            tool.set_user_id(self._current_user_id)
        timeout = CURSOR_TOOL_TIMEOUT if resolved == "cursor" else TOOL_TIMEOUT
        try:
            raw = await asyncio.wait_for(tool.run(query), timeout=timeout)
        except TimeoutError:
            log.warning("Tool %s timed out after %.0fs", resolved, TOOL_TIMEOUT)
            return ToolResult(
                tool=tool_name,
                query=query,
                result=f"Tool timed out after {TOOL_TIMEOUT:.0f}s.",
                success=False,
                error_kind="timeout",
            )
        result = classify_result(tool_name, query, raw)

        # Auto-retry on retryable errors (max 1 retry per call)
        if not result.success and result.retry_hint:
            retry_q = build_retry_query(tool_name, query, result.error_kind)
            if retry_q:
                if result.retry_hint == "retry":
                    await asyncio.sleep(0.5)
                log.info(
                    "Retrying %s (%s → %s): %s",
                    tool_name,
                    result.error_kind,
                    result.retry_hint,
                    retry_q[:80],
                )
                try:
                    raw2 = await asyncio.wait_for(tool.run(retry_q), timeout=TOOL_TIMEOUT)
                except TimeoutError:
                    result.result += f"\n(Retry also timed out after {TOOL_TIMEOUT:.0f}s)"
                    return result
                retry = classify_result(tool_name, retry_q, raw2)
                original_error = result.result[:200]
                if retry.success:
                    retry.retried = True
                    retry.original_error = original_error
                    return retry
                result.result += f"\n(Retry also failed: {retry.result[:200]})"

        return result

    async def _run_tool_calls(self, reply: str) -> list[ToolResult] | None:
        if not self._tools:
            return None
        calls = extract_tool_calls(reply)
        if not calls:
            return None

        results: list[ToolResult] = []
        for name, q in calls:
            result = await self._execute_tool(name, q)

            # Auto-pivot: if the tool failed and there's no explicit
            # ON_FAIL chain, try default cross-tool fallbacks.
            if not result.success and result.error_kind in (
                "auth",
                "not_configured",
                "connection",
                "server",
                "timeout",
            ):
                fallbacks = get_default_fallbacks(name, q)
                # Only try fallbacks for tools that are actually registered
                fallbacks = [fb for fb in fallbacks if resolve_tool_name(self._tools, fb.tool)]
                if fallbacks:
                    primary_error = f"{name}: {result.result[:150]}"
                    for fb in fallbacks:
                        log.info(
                            "Auto-pivot: %s failed (%s) → trying %s(%s)",
                            name,
                            result.error_kind,
                            fb.tool,
                            fb.query[:60],
                        )
                        fb_result = await self._execute_tool(fb.tool, fb.query)
                        if fb_result.success:
                            fb_result.retried = True
                            fb_result.original_error = primary_error
                            result = fb_result
                            break

            results.append(result)
        return results

    async def _run_action_chains(self, reply: str) -> list[ToolResult] | None:
        """Execute ACTION chains with ON_FAIL fallbacks.

        Each chain tries its primary action. If it fails (after auto-retry),
        tries each ON_FAIL fallback in order until one succeeds.
        """
        if not self._tools:
            return None
        chains = extract_action_chains(reply)
        if not chains:
            return None

        results: list[ToolResult] = []
        for chain in chains[:5]:  # max 5 chains per turn
            result = await self._execute_tool(chain.primary.tool, chain.primary.query)

            if not result.success and chain.fallbacks:
                primary_error = f"{chain.primary.tool}: {result.result[:150]}"
                for fb in chain.fallbacks:
                    log.info(
                        "Action fallback: %s → %s(%s)",
                        chain.primary.tool,
                        fb.tool,
                        fb.query[:60],
                    )
                    fb_result = await self._execute_tool(fb.tool, fb.query)
                    if fb_result.success:
                        fb_result.retried = True
                        fb_result.original_error = primary_error
                        result = fb_result
                        break
                else:
                    # All fallbacks also failed
                    result.result += "\n(All fallback attempts also failed)"

            results.append(result)
        return results

    async def _auto_run_tools(self, user_text: str) -> str:
        if not self._tools:
            return ""
        hints = detect_tool_hints(user_text)
        if not hints:
            return ""

        # Build tasks for parallel execution (with classification + retry)
        async def _run_one(tool_name: str, label: str, query: str) -> str | None:
            tool = self._tools.get(tool_name)
            if not tool:
                return None
            try:
                log.info("Auto-running %s", tool_name)
                raw = await tool.run(query)
                result = classify_result(tool_name, query, raw)

                if not result.success and result.retry_hint:
                    retry_q = build_retry_query(tool_name, query, result.error_kind)
                    if retry_q:
                        if result.retry_hint == "retry":
                            await asyncio.sleep(0.5)
                        log.info(
                            "Auto-tool %s retry (%s): %s",
                            tool_name,
                            result.error_kind,
                            retry_q[:80],
                        )
                        raw2 = await tool.run(retry_q)
                        retry = classify_result(tool_name, retry_q, raw2)
                        if retry.success:
                            return f"[{label}]\n{retry.result}"
                    log.warning(
                        "Auto-tool %s failed (%s): %s",
                        tool_name,
                        result.error_kind,
                        result.result[:100],
                    )
                    return None

                return f"[{label}]\n{result.result}" if result.success else None
            except Exception:
                log.exception("Auto-tool %s failed", tool_name)
                return None

        def _resolve(name: str, fallback: str = "") -> str | None:
            """Find a tool by name, falling back to MCP gateway if REST tool missing."""
            if self._tools.get(name):
                return name
            if fallback and self._tools.get(fallback):
                return fallback
            return None

        tasks = []
        for tool_name, _ in hints:
            if tool_name == "calendar":
                tasks.append(_run_one("calendar", "Calendar data", "show week"))
            elif tool_name == "search":
                tasks.append(_run_one("search", "Search results", user_text))
            elif tool_name == "kb":
                tasks.append(_run_one("kb", "Knowledge base", f"search {user_text}"))
            elif tool_name == "jira":
                resolved = _resolve("jira", "atlassian")
                if resolved:
                    query = "my issues" if resolved == "jira" else f"search jira issues: {user_text}"
                    tasks.append(_run_one(resolved, "Jira issues", query))
            elif tool_name == "confluence":
                resolved = _resolve("confluence", "atlassian")
                if resolved:
                    query = f"search {user_text}" if resolved == "confluence" else f"search confluence: {user_text}"
                    tasks.append(_run_one(resolved, "Confluence results", query))
            elif tool_name == "files":
                tasks.append(_run_one("files", "Documents", "list"))
            elif tool_name == "12wy-reports":
                tasks.append(_run_one("12wy-reports", "12WY coaching", "coaching brief"))

        if not tasks:
            return ""

        results = await asyncio.gather(*tasks)
        return "\n\n".join(r for r in results if r)

    def _pick_backend(self, route: str) -> InferenceBackend:
        if route == "heavy" and self._heavy:
            return self._heavy
        if route == "light" and self._light:
            return self._light
        if route in ("light", "heavy") and (self._light or self._heavy):
            return self._light or self._heavy
        return self._local

    def _date_context(self) -> str:
        now = datetime.now(self._tz)
        return f"\n\nCurrent date and time: {now.strftime('%A, %B %d, %Y %I:%M %p %Z')}"

    def _build_system_prompt(self, backend: InferenceBackend, memory_context: str, plans_context: str) -> str:
        # Always include date — it's small and prevents wrong-date hallucinations
        date_ctx = self._date_context()
        if backend is self._local:
            return self._system_prompt + date_ctx
        # Static suffix (tools + plans instructions + extra) is pre-computed at init
        system = self._system_prompt + date_ctx + self._static_suffix
        # Only the dynamic parts are built per-request
        if memory_context:
            system += (
                "\n\nYou have the following memories about this user. "
                "Use them naturally — don't list them back unless asked.\n" + memory_context
            )
        if plans_context:
            system += "\n\n" + plans_context
        return system

    async def handle(self, user_text: str, user_id: str = "default", source: str = "") -> str:
        self._current_user_id = user_id
        engine_task = parse_engine_task(user_text)
        if engine_task is not None:
            return await self.run_sovereign_engine(engine_task, user_id=user_id, source=source)
        cursor_task = parse_cursor_task(user_text)
        if cursor_task is not None:
            return await self.run_cursor_delegate(cursor_task, user_id=user_id, source=source)
        with self._tracer.trace_turn(user_id, user_text) as trace:
            return await self._handle_traced(user_text, user_id, trace)

    async def handle_stream(
        self, user_text: str, user_id: str = "default", source: str = ""
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield (event, data) tuples for streaming UX.

        Events:
          ("status", "thinking")   — started processing
          ("status", "tools")      — running tool calls
          ("chunk", "partial")     — incremental LLM text
          ("done", "full reply")   — final post-processed reply
          ("error", "message")     — something went wrong
        """
        self._current_user_id = user_id
        engine_task = parse_engine_task(user_text)
        if engine_task is not None:
            yield ("status", "thinking")
            result = await self.run_sovereign_engine(engine_task, user_id=user_id, source=source)
            yield ("done", result)
            return

        cursor_task = parse_cursor_task(user_text)
        if cursor_task is not None:
            yield ("status", "thinking")
            result = await self.run_cursor_delegate(cursor_task, user_id=user_id, source=source)
            yield ("done", result)
            return

        yield ("status", "thinking")
        with self._tracer.trace_turn(user_id, user_text) as trace:
            try:
                async for event in self._handle_traced_stream(user_text, user_id, trace):
                    yield event
            except Exception:
                log.exception("Streaming handle failed")
                yield ("error", "Something went wrong — check the logs.")

    async def _handle_traced(self, user_text: str, user_id: str, trace) -> str:
        alignment = self._check_goal_alignment(user_text)

        # Alignment gating is enforced by the sovereign engine (/engine).
        # The chat loop logs alignment info but never blocks — it's a
        # conversational assistant, not an autonomous executor.

        # --- Phase 1: Parallel context gathering ---
        tool_hints = detect_tool_hints(user_text) if self._tools else []
        has_tool_hints = bool(tool_hints)

        if has_tool_hints and self._has_cloud:
            route = "light"
        elif self._has_cloud:
            route = route_fast(user_text, has_tool_hints=has_tool_hints)
        else:
            route = "local"

        backend = self._pick_backend(route)
        backend_name = type(backend).__name__
        log.info("Route → %s (%s)", route, backend_name)
        trace.record_route(route, backend_name)

        # Gather context in parallel
        tasks = {}
        tasks["memory"] = asyncio.ensure_future(self._build_memory_context(user_id, query=user_text))

        if self._plans:
            tasks["plans"] = asyncio.ensure_future(self._plans.get_active_plans(user_id))

        if tool_hints and self._tools:
            tasks["tools"] = asyncio.ensure_future(self._auto_run_tools(user_text))

        if self._memory:
            limit = 6 if backend is self._local else 20
            tasks["history"] = asyncio.ensure_future(self._memory.get_history(user_id, limit=limit))

        results = {}
        for key, task in tasks.items():
            try:
                results[key] = await task
            except Exception:
                log.exception("Context gather failed: %s", key)
                results[key] = [] if key in ("plans", "history") else ""

        memory_context = results.get("memory", "")
        active_plans = results.get("plans", [])
        plans_context = format_plans_for_context(active_plans) if active_plans else ""
        auto_context = results.get("tools", "")
        history = results.get("history", [])

        # Record auto-tool results
        if auto_context:
            trace.record_tool_auto("auto-tools", user_text, auto_context)

        # Upgrade to cloud if auto-tools returned data
        if auto_context and backend is self._local and self._has_cloud:
            backend = self._light or self._heavy
            log.info("Upgraded to cloud (tool data in context)")

        # --- Phase 2: Build messages and run inference ---
        system = self._build_system_prompt(backend, memory_context, plans_context)
        # Aligned tasks reach here; misaligned are already blocked above.
        # Only inject a load-status note if goals couldn't be loaded but
        # alignment still passed (e.g. aligner is None).
        load_status = alignment.get("load_status") if alignment else None
        if alignment and load_status and load_status != "ok":
            system += (
                f"\n\n12WY alignment note: Goals could not be loaded ({load_status}). "
                "Mention that objectives are misconfigured if relevant."
            )
        messages = [Message(role="system", content=system)]
        messages.extend(history)

        if self._memory:
            _safe_ensure_future(self._memory.append(user_id, "user", user_text), label="save-user-msg")

        if auto_context:
            messages.append(
                Message(
                    role="user",
                    content=f"{user_text}\n\n---\nHere is relevant data from your tools:\n{auto_context}",
                )
            )
        else:
            messages.append(Message(role="user", content=user_text))

        try:
            reply = await backend.complete(messages)
            model_name = getattr(backend, "_model", type(backend).__name__)
            trace.record_generation(
                model=model_name,
                input_messages=[{"role": m.role, "content": m.content[:500]} for m in messages[-3:]],
                output=reply,
            )

            # Tool + action execution loop — max 5 rounds; follow-up uses cloud when available
            follow_backend = backend if backend is not self._local else (self._light or self._heavy or backend)
            seen_keys: set[frozenset] = set()
            for _ in range(5):
                tool_results = await self._run_tool_calls(reply)
                action_results = await self._run_action_chains(reply)

                all_results: list[ToolResult] = []
                if tool_results:
                    all_results.extend(tool_results)
                if action_results:
                    all_results.extend(action_results)

                if not all_results:
                    break
                rkey = _results_key(all_results)
                if rkey in seen_keys:
                    log.warning("Duplicate tool call, breaking loop")
                    break
                seen_keys.add(rkey)

                for r in all_results:
                    trace.record_tool_call(
                        r.tool,
                        r.query,
                        r.result,
                        error_kind=r.error_kind,
                        success=r.success,
                        retried=r.retried,
                    )

                formatted = _format_tool_results(all_results)
                messages.append(Message(role="assistant", content=reply))
                messages.append(
                    Message(
                        role="user",
                        content=(
                            "Tool results:\n"
                            f"{formatted}\n\n"
                            "Respond using ONLY the tool results above. "
                            "Do not say you are fetching or about to check — the data is already here."
                        ),
                    )
                )
                reply = await follow_backend.complete(messages)
                trace.record_generation(
                    model=getattr(follow_backend, "_model", type(follow_backend).__name__),
                    input_messages=[{"role": "user", "content": "(tool follow-up round)"}],
                    output=reply,
                )

        except Exception:
            log.exception("Inference failed")
            return "Something went wrong — check the logs."

        reply = _strip_tool_markers(reply)

        # Evaluator — record issues before correction
        eval_issues = check_date_claims(reply, self._tz) + check_capability_claims(reply)
        if eval_issues:
            trace.record_evaluator(eval_issues)
        reply = evaluate_response(reply, self._tz)

        trace.record_reply(reply)

        # --- Phase 3: Post-processing (fire-and-forget) ---
        if self._memory:
            _safe_ensure_future(self._memory.append(user_id, "assistant", reply), label="save-reply")
        _safe_ensure_future(self._post_process(user_id, user_text, reply), label="post-process")

        return reply

    async def _post_process(self, user_id: str, user_text: str, reply: str) -> None:
        """Run memory extraction, plan processing, and conversation
        summarization without blocking the reply."""
        try:
            await asyncio.gather(
                self._extract_and_store(user_id, user_text, reply),
                self._process_plan_output(user_id, reply),
                self._maybe_summarize(user_id),
                return_exceptions=True,
            )
        except Exception:
            log.exception("Post-processing failed (non-fatal)")

    async def _maybe_summarize(self, user_id: str) -> None:
        """Summarize older conversation messages if enough have accumulated."""
        if not self._memory or not self._memory.needs_summary(user_id):
            return

        from palmtop.memory.conversation import SUMMARIZE_PROMPT

        messages, first_id, last_id = await self._memory.get_unsummarized_messages(user_id)
        if not messages:
            return

        # Build the conversation text for the summarizer
        conv_lines = []
        for m in messages:
            label = "User" if m.role == "user" else "Assistant"
            # Truncate very long messages (tool results, etc.)
            content = m.content[:500] if len(m.content) > 500 else m.content
            conv_lines.append(f"{label}: {content}")

        conv_text = "\n".join(conv_lines)
        # Cap total input to avoid burning too many tokens
        if len(conv_text) > 4000:
            conv_text = conv_text[:4000] + "\n... (truncated)"

        prompt = SUMMARIZE_PROMPT.format(conversation=conv_text)
        summarizer = self._pick_extractor()

        try:
            summary = await summarizer.complete(
                [Message(role="user", content=prompt)],
                max_tokens=300,
            )
            summary = summary.strip()
            if summary and summary.upper() != "NONE":
                await self._memory.store_summary(user_id, summary, first_id, last_id)
        except Exception:
            log.debug("Conversation summarization failed (non-fatal)", exc_info=True)

    async def _handle_traced_stream(self, user_text: str, user_id: str, trace) -> AsyncIterator[tuple[str, str]]:
        """Streaming variant of _handle_traced — yields (event, data) tuples."""
        alignment = self._check_goal_alignment(user_text)

        # --- Phase 1: context gathering (same as non-streaming) ---
        tool_hints = detect_tool_hints(user_text) if self._tools else []
        has_tool_hints = bool(tool_hints)

        if has_tool_hints and self._has_cloud:
            route = "light"
        elif self._has_cloud:
            route = route_fast(user_text, has_tool_hints=has_tool_hints)
        else:
            route = "local"

        backend = self._pick_backend(route)
        trace.record_route(route, type(backend).__name__)

        tasks = {}
        tasks["memory"] = asyncio.ensure_future(self._build_memory_context(user_id, query=user_text))
        if self._plans:
            tasks["plans"] = asyncio.ensure_future(self._plans.get_active_plans(user_id))
        if tool_hints and self._tools:
            yield ("status", "tools")
            tasks["tools"] = asyncio.ensure_future(self._auto_run_tools(user_text))
        if self._memory:
            limit = 6 if backend is self._local else 20
            tasks["history"] = asyncio.ensure_future(self._memory.get_history(user_id, limit=limit))

        results = {}
        for key, task in tasks.items():
            try:
                results[key] = await task
            except Exception:
                log.exception("Context gather failed: %s", key)
                results[key] = [] if key in ("plans", "history") else ""

        memory_context = results.get("memory", "")
        active_plans = results.get("plans", [])
        plans_context = format_plans_for_context(active_plans) if active_plans else ""
        auto_context = results.get("tools", "")
        history = results.get("history", [])

        if auto_context and backend is self._local and self._has_cloud:
            backend = self._light or self._heavy

        # --- Phase 2: build messages ---
        system = self._build_system_prompt(backend, memory_context, plans_context)
        load_status = alignment.get("load_status") if alignment else None
        if alignment and load_status and load_status != "ok":
            system += (
                f"\n\n12WY alignment note: Goals could not be loaded ({load_status}). "
                "Mention that objectives are misconfigured if relevant."
            )
        messages = [Message(role="system", content=system)]
        messages.extend(history)

        if self._memory:
            _safe_ensure_future(self._memory.append(user_id, "user", user_text), label="save-user-msg")

        if auto_context:
            messages.append(
                Message(
                    role="user",
                    content=f"{user_text}\n\n---\nHere is relevant data from your tools:\n{auto_context}",
                )
            )
        else:
            messages.append(Message(role="user", content=user_text))

        # --- Phase 3: streaming inference ---
        has_stream = hasattr(backend, "stream_complete")
        accumulated = ""

        try:
            if has_stream:
                async for chunk in backend.stream_complete(messages):
                    accumulated += chunk
                    yield ("chunk", accumulated)
                reply = accumulated
            else:
                reply = await backend.complete(messages)
                yield ("chunk", reply)

            trace.record_generation(
                model=getattr(backend, "_model", type(backend).__name__),
                input_messages=[{"role": m.role, "content": m.content[:500]} for m in messages[-3:]],
                output=reply,
            )

            # --- Tool call follow-up (non-streaming, sends status) ---
            follow_backend = backend if backend is not self._local else (self._light or self._heavy or backend)
            seen_keys: set[frozenset] = set()
            for _ in range(5):
                tool_results = await self._run_tool_calls(reply)
                action_results = await self._run_action_chains(reply)
                all_results: list[ToolResult] = []
                if tool_results:
                    all_results.extend(tool_results)
                if action_results:
                    all_results.extend(action_results)
                if not all_results:
                    break
                rkey = _results_key(all_results)
                if rkey in seen_keys:
                    break
                seen_keys.add(rkey)

                yield ("status", "tools")

                for r in all_results:
                    trace.record_tool_call(
                        r.tool,
                        r.query,
                        r.result,
                        error_kind=r.error_kind,
                        success=r.success,
                        retried=r.retried,
                    )

                formatted = _format_tool_results(all_results)
                messages.append(Message(role="assistant", content=reply))
                messages.append(
                    Message(
                        role="user",
                        content=(
                            "Tool results:\n"
                            f"{formatted}\n\n"
                            "Respond using ONLY the tool results above. "
                            "Do not say you are fetching or about to check — the data is already here."
                        ),
                    )
                )

                # Stream follow-up response too if possible
                has_follow_stream = hasattr(follow_backend, "stream_complete")
                if has_follow_stream:
                    accumulated = ""
                    async for chunk in follow_backend.stream_complete(messages):
                        accumulated += chunk
                        yield ("chunk", accumulated)
                    reply = accumulated
                else:
                    reply = await follow_backend.complete(messages)
                    yield ("chunk", reply)

                trace.record_generation(
                    model=getattr(follow_backend, "_model", type(follow_backend).__name__),
                    input_messages=[{"role": "user", "content": "(tool follow-up round)"}],
                    output=reply,
                )

        except Exception:
            log.exception("Streaming inference failed")
            yield ("error", "Something went wrong — check the logs.")
            return

        reply = _strip_tool_markers(reply)

        eval_issues = check_date_claims(reply, self._tz) + check_capability_claims(reply)
        if eval_issues:
            trace.record_evaluator(eval_issues)
        reply = evaluate_response(reply, self._tz)

        trace.record_reply(reply)

        if self._memory:
            _safe_ensure_future(self._memory.append(user_id, "assistant", reply), label="save-reply")
        _safe_ensure_future(self._post_process(user_id, user_text, reply), label="post-process")

        yield ("done", reply)


def _format_tool_results(results: list[ToolResult]) -> str:
    """Format structured tool results for injection into the LLM message stream.

    Includes error guidance so the LLM knows what to tell the user, and
    retry context so it can explain what happened naturally.
    """
    parts = []
    for r in results:
        if r.success:
            if r.retried:
                parts.append(
                    f"[{r.tool}] (First attempt failed: {r.original_error}. Retried successfully.)\n{r.result}"
                )
            else:
                parts.append(f"[{r.tool}] {r.result}")
        else:
            kind = f" ({r.error_kind})" if r.error_kind else ""
            guidance = ERROR_GUIDANCE.get(r.error_kind, "")
            hint = f"\nContext for your response: {guidance}" if guidance else ""
            parts.append(f"[{r.tool}] Error{kind}: {r.result}{hint}")
    return "\n\n".join(parts)


def _results_key(results: list[ToolResult]) -> frozenset[tuple[str, str]]:
    """Hashable key for dedup — based on tool + first 200 chars of result."""
    return frozenset((r.tool, r.result[:200]) for r in results)


def _strip_tool_markers(text: str) -> str:
    text = re.sub(r"\[TOOL:[\w-]+\][^\n]*\n?", "", text)
    text = re.sub(r"\[/TOOL\]\n?", "", text)
    # Strip ACTION chain markers
    text = re.sub(r"\[ACTION:[\w-]+\][^\n]*\n?", "", text)
    text = re.sub(r"\[ON_FAIL:[\w-]+\][^\n]*\n?", "", text)
    # Strip internal metadata the user shouldn't see
    text = re.sub(r"\[event_ids:[^\]]*\]", "", text)
    text = re.sub(r"\[id:\w+\]", "", text)
    return text.strip()
