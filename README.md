<div align="center">

# J.A.R.V.I.S. for Windows

**A hands-free voice assistant that runs your Windows PC.**
Talk to it and it opens apps, websites and files, plays music, sends WhatsApp
messages, sets alarms, reads your screen, and remembers things. You choose
whether it thinks on your own PC or in the cloud.

[![Release](https://img.shields.io/github/v/release/sakethpragallapati/OpenJarvis?label=download)](../../releases/latest)
![Windows 10/11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

</div>

---

## Download and install

1. Download **`OpenJarvis-Setup-<version>.exe`** from
   [Releases](../../releases/latest) and run it. It installs for your Windows
   account only, so no admin rights are needed.
2. Choose Jarvis's brain. You can switch later in Jarvis's settings.
   - **On this PC:** Ollama with `qwen3.5:4b`. Private, works offline, and is
     best with an NVIDIA GPU.
   - **Groq cloud:** fast on any PC. You'll need a free API key from
     [console.groq.com/keys](https://console.groq.com/keys).
3. Setup downloads what Jarvis needs (3-7 GB; 10-30 minutes). Then start
   **Jarvis** from the Start menu and talk.

Windows SmartScreen may warn about an unknown publisher, because the setup
isn't code-signed. Click **More info**, then **Run anyway**. You can check the
download against the `.sha256` file attached to the release.

### Requirements

- Windows 10 (1809+) or 11, 64-bit, with a microphone and speakers.
- About 15 GB of free disk space.
- For the local brain: an NVIDIA GPU with 6 GB+ of VRAM is recommended. Jarvis
  runs on the CPU too, just slower, and Groq is fast on any PC.

## What you can say

| Say | Jarvis |
|---|---|
| "Open VS Code", "open the Minecraft server folder", "open my CC notes in desktop" | Opens apps, folders and files, even when the name is partial or misheard |
| "Open the Amazon page of the iPhone 17", "open the first 3 results" | Opens the real web pages |
| "Play some Telugu songs", "play my workout playlist", "next song" | Plays in YouTube Music |
| "What's the weather in Hyderabad?", "compare these two phones" | Searches the web and answers |
| "Send hi to Ravi on WhatsApp", "call Mom on WhatsApp" | WhatsApp messages and calls (asks before sending) |
| "Wake me up at 6:30", "remind me in 20 minutes to drink water" | Real Windows alarms and reminders |
| "What's on my screen?", "click Sign in" | Looks at the screen and clicks by name |
| "Find my resume", "move it to Documents", "delete the old one" | File tools, limited to your own folders (deletes go to the Recycle Bin) |
| "Write a Python program that …", "run it" | Writes the code, opens VS Code and reads you the output |
| "Remember that my bike service is every Friday", "when is my bike service?" | Long-term memory |

Say **"Jarvis"** to interrupt it while it talks. Closing the window keeps
Jarvis in the tray, muted until you say **"Hey Jarvis"**.

## Settings and privacy

Click the **gear** in the JARVIS window:

- **Brain:** a local model (nothing leaves your PC) or Groq. With Groq, your
  requests, and screenshots when Jarvis looks at the screen, are sent to Groq.
- **Groq API key:** checked with Groq, then stored in the Windows Credential
  Manager, never in a file.
- **Speech-to-text:** local Whisper or Groq Whisper. Groq Whisper helps PCs
  without an NVIDIA GPU.

Everything else is in `%USERPROFILE%\.openjarvis\config.toml`. The full guide
is [docs/user-guide/voice-assistant.md](docs/user-guide/voice-assistant.md):
features, settings, voices, contacts, alarms and troubleshooting.

## Run from source

```bash
git clone https://github.com/sakethpragallapati/OpenJarvis.git
cd OpenJarvis
uv sync --extra desktop --extra voice --extra wakeword --extra agent --group desktop-native
uv run jarvis app --install    # Desktop and Start-menu shortcuts
uv run jarvis app              # the JARVIS window (or: uv run jarvis listen)
```

This needs [uv](https://docs.astral.sh/uv/), Rust with the MSVC build tools
(for the native extension) and, for the local brain,
[Ollama](https://ollama.com) with `ollama pull qwen3.5:4b`.

To build the setup wizard (needs Inno Setup 6 too):

```bash
powershell -ExecutionPolicy Bypass -File scripts/windows/build-installer.ps1 -Version 0.1.0
```

## Contributing

Bug reports, ideas and pull requests are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md), and please follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Credits and license

Jarvis for Windows is made by **Pragallapati Saketh**
([@sakethpragallapati](https://github.com/sakethpragallapati)). It's built on
[OpenJarvis](https://github.com/open-jarvis/OpenJarvis) by The OpenJarvis
Authors (Hazy Research and the Scaling Intelligence Lab, Stanford), described
in [OpenJarvis: Personal AI, On Personal Devices](https://arxiv.org/abs/2605.17172).

It also uses Chatterbox (Resemble AI), faster-whisper, openWakeWord, Ollama,
LangGraph, FastMCP, the MCP filesystem server and pywebview, each under its
own license.

Licensed under the [Apache License 2.0](LICENSE).
