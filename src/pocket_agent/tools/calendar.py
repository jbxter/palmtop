from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from pocket_agent.tools.base import Tool

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleCalendarTool(Tool):
    name = "calendar"
    description = (
        "Manage your Google Calendar. Usage:\n"
        "  [TOOL:calendar] add 2025-06-15 14:00 Meeting with Sarah @ Coffee Lab\n"
        "  [TOOL:calendar] add 2025-06-15 14:00-15:30 Meeting with Sarah\n"
        "  [TOOL:calendar] show week\n"
        "  [TOOL:calendar] show today\n"
        "  [TOOL:calendar] show 2025-06-15\n"
        "  [TOOL:calendar] remove <event_id>"
    )

    def __init__(self, data_dir: Path, timezone: str = "America/Los_Angeles") -> None:
        self._data_dir = data_dir
        self._timezone = timezone
        self._creds_path = data_dir / "google_credentials.json"
        self._tokens_path = data_dir / "google_tokens.json"
        self._client: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._client_id: str = ""
        self._client_secret: str = ""

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15.0)
        return self._client

    async def init(self) -> None:
        if not self._creds_path.exists():
            log.warning("Google credentials not found at %s — calendar disabled", self._creds_path)
            return
        if not self._tokens_path.exists():
            log.warning("Google tokens not found — run: python -m pocket_agent.tools.google_auth")
            return

        with open(self._creds_path) as f:
            creds = json.load(f)
        client_info = creds.get("installed") or creds.get("web")
        self._client_id = client_info["client_id"]
        self._client_secret = client_info["client_secret"]
        log.info("Google Calendar credentials loaded (token refresh on first use)")

    async def _refresh_token(self) -> None:
        if not self._tokens_path.exists():
            return
        with open(self._tokens_path) as f:
            tokens = json.load(f)

        client = self._get_client()
        resp = await client.post(TOKEN_URL, data={
            "refresh_token": tokens["refresh_token"],
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
        })
        if resp.status_code != 200:
            log.error("Token refresh failed: %s", resp.text[:200])
            self._access_token = None
            return

        data = resp.json()
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            tokens["refresh_token"] = data["refresh_token"]
            with open(self._tokens_path, "w") as f:
                json.dump(tokens, f, indent=2)

    async def _api(self, method: str, path: str, **kwargs) -> httpx.Response:
        if not self._access_token:
            await self._refresh_token()
        if not self._access_token:
            raise RuntimeError("Not authenticated — run: python -m pocket_agent.tools.google_auth")

        client = self._get_client()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        url = f"{API_BASE}{path}"
        resp = await client.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401:
            await self._refresh_token()
            if self._access_token:
                headers = {"Authorization": f"Bearer {self._access_token}"}
                resp = await client.request(method, url, headers=headers, **kwargs)
        return resp

    async def run(self, query: str) -> str:
        if not self._access_token and not self._tokens_path.exists():
            return "Calendar not connected. Run: python -m pocket_agent.tools.google_auth"

        parts = query.strip().split(None, 1)
        if not parts:
            return "Usage: add <date> <time> <title> | show <date|week|today> | remove <id>"

        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        try:
            if action == "add":
                return await self._add(rest)
            elif action == "show":
                return await self._show(rest)
            elif action in ("remove", "delete", "cancel"):
                return await self._remove(rest)
            else:
                return f"Unknown calendar action: {action}"
        except Exception as e:
            log.exception("Calendar operation failed")
            return f"Calendar error: {e}"

    async def _add(self, text: str) -> str:
        parts = text.strip().split(None, 2)
        if len(parts) < 2:
            return "Need at least a date and title. Format: add 2025-06-15 14:00 Title"

        date_str = parts[0]
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return f"Invalid date format: {date_str}. Use YYYY-MM-DD."

        time_str = None
        end_time_str = None
        title_start = 1

        if len(parts) >= 3:
            time_part = parts[1]
            if "-" in time_part and ":" in time_part:
                start_t, end_t = time_part.split("-", 1)
                time_str = start_t
                end_time_str = end_t
                title_start = 2
            elif ":" in time_part and len(time_part) <= 5:
                time_str = time_part
                title_start = 2

        title = " ".join(parts[title_start:]) if title_start < len(parts) else parts[-1]

        if time_str:
            start_dt = f"{date_str}T{time_str}:00"
            if end_time_str:
                end_dt = f"{date_str}T{end_time_str}:00"
            else:
                st = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                end_dt = (st + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
            event = {
                "summary": title,
                "start": {"dateTime": start_dt, "timeZone": self._timezone},
                "end": {"dateTime": end_dt, "timeZone": self._timezone},
            }
        else:
            event = {
                "summary": title,
                "start": {"date": date_str},
                "end": {"date": date_str},
            }

        resp = await self._api("POST", "/calendars/primary/events", json=event)
        if resp.status_code not in (200, 201):
            return f"Failed to create event: {resp.text[:200]}"

        data = resp.json()
        friendly = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %b %d")
        return f"✅ Added: {title} — {friendly}" + (f" at {time_str}" if time_str else "")

    def _now(self) -> datetime:
        """Timezone-aware 'now' using configured timezone."""
        return datetime.now(ZoneInfo(self._timezone))

    async def _show(self, text: str) -> str:
        text = text.strip().lower()
        tz = ZoneInfo(self._timezone)
        now = self._now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)

        if text in ("today", ""):
            start = today
            end = today + timedelta(days=1)
            label = "Today"
        elif text == "week":
            start = today
            end = today + timedelta(days=7)
            label = "This week"
        elif text == "month":
            start = today
            end = today + timedelta(days=30)
            label = "Next 30 days"
        elif text == "tomorrow":
            start = today + timedelta(days=1)
            end = today + timedelta(days=2)
            label = "Tomorrow"
        else:
            try:
                start = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=tz)
                end = start + timedelta(days=1)
                label = text
            except ValueError:
                return f"Can't parse date: {text}. Use YYYY-MM-DD, today, week, or month."

        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": self._timezone,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "25",
        }

        resp = await self._api("GET", "/calendars/primary/events", params=params)
        if resp.status_code != 200:
            return f"Failed to fetch events: {resp.text[:200]}"

        items = resp.json().get("items", [])
        if not items:
            return f"No events for {label}."

        # Group events by date for clean display
        days: dict[str, list[tuple[str, str, str]]] = {}  # date_key → [(time, summary, id)]
        for ev in items:
            summary = ev.get("summary", "(no title)")
            eid = ev.get("id", "")
            start_info = ev.get("start", {})
            if "dateTime" in start_info:
                dt_str = start_info["dateTime"]
                date_key = dt_str[:10]
                time_display = dt_str[11:16] if len(dt_str) > 16 else ""
                # Convert 24h to 12h
                try:
                    t = datetime.strptime(time_display, "%H:%M")
                    time_display = t.strftime("%I:%M %p").lstrip("0")
                except ValueError:
                    pass
            else:
                date_key = start_info.get("date", "")
                time_display = "All day"

            if date_key not in days:
                days[date_key] = []
            days[date_key].append((time_display, summary, eid))

        lines = [f"📅 {label}"]
        for date_key in sorted(days.keys()):
            # Friendly date header
            try:
                dt = datetime.strptime(date_key, "%Y-%m-%d")
                friendly = dt.strftime("%A, %b %d")
            except ValueError:
                friendly = date_key
            lines.append(f"\n{friendly}")
            lines.append("─" * 28)
            for time_display, summary, _ in days[date_key]:
                lines.append(f"  {time_display:<10} {summary}")

        # Store event IDs in a hidden section for tool use (model sees it, user doesn't care)
        id_map = []
        for date_key in sorted(days.keys()):
            for _, summary, eid in days[date_key]:
                if eid:
                    id_map.append(f"{summary[:30]}={eid}")
        if id_map:
            lines.append(f"\n[event_ids: {'; '.join(id_map)}]")

        return "\n".join(lines)

    async def list_events_structured(
        self, start_iso: str, end_iso: str
    ) -> list[dict]:
        """Return raw event data for monitoring/diffing.

        Each dict: {id, summary, start, end, status}.
        Returns [] on error or if unauthenticated.
        """
        if not self._access_token and not self._tokens_path.exists():
            return []

        params = {
            "timeMin": start_iso,
            "timeMax": end_iso,
            "timeZone": self._timezone,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "50",
        }
        try:
            resp = await self._api("GET", "/calendars/primary/events", params=params)
        except Exception:
            log.debug("Calendar structured list failed", exc_info=True)
            return []

        if resp.status_code != 200:
            return []

        results = []
        for ev in resp.json().get("items", []):
            start_info = ev.get("start", {})
            end_info = ev.get("end", {})
            results.append({
                "id": ev.get("id", ""),
                "summary": ev.get("summary", "(no title)"),
                "start": start_info.get("dateTime", start_info.get("date", "")),
                "end": end_info.get("dateTime", end_info.get("date", "")),
                "status": ev.get("status", "confirmed"),
            })
        return results

    async def _remove(self, text: str) -> str:
        # Extract just the ID — model often appends conversational text after it
        event_id = text.strip().split()[0].strip() if text.strip() else ""
        # Remove any non-URL-safe characters
        event_id = re.sub(r"[^a-zA-Z0-9_\-]", "", event_id)
        if not event_id:
            return "Need an event ID. Use 'show' to see events with their IDs."

        resp = await self._api("DELETE", f"/calendars/primary/events/{event_id}")
        if resp.status_code in (200, 204):
            return f"Event removed."
        elif resp.status_code == 404:
            return "Event not found. Check the ID."
        else:
            return f"Failed to remove event: {resp.text[:200]}"

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
