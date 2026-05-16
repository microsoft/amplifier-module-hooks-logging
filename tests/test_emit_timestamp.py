"""Tests for kernel emit-time timestamp usage in hooks-logging.

The Rust kernel (amplifier-core/crates/amplifier-core/src/hooks.rs:196-203) stamps
every emit with data["timestamp"] at the moment of dispatch — before any handler
runs.  hooks-logging must use that value for rec["ts"] instead of capturing a fresh
datetime.now() at handler-execution time, which can lag by hundreds of milliseconds
for events with slow upstream handlers.

Contract being tested:
  1. When data["timestamp"] is present (always in production), rec["ts"] must equal
     it exactly — not a locally-captured datetime.now().
  2. When data["timestamp"] is absent (legacy / test emit paths that bypass the Rust
     kernel), rec["ts"] falls back to a current timestamp (defense in depth).
  3. data["timestamp"] is NOT duplicated inside rec["data"]; it is promoted to the
     top-level rec["ts"] and stripped from the event-specific payload.

Test infrastructure note
------------------------
MockCoordinator wraps the real Rust ModuleCoordinator.  The Rust kernel overwrites
any "timestamp" value in the data dict with its own nanosecond-precision timestamp
at emit time (before handlers run).  Tests #1 and #3 therefore use a spy handler
to capture the actual kernel-injected value and assert equality with rec["ts"].
Test #2 uses a thin Python mock that does NOT inject timestamps, allowing the
fallback (_ts()) path to be exercised in isolation.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
import pytest
from amplifier_core import HookResult
from amplifier_core.testing import MockCoordinator

from amplifier_module_hooks_logging import _setup_and_register, mount, on_session_ready


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator():
    return MockCoordinator()


async def _mount_with_tempdir(coordinator, tmp_path, *, auto_discover: bool = False):
    """Helper: mount the hook with a temp session log dir."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {
        "session_log_template": template,
        "auto_discover": auto_discover,
    }
    await mount(coordinator, config)
    await on_session_ready(coordinator)


async def _get_logged_record(tmp_path) -> dict:
    """Read the last JSONL record written to the temp session log."""
    log_file = tmp_path / "sessions" / "test-session" / "events.jsonl"
    assert log_file.exists(), f"Expected log file at {log_file}"
    lines = log_file.read_text().splitlines()
    assert lines, "Expected at least one log record"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Minimal Python mock coordinator — does NOT inject data["timestamp"].
# Used to exercise the fallback (_ts()) code path in isolation from the
# Rust kernel.
# ---------------------------------------------------------------------------


class _PythonHooks:
    """Minimal hook registry that dispatches to registered handlers as-is."""

    def __init__(self):
        self._handlers: list = []

    def register(self, event: str, handler, *, priority: int = 100, name: str = ""):
        self._handlers.append(handler)

    async def emit(self, event: str, data: dict) -> HookResult:
        # No kernel timestamp injection — data is passed through unchanged.
        for handler in self._handlers:
            await handler(event, data)
        return HookResult(action="continue")


class _MinimalCoordinator:
    """Minimal coordinator that satisfies _setup_and_register's interface."""

    def __init__(self):
        self.hooks = _PythonHooks()

    def get_capability(self, key: str) -> Any:
        return None

    def register_capability(self, key: str, value: Any) -> None:
        pass

    async def collect_contributions(self, key: str) -> list:
        return []


# ---------------------------------------------------------------------------
# Test 1: kernel timestamp is used when present
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uses_kernel_timestamp_when_present(coordinator, tmp_path):
    """rec["ts"] must equal the kernel-injected data["timestamp"], not _ts().

    The Rust kernel stamps data["timestamp"] at emit time, before any handler
    runs.  hooks-logging must honour that value.  If it calls _ts() instead,
    rec["ts"] will have millisecond precision (Python format: "...HH:MM:SS.MMM+00:00")
    and will not equal the kernel's nanosecond value ("...HH:MM:SS.NNNNNNNNN+00:00").

    Strategy: register a spy at priority=50 (runs before hooks-logging at 100)
    to capture the actual value of data["timestamp"] injected by the kernel,
    then assert that rec["ts"] in the written JSONL record equals it exactly.
    """
    await _mount_with_tempdir(coordinator, tmp_path)

    # Spy captures data["timestamp"] as the Rust kernel injected it.
    kernel_ts_seen: dict = {}

    async def spy(event: str, data: dict) -> HookResult:
        kernel_ts_seen["value"] = data.get("timestamp")
        return HookResult(action="continue")

    coordinator.hooks.register("session:start", spy, priority=50, name="test-spy")

    await coordinator.hooks.emit(
        "session:start",
        {"session_id": "test-session"},
    )

    assert kernel_ts_seen.get("value"), (
        "Rust kernel must have injected data['timestamp'] — spy saw nothing"
    )

    rec = await _get_logged_record(tmp_path)
    assert rec["ts"] == kernel_ts_seen["value"], (
        f"rec['ts'] must equal the kernel-injected data['timestamp'] "
        f"({kernel_ts_seen['value']!r}), but got {rec['ts']!r}.  "
        "The handler is still calling _ts() instead of reading data['timestamp']."
    )


# ---------------------------------------------------------------------------
# Test 2: fallback to _ts() when data["timestamp"] is absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_falls_back_to_handler_ts_when_timestamp_absent(tmp_path):
    """rec["ts"] falls back to a current timestamp when data["timestamp"] is absent.

    This exercises the defense-in-depth fallback: ``data.get("timestamp") or _ts()``.
    The Rust kernel always injects a timestamp, so the fallback only fires on legacy
    emit paths that bypass the kernel.  We use a minimal Python mock coordinator
    to simulate that scenario — its emit() passes data through without injection.
    """
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {
        "session_log_template": template,
        "auto_discover": False,
    }

    coord = _MinimalCoordinator()
    await _setup_and_register(coord, config, use_collect=False)

    before = datetime.now(UTC)
    await coord.hooks.emit(
        "session:start",
        {
            "session_id": "test-session",
            # no "timestamp" key — simulates a legacy emit path
        },
    )
    after = datetime.now(UTC)

    rec = await _get_logged_record(tmp_path)
    ts_str = rec["ts"]
    assert ts_str, "rec['ts'] must not be empty when data['timestamp'] is absent"

    try:
        ts = datetime.fromisoformat(ts_str)
    except ValueError:
        pytest.fail(f"rec['ts'] {ts_str!r} is not a valid ISO-8601 string")

    # Allow a 5-second window around the emit for slow test machines.
    assert before - timedelta(seconds=5) <= ts <= after + timedelta(seconds=5), (
        f"Fallback rec['ts'] ({ts_str!r}) should be close to the current time "
        f"(window: {before.isoformat()} – {after.isoformat()})"
    )


# ---------------------------------------------------------------------------
# Test 3: timestamp not duplicated in rec["data"]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timestamp_not_duplicated_in_data(coordinator, tmp_path):
    """data["timestamp"] must be promoted to rec["ts"] and NOT appear in rec["data"].

    If the timestamp is left in the event-data dict it appears twice in every JSONL
    record — once at rec["ts"] (correct) and once at rec["data"]["timestamp"]
    (redundant and confusing for downstream consumers parsing timing data).

    Strategy: spy captures the kernel-injected timestamp; we then verify
    rec["ts"] == spy value AND "timestamp" is absent from rec["data"].
    Other event-specific fields (e.g. "model") must still appear in rec["data"].
    """
    await _mount_with_tempdir(coordinator, tmp_path)

    kernel_ts_seen: dict = {}

    async def spy(event: str, data: dict) -> HookResult:
        kernel_ts_seen["value"] = data.get("timestamp")
        return HookResult(action="continue")

    coordinator.hooks.register("llm:request", spy, priority=50, name="test-spy")

    await coordinator.hooks.emit(
        "llm:request",
        {
            "session_id": "test-session",
            "model": "gpt-4o",  # a representative event-specific field
        },
    )

    assert kernel_ts_seen.get("value"), (
        "Rust kernel must have injected data['timestamp']"
    )

    rec = await _get_logged_record(tmp_path)

    # The kernel timestamp must be promoted to the top-level ts field.
    assert rec["ts"] == kernel_ts_seen["value"], (
        f"rec['ts'] must equal kernel timestamp {kernel_ts_seen['value']!r}, "
        f"got {rec['ts']!r}"
    )

    # timestamp must NOT appear inside rec["data"].
    event_data = rec.get("data", {})
    assert "timestamp" not in event_data, (
        f"data['timestamp'] must not be duplicated inside rec['data'], "
        f"but found it: rec['data'] = {event_data!r}"
    )

    # Other event-specific fields must still be present under rec["data"].
    assert event_data.get("model") == "gpt-4o", (
        f"Other event data must be preserved in rec['data']; "
        f"expected model='gpt-4o', got {event_data!r}"
    )
