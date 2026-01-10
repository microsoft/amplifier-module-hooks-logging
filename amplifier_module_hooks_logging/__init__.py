"""
Unified JSONL logging hook.
Writes structured logs to per-session event files.
"""

# Amplifier module metadata
__amplifier_module_type__ = "hook"

import json
import logging
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from amplifier_core import HookResult
from amplifier_core import ModuleCoordinator
from amplifier_core.events import ALL_EVENTS

logger = logging.getLogger(__name__)

SCHEMA = {"name": "amplifier.log", "ver": "1.0.0"}


def _ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _get_project_slug() -> str:
    """Generate project slug from CWD."""
    cwd = Path.cwd().resolve()
    slug = str(cwd).replace("/", "-").replace("\\", "-").replace(":", "")
    if not slug.startswith("-"):
        slug = "-" + slug
    return slug


def _sanitize_for_json(value: Any) -> Any:
    """Recursively sanitize a value to ensure JSON serializability."""
    if value is None or isinstance(value, bool | int | float | str):
        return value

    if isinstance(value, dict):
        return {k: _sanitize_for_json(v) for k, v in value.items()}

    if isinstance(value, list | tuple):
        return [_sanitize_for_json(item) for item in value]

    # Handle objects with __dict__ (like ThinkingBlock, TextBlock)
    if hasattr(value, "__dict__"):
        try:
            # Try to extract meaningful data from object
            obj_dict = {}
            for attr_name in dir(value):
                if not attr_name.startswith("_"):
                    try:
                        attr_value = getattr(value, attr_name)
                        if not callable(attr_value):
                            obj_dict[attr_name] = _sanitize_for_json(attr_value)
                    except Exception:
                        continue
            return obj_dict if obj_dict else str(value)
        except Exception:
            return str(value)

    # Try str() as last resort
    try:
        return str(value)
    except Exception:
        return "<unserializable>"


async def mount(coordinator: ModuleCoordinator, config: dict[str, Any] | None = None):
    config = config or {}
    priority = int(config.get("priority", 100))
    session_log_template = config.get(
        "session_log_template",
        "~/.amplifier/projects/{project}/sessions/{session_id}/events.jsonl",
    )

    # Auto-discovery: enabled by default
    auto_discover = config.get("auto_discover", True)

    # Session log writer
    class _SessionLogger:
        def __init__(self, template: str):
            self.template = template

        def write(self, rec: dict[str, Any]):
            session_id = rec.get("session_id")
            if not session_id:
                return  # No session context, skip

            try:
                project_slug = _get_project_slug()
                log_path = Path(
                    self.template.format(project=project_slug, session_id=session_id)
                ).expanduser()

                log_path.parent.mkdir(parents=True, exist_ok=True)

                # Sanitize record to ensure JSON serializability
                sanitized_rec = _sanitize_for_json(rec)

                with log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(sanitized_rec, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.error(f"Failed to write session log: {e}")

    session_logger = _SessionLogger(session_log_template)

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        rec = {
            "ts": _ts(),
            "lvl": data.get("lvl", "INFO"),  # Use provided level or default to INFO
            "schema": SCHEMA,
            "event": event,
        }
        # Merge data (ensure serializable)
        payload = {}
        try:
            for k, v in (data or {}).items():
                if k in (
                    "redaction",
                    "status",
                    "duration_ms",
                    "module",
                    "component",
                    "error",
                    "request_id",
                    "span_id",
                    "parent_span_id",
                    "session_id",
                ):
                    payload[k] = v
            # Store all event-specific data under "data" field for JSONL output
            event_data = {k: v for k, v in (data or {}).items() if k not in payload}
            if event_data:
                payload["data"] = event_data
            rec.update(payload)
            # Upgrade level based on payload (but don't downgrade from DEBUG)
            if rec["lvl"] != "DEBUG" and (
                (payload.get("status") == "error")
                or payload.get("error")
                or ("error" in event)
            ):
                rec["lvl"] = "ERROR"
        except Exception as e:
            rec["error"] = {"type": type(e).__name__, "msg": str(e)}

        # Write to per-session log
        try:
            session_logger.write(rec)
        except Exception as e:
            logger.error(f"Failed to log event {event}: {e}")

        return HookResult(action="continue")

    # Use canonical events from core as the base (single source of truth)
    # This ensures hooks-logging automatically picks up new events added to core
    events = list(ALL_EVENTS)

    # Auto-discover module events via capability
    if auto_discover:
        discovered = coordinator.get_capability("observability.events") or []
        if discovered:
            events.extend(discovered)
            logger.info(
                f"Auto-discovered {len(discovered)} module events: {discovered}"
            )

    # Add additional events from config
    additional = config.get("additional_events", [])
    if additional:
        events.extend(additional)
        logger.info(f"Added {len(additional)} configured events: {additional}")

    # Register handlers for all events
    for ev in events:
        coordinator.hooks.register(ev, handler, priority=priority, name="hooks-logging")

    logger.info("Mounted hooks-logging (JSONL)")
    return
