# Contributing to Jarvis for Windows

Thanks for helping! Bug reports, ideas, docs fixes and pull requests are all
welcome.

## Reporting a bug

Open an [issue](../../issues) with:

- what you said or typed, what Jarvis did, and what you expected;
- your Windows version, GPU, and brain (local Ollama or Groq);
- the relevant part of `%USERPROFILE%\.openjarvis\logs\jarvis-app.log`, or
  of `%LOCALAPPDATA%\OpenJarvis\install.log` for setup problems.

**Remove anything private first:** contact names, messages, file names,
paths with your Windows username, and API keys.

## Setting up

You need Windows 10/11, [uv](https://docs.astral.sh/uv/), Rust with the MSVC
build tools, and [Ollama](https://ollama.com) (`ollama pull qwen3.5:4b`), or a
Groq key.

```bash
git clone https://github.com/<you>/OpenJarvis.git
cd OpenJarvis
uv sync --extra dev --extra desktop --extra voice --extra wakeword --extra agent --group desktop-native
uv run pre-commit install
uv run jarvis app
```

## Where things are

| Path | What |
|---|---|
| `src/openjarvis/agents/desktop.py` | The desktop agent: setup, fast path, model calls |
| `src/openjarvis/desktop/` | Tools (`server.py`), the agent loop and guards (`graph.py`), the no-LLM fast path, WhatsApp, YouTube Music, contacts, alarms, files (`files_mcp.py`) |
| `src/openjarvis/hud/` | The JARVIS window (`app.py`, `web/index.html`) and its settings |
| `src/openjarvis/speech/` | Speech-to-text, Chatterbox voice, wake word, Groq Whisper |
| `src/openjarvis/engine/groq.py` | Groq as a brain |
| `deploy/windows/installer/` | The setup wizard: Inno Setup script, `setup-env.ps1`, pinned requirements |
| `tests/desktop/` | Tests for all of the above |

## Making a change

1. For anything big (a new tool, a change to how the agent decides), open an
   issue first so we can agree on the approach.
2. Keep it working with the small local model (`qwen3.5:4b`): short tool
   descriptions and prompts, and a guard or a fast-path rule rather than
   hoping the model gets it right.
3. Anything that sends, deletes, calls or pays must ask the user first.
4. Add tests. Tests must never click, type into or open real apps on the PC
   running them (patch `os.startfile`, `server._launch` and the like), and
   their names and data must be made up, never real people's.
5. Check before you push:

   ```bash
   uv run pytest tests/desktop tests/cli tests/speech -q
   uv run ruff check src tests
   uv run ruff format --check src tests
   ```

6. Write commit messages as `type: what changed`, for example
   `fix: open WhatsApp chats by nickname` or `feat: …`, `docs: …`, `build: …`.
7. Open a pull request against `feat/windows-voice-assistant`. Say what
   changed and how you tested it, including any manual test in the app.

Changes to the general OpenJarvis framework (engines, agents, evals) belong
upstream at [open-jarvis/OpenJarvis](https://github.com/open-jarvis/OpenJarvis).

## Releasing

Maintainers build the installer and publish it as a GitHub release:

```bash
powershell -ExecutionPolicy Bypass -File scripts/windows/build-installer.ps1 -Version X.Y.Z -RepoUrl https://github.com/sakethpragallapati/OpenJarvis
```

Tag releases `jarvis-vX.Y.Z`. Plain `vX.Y.Z` tags already exist upstream.

## Code of Conduct and license

Please follow the [Code of Conduct](CODE_OF_CONDUCT.md). By contributing, you
agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
