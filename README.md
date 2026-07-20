# Ultimate Sacrifice

**AI-accelerated, context-rich disk scanner and cleanup TUI.**

Scan a folder for the largest files and directories, let an AI model judge — with real local
metrics (temp/cache detection, build artifacts, how long since you last touched it) — whether each
item is safe to delete, then clean up from a rich terminal UI. Safe by default: deletions go to the
**Recycle Bin**, there's a **dry-run** mode, and system paths are hard-guarded.

## Quick start (Windows)

The run scripts bootstrap everything — they create a local `.venv`, install dependencies on first
run, then launch. No manual setup needed. Any arguments pass straight through.

```powershell
.\run.ps1                                   # PowerShell
.\run.ps1 --root "C:\Users\me" --provider claude_cli
.\run.ps1 -Reinstall                        # wipe .venv and reinstall
```

```bat
run.bat                                     :: cmd.exe / double-click
run.bat --provider ollama
```

If PowerShell blocks the script, run: `powershell -ExecutionPolicy Bypass -File .\run.ps1`.

## Manual install

Requires **Python 3.11+**.

```bash
pip install -e .
```

## Run

```bash
python -m ultimate_sacrifice
# or
ultimate-sacrifice --root "C:\Users\you" --provider ollama
```

1. **Scan screen** — pick a folder, a minimum item size, and an AI provider. Toggle Recycle Bin /
   dry-run.
2. **Results** — a table of the largest candidates, sorted by size, with category and a local
   "junk score". Press **`a`** to assess with AI (adds a color-coded keep/review/delete verdict +
   reason), **`space`** to select rows, **`d`** to delete, **`r`** to rescan.
3. **Confirm** — review the selection and total reclaimable size, then confirm.

## AI providers

| Provider     | Needs                              | Notes                                           |
|--------------|------------------------------------|-------------------------------------------------|
| `ollama`     | Ollama running + a model pulled    | **Default.** Fully local. `qwen3:8b` by default.|
| `claude_cli` | The `claude` CLI, already logged in| **No API key** — uses your Claude account.      |
| `anthropic`  | `ANTHROPIC_API_KEY`                | Direct Anthropic API.                           |

Pick one on the scan screen, via `--provider`, or in `config.toml` (`[ai] provider = "..."`).
Pull a local model first if using Ollama, e.g. `ollama pull qwen3:8b`.

## Configuration

Copy `config.example.toml` to `config.toml` and edit. Everything is optional — the app runs with
built-in defaults if no config exists.

## Assessment cache

AI verdicts are cached and reused across runs, so a re-scan only asks the AI about **new or changed
files**. The cache is keyed by each file's `(path, size, mtime)` plus the provider/model, and lives
in your per-user cache dir (never inside a scanned folder). After a scan, unchanged items show their
saved verdict immediately (marked with a trailing `*`); pressing `a` only assesses the rest. Disable
with `--no-cache`, or `[cache] enabled = false` in `config.toml`.

## Safety

- **Recycle Bin by default** (recoverable via `send2trash`); permanent delete is opt-in and requires
  typing `DELETE`.
- **Dry-run** frees nothing and just reports what it would do.
- **Guarded paths** — drive roots, `C:\Windows`, `C:\Program Files*`, `ProgramData`, and the app's
  own directory are never deleted, regardless of the AI's recommendation.
- **Containers, not targets** — big folders that aren't recognized junk (e.g. `C:\Dev`, `Videos`)
  and the scan root itself are shown for context but marked `protected`: never AI-flagged, never
  selectable, never deleted. Only known-disposable dirs (`node_modules`, `build`, caches, temp) and
  large individual files are offered for cleanup.

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q
```

Tests cover the pure cores (heuristics, prompt parsing, scanner aggregation, delete guards) and run
with no GPU or network. See `CLAUDE.md` for architecture and the headless verification recipe.

## License

Public domain — see `UNLICENSE.txt`.
