Jarvis for Windows (OpenJarvis)
================================

Jarvis is a voice assistant that runs your Windows PC: say "Hey Jarvis",
or just talk while the window is open.

Start it from the Start menu or the desktop shortcut. Closing the window
keeps Jarvis in the tray, muted until you say "Hey Jarvis"; right-click
the tray icon to quit.

Choosing the brain
------------------
Click the gear icon in the JARVIS window:

  * On this PC (Ollama) - private and works offline. Uses the qwen3.5:4b
    model; fast with an NVIDIA GPU (6 GB or more).
  * Groq cloud - fast on any PC. Paste a free API key from
    https://console.groq.com/keys (kept in the Windows Credential Manager).
    Your requests, and screenshots when Jarvis looks at the screen, go to
    Groq. Free keys have per-minute limits.

Speech-to-text can also run on Groq (helps without an NVIDIA GPU).

Where things are
----------------
  Program:   this folder (Python, packages, Node.js for the file tools)
  Settings:  %USERPROFILE%\.openjarvis\config.toml
  Logs:      %USERPROFILE%\.openjarvis\logs
  Voice:     %USERPROFILE%\.openjarvis\voices\jarvis.wav

Uninstalling (Settings > Apps) removes this folder but keeps your settings,
memory and contacts in %USERPROFILE%\.openjarvis, and leaves Ollama
installed. Delete those yourself if you want them gone.

If something broke, run "Jarvis - repair install" from the Start menu.

Licence: Apache-2.0 (see LICENSE.txt). Built on OpenJarvis,
https://github.com/open-jarvis/openjarvis. Chatterbox (Resemble AI),
faster-whisper, openWakeWord, Ollama, Node.js and the MCP filesystem
server are separate projects under their own licences.
