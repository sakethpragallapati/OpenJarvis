# Hands-free Voice Assistant

`jarvis listen` turns OpenJarvis into an always-on desktop assistant: say
**"Hey Jarvis"**, then your request. Replies are spoken, and Jarvis can operate
your PC, read your clipboard and set reminders. Everything — wake word,
speech-to-text, the LLM and the voice — runs locally, unless you choose
[Groq's cloud](#brain-local-or-groq) for the model.

## Install

### The setup wizard (Windows 10/11)

Download `OpenJarvis-Setup-<version>.exe` from the project's GitHub
**Releases** page and run it. No admin rights are needed; it installs for
your Windows account into `%LOCALAPPDATA%\OpenJarvis`. You choose:

- **Brain:** a local model (Ollama + `qwen3.5:4b`, about 3.4 GB; private and
  offline; best with an NVIDIA GPU with 6 GB+) or **Groq cloud** (fast on any
  PC; paste a free API key in Jarvis's settings).
- A desktop shortcut, and whether Jarvis starts muted in the tray at
  sign-in.

The wizard is small (~27 MB). It then downloads what Jarvis needs: Python
3.11, its packages (PyTorch with CUDA if it finds an NVIDIA GPU, the CPU
build otherwise), a pinned Node.js for the [file tools](#files-the-mcp-filesystem-server),
and Ollama with the model if you picked the local brain. Allow 10-30 minutes.
Your settings go to `%USERPROFILE%\.openjarvis\config.toml`, and existing
settings are kept. **Jarvis - repair install** in the Start menu runs that
step again. Uninstalling removes the program folder but keeps
`~\.openjarvis` (settings, memory, contacts) and Ollama.

To build the wizard yourself: `.\scripts\windows\build-installer.ps1 -Version 0.1.0`
(needs uv, Rust with the MSVC build tools and Inno Setup 6).

### From source

The voice pieces are optional extras:

```bash
uv sync --extra desktop --extra voice --extra wakeword --extra agent
```

On native Windows the installer can do this for you: answer **y** to
"Install voice mode", or run it with `-Voice` (`$env:OPENJARVIS_VOICE = '1'`
under `irm | iex`). On Windows, uv installs the CUDA 12.8 build of PyTorch
for the voice.

The first `jarvis listen` downloads the ~1 MB wake-word model and the
Chatterbox-Turbo voice model (~3.8 GB, once).

## Brain: local or Groq

Click the **gear** in the JARVIS window to choose the model that thinks for
Jarvis:

| | On this PC (Ollama) | Groq cloud |
|---|---|---|
| Model | `qwen3.5:4b` (or any you've pulled) | `qwen/qwen3.8-27b` by default (tools and screen vision); any Groq chat model |
| Privacy | Nothing leaves the PC | Requests, and screenshots when Jarvis looks at the screen, go to Groq |
| Speed | Needs a good NVIDIA GPU | Fast on any PC |
| Cost | Free | Free key with per-minute limits; paid plans for heavy use |

For Groq, paste a key from [console.groq.com/keys](https://console.groq.com/keys)
into the settings and press **Save key**. Jarvis checks it with Groq and
stores it in the **Windows Credential Manager** (as `OpenJarvis/groq`),
never in a file. A `GROQ_API_KEY` environment variable also works and takes
precedence. **Save and restart** writes your choice to `config.toml`:

```toml
[engine]
default = "groq"            # or "ollama"

[engine.groq]
model = "qwen/qwen3.8-27b"  # [intelligence] default_model stays the local one
```

Every agent step sends the system prompt and the tool list (a few thousand
tokens), so free keys' per-minute token limits are reached quickly when
you use Jarvis heavily. Simple commands ("open VS Code", volume, alarms)
skip the model and cost nothing.

**Speech-to-text on Groq.** Speech-to-text can use **Groq Whisper**
(`whisper-large-v3-turbo`, `[speech] backend = "groq"`) instead of local
Whisper. This helps PCs without an NVIDIA GPU, but your recordings are sent
to Groq. Contact and app names still steer its spelling.

## The voice

Replies are spoken by **Chatterbox-Turbo** (Resemble AI, MIT): a 427M-parameter
speech-token model plus a 266M-parameter audio decoder (~8× Kokoro-82M),
running on the NVIDIA GPU next to the language model (~2.7 GB of VRAM). It
clones a voice from a short clip: `voice_id = "jarvis"` is
`~/.openjarvis/voices/jarvis.wav`; drop any clean 10-20 s recording there as
`<name>.wav` and set `voice_id = "<name>"` (or a full path), or use
`"default"` for the model's own voice.

Stock Chatterbox decodes token by token in eager PyTorch, which on a laptop
GPU (especially on battery) is about real time. OpenJarvis captures each
decoding step as a CUDA graph over a fixed KV cache, keeps the voice prompt
cached between sentences, and samples on the GPU - same weights and
sampling settings, same transcripts in Whisper checks, ~3-4× faster. On an
RTX 5050 laptop on battery: "Opening VS Code." in ~0.65 s, a 4-second
sentence in ~1.3-1.8 s; replies are spoken sentence by sentence, so the
first one starts while the rest are still being made. Loading takes ~40 s
when `jarvis listen` starts. Without an NVIDIA GPU it runs on the CPU, slower
than real time; `uv sync --extra voice-kokoro` and `tts_backend = "kokoro"`
is the light CPU alternative.

## The JARVIS window

Double-click **Jarvis** on the Desktop or in the Start menu: a window with a
glowing ring that moves with your voice and Jarvis's, the conversation (what
you said, each step Jarvis takes, its replies) and a box to type commands.
There's no console; everything `jarvis listen` would print goes to
`~/.openjarvis/logs/jarvis-app.log` (the **LOG** button).

- **Mic button:** pause always-listening (Jarvis then waits for "Hey Jarvis");
  press again to resume.
- **Stop button / Esc:** stop speaking at once; a request in progress stops
  after its current step.
- **Typing:** start typing anywhere, Enter sends. Typed commands work while
  paused too. Questions Jarvis asks ("Send it?") get **Yes**/**No** buttons.
- **Closing the window keeps Jarvis running, muted,** in the tray (the ring
  icon by the clock) with the speech models loaded. Muted means it ignores
  conversation and waits for "Hey Jarvis"; say it with a request and the
  window opens by itself and does it. Opening Jarvis again (the icon, or the
  shortcut) is instant and turns always-listening back on. Right-click the
  icon for **Pause listening** or **Quit Jarvis** (quitting frees the GPU
  memory). "Stop listening" by voice also quits.
- **Text follows the voice:** while Jarvis speaks, the sentence it's saying
  appears under the ring and its reply fills in the conversation panel at the
  same pace, each word fading and gliding in with a brief glow.
- The conversation panel opens and closes with the button at the top right
  (remembered); while it's closed, the last reply shows under the ring.
- Only one Jarvis runs at a time: opening it again brings the window forward.
  Close `jarvis listen` in any terminal first, or both will answer.

```bash
jarvis app                   # open the window (same as the shortcut)
jarvis app --install         # (re)make the icon and the Desktop/Start-menu shortcuts
jarvis app --autostart       # load it into the tray when you sign in (--no-autostart)
jarvis app --demo            # the window with a scripted demo: no mic, no models
jarvis app --uninstall       # remove the shortcuts
```

The shortcut runs `.venv\Scripts\jarvis-app.exe`, a windowless launcher
(`[project.gui-scripts]`); the window is the Edge WebView2 built into
Windows 11, through `pywebview`.

## Use

```bash
jarvis listen                # say "Hey Jarvis", wait for the chime, speak
jarvis listen --autostart    # also start it at Windows logon
jarvis listen --no-autostart
```

- **Interrupt by name:** say "Jarvis" (or "Hey Jarvis") while it's speaking or
  working on something: it stops (speech at once; a request after its
  current step). Say a new request in the same breath ("Jarvis, open
  WhatsApp instead") and that's what it does next; "Jarvis, stop" just stops.
  In a terminal, any key also stops a reply.
- **Quit:** say "stop listening" or press Ctrl-C.
- `jarvis chat --voice` is the push-to-talk version (press Enter, then speak);
  `jarvis chat --wake` is the same loop as `jarvis listen` with chat's defaults.

## What it can do

`jarvis listen` uses the **desktop agent** (needs `uv sync --extra agent`;
without it, the simpler `orchestrator` agent and the tools in the last table).
It can chain steps ("find the Minecraft server folder and open
server.properties"), see your screen, remember things, and set real Windows
alarms.

| Say | What happens |
|-----|------------|
| "Open Task Manager", "open the Minecraft server folder", "open Netflix" | `open`: apps, folders and files found by name (through the filesystem MCP server), websites, Downloads/Desktop/... |
| "Open YouTube and search for vision quiz", "play the Nord CE 6 Lite review", "open the first video" | `browser_search`, `play_youtube` (plays the top result, or the one you name) |
| "Play Dhurandhar songs", "play lofi music" | Any song or music request goes to the YouTube Music app, never the YouTube website, unless you say "on the YouTube website" or ask for a video, trailer or review |
| "Open YouTube Music and play Stay Alive", "play Believer on YouTube Music", "next song", "play the song that's paused" | `play_youtube` in your installed YouTube Music app (its own window, not a browser tab); next/previous/pause use the app's queue |
| "Play my anime music playlist", "play my liked songs" | Your own playlist, from the YouTube Music app's side panel (its Play button), not a YouTube search; an unknown name lists your playlists and asks which |
| "What's the weather in Hyderabad?", "best laptop under 60,000 for coding?" vs "show me the weather on Google" | a question gets `web_search` and an answer (prices and other numbers must come from the results); "search X on Amazon" opens Amazon's own search |
| "Open the Amazon page of the OnePlus Nord CE6 Lite", "open the first three websites" | `open_page`: the real pages (the product's Amazon/Flipkart page, the top results of your last search), not a Google search listing |
| "Close Chrome", "close all the YouTube windows", "switch to VS Code", "snap this left" | `window` |
| "Volume 30", "pause", "next song", "brightness 60", "lock the screen", "take a screenshot" | `system` |
| "Set an alarm for 6:30 tomorrow morning called gym", "medication alarm for 9 pm every monday and friday", "timer for 10 minutes", "remind me to call mom in 20 minutes", "stop the alarm", "snooze for 10 minutes" | `schedule`: real Windows alarms that speak (see below) |
| "What's on my screen?", "click the Subscribe button", "read this page" | `screen`: screenshot + the vision model |
| "Open chat with Daddy", "send hi to Daddy on WhatsApp", "any new WhatsApp messages?", "call Anand" | `whatsapp`: your signed-in WhatsApp Web/Desktop (sending and calling are confirmed) |
| "Write me a Hello World program in Python", "run it" | `code`: saves the file in `Desktop\Jarvis Code`, opens it in VS Code, runs it and reads you the real output |
| "Compare Nvidia's stock price with Google's", "what's the weather in Hyderabad?" | `web_search`, then an answer with the numbers it found |
| "Open my contacts list", "add Trisha to my contacts as Thrisha ECE2" | `contacts` (see below) |
| "Any new emails?", "email Ravi asking if he's free Sunday", "find the mail from the placement cell" | `gmail_*` (Gmail MCP, see below); without Gmail connected, `email` opens a filled-in browser draft |
| "Click the Subscribe button", "type hello in the search box" | `ui` (buttons, links, text boxes of the active window), `keyboard` |
| "Remember that my server uses port 25565", "what music do I like?" | `memory` |
| "Find my resume", "read it", "move it to Documents", "delete the old one" | `fs_*` tools ([MCP filesystem server](#files-the-mcp-filesystem-server)), `recycle` (Recycle Bin) |
| Anything else ("what's my IP address?") | `run_command` (PowerShell) |

Common one-step commands ("open X", volume, media, alarms, "press enter",
"new tab") skip the language model entirely and finish in well under a
second. Everything else goes through a LangGraph loop that plans and calls
the tools one step at a time.

**Follow-ups.** Jarvis keeps track of what it just did (the app it opened,
the chat it's in, the search it ran, the video playing, the program it
wrote, and what failed), so the next sentence can refer to it:

| After | You can say |
|-------|-------------|
| a chat opened | "tell him I'm on my way", "send hi to her", "did he reply?", "call him", "open his chat again" |
| a video started | "play the next one", "skip this video", "play the second one" (same search, no new lookup) |
| a search | "now compare it with Google's stock price", "search that on YouTube" |
| a program written | "run it", "save and run it and show me the output" |
| an app opened | "close it" |
| something failed | "it's Anand", "the second one", "T-H-R-I-S-H-A ECE2", "try again" |

In always-listening mode, anything you say within 45 seconds of a reply
counts as a follow-up even without "Jarvis" (90 seconds after Jarvis asked
something or failed). Outside a conversation, commands ("open…", "play…"),
requests with "please" / "can you", and questions about your screen or PC
are taken; other talk nearby is ignored.

**Long answers.** Replies are short unless you ask for details, a list, a
report or a comparison; then the screen shows a summary line and bullet
points (formatted), and the voice reads them as plain sentences: list
marks, bold, brackets and links are left out, "$4.01T" is "4.01 trillion
dollars", "₹29,200" is "29,200 rupees", "P/E", "vs", "~", "%" and "YoY" are
said as words. After about 80 seconds it stops with "The rest is on the
screen." Run-on sentences are cut at commas so the voice stays fast.

**Web questions.** A question ("what's", "which", "best", "compare",
"check") is answered from `web_search` (at most three searches per request)
without opening tabs; "open / show / search on <site>" opens the browser.
Each tool has a per-request limit, so a request can't loop through eight
searches.

**Checking its own work.** Key presses and clicks report which window came
up afterwards (e.g. "the window in front is now 'Save As'"), `screen check`
answers yes/no questions about the screen, and the reply can't claim
something worked when the last step failed or you said no. A price or score
in a reply has to come from a tool result: if the model quotes one from
memory, it's sent back once to look it up. Jarvis also refuses to lock,
shut down, sleep or close things you didn't ask for.

**Safety.** Shutting down, restarting, sleeping, force-closing a program,
deleting files (always to the Recycle Bin), emptying the Recycle Bin, and
any PowerShell command that isn't a read-only query are confirmed first:
Jarvis asks out loud and waits for "yes". Jarvis never overwrites text
unless you asked it to, and "open Notepad" starts a new tab rather than
typing into a restored one.

**Alarms** are Windows Task Scheduler tasks under `\OpenJarvis\`. When one
goes off, a Windows voice announces it ("Alarm. Gym. It's 6:30 AM.") and a
Windows alarm notification loops its sound with Snooze and Dismiss. They
ring even when Jarvis is closed and may wake the laptop from sleep; one the
laptop was off for leaves a "Missed alarm" notice instead. The features
follow [Windows Alarm Pro](https://github.com/ponsivs/Windows-Alarm-Pro),
as a command line instead of a GUI, so Jarvis and you use the same alarms:

```bash
jarvis alarm                                    # time, next alarm, all alarms
jarvis alarm add 6:30 am -l Gym -r weekdays     # once/daily/weekly/weekdays/weekends/monthly/yearly
jarvis alarm add 9 pm -l Tablets -r "mon,wed,fri" -c medication -v female
jarvis alarm add 10:00 -l Standup -c meeting -a 10    # 10-minute early warning
jarvis alarm add 7am --ring 60                  # ring 60 s (default: until dismissed)
jarvis alarm timer 15                           # timer; `jarvis alarm remind call mom --at 6pm`
jarvis alarm off gym  /  jarvis alarm on all    # keep it but don't ring
jarvis alarm stop  /  jarvis alarm snooze 10    # the ringing alarm
jarvis alarm cancel gym  /  jarvis alarm cancel all
jarvis alarm test -v female                     # hear a sample alarm now
jarvis alarm export alarms.csv  /  jarvis alarm import alarms.csv
jarvis alarm import                             # Windows Alarm Pro's own alarms.xml
jarvis alarm clock                              # live clock + countdown; jarvis alarm log
```

By voice: "set a medication alarm for 9 pm every monday and friday",
"wake me up at 6 on weekdays", "alarm for 8 am every month called rent",
"what alarms do I have?", "stop the alarm", "snooze for 10 minutes",
"turn off the gym alarm". `[desktop] alarm_voice` ("male", "female",
"none") and `alarm_advance_minutes` set the defaults.

**Memory** lives in `~/.openjarvis/desktop/memory.sqlite`: facts you asked
it to remember, places it had to search for (so "the Minecraft server
folder" is instant next time), and a log of recent tasks. Relevant entries
are recalled into each request automatically. `jarvis desktop memory`
lists them; `jarvis desktop memory forget <id>` removes one. The day's
conversation (with tool results) is kept in `threads.sqlite` next to it, so
follow-ups work across restarts; `/clear` starts fresh.

**WhatsApp** is driven through the WhatsApp Web (or Desktop) window you're
already signed into, using the same accessibility tree a screen reader
reads: chats are found by name, and a message is only typed after the
message box confirms it belongs to the right person ("Type a message to
Daddy"). No unofficial WhatsApp API, so no account risk. (Personal-account
WhatsApp MCP servers exist, but they all pair an unofficial client with
your number; you can mount one via `mcp_servers` if you accept that.)
Long or multi-line messages are pasted in one piece (typing a line break
would send each line separately). Calls click the chat's own voice/video
call button after you say yes; if WhatsApp shows no call button (some
browser versions), Jarvis says so instead of calling.

**Contacts.** Names get misheard ("Trisha" for "Thrisha ECE2", "Arnold" for
"Anand"). Jarvis matches chat names by sound and prefix, understands
spelled-out names ("T-H-R-I-S-H-A"), searches WhatsApp with shorter pieces
of the name, and when it still can't tell, offers its best guesses ("Did you
mean Thrisha ECE-2 or Trishul Reddy?") and waits for your answer instead of
opening a guess. A correction that doesn't sound like the name ("Arnold" →
Anand) is remembered for next time. A shared tag never makes two people
match: "Thrisha ECE-2" is not "Harsha ECE-2", and when you name one saved
contact Jarvis never opens a different one. Chats scrolled out of sight in
WhatsApp's list are scrolled into view before the click, and the search box
is emptied again afterwards.

The names live in `~/.openjarvis/contacts.txt` ("open my contacts list"
opens it in Notepad). Because matching goes by sound and spelling, you never
list how you say a name; `[nicknames]` is only for names that sound nothing
alike:

```text
[nicknames]
Mom = Amma
Bro = Sai Teja (Tammudu)

[contacts]
# 135 names from contacts.csv, imported 15 Nov 2026. Importing again replaces this list.
Anand ECE-2
Daddy
Thrisha ECE-2
...
```

Your phone's contact names are the names WhatsApp shows, so the quickest
start is importing them: export from [Google Contacts](https://contacts.google.com)
(Export → Google CSV, or vCard) and run

```bash
jarvis desktop contacts import "%USERPROFILE%\Downloads\contacts.csv"
jarvis desktop contacts find Krishna        # how a spoken name resolves
```

Only names (and nicknames) are copied, not phone numbers. Importing again
replaces the `[contacts]` list and keeps your `[nicknames]`. When a name fits
several contacts equally ("Krishna" → Krishna IT-1 Jr, Krishna Kaushik,
Krishna Sameer), Jarvis asks which; answer "the second one" or the name.

**Programs.** "Write me a … program" saves a real file in
`Desktop\Jarvis Code` (`[desktop] code_dir` to change it) and opens it in VS
Code, instead of typing into whatever editor has focus; "run it" runs it
with Python / Node / PowerShell / cmd and reads back the actual output or
error. Programs in that folder run without asking; running a file anywhere
else is confirmed first.

**Gmail (MCP).** `jarvis-gmail` is a FastMCP server on the official Gmail
API: `gmail_unread`, `gmail_search`, `gmail_read`, `gmail_draft`,
`gmail_send`. Say a name instead of an address and it finds the address in
your own mail; before sending, Jarvis reads the real address back and waits
for "yes". One-time setup with your own Google OAuth client:

1. In [Google Cloud Console](https://console.cloud.google.com/) create a
   project and enable the **Gmail API**.
2. *OAuth consent screen*: External; add your Gmail address as a test user.
3. *Credentials → Create credentials → OAuth client ID → Desktop app*;
   download the JSON.
4. `jarvis desktop gmail-auth --credentials path\to\client_secret.json`
   and approve in the browser. Restart `jarvis listen`.

While the consent screen is in *Testing*, Google expires the sign-in after
7 days; run step 4 again, or publish the app (it stays private to you).
`jarvis desktop gmail-mcp` serves just these tools to other MCP apps.

**MCP.** The PC tools are a FastMCP server. Other MCP apps (Claude Desktop,
Cursor) can use them:

```json
{"mcpServers": {"jarvis": {"command": "C:\path\to\.venv\Scripts\jarvis.exe",
                           "args": ["desktop", "mcp"]}}}
```

and Jarvis can use other MCP servers: put them in `[desktop] mcp_servers`
(or `[tools.mcp] servers`), e.g.
`mcp_servers = '{"gh": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}}'`.
Their tools appear next to the built-in ones, named `<name>_<tool>`.

### Files: the MCP filesystem server

Jarvis's file tools are the official
[MCP filesystem server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem)
(`@modelcontextprotocol/server-filesystem`, pinned at 2.2.0), mounted as
`fs_<tool>`. On first start Jarvis installs it with npm into
`~/.openjarvis/mcp/server-filesystem`, so it needs
[Node.js](https://nodejs.org) 24 or newer. `jarvis desktop files` shows the
version and folders, and `jarvis desktop files --install` reinstalls it.

It can only reach your Desktop, Documents, Downloads, Pictures, Music and
Videos (and their OneDrive copies). To allow more folders, list them under
`[desktop] file_folders = "D:\\Games;E:\\Work"`. Anything outside them is
refused.

| Say | Tool |
|-----|------|
| "What's in my Downloads?" | `fs_list_directory` |
| "Find my resume" | `fs_search_files` |
| "Read / summarise the CC notes PDF" | `fs_read_text_file` (PDF and Office too) |
| "Make a file todo.txt on my desktop saying …" | `fs_write_file` |
| "Add 'buy milk' to todo.txt" | `fs_append_file` / `fs_edit_file` |
| "Make a folder Projects in Documents" | `fs_create_directory` |
| "Move it to Documents", "rename it to old notes" | `fs_move_file` |
| "Copy it to the desktop" | `fs_copy_file` |
| "How big is it?" | `fs_get_file_info`, `fs_list_directory_with_sizes` |
| "Delete the old one" | `recycle` (the server has no delete; this goes to the Recycle Bin) |

Jarvis asks before moving, renaming, replacing a file's contents or changing
its text. New files, folders, copies and appends go ahead.

**Opening files by a rough name.** The server can't launch anything, so `open`
opens files in their app. It looks them up through the server first. You
don't need the exact file name: "open CC mid one notes", "open bite bite go"
or a misheard "CC mid-1 codes" find the file by spelling and by sound. Add
"in my desktop" (or documents, downloads, pictures, music, videos) to look
only there. Jarvis checks two levels of Desktop, Documents and Downloads
first, which is instant, and searches deeper by name only when that finds
nothing. When two files are equally close ("Mid-1 or Mid-2?"), it asks which
one and opens the one you say. It never clicks through Explorer to open a
file.

### Without the agent extra

The `orchestrator` agent uses these tools instead; `--tools a,b` picks your
own set for either agent.

| Say | Tool |
|-----|------|
| "Open VS Code", "open YouTube" | `pc_control` (Start Menu apps, Store apps, websites) |
| "Volume to 30", "mute", "next song", "pause" | `pc_control` (volume and media keys) |
| "Brightness 60", "how much battery is left?" | `pc_control` |
| "Lock my laptop" | `lock_screen` (asks you to confirm with `y`) |
| "Summarize what I copied" | `clipboard` |
| "Remind me in 20 minutes to stretch", "what reminders do I have?" | `set_reminder`, `list_reminders`, `cancel_reminder` |

Those reminders are stored in `~/.openjarvis/reminders.json` and are
delivered by a running `jarvis listen` / `jarvis chat` (one that came due
while Jarvis wasn't running fires the next time it starts).

## Speech recognition

Speech-to-text is [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
With an NVIDIA GPU, run it there: voice mode's CUDA PyTorch already ships the
cuBLAS/cuDNN libraries it needs (Jarvis finds them in `torch\lib`, or in
`nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels). On 200 noisy spoken
commands in five voices (two Indian-English-like), on an RTX 5050 laptop on
battery:

| Model, device | Commands fully right | Time per command |
|---------------|---------------------:|-----------------:|
| `small.en`, GPU `int8_float16` (recommended) | 95% | 0.4 s |
| `medium.en`, GPU | 94% | 0.9 s |
| `large-v3-turbo`, GPU | 91% | 0.6 s |
| `distil-large-v3.5`, GPU | 82% | 0.6 s |
| `base.en`, CPU `int8` | 91% | 0.9 s |

Bigger models did not help on these clips (the large ones also run words
together, "OpenAsk Manager"), but they can on a strong accent: try
`medium.en` or `large-v3-turbo` if `small.en` keeps mishearing you. The
first transcription after start compiles GPU kernels (8 s, cached after
the first run ever); `jarvis listen` does that during its warm-up.

Whisper is given the names people say to an assistant (VS Code, YouTube,
WhatsApp, Task Manager, ...) as a hint, so it writes "VS Code" instead of
"V's code". Mishearings every model makes are fixed afterwards ("VS core",
"vase code", "BS code" → VS Code; "set them along for 11 43 p.m." → "set an
alarm for 11:43 pm"; "snoose" → snooze), and its phantom phrases on
background noise ("Thanks for watching", "See you in the next one") are
dropped. Add your own names, people or games:

```toml
[speech]
vocabulary = "Valorant, OBS Studio, Priya"
```

**Your contacts' names.** Whisper also gets the names of the people in your
contacts list as hints, the way you say them ("Anand", not "Anand ECE-2"),
starred contacts and recent chats first (`contact_hints = false` turns this
off). Names it still splits or garbles in a sentence about chats, calls or
messages are repaired to the closest contact: "open on and in WhatsApp" →
"open Anand in WhatsApp", "open Anand.jar" → "open Anand chat".

**Testing on your own voice.** The table above used synthetic voices; your
accent and your names are what matter. Record yourself once and let Jarvis
compare the models on those recordings:

```bash
jarvis voice enroll              # read ~24 short commands aloud, ~3 minutes
jarvis voice evaluate            # words right, names right, speed per model
jarvis voice evaluate --apply    # ...and switch to the best one
```

The recordings stay in `~/.openjarvis/voice/samples`; they're also the data a
fine-tune of the speech model on your voice would start from.

## Settings

```toml
[speech]
model = "small.en"         # Whisper size (see Speech recognition); base.en on a CPU-only PC
device = "cuda"            # "cpu" without an NVIDIA GPU (CUDA errors fall back to CPU)
compute_type = "int8_float16"   # "int8" on the CPU
tts_backend = "chatterbox"  # or "kokoro" (voice-kokoro extra), "openai_tts", ...
voice_id = "jarvis"        # ~/.openjarvis/voices/jarvis.wav; "default"; or a clip path
silence_threshold = 0      # 0 = measure room noise at start; or pin a mic level
wake_word = "hey_jarvis"   # or "alexa", "hey_mycroft", or a path to a custom .onnx
wake_word_threshold = 0.5  # raise if it wakes by itself, lower if it misses you
wake_word_barge_in = true
end_silence_seconds = 0.9  # pause that ends your sentence (0 = default 1.5 s)
follow_up_seconds = 6      # after a reply, keep listening this long without the wake word

[desktop]
fast_path = true           # simple commands without the language model
vision = true              # screen understanding (qwen3.5 reads images)
vision_model = ""          # or a separate Ollama vision model
max_steps = 12             # tool steps per request
history_messages = 32      # conversation messages the model sees
mcp_servers = ""           # extra MCP servers (JSON), see above
code_dir = ""              # where written programs go ("" = Desktop\Jarvis Code)
plan = false               # experimental: plan multi-part requests first (extra model call)
alarm_voice = "male"       # voice announcing alarms: male, female, none
alarm_advance_minutes = 0  # default early warning before alarms

[engine.ollama]
keep_alive = "30m"         # keep the model loaded between commands ("-1" = forever)

[power]
battery_saver = true       # on battery: speech on CPU (base.en), and switch model ...
battery_model = "qwen3.5:2b"   # ... to this one (empty = keep the normal model)
# false keeps speech on the GPU on battery; the LLM uses the GPU anyway.
low_vram_warning_mb = 1024
```

Voice mode already asks Ollama to keep the model for 30 minutes when
`keep_alive` is unset, and warms it up at start, so the first command doesn't
wait for a model load.

## Troubleshooting

- **"Wake word unavailable"** — install the extra: `uv sync --extra wakeword`.
- **It hears you but never answers aloud** — no TTS backend:
  `uv sync --extra voice`, or set `OPENAI_API_KEY` / `ELEVENLABS_API_KEY`.
- **The voice model download stalls at 0 %** — Hugging Face's xet transfer
  can hang on some networks; run once with `$env:HF_HUB_DISABLE_XET = '1'`.
- **"Fast speech decoding failed … using stock"** in the log — the CUDA-graph
  decoder hit something unexpected (e.g. out of GPU memory) and Chatterbox
  continued with its normal, slower decoder; restart `jarvis listen` to retry.
- **Recording never stops in a noisy room / cuts you off** — set
  `silence_threshold` explicitly (the calibration line printed at start shows
  the measured background level; use about 3× that).
- **"DLL load failed … filename or extension is too long"** — the install path
  is over Windows' 260-character limit for native libraries. Install into a
  shorter folder such as `C:\Users\<you>\OpenJarvis`.
