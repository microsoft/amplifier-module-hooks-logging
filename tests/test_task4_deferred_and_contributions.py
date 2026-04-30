"""Tests for on_session_ready and collect_contributions behaviour.

Verifies:
- mount() defers registration — no hooks until on_session_ready()
- on_session_ready() exists and performs registration
- on_session_ready() calls coordinator.collect_contributions("observability.events")
  when auto_discover=True
- collect_contributions results are handled for both list and str shapes
- on_session_ready() does NOT call collect_contributions when auto_discover=False
- Each coordinator's setup is isolated — no cross-session contamination
"""

import json
from unittest.mock import AsyncMock

import pytest
from amplifier_core.testing import MockCoordinator

from amplifier_module_hooks_logging import mount, on_session_ready


@pytest.fixture
def coordinator():
    return MockCoordinator()


# --- mount() defers registration -----------------------------------------------


@pytest.mark.asyncio
async def test_mount_does_not_register_hooks(coordinator, tmp_path):
    """mount() must NOT register hooks — that is deferred to on_session_ready."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    await mount(coordinator, {"session_log_template": template, "auto_discover": False})

    # Emit an event BEFORE on_session_ready — no log should appear
    await coordinator.hooks.emit("session:start", {"session_id": "test-session"})

    log_file = tmp_path / "sessions" / "test-session" / "events.jsonl"
    assert not log_file.exists(), (
        "mount() must not register hooks; log must not appear until on_session_ready"
    )


# --- on_session_ready() --------------------------------------------------------


@pytest.mark.asyncio
async def test_on_session_ready_exists():
    """on_session_ready must be importable from the module."""
    assert callable(on_session_ready)


@pytest.mark.asyncio
async def test_on_session_ready_registers_hooks_and_logs(coordinator, tmp_path):
    """on_session_ready() must register hooks so events get logged."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {"session_log_template": template, "auto_discover": False}

    await mount(coordinator, config)
    await on_session_ready(coordinator)

    # Emit an event and verify the log file was created
    await coordinator.hooks.emit(
        "session:start",
        {"session_id": "test-session", "model": "test-model"},
    )

    log_file = tmp_path / "sessions" / "test-session" / "events.jsonl"
    assert log_file.exists(), "Hook must have registered and written the log file"


# --- Per-coordinator isolation -------------------------------------------------


@pytest.mark.asyncio
async def test_separate_coordinators_are_independent(tmp_path):
    """Each coordinator stores its own setup — no cross-session contamination.

    This is the regression test for the _deferred_configs module-level list bug:
    if two sessions mount() before either calls on_session_ready(), the second
    session's config must not bleed into the first session's handler.
    """
    coordinator1 = MockCoordinator()
    coordinator2 = MockCoordinator()

    template1 = str(tmp_path / "s1" / "{session_id}" / "events.jsonl")
    template2 = str(tmp_path / "s2" / "{session_id}" / "events.jsonl")

    # Both sessions mount before either calls on_session_ready
    await mount(
        coordinator1, {"session_log_template": template1, "auto_discover": False}
    )
    await mount(
        coordinator2, {"session_log_template": template2, "auto_discover": False}
    )

    # Only coordinator1 reaches on_session_ready
    await on_session_ready(coordinator1)

    await coordinator1.hooks.emit("session:start", {"session_id": "test"})

    log1 = tmp_path / "s1" / "test" / "events.jsonl"
    log2 = tmp_path / "s2" / "test" / "events.jsonl"
    assert log1.exists(), "coordinator1's handler must have fired"
    assert not log2.exists(), (
        "coordinator2's config must not have contaminated coordinator1's setup"
    )


# --- collect_contributions call ------------------------------------------------


@pytest.mark.asyncio
async def test_on_session_ready_calls_collect_contributions_when_auto_discover(
    coordinator, tmp_path
):
    """on_session_ready() must call coordinator.collect_contributions when auto_discover=True."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {"session_log_template": template, "auto_discover": True}

    coordinator.collect_contributions = AsyncMock(return_value=[])

    await mount(coordinator, config)
    await on_session_ready(coordinator)

    coordinator.collect_contributions.assert_called_once_with("observability.events")


@pytest.mark.asyncio
async def test_on_session_ready_no_collect_contributions_when_auto_discover_false(
    coordinator, tmp_path
):
    """on_session_ready() must NOT call collect_contributions when auto_discover=False."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {"session_log_template": template, "auto_discover": False}

    coordinator.collect_contributions = AsyncMock(return_value=[])

    await mount(coordinator, config)
    await on_session_ready(coordinator)

    coordinator.collect_contributions.assert_not_called()


# --- collect_contributions shapes ----------------------------------------------


@pytest.mark.asyncio
async def test_collect_contributions_list_shape_registers_events(coordinator, tmp_path):
    """Contributions returned as lists must result in those events being handled."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {"session_log_template": template, "auto_discover": True}

    contributed_events = ["custom:event_a", "custom:event_b"]
    # Return a list containing one contribution that is itself a list
    coordinator.collect_contributions = AsyncMock(return_value=[contributed_events])

    await mount(coordinator, config)
    await on_session_ready(coordinator)

    # Verify custom:event_a gets handled by emitting it and checking the log
    await coordinator.hooks.emit(
        "custom:event_a",
        {"session_id": "test-session", "payload": "test"},
    )
    log_file = tmp_path / "sessions" / "test-session" / "events.jsonl"
    assert log_file.exists(), "custom:event_a from list contribution must produce a log"
    records = [
        json.loads(line) for line in log_file.read_text().splitlines() if line.strip()
    ]
    events_logged = [r["event"] for r in records]
    assert "custom:event_a" in events_logged, (
        f"custom:event_a must be logged; found: {events_logged}"
    )


@pytest.mark.asyncio
async def test_collect_contributions_str_shape_registers_events(coordinator, tmp_path):
    """Contributions returned as individual strings must result in those events being handled."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {"session_log_template": template, "auto_discover": True}

    # Return a list with individual string items
    coordinator.collect_contributions = AsyncMock(return_value=["custom:str_event"])

    await mount(coordinator, config)
    await on_session_ready(coordinator)

    await coordinator.hooks.emit(
        "custom:str_event",
        {"session_id": "test-session", "payload": "test"},
    )
    log_file = tmp_path / "sessions" / "test-session" / "events.jsonl"
    assert log_file.exists(), (
        "custom:str_event from str contribution must produce a log"
    )
    records = [
        json.loads(line) for line in log_file.read_text().splitlines() if line.strip()
    ]
    events_logged = [r["event"] for r in records]
    assert "custom:str_event" in events_logged, (
        f"custom:str_event must be logged; found: {events_logged}"
    )
