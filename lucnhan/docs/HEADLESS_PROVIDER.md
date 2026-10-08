# Headless Provider Configuration

This guide explains how the headless CLI (`python -m lucnhan.cli.headless ask|serve`)
selects the LLM provider and model, how to override them per run, and how to
troubleshoot authentication issues.

## How Provider/Model Are Resolved

Headless mode uses the **same config file** as the GUI:

```
<IDA user dir>/lucnhan/config.json
```

Resolution order (applied in `headless_bootstrap.py` after loading the config):

1. **Saved GUI config** — provider, model, API key, and API base from the
   last session you configured in the Luc Nhan UI.
2. **CLI overrides** — `--provider`, `--model`, `--api-base` flags (if
   provided) override the saved values **in memory only**. They are never
   saved back to disk.
3. **Provider default model** — if no model is configured (empty string) after
   steps 1–2, the provider's built-in default is used (see table below).

| Provider       | Default Model              |
| -------------- | -------------------------- |
| `anthropic`    | `claude-sonnet-4-20250514` |
| `openai`       | `gpt-4o`                   |
| `gemini`       | `gemini-2.0-flash`         |
| `ollama`       | `llama3.1`                 |
| `minimax`      | `MiniMax-M2.5`             |
| `openai_compat`| *(none — must configure)*  |

## Configuring Provider/Model in the GUI (Recommended)

1. Open IDA Pro with Luc Nhan loaded.
2. Click the gear/settings icon in the Luc Nhan panel.
3. Select your provider and model from the dropdowns.
4. Enter your API key (or use the environment variable approach below).
5. Save settings.

After saving, headless mode will pick up these settings automatically with
no extra flags needed:

```bash
python -m lucnhan.cli.headless ask binary.exe "Analyze this binary"
python -m lucnhan.cli.headless serve binary.exe
```

## Per-Run Overrides

You can override the provider, model, or API base for a single headless run
without changing your saved settings:

```bash
# Use OpenAI instead of whatever is saved
python -m lucnhan.cli.headless ask binary.exe "Analyze" --provider openai --model gpt-4o

# Use a local Ollama model
python -m lucnhan.cli.headless ask binary.exe "Analyze" --provider ollama --model codellama

# Use a custom endpoint (e.g., a local proxy or alternative API)
python -m lucnhan.cli.headless ask binary.exe "Analyze" --provider openai --api-base http://localhost:8080/v1

# Overrides work for serve mode too
python -m lucnhan.cli.headless serve binary.exe --provider anthropic --model claude-sonnet-4-20250514
```

**Note:** `--api-key` is intentionally NOT available as a CLI flag. API keys
should be set in the GUI settings or via environment variables (see below).
This prevents secrets from appearing in shell history or process lists.

## Setting API Keys

API keys are resolved in this order:

1. **Saved config** — the key stored in `config.json` (set via the GUI settings dialog).
2. **Environment variables** — provider-specific env vars checked at runtime.

| Provider         | Environment Variable(s)                                  |
| ---------------- | -------------------------------------------------------- |
| `anthropic`      | `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN`        |
| `openai`         | `OPENAI_API_KEY`                                        |
| `gemini`         | `GOOGLE_API_KEY` or `GEMINI_API_KEY`                    |
| `ollama`         | No API key required; optional `OLLAMA_BASE_URL`          |
| `minimax`        | `MINIMAX_API_KEY` (or saved config / env)                |
| `openai_compat`  | Set via saved config or the provider's expected env var  |

Example (PowerShell):

```powershell
$env:OPENAI_API_KEY = "sk-..."
python -m lucnhan.cli.headless ask binary.exe "Summarize"
```

Example (bash):

```bash
export OPENAI_API_KEY="sk-..."
python -m lucnhan.cli.headless ask binary.exe "Summarize"
```

## Verifying Your Configuration

Check that your provider and model are set correctly without launching a
full analysis — use `/health` on a running `serve` instance:

1. Start the server:
   ```bash
   python -m lucnhan.cli.headless serve binary.exe
   # Output: {"mode":"serve","url":"http://127.0.0.1:14913",...}
   ```

2. Check status:
   ```bash
   python -m lucnhan.cli.headless status --server http://127.0.0.1:14913 --token <token>
   ```

## Troubleshooting

### "Provider error: Invalid or missing API key"

This means no API key was found for the active provider. Check:

- **GUI**: Open IDA Pro, go to Luc Nhan Settings, and verify your API key is entered.
- **Environment**: Ensure the correct environment variable is set for your provider
  (see table above).
- **Provider mismatch**: You may have saved keys for one provider but are trying to
  use another. Use `--provider` to explicitly select the one you have keys for.

### "Unknown provider: '...'"

The provider name you specified (via `--provider` or in saved config) is not
recognized. Valid built-in providers: `anthropic`, `openai`, `gemini`, `ollama`,
`minimax`, `openai_compat`.

Custom OpenAI-compatible providers can be registered via the GUI Settings dialog.

### "Cannot locate IDA executable"

Headless mode needs `idat.exe` (Windows) or `idat` (Linux/macOS). Set the
`IDA_PATH` environment variable or pass `--ida <path>`:

```bash
python -m lucnhan.cli.headless ask binary.exe "Analyze" --ida "C:\Program Files\IDA Pro 9.2\idat.exe"
```
