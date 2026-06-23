"""
Unified JSONL logging hook.
Writes structured logs to per-session event files.

Configuration keys
------------------
priority : int, default 100
    Hook execution priority.
session_log_template : str
    Path template for per-session event files.  ``{project}`` and
    ``{session_id}`` are interpolated at write time.
    Default: ``~/.amplifier/projects/{project}/sessions/{session_id}/events.jsonl``
auto_discover : bool, default True
    When True, register handlers for module-contributed events discovered
    via ``coordinator.get_capability("observability.events")`` and
    ``coordinator.collect_contributions("observability.events")``.
strip_raw : bool, default False
    When True, strip the ``raw`` field from event data before writing.
additional_events : list[str], default []
    Extra event names to register handlers for (beyond those in ALL_EVENTS
    and auto-discovered events).
exclude_events : list[str], default ["llm:stream_*delta"]
    List of event-name patterns (``fnmatch`` semantics) that are silently
    dropped from ``events.jsonl``.  The handler returns ``continue``
    immediately without building or writing a record.

    Patterns follow ``fnmatch.fnmatch`` rules — ``*`` matches any sequence
    of characters, ``?`` matches exactly one character, ``[seq]`` matches
    any character in *seq*.  Matching is case-sensitive.

    Default ``["llm:stream_*delta"]`` expresses the *transient streaming
    delta category* — events whose names end in ``delta`` — as defined by
    the "Event dispositions" convention in the provider streaming contract.
    The glob matches ``llm:stream_block_delta`` (and any future ``*_delta``
    variants) while intentionally sparing the structural events
    ``llm:stream_block_start``, ``llm:stream_block_end``, and
    ``llm:stream_aborted``, which carry audit value and are kept.

    This default is intentionally identical to the context-intelligence
    hook's default.  Alignment is maintained via the provider streaming
    contract — NOT by shared code.  Never extract a shared constant or
    helper module; keep the hooks in sync through the contract instead.

    To disable the filter entirely set ``exclude_events: []`` in config.

    Examples::

        # Default — drop transient streaming deltas (convention pattern)
        exclude_events: ["llm:stream_*delta"]

        # Drop all four streaming-related events
        exclude_events: ["llm:stream_*"]

        # Disable the filter — write every event (useful for debugging)
        exclude_events: []
"""

# Amplifier module metadata
__amplifier_module_type__ = "hook"

import fnmatch
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

# Per-coordinator setup key — avoids module-level shared state across sessions.
# State is stashed on the coordinator (which is per-session) so concurrent
# sessions cannot drain each other's config.
_SETUP_KEY = "hooks_logging._setup"

# Default patterns suppressed from events.jsonl.
#
# Pattern: "llm:stream_*delta" — the *transient streaming delta* category from the
# provider streaming contract's "Event dispositions" convention.  The glob matches
# any event whose name ends in "delta" under the "llm:stream_" namespace (e.g.
# llm:stream_block_delta, and any future *_delta variants), while intentionally
# sparing the structural events block_start / block_end / stream_aborted whose
# bookkeeping value justifies persisting them to events.jsonl.
#
# This default is intentionally IDENTICAL to the context-intelligence hook's
# default.  Alignment is maintained via the provider streaming contract, NOT by
# shared code — keep them in sync through the contract; never extract a shared
# constant or helper module.
_DEFAULT_EXCLUDE_EVENTS: list[str] = ["llm:stream_*delta"]


def _ts() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _get_project_slug(working_dir: Path | None = None) -> str:
    """Generate project slug from working directory.

    Args:
        working_dir: Working directory to use. Falls back to cwd if not provided.
    """
    cwd = (working_dir or Path.cwd()).resolve()
    slug = str(cwd).replace("/", "-").replace("\\", "-").replace(":", "")
    if not slug.startswith("-"):
        slug = "-" + slug
    return slug


def _sanitize_for_json(value: Any) -> Any:
    """Recursively sanitize a value to ensure JSON serializability.

    Optimized to avoid expensive introspection on already-safe types.
    Performance: O(n) instead of O(n×m) where m = attributes per object.
    """
    # Fast path for primitives
    if value is None or isinstance(value, bool | int | float | str):
        return value

    # Fast path for collections - test if already JSON-safe
    if isinstance(value, (dict, list, tuple)):
        try:
            json.dumps(value)  # Quick serializability test
            return value  # Already safe, skip expensive recursion
        except (TypeError, ValueError):
            # Contains non-JSON objects, need recursive sanitization
            if isinstance(value, dict):
                return {k: _sanitize_for_json(v) for k, v in value.items()}
            return [_sanitize_for_json(item) for item in value]

    # Pydantic models: use model_dump(mode='json') so Pydantic converts
    # non-JSON-native types (Decimal, datetime, UUID, etc.) to JSON-safe
    # primitives before we return. Plain model_dump() preserves Python types
    # like Decimal, which then survive into the sanitized record and crash
    # json.dumps() at write time.
    if hasattr(value, "model_dump"):
        try:
            return value.model_dump(mode="json")
        except Exception:
            pass  # Fall through to __dict__ approach

    # Objects: use __dict__ directly instead of dir() + getattr() loop
    if hasattr(value, "__dict__"):
        try:
            return _sanitize_for_json(value.__dict__)
        except Exception:
            return str(value)

    # Try str() as last resort
    try:
        return str(value)
    except Exception:
        return "<unserializable>"


async def _setup_and_register(
    coordinator: ModuleCoordinator,
    config: dict[str, Any],
    *,
    use_collect: bool,
) -> None:
    """Build session logger and handler, discover events, register hook.

    Args:
        coordinator: The session coordinator.
        config: Resolved hook configuration dict.
        use_collect: When True, also calls coordinator.collect_contributions()
            to pick up events contributed via the contribution channel
            (amplifier-core >= 1.4.1). When False, uses only the legacy
            get_capability() path (safe for older kernels).
    """
    priority = int(config.get("priority", 100))
    session_log_template = config.get(
        "session_log_template",
        "~/.amplifier/projects/{project}/sessions/{session_id}/events.jsonl",
    )

    # Auto-discovery: enabled by default
    auto_discover = config.get("auto_discover", True)
    strip_raw = config.get("strip_raw", False)

    # Event-exclusion filter: fnmatch patterns whose matching events are
    # silently dropped from events.jsonl.  Default is the "llm:stream_*delta"
    # convention glob from the provider streaming contract — aligns with the
    # context-intelligence hook's default via contract, not shared code.
    exclude_events: list[str] = config.get("exclude_events", _DEFAULT_EXCLUDE_EVENTS)

    # Get working directory from capability (falls back to cwd for backward compatibility)
    working_dir = coordinator.get_capability("session.working_dir")
    working_dir_path = Path(working_dir) if working_dir else None

    # Session log writer
    class _SessionLogger:
        def __init__(self, template: str, working_dir: Path | None = None):
            self.template = template
            self.working_dir = working_dir

        def write(self, rec: dict[str, Any]):
            session_id = rec.get("session_id")
            if not session_id:
                return  # No session context, skip

            try:
                project_slug = _get_project_slug(self.working_dir)
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

    session_logger = _SessionLogger(session_log_template, working_dir=working_dir_path)

    async def handler(event: str, data: dict[str, Any]) -> HookResult:
        # --- Event-exclusion filter (single choke point before any processing) ---
        # If the event name matches any pattern in exclude_events, skip it entirely.
        # No record is built and no write occurs — just return continue.
        # This is the sole write choke point; session_logger.write() is downstream.
        if exclude_events and any(fnmatch.fnmatch(event, p) for p in exclude_events):
            return HookResult(action="continue")

        # Prefer the kernel-stamped emit-time timestamp from data["timestamp"]
        # (amplifier-core/.../hooks.rs:196-203 stamps every emit before handlers run).
        # Fall back to _ts() only when absent — defense in depth for legacy emit paths
        # that bypass the kernel registry. The kernel's timestamp reflects actual emit
        # time; _ts() reflects handler-execution time, which can skew by hundreds of
        # milliseconds for events with slow upstream handlers in the chain.
        rec = {
            "ts": data.get("timestamp") or _ts(),
            "lvl": data.get("lvl", "INFO"),  # Use provided level or default to INFO
            "schema": SCHEMA,
            "event": event,
        }
        # Strip "timestamp" before building the payload so it is not duplicated:
        # it is already promoted to rec["ts"] above.
        data_remaining = {k: v for k, v in (data or {}).items() if k != "timestamp"}
        # Merge data (ensure serializable)
        payload = {}
        try:
            for k, v in data_remaining.items():
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
            event_data = {k: v for k, v in data_remaining.items() if k not in payload}
            if strip_raw:
                event_data.pop("raw", None)
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

    # Auto-discover module events via capability (existing — backward compat)
    if auto_discover:
        discovered = coordinator.get_capability("observability.events") or []
        if discovered:
            events.extend(discovered)
            logger.info(
                f"Auto-discovered {len(discovered)} module events: {discovered}"
            )

    # Auto-discover module events via collect_contributions (new — additive, same guard)
    # Only called when use_collect=True (amplifier-core >= 1.4.1 path).
    if auto_discover and use_collect:
        contributions = await coordinator.collect_contributions("observability.events")
        for contrib in contributions:
            if isinstance(contrib, list):
                events.extend(contrib)
            elif isinstance(contrib, str):
                events.append(contrib)

    # Add additional events from config
    additional = config.get("additional_events", [])
    if additional:
        events.extend(additional)
        logger.info(f"Added {len(additional)} configured events: {additional}")

    # Deduplicate while preserving order — contributors may list events that are
    # already in ALL_EVENTS (e.g. provider-gemini contributes llm:request /
    # llm:response; loop-streaming contributes execution:start / execution:end).
    # The Rust HookRegistry.register() has no dedup-by-name guard, so without
    # this step every such duplicate causes double-firing: one emit → two log
    # lines.  dict.fromkeys() preserves insertion order (Python 3.7+).
    events = list(dict.fromkeys(events))

    # Register handlers for all events
    for ev in events:
        coordinator.hooks.register(ev, handler, priority=priority, name="hooks-logging")

    logger.info("Mounted hooks-logging (JSONL)")


async def mount(coordinator: ModuleCoordinator, config: dict[str, Any] | None = None):
    config = config or {}
    if not hasattr(coordinator, "collect_contributions"):
        # amplifier-core < 1.4.1: on_session_ready is never dispatched, so
        # register handlers eagerly at mount() time using the legacy path.
        logger.warning(
            "hooks-logging: amplifier-core < 1.4.1 detected — "
            "late-contributed events may be missed. Upgrade for full coverage."
        )
        await _setup_and_register(coordinator, config, use_collect=False)
        return
    # Store config on the coordinator so on_session_ready() can retrieve it.
    # Using the coordinator (which is per-session) scopes state correctly —
    # concurrent sessions cannot drain each other's config.
    coordinator.register_capability(_SETUP_KEY, config)


async def on_session_ready(coordinator: ModuleCoordinator) -> None:
    config = coordinator.get_capability(_SETUP_KEY)
    if config is None:
        return
    await _setup_and_register(coordinator, config, use_collect=True)
