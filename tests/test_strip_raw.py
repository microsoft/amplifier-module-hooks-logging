"""Tests for strip_raw config option in hooks-logging.

Tests that the `strip_raw` config key controls whether the `raw` field
is stripped from event data before writing to JSONL.
"""

import json

import pytest
from amplifier_core.testing import MockCoordinator

from amplifier_module_hooks_logging import mount, on_session_ready


@pytest.fixture
def coordinator():
    return MockCoordinator()


async def _mount_with_tempdir(coordinator, strip_raw_value, tmp_path):
    """Helper: mount the hook with a temp session log dir and given strip_raw setting."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {
        "session_log_template": template,
        "auto_discover": False,
        "strip_raw": strip_raw_value,
    }
    await mount(coordinator, config)
    await on_session_ready(coordinator)


async def _get_logged_record(tmp_path) -> dict:
    """Read the first JSONL record written to the temp session log."""
    log_file = tmp_path / "sessions" / "test-session" / "events.jsonl"
    assert log_file.exists(), f"Expected log file at {log_file}"
    lines = log_file.read_text().splitlines()
    assert lines, "Expected at least one log record"
    return json.loads(lines[-1])


@pytest.mark.asyncio
async def test_strip_raw_true_removes_raw_field(coordinator, tmp_path):
    """When strip_raw is true, the raw field must NOT appear in written JSONL."""
    await _mount_with_tempdir(coordinator, strip_raw_value=True, tmp_path=tmp_path)

    await coordinator.hooks.emit(
        "session:start",
        {
            "session_id": "test-session",
            "raw": {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            "model": "gpt-4o",
        },
    )

    rec = await _get_logged_record(tmp_path)
    assert "raw" not in rec.get("data", {}), (
        f"Expected 'raw' to be stripped, but found it in data: {rec.get('data')}"
    )


@pytest.mark.asyncio
async def test_strip_raw_false_preserves_raw_field(coordinator, tmp_path):
    """When strip_raw is false (default), the raw field IS written to JSONL."""
    await _mount_with_tempdir(coordinator, strip_raw_value=False, tmp_path=tmp_path)

    raw_payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]}
    await coordinator.hooks.emit(
        "session:start",
        {
            "session_id": "test-session",
            "raw": raw_payload,
            "model": "gpt-4o",
        },
    )

    rec = await _get_logged_record(tmp_path)
    assert "raw" in rec.get("data", {}), (
        f"Expected 'raw' to be preserved when strip_raw=False, but it was absent. data={rec.get('data')}"
    )


@pytest.mark.asyncio
async def test_strip_raw_default_preserves_raw_field(coordinator, tmp_path):
    """When strip_raw is absent from config (default), the raw field IS written."""
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config = {
        "session_log_template": template,
        "auto_discover": False,
        # no strip_raw key — should default to False
    }
    await mount(coordinator, config)
    await on_session_ready(coordinator)

    raw_payload = {"tokens": 1000}
    await coordinator.hooks.emit(
        "llm:request",
        {
            "session_id": "test-session",
            "raw": raw_payload,
        },
    )

    rec = await _get_logged_record(tmp_path)
    assert "raw" in rec.get("data", {}), (
        f"Expected 'raw' preserved by default, but absent. data={rec.get('data')}"
    )


@pytest.mark.asyncio
async def test_strip_raw_preserves_other_event_data(coordinator, tmp_path):
    """When strip_raw is true, only raw is removed — other event data is preserved."""
    await _mount_with_tempdir(coordinator, strip_raw_value=True, tmp_path=tmp_path)

    await coordinator.hooks.emit(
        "llm:request",
        {
            "session_id": "test-session",
            "raw": {"big": "payload"},
            "model": "gpt-4o",
            "tokens": 42,
        },
    )

    rec = await _get_logged_record(tmp_path)
    data = rec.get("data", {})
    assert "raw" not in data, "raw should be stripped"
    assert data.get("model") == "gpt-4o", "model should be preserved"
    assert data.get("tokens") == 42, "tokens should be preserved"
