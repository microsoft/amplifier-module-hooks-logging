"""Tests for exclude_events config option in hooks-logging.

Verifies that the `exclude_events` config key suppresses matching events
from JSONL output (no record written, handler returns continue immediately).

Contracts:
- Default config: ``["llm:stream_block_delta"]`` — that event is silently
  dropped while all other events (including other streaming events) pass.
- ``exclude_events: []`` — disables the filter; every event is written.
- Custom pattern ``"llm:stream_*"`` — drops all four streaming events.
- fnmatch wildcard semantics (``?``, ``*``, ``[…]``) are used for matching.
"""

import json

import pytest
from amplifier_core.testing import MockCoordinator

from amplifier_module_hooks_logging import mount, on_session_ready

# All four streaming events introduced by the provider streaming feature.
STREAMING_EVENTS = [
    "llm:stream_block_delta",
    "llm:stream_block_start",
    "llm:stream_block_end",
    "llm:stream_aborted",
]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def coordinator():
    return MockCoordinator()


async def _mount_with_tempdir(
    coordinator,
    tmp_path,
    *,
    exclude_events=None,
    extra_additional_events: list | None = None,
) -> None:
    """Mount the hook with a temp session log dir.

    Args:
        coordinator: MockCoordinator instance.
        tmp_path: pytest tmp_path fixture.
        exclude_events: Value for ``exclude_events`` config key.
            Pass ``None`` to omit (uses default).  Pass ``[]`` to disable.
        extra_additional_events: Extra event names to register in addition
            to STREAMING_EVENTS (which are always added so the handler is
            registered even if they're not yet in ALL_EVENTS).
    """
    template = str(tmp_path / "sessions" / "{session_id}" / "events.jsonl")
    config: dict = {
        "session_log_template": template,
        "auto_discover": False,
        # Always register the streaming events so the handler fires for them.
        # Without this, an event not in ALL_EVENTS would never reach the handler
        # and we couldn't distinguish "handler excluded it" from "not registered".
        "additional_events": STREAMING_EVENTS + (extra_additional_events or []),
    }
    if exclude_events is not None:
        config["exclude_events"] = exclude_events
    await mount(coordinator, config)
    await on_session_ready(coordinator)


def _read_events(tmp_path, session_id: str = "test-session") -> list:
    """Return all JSONL records for the given session, or [] if the file is absent."""
    log_file = tmp_path / "sessions" / session_id / "events.jsonl"
    if not log_file.exists():
        return []
    return [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _event_names(records: list) -> set:
    return {r["event"] for r in records}


# ---------------------------------------------------------------------------
# Test 1 — default config drops llm:stream_block_delta only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_drops_stream_block_delta_not_other_events(
    coordinator, tmp_path
):
    """Default config silently drops llm:stream_block_delta; all others are written.

    The default value of exclude_events is ["llm:stream_block_delta"].
    The three related streaming events (block_start, block_end, aborted) and
    ordinary events like llm:response must continue to be logged.
    """
    # Mount WITHOUT specifying exclude_events -> should use default
    await _mount_with_tempdir(
        coordinator,
        tmp_path,
        extra_additional_events=["llm:response"],
    )

    # Emit the event that the default filter should suppress
    await coordinator.hooks.emit(
        "llm:stream_block_delta",
        {"session_id": "test-session", "delta": "hello"},
    )

    # Emit events that must still be written
    for ev in ("llm:stream_block_start", "llm:stream_block_end", "llm:stream_aborted"):
        await coordinator.hooks.emit(ev, {"session_id": "test-session"})

    await coordinator.hooks.emit(
        "llm:response",
        {"session_id": "test-session", "content": "done"},
    )

    events = _event_names(_read_events(tmp_path))

    # The delta must NOT appear in the log
    assert "llm:stream_block_delta" not in events, (
        "llm:stream_block_delta must be excluded by the default filter, "
        f"but it appeared in: {events}"
    )

    # All other events MUST appear
    for expected in (
        "llm:stream_block_start",
        "llm:stream_block_end",
        "llm:stream_aborted",
        "llm:response",
    ):
        assert expected in events, (
            f"Expected {expected!r} to be logged, but events written were: {events}"
        )


# ---------------------------------------------------------------------------
# Test 2 — empty exclude_events disables the filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_exclude_events_writes_block_delta(coordinator, tmp_path):
    """exclude_events: [] disables all filtering; llm:stream_block_delta IS written."""
    await _mount_with_tempdir(coordinator, tmp_path, exclude_events=[])

    await coordinator.hooks.emit(
        "llm:stream_block_delta",
        {"session_id": "test-session", "delta": "tok"},
    )

    events = _event_names(_read_events(tmp_path))
    assert "llm:stream_block_delta" in events, (
        "With exclude_events=[], llm:stream_block_delta must be written to the log, "
        f"but events written were: {events}"
    )


# ---------------------------------------------------------------------------
# Test 3 — custom wildcard pattern excludes all four streaming events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_wildcard_pattern_excludes_all_streaming_events(
    coordinator, tmp_path
):
    """exclude_events: ['llm:stream_*'] drops all four streaming events but not others."""
    await _mount_with_tempdir(
        coordinator,
        tmp_path,
        exclude_events=["llm:stream_*"],
        extra_additional_events=["llm:response"],
    )

    # Emit all four streaming events — all should be suppressed
    for ev in STREAMING_EVENTS:
        await coordinator.hooks.emit(ev, {"session_id": "test-session"})

    # Emit a non-streaming event — must still be written
    await coordinator.hooks.emit("llm:response", {"session_id": "test-session"})

    events = _event_names(_read_events(tmp_path))

    for ev in STREAMING_EVENTS:
        assert ev not in events, (
            f"Expected {ev!r} to be excluded by pattern 'llm:stream_*', "
            f"but it appeared in: {events}"
        )

    assert "llm:response" in events, (
        "llm:response must not be excluded by 'llm:stream_*', "
        f"but events written were: {events}"
    )


# ---------------------------------------------------------------------------
# Test 4 — fnmatch wildcard semantics (? matches exactly one character)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fnmatch_question_mark_matches_single_character(coordinator, tmp_path):
    """fnmatch '?' matches exactly one character — verifying real fnmatch semantics.

    Pattern 'llm:stream_block_?????' matches events whose suffix after
    'llm:stream_block_' is exactly 5 characters:
      - 'delta' (5 chars) -> excluded
      - 'start' (5 chars) -> excluded
      - 'end'   (3 chars) -> NOT excluded
    'llm:stream_aborted' doesn't match the pattern at all -> NOT excluded.
    """
    await _mount_with_tempdir(
        coordinator,
        tmp_path,
        exclude_events=["llm:stream_block_?????"],
    )

    for ev in STREAMING_EVENTS:
        await coordinator.hooks.emit(ev, {"session_id": "test-session"})

    events = _event_names(_read_events(tmp_path))

    # 5-char suffixes -> excluded
    assert "llm:stream_block_delta" not in events, (
        "'llm:stream_block_delta' must be excluded by 'llm:stream_block_?????'"
    )
    assert "llm:stream_block_start" not in events, (
        "'llm:stream_block_start' must be excluded by 'llm:stream_block_?????'"
    )

    # 3-char suffix -> NOT excluded
    assert "llm:stream_block_end" in events, (
        "'llm:stream_block_end' must NOT be excluded by 'llm:stream_block_?????' "
        "(suffix 'end' is 3 chars, not 5)"
    )

    # Different prefix -> NOT excluded
    assert "llm:stream_aborted" in events, (
        "'llm:stream_aborted' must NOT be excluded by 'llm:stream_block_?????' "
        "(prefix does not contain 'block_')"
    )


# ---------------------------------------------------------------------------
# Test 5 — multiple patterns in exclude_events (OR semantics)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_patterns_are_combined_with_or_semantics(coordinator, tmp_path):
    """Multiple patterns in exclude_events suppress events matching ANY pattern.

    Pattern list: ["llm:stream_block_delta", "llm:stream_block_start"]
    Both events must be excluded; the other two streaming events must be written.
    """
    await _mount_with_tempdir(
        coordinator,
        tmp_path,
        exclude_events=["llm:stream_block_delta", "llm:stream_block_start"],
    )

    for ev in STREAMING_EVENTS:
        await coordinator.hooks.emit(ev, {"session_id": "test-session"})

    events = _event_names(_read_events(tmp_path))

    assert "llm:stream_block_delta" not in events, (
        "llm:stream_block_delta must be excluded (explicit pattern)"
    )
    assert "llm:stream_block_start" not in events, (
        "llm:stream_block_start must be excluded (explicit pattern)"
    )
    assert "llm:stream_block_end" in events, (
        "llm:stream_block_end must be written (no matching pattern)"
    )
    assert "llm:stream_aborted" in events, (
        "llm:stream_aborted must be written (no matching pattern)"
    )
