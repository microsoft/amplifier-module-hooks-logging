# Amplifier Logging Hook Module

Provides visibility into agent execution through lifecycle event logging.

## Overview

This hook module integrates with Amplifier's hook system to log lifecycle events from the session and all loaded modules.

**Standard Events** (always logged):
- Session start/end
- Tool invocations and results
- Sub-agent spawning and completion
- Errors and warnings

**Module Events** (auto-discovered):
- Custom lifecycle events from loaded modules
- Module-specific operations (e.g., `skills:discovered`, `skills:loaded`)
- Automatically detected via the `observability.events` capability

## Features

- **Zero code changes required** - pure configuration
- **Auto-discovery of module events** - modules declare observable events, logging "just works"
- **Standard Python logging** - no external dependencies
- **Configurable levels** - DEBUG, INFO, WARNING, ERROR
- **Flexible output** - console, file, or both
- **Clean formatting** - timestamp, level, module, message

## Custom Event Logging

### Auto-Discovery (Recommended)

Modules can declare their observable lifecycle events using the `observability.events` capability. hooks-logging automatically discovers and logs these events.

**Module declares events** (`amplifier-module-skills/__init__.py`):
```python
async def mount(coordinator, config):
    # Declare observable events for this module
    # Get existing list, extend, then re-register (aggregation pattern)
    obs_events = coordinator.get_capability("observability.events") or []
    obs_events.extend([
        "skills:discovered",  # When skills are found
        "skills:loaded",      # When skill loaded successfully
        "skill:executed"      # When skill runs
    ])
    coordinator.register_capability("observability.events", obs_events)

    # ... rest of mount logic
```

**hooks-logging auto-discovers** (zero configuration needed):
- Queries `coordinator.get_capability("observability.events")` at mount time
- Registers handlers for all discovered events
- Module events logged alongside standard events

**Result**: Custom module events appear in logs automatically, no loader configuration needed.

### Manual Configuration

For precise control, explicitly list events in configuration:

```yaml
hooks:
  - module: hooks-logging
    config:
      auto_discover: false  # Disable auto-discovery
      additional_events:
        - "skills:discovered"
        - "skills:loaded"
        - "custom:my:event"
```

### Hybrid Approach

Combine auto-discovery with manual additions:

```yaml
hooks:
  - module: hooks-logging
    config:
      auto_discover: true  # Default: discovers module events
      additional_events:
        - "custom:debug:event"  # Add events not declared by modules
```

### Module Author Guide

**When creating modules that emit lifecycle events:**

1. **Declare your observable events** in `mount()` using the aggregation pattern:
   ```python
   # Get existing list, extend with your events, re-register
   obs_events = coordinator.get_capability("observability.events") or []
   obs_events.extend([
       "mymodule:started",
       "mymodule:completed"
   ])
   coordinator.register_capability("observability.events", obs_events)
   ```

2. **Emit events at appropriate lifecycle points**:
   ```python
   await coordinator.hooks.emit("mymodule:started", {"config": config})
   # ... module logic
   await coordinator.hooks.emit("mymodule:completed", {"status": "success"})
   ```

3. **Document your events** in module README for power users who disable auto-discovery

**Benefits**:
- ✅ Logging "just works" for users
- ✅ No fragile coupling between module and logging config
- ✅ Events discoverable by other observability hooks
- ✅ Power users can still manually configure if needed

## Prerequisites

- **Python 3.11+**
- **[UV](https://github.com/astral-sh/uv)** - Fast Python package manager

### Installing UV

```bash
# macOS/Linux/WSL
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Installation

```bash
uv pip install -e ./amplifier-module-hooks-logging
```

## Configuration

Add to your Amplifier configuration file (e.g., `test-full-features.toml`):

```toml
[hooks]
enabled = ["logging"]

[hooks.logging]
level = "INFO"           # DEBUG, INFO, WARNING, ERROR, CRITICAL
output = "console"       # console, file, or both
file = "amplifier.log"   # Required if output includes "file"
```

## Log Levels

### INFO (Recommended)

Shows key events without overwhelming detail:

- Session lifecycle
- Tool invocations (name only)
- Sub-agent activity
- Errors and warnings

### DEBUG

Shows all details INCLUDING detailed LLM request/response logging:

**Standard Events**:
- Tool arguments and results
- Full message content
- All lifecycle events

**LLM Debug Events** (requires `providers.*.config.debug: true`):
- `llm:request:debug` - Detailed request summary (message count, model, parameters)
- `llm:response:debug` - Detailed response summary (content preview, usage, timings)
- `llm:request:raw` - Complete raw request params sent to vendor API (requires `raw_debug: true`)
- `llm:response:raw` - Complete raw response object from vendor API (requires `raw_debug: true`)

**Configuration Example (Standard Debug)**:
```yaml
providers:
  - module: provider-anthropic
    config:
      debug: true  # Enable standard DEBUG event emission
      timeout: 300.0
      api_key: ${ANTHROPIC_API_KEY}

hooks:
  - module: hooks-logging
    config:
      level: "DEBUG"  # Capture DEBUG events in logs
```

**Configuration Example (Ultra-Verbose Raw Debug)**:
```yaml
providers:
  - module: provider-anthropic
    config:
      debug: true      # Enable standard DEBUG events
      raw_debug: true  # Enable RAW DEBUG events (complete vendor API I/O)
      timeout: 300.0
      api_key: ${ANTHROPIC_API_KEY}

hooks:
  - module: hooks-logging
    config:
      level: "DEBUG"  # Capture all DEBUG events including raw
```

**Debug Levels**:
- `debug: false` (default) - INFO events only, no debug details
- `debug: true` - Standard debug events with summaries and previews
- `debug: true, raw_debug: true` - Ultra-verbose with complete raw API I/O

**Note**: DEBUG level can generate significant log volume. RAW DEBUG generates extreme log volume with complete LLM request/response payloads. Use `raw_debug` only for deep debugging of provider integration issues.

**Log Location**: Session logs are written to `~/.amplifier/projects/<project>/sessions/<session_id>/events.jsonl`

**Project Directory Detection**: The `<project>` slug is determined by:
1. The `session.working_dir` coordinator capability (if registered)
2. Falls back to `Path.cwd()` for backward compatibility

This enables correct project identification in server/web deployments where the process cwd differs from the user's project directory.

### WARNING

Shows only warnings and errors:

- Tool failures
- Performance issues
- Configuration problems

### ERROR

Shows only errors:

- Critical failures
- Unhandled exceptions

## Usage

Once configured, logging happens automatically. No code changes needed.

```bash
# Start Amplifier with logging enabled
amplifier run --config test-full-features.toml --mode chat
```

## Example Output

```
2025-10-06 12:00:00 [INFO] amplifier_module_hooks_logging: === Session Started ===
2025-10-06 12:00:01 [INFO] amplifier_module_hooks_logging: Tool invoked: grep
2025-10-06 12:00:02 [INFO] amplifier_module_hooks_logging: Tool completed: grep ✓
2025-10-06 12:00:05 [INFO] amplifier_module_hooks_logging: Sub-agent spawning: architect
2025-10-06 12:00:10 [INFO] amplifier_module_hooks_logging: Sub-agent completed: architect
2025-10-06 12:00:11 [INFO] amplifier_module_hooks_logging: === Session Ended ===
```

## Philosophy Alignment

This module follows Amplifier's kernel philosophy:

- **Mechanism, not policy**: Provides logging mechanism; modules declare what to log (policy)
- **Auto-discovery via capabilities**: Uses coordinator capability system for module self-declaration
- **Ruthless simplicity**: Reuses existing hook and capability mechanisms, no new abstractions
- **Modular design**: Modules own their observability contract; logging discovers it
- **Zero loader coupling**: Modules declare events; logging auto-discovers; loaders configure nothing
- **Backward compatible**: Auto-discovery default; power users can disable and configure manually

## Development

### Adding Dependencies

```bash
# Add runtime dependency
uv add pydantic

# Add development dependency
uv add --dev pytest

# Update dependencies
uv lock --upgrade
```

### Running Tests

```bash
uv run pytest
```

### Running Tests

```bash
uv run pytest tests/
```

## Contributing

> [!NOTE]
> This project is not currently accepting external contributions, but we're actively working toward opening this up. We value community input and look forward to collaborating in the future. For now, feel free to fork and experiment!

Most contributions require you to agree to a
Contributor License Agreement (CLA) declaring that you have the right to, and actually do, grant us
the rights to use your contribution. For details, visit [Contributor License Agreements](https://cla.opensource.microsoft.com).

When you submit a pull request, a CLA bot will automatically determine whether you need to provide
a CLA and decorate the PR appropriately (e.g., status check, comment). Simply follow the instructions
provided by the bot. You will only need to do this once across all repos using our CLA.

This project has adopted the [Microsoft Open Source Code of Conduct](https://opensource.microsoft.com/codeofconduct/).
For more information see the [Code of Conduct FAQ](https://opensource.microsoft.com/codeofconduct/faq/) or
contact [opencode@microsoft.com](mailto:opencode@microsoft.com) with any additional questions or comments.

## Trademarks

This project may contain trademarks or logos for projects, products, or services. Authorized use of Microsoft
trademarks or logos is subject to and must follow
[Microsoft's Trademark & Brand Guidelines](https://www.microsoft.com/legal/intellectualproperty/trademarks/usage/general).
Use of Microsoft trademarks or logos in modified versions of this project must not cause confusion or imply Microsoft sponsorship.
Any use of third-party trademarks or logos are subject to those third-party's policies.
