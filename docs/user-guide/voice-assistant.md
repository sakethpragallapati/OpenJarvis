# Hands-free Voice Assistant

`jarvis listen` turns OpenJarvis into an always-on desktop assistant: say
**"Hey Jarvis"**, then your request. Replies are spoken, and Jarvis can operate
your PC, read your clipboard and set reminders. Everything — wake word,
speech-to-text, the LLM and the voice — runs locally.

## Install

The voice pieces are optional extras:

```bash
uv sync --extra desktop --extra voice --extra wakeword
```

On native Windows the installer can do this for you: answer **y** to
"Install voice mode", or run it with `-Voice` (`$env:OPENJARVIS_VOICE = '1'`
under `irm | iex`). It also installs `espeak-ng` with winget, which Kokoro uses
for words missing from its dictionary.

The first `jarvis listen` downloads the ~1 MB wake-word model.

## Use

```bash
jarvis listen                # say "Hey Jarvis", wait for the chime, speak
jarvis listen --autostart    # also start it at Windows logon
jarvis listen --no-autostart
```

- **Interrupt a reply:** say "Hey Jarvis" again (it goes straight to listening)
  or press any key in the window.
- **Quit:** say "stop listening" or press Ctrl-C.
- `jarvis chat --voice` is the push-to-talk version (press Enter, then speak);
  `jarvis chat --wake` is the same loop as `jarvis listen` with chat's defaults.

Things you can ask. `jarvis listen` uses the `orchestrator` agent (unless you
configured another tool-using agent) with the desktop tools below, not your
`[tools] enabled` chat list: every tool adds prompt tokens, and so latency, to
each spoken request. `--tools a,b` picks the set yourself.

| Say | Tool |
|-----|------|
| "Open VS Code", "open YouTube" | `pc_control` (Start Menu apps, Store apps, websites) |
| "Volume to 30", "mute", "next song", "pause" | `pc_control` (volume and media keys) |
| "Brightness 60", "how much battery is left?" | `pc_control` |
| "Lock my laptop" | `lock_screen` (asks you to confirm with `y`) |
| "Summarize what I copied" | `clipboard` |
| "Remind me in 20 minutes to stretch", "what reminders do I have?" | `set_reminder`, `list_reminders`, `cancel_reminder` |

Reminders are stored in `~/.openjarvis/reminders.json`. Any running
`jarvis listen` / `jarvis chat` delivers them as a Windows notification (and
reads them aloud in voice mode); one that came due while Jarvis wasn't running
fires the next time it starts.

## Settings

```toml
[speech]
model = "small"            # Whisper size: tiny, base, small, medium
device = "cpu"             # keep the GPU free for the LLM (auto falls back to CPU on CUDA errors)
compute_type = "int8"
voice_id = "bm_george"     # Kokoro voice
silence_threshold = 0      # 0 = measure room noise at start; or pin a mic level
wake_word = "hey_jarvis"   # or "alexa", "hey_mycroft", or a path to a custom .onnx
wake_word_threshold = 0.5  # raise if it wakes by itself, lower if it misses you
wake_word_barge_in = true
end_silence_seconds = 0.9  # pause that ends your sentence (0 = default 1.5 s)
follow_up_seconds = 6      # after a reply, keep listening this long without the wake word

[engine.ollama]
keep_alive = "30m"         # keep the model loaded between commands ("-1" = forever)

[power]
battery_saver = true       # on battery: speech on CPU, and switch model ...
battery_model = "qwen3.5:2b"   # ... to this one (empty = keep the normal model)
low_vram_warning_mb = 1024
```

Voice mode already asks Ollama to keep the model for 30 minutes when
`keep_alive` is unset, and warms it up at start, so the first command doesn't
wait for a model load.

## Troubleshooting

- **"Wake word unavailable"** — install the extra: `uv sync --extra wakeword`.
- **It hears you but never answers aloud** — no TTS backend:
  `uv sync --extra voice`, or set `OPENAI_API_KEY` / `ELEVENLABS_API_KEY`.
- **Recording never stops in a noisy room / cuts you off** — set
  `silence_threshold` explicitly (the calibration line printed at start shows
  the measured background level; use about 3× that).
- **"DLL load failed … filename or extension is too long"** — the install path
  is over Windows' 260-character limit for native libraries. Install into a
  shorter folder such as `C:\Users\<you>\OpenJarvis`.
