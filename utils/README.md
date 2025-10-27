# ali-bot Utilities

Utility scripts for managing ALICE CI infrastructure, GitHub PR statuses, and build monitoring.

## Tools

### `cibuildhistory`
Query recent CI build completions and their durations from InfluxDB.

```bash
cibuildhistory --help
```

### `clean_pr`
Interactive tool to reset GitHub PR check statuses using fuzzy selection.

```bash
clean_pr --help
```

### `nomad-diskfree`
Check available disk space on Nomad CI workers and identify hosts needing cleanup.

```bash
nomad-diskfree --help
```

### `clean_sw_from_allocs`
Remove `sw` directories from running Nomad CI jobs when builds are stuck and require manual intervention.

**Warning**: Affects all ongoing builds!

```bash
clean_sw_from_allocs
```

### `worker_status`
Shows what each CI builder instance is currently doing by checking Nomad allocations and GitHub statuses.

```bash
worker_status --help
```

## Requirements

- Python 3.8+
- `requests`
- `python-dotenv`

Additional requirements by tool:
- **clean_pr**: `pyfzf`
- **nomad-diskfree**, **worker_status**: `python-nomad`
- **worker_status**: `gh` (GitHub CLI)

## Configuration

Most tools support configuration via environment variables or `.env` files in the project root.
