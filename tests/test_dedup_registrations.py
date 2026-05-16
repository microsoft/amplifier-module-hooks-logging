"""Tests for event-name deduplication before handler registration.

Root cause being tested
-----------------------
hooks-logging builds its events list from three additive sources:
  1. ALL_EVENTS (canonical kernel events)
  2. coordinator.get_capability("observability.events")
  3. coordinator.collect_contributions("observability.events")
  4. config["additional_events"]

If a contributor returns an event name that is already in ALL_EVENTS, the
handler gets registered *twice* for that event.  The Rust HookRegistry.register()
appends without dedup-by-name, so both registrations survive and both fire on
emit().  Result: one emit → two log lines (identical content, corrupted event log).

Empirical impact from production session: 158 duplicate llm:response pairs,
154 llm:request, 22-23 execution:start/end pairs.

The fix: `events = list(dict.fromkeys(events))` before the registration loop.

Contract being tested
---------------------
After any combination of additive event sources, if the same event name appears
more than once, the handler must be registered *exactly once* — verified by
emitting the event once and asserting exactly one log line is written.
"""

import json
from unittest.mock import AsyncMock

import pytest
from amplifier_core.events import ALL_EVENTS
from amplifier_core.testing import MockCoordinator

from amplifier_module_hooks_logging import mount, on_session_ready


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator():
    return MockCoordinator()


async def _setup(coordinator, tmp_path, *, config_extra: dict | None = None):
    """Mount and fully initialise the hook with a temp log directory."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {
        "session_log_template": template,
        "auto_discover": True,
        **(config_extra or {}),
    }
    await mount(coordinator, config)
    await on_session_ready(coordinator)


def _read_log_lines(tmp_path, session_id: str = "dedup-test-session") -> list[dict]:
    """Return all JSONL records for the given session."""
    log_file = tmp_path / "sessions" / session_id / "events.jsonl"
    if not log_file.exists():
        return []
    return [
        json.loads(line)
        for line in log_file.read_text().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Test 1 — duplicate via collect_contributions (primary production path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_event_names_only_registered_once(coordinator, tmp_path):
    """Handler must be registered exactly once even when a contributor duplicates
    an event name already in ALL_EVENTS.

    Setup: collect_contributions returns ["llm:request"] which is already in ALL_EVENTS.
    Action: emit "llm:request" once.
    Expected: exactly 1 log line.
    Bug behaviour (unfixed): 2 log lines — handler fired twice from one emit.
    """
    # "llm:request" IS in ALL_EVENTS — confirmed by the root-cause investigation
    assert "llm:request" in ALL_EVENTS, (
        "Test precondition: llm:request must be in ALL_EVENTS for this test to be valid"
    )

    # Simulate provider-gemini / loop-streaming contributing events already in ALL_EVENTS
    coordinator.collect_contributions = AsyncMock(return_value=[["llm:request"]])

    await _setup(coordinator, tmp_path)

    # Single emit — should produce a single log line
    await coordinator.hooks.emit(
        "llm:request",
        {"session_id": "dedup-test-session", "model": "test-model"},
    )

    records = _read_log_lines(tmp_path)
    assert len(records) == 1, (
        f"Expected exactly 1 log line after one emit of 'llm:request', "
        f"but got {len(records)}.  Duplicate registration detected — "
        f"the handler was registered {len(records)} times for 'llm:request'."
    )
    assert records[0]["event"] == "llm:request"


# ---------------------------------------------------------------------------
# Test 2 — new event (not in ALL_EVENTS) must still be registered once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_events_all_registered_once(coordinator, tmp_path):
    """A contributed event that does NOT exist in ALL_EVENTS must still be
    registered exactly once (dedup must not remove genuinely new events).

    Setup: collect_contributions returns a custom event NOT in ALL_EVENTS.
    Action: emit that custom event once.
    Expected: exactly 1 log line.
    """
    custom_event = "custom:unique_dedup_test_event"
    assert custom_event not in ALL_EVENTS, (
        f"Test precondition: '{custom_event}' must NOT be in ALL_EVENTS"
    )

    coordinator.collect_contributions = AsyncMock(return_value=[[custom_event]])

    await _setup(coordinator, tmp_path)

    await coordinator.hooks.emit(
        custom_event,
        {"session_id": "dedup-test-session", "source": "dedup-test"},
    )

    records = _read_log_lines(tmp_path)
    assert len(records) == 1, (
        f"Expected exactly 1 log line for '{custom_event}', got {len(records)}."
    )
    assert records[0]["event"] == custom_event


# ---------------------------------------------------------------------------
# Test 3 — duplicate via config additional_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_additional_events_deduped_against_all_events(
    coordinator, tmp_path
):
    """Handler must be registered exactly once when additional_events config
    lists an event name already in ALL_EVENTS.

    Setup: auto_discover=False, additional_events=["llm:request"] in config.
    Action: emit "llm:request" once.
    Expected: exactly 1 log line.
    Bug behaviour (unfixed): 2 log lines.
    """
    assert "llm:request" in ALL_EVENTS

    # additional_events duplicates an event already in ALL_EVENTS
    await _setup(
        coordinator,
        tmp_path,
        config_extra={
            "auto_discover": False,          # disable capability/contributions paths
            "additional_events": ["llm:request"],
        },
    )

    await coordinator.hooks.emit(
        "llm:request",
        {"session_id": "dedup-test-session", "model": "test-model"},
    )

    records = _read_log_lines(tmp_path)
    assert len(records) == 1, (
        f"Expected exactly 1 log line after one emit of 'llm:request' "
        f"(additional_events duplicate), but got {len(records)}."
    )
    assert records[0]["event"] == "llm:request"
