"""``jarvis voice`` - test speech recognition on *your* voice.

Whisper models are compared on generic voices; how they do on your accent
and the names you say (Indian names, your contacts) can be quite different.

    jarvis voice enroll      # read ~24 short commands aloud (about 3 minutes)
    jarvis voice evaluate    # try each Whisper model on them, pick the best

The recordings stay in ``~/.openjarvis/voice/samples`` and are also the
data a later fine-tune of the speech model would need.
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import click

DEFAULT_MODELS = ("small.en", "medium.en", "large-v3-turbo", "distil-large-v3.5")
_COMMANDS = (
    "Open VS Code",
    "Open the Minecraft server folder",
    "Set an alarm for six thirty tomorrow morning",
    "Play the next song",
    "Open YouTube Music and play Stay Alive",
    "What's the weather in Hyderabad today",
    "Compare Nvidia's stock price with Google's",
    "Take a screenshot",
    "Turn the volume down",
    "Open Amazon and search for phones under thirty thousand",
)
_PERSON_TEMPLATES = (
    "Open {name}'s chat on WhatsApp",
    "Send hi to {name} on WhatsApp",
    "Call {name}",
    "Did {name} reply",
    "Tell {name} I'm on my way",
    "Open chat with {name}",
)


def samples_dir() -> Path:
    from openjarvis.core.paths import get_config_dir

    return get_config_dir() / "voice" / "samples"


def _people(limit: int) -> List[str]:
    """Contacts to practise with: starred and recent chats first."""
    try:
        from openjarvis.desktop.contacts import directory

        names = directory().hint_names(limit_chars=2000)
    except Exception:
        names = []
    names = [n for n in names if n[:1].isupper()]
    head, tail = names[:8], names[8:]
    random.shuffle(tail)
    return (head + tail)[:limit]


def build_phrases(count: int, people: List[str]) -> List[Dict[str, str]]:
    """Commands to read: about two thirds with a contact's name in them."""
    phrases: List[Dict[str, str]] = []
    with_names = max(0, min(len(people), count - len(_COMMANDS) // 2))
    for i, name in enumerate(people[:with_names]):
        template = _PERSON_TEMPLATES[i % len(_PERSON_TEMPLATES)]
        phrases.append({"text": template.format(name=name), "name": name})
    for command in _COMMANDS:
        if len(phrases) >= count:
            break
        phrases.append({"text": command, "name": ""})
    return phrases[:count]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _words(text: str) -> List[str]:
    text = text.lower().replace("'s", "s")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    numbers = {
        "six": "6",
        "thirty": "30",
        "thousand": "000",
        "one": "1",
        "two": "2",
    }
    return [numbers.get(w, w) for w in text.split()]


def word_errors(reference: str, hypothesis: str) -> Tuple[int, int]:
    """(edit distance in words, reference length)."""
    ref, hyp = _words(reference), _words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        previous, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            current = min(row[j] + 1, row[j - 1] + 1, previous + (r != h))
            previous, row[j] = row[j], current
    return row[-1], max(len(ref), 1)


def name_heard(name: str, hypothesis: str) -> bool:
    if not name:
        return True
    wanted = re.sub(r"[^a-z]", "", name.lower())
    heard = re.sub(r"[^a-z]", "", hypothesis.lower())
    return wanted in heard


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@click.group("voice", help="Test speech recognition on your own voice.")
def voice() -> None:
    pass


@voice.command("enroll")
@click.option("--count", default=24, show_default=True, help="Phrases to record.")
def enroll(count: int) -> None:
    """Read short commands aloud so models can be tested on your voice."""
    from openjarvis.cli._voice_chat import VoiceSession
    from openjarvis.speech import voice_io

    folder = samples_dir()
    folder.mkdir(parents=True, exist_ok=True)
    index = folder / "samples.jsonl"
    phrases = build_phrases(count, _people(count))
    session = VoiceSession()
    click.echo("Measuring background noise - stay quiet a moment...")
    session.calibrate_microphone()
    click.echo(
        f"\nRead each line naturally, the way you'd talk to Jarvis. "
        f"{len(phrases)} lines; press Enter before each one (Ctrl-C stops).\n"
    )
    start = len(index.read_text(encoding="utf-8").splitlines()) if index.exists() else 0
    saved = 0
    try:
        for number, phrase in enumerate(phrases, 1):
            click.prompt(
                f"[{number}/{len(phrases)}]  Say:  {phrase['text']}\n  Enter to start",
                default="",
                show_default=False,
            )
            audio = voice_io.record_until_silence(
                **session.record_kwargs(require_speech=True, max_seconds=10)
            )
            if not audio:
                click.echo("  (nothing heard, skipped)")
                continue
            name = f"{start + saved + 1:04d}.wav"
            (folder / name).write_bytes(audio)
            with index.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"audio": name, **phrase}) + "\n")
            saved += 1
    except (KeyboardInterrupt, click.Abort):
        click.echo("\nStopped.")
    click.echo(f"\nSaved {saved} recordings in {folder}. Next: jarvis voice evaluate")


def _load_samples(folder: Path) -> List[Dict[str, Any]]:
    index = folder / "samples.jsonl"
    rows = []
    try:
        lines = index.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (folder / str(row.get("audio", ""))).is_file():
            rows.append(row)
    return rows


def evaluate_model(
    model: str,
    samples: List[Dict[str, Any]],
    folder: Path,
    *,
    device: str,
    compute_type: str,
    hints: str,
    correct: Any,
) -> Dict[str, Any]:
    from openjarvis.speech.faster_whisper import FasterWhisperBackend

    backend = FasterWhisperBackend(model, device=device, compute_type=compute_type)
    first = (folder / samples[0]["audio"]).read_bytes()
    backend.transcribe(first, format="wav", language="en")  # load + warm up
    errors = words = names_ok = names_total = 0
    seconds = 0.0
    misses: List[Tuple[str, str]] = []
    for row in samples:
        audio = (folder / row["audio"]).read_bytes()
        started = time.perf_counter()
        result = backend.transcribe(
            audio,
            format="wav",
            language="en",
            hotwords=hints or None,
            short_utterance=True,
        )
        seconds += time.perf_counter() - started
        heard = correct(result.text or "")
        e, n = word_errors(row["text"], heard)
        errors, words = errors + e, words + n
        if row.get("name"):
            names_total += 1
            if name_heard(row["name"], heard):
                names_ok += 1
            elif len(misses) < 4:
                misses.append((row["text"], heard))
    del backend
    return {
        "model": model,
        "wer": errors / max(words, 1),
        "names": f"{names_ok}/{names_total}" if names_total else "-",
        "names_rate": names_ok / names_total if names_total else 1.0,
        "seconds": seconds / max(len(samples), 1),
        "misses": misses,
    }


def set_speech_model(config_path: Path, model: str) -> None:
    """Change ``[speech] model`` in config.toml, keeping comments."""
    text = config_path.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^\[speech\]\s*$(.*?)(?=^\[|\Z)", text)
    if section is None:
        text = text.rstrip("\n") + f'\n\n[speech]\nmodel = "{model}"\n'
    else:
        body = section.group(1)
        line = re.compile(r'(?m)^model\s*=\s*"[^"]*"')
        new_body = (
            line.sub(f'model = "{model}"', body, count=1)
            if line.search(body)
            else f'\nmodel = "{model}"' + body
        )
        text = text[: section.start(1)] + new_body + text[section.end(1) :]
    config_path.write_text(text, encoding="utf-8")


@voice.command("evaluate")
@click.option(
    "--models",
    default=",".join(DEFAULT_MODELS),
    show_default=True,
    help="Comma-separated Whisper models to compare.",
)
@click.option("--device", default="", help="cuda or cpu (default: from config).")
@click.option(
    "--apply/--no-apply", default=False, help="Use the best model from now on."
)
def evaluate(models: str, device: str, apply: bool) -> None:
    """Compare Whisper models on your recordings (accuracy, names, speed)."""
    from openjarvis.cli._voice_chat import VoiceSession
    from openjarvis.core.config import load_config
    from openjarvis.core.paths import get_config_dir

    folder = samples_dir()
    samples = _load_samples(folder)
    if len(samples) < 5:
        raise click.ClickException(
            "Record yourself first (at least 5 lines): jarvis voice enroll"
        )
    config = load_config()
    device = device or config.speech.device or "auto"
    compute = config.speech.compute_type or "int8"
    if device == "cpu" and "float16" in compute:
        compute = "int8"
    session = VoiceSession(config)
    from types import SimpleNamespace

    hints = session.stt_options(SimpleNamespace(backend_id="faster-whisper")).get(
        "hotwords", ""
    )
    click.echo(f"{len(samples)} recordings, {device}, with your names as hints.\n")
    results: List[Dict[str, Any]] = []
    for model in [m.strip() for m in models.split(",") if m.strip()]:
        click.echo(f"  {model} ...", nl=False)
        try:
            row = evaluate_model(
                model,
                samples,
                folder,
                device=device,
                compute_type=compute,
                hints=hints,
                correct=session.correct,
            )
        except Exception as exc:
            click.echo(f" failed: {exc}")
            continue
        results.append(row)
        click.echo(
            f" words right {1 - row['wer']:.0%}, names {row['names']}, "
            f"{row['seconds']:.2f} s each"
        )
    if not results:
        raise click.ClickException("No model could be tested.")
    best = min(results, key=lambda r: (r["wer"] - 0.5 * r["names_rate"], r["seconds"]))
    current = config.speech.model
    click.echo(f"\nBest on your voice: {best['model']} (now using {current}).")
    for said, heard in best["misses"]:
        click.echo(f"  missed: said {said!r}, heard {heard!r}")
    if apply and best["model"] != current:
        set_speech_model(get_config_dir() / "config.toml", best["model"])
        click.echo(f'Set [speech] model = "{best["model"]}". Restart jarvis listen.')
    elif best["model"] != current:
        click.echo("Run again with --apply to switch to it.")


__all__ = ["build_phrases", "name_heard", "set_speech_model", "voice", "word_errors"]
