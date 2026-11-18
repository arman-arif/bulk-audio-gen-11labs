#!/usr/bin/env python3
"""Generate audio files with ElevenLabs from AUDIO_FILES.txt.

Each non-empty line of the input file has the form:
    <output file path> | <language code> | <text>

Usage:
    python3 generate_audio.py                 # generate all missing files
    python3 generate_audio.py --force         # regenerate even if files exist
    python3 generate_audio.py --limit 3       # only the first 3 entries (testing)
    python3 generate_audio.py --dry-run       # list what would be generated

The API key is read from ELEVENLABS_API_KEY (environment or .env.local).
Only the Python standard library is required.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# Built-in defaults. Override them without editing code by setting
# ELEVENLABS_VOICE_ID / ELEVENLABS_MODEL_ID in .env.local or the environment.
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah (Arfa: 5WbDBNLwAg4h26Km0GgQ, needs a paid plan)
DEFAULT_MODEL_ID = "eleven_v4"
OUTPUT_FORMAT = "mp3_44100_128"
API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
MAX_RETRIES = 5


def load_env(path: Path) -> None:
    """Minimal .env loader; real environment variables take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_env(BASE_DIR / ".env.local")
VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE_ID
MODEL_ID = os.environ.get("ELEVENLABS_MODEL_ID") or DEFAULT_MODEL_ID


def parse_text(text: str):
    """Parse "path | lang | text" lines. Returns (entries, problems)."""
    entries, problems = [], []
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        parts = [p.strip() for p in raw.split("|", 2)]
        if len(parts) != 3 or not all(parts):
            problems.append(f"line {lineno}: expected 'path | lang | text'")
            continue
        err = check_output_path(parts[0])
        if err:
            problems.append(f"line {lineno}: {err}")
            continue
        entries.append((lineno, *parts))
    return entries, problems


def parse_entries(path: Path):
    entries, problems = parse_text(path.read_text(encoding="utf-8"))
    for msg in problems:
        print(f"warning: skipping {msg}", file=sys.stderr)
    return entries


def check_output_path(rel_path: str):
    """Return an error string if rel_path is not a safe .mp3 target, else None."""
    out = (BASE_DIR / rel_path).resolve()
    if BASE_DIR not in out.parents:
        return f"output path escapes project directory: {rel_path}"
    if out.suffix.lower() != ".mp3":
        return f"output file must end in .mp3: {rel_path}"
    if any(part.startswith(".") for part in out.relative_to(BASE_DIR).parts):
        return f"hidden paths are not allowed: {rel_path}"
    return None


def synthesize(api_key: str, text: str, language: str, voice_id: str = VOICE_ID) -> bytes:
    body = json.dumps(
        {"text": text, "model_id": MODEL_ID, "language_code": language}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{API_URL}/{voice_id}?output_format={OUTPUT_FORMAT}",
        data=body,
        method="POST",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
    )
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            retryable = e.code in (429, 500, 502, 503, 504)
            if not retryable or attempt == MAX_RETRIES:
                raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"network error: {e}") from None
        time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def process(api_key, entry, force, delay, voice_id=VOICE_ID):
    lineno, rel_path, language, text = entry
    err = check_output_path(rel_path)
    if err:
        raise RuntimeError(err)
    out = (BASE_DIR / rel_path).resolve()
    if out.exists() and out.stat().st_size > 0 and not force:
        return rel_path, "skipped (exists)"
    audio = synthesize(api_key, text, language, voice_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".part")
    tmp.write_bytes(audio)
    tmp.replace(out)
    time.sleep(delay)  # gap before the next generation
    return rel_path, f"ok ({len(audio)} bytes)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=str(BASE_DIR / "AUDIO_FILES.txt"))
    ap.add_argument("--force", action="store_true", help="regenerate existing files")
    ap.add_argument("--limit", type=int, help="only process the first N entries")
    ap.add_argument("--workers", type=int, default=1, help="parallel requests (default 1, i.e. sequential)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds to wait after each generation (default 0.5)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    load_env(BASE_DIR / ".env.local")
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key and not args.dry_run:
        print("error: ELEVENLABS_API_KEY not found in environment or .env.local", file=sys.stderr)
        return 1

    entries = parse_entries(Path(args.input))
    if args.limit:
        entries = entries[: args.limit]
    print(f"{len(entries)} entries, voice={VOICE_ID}, model={MODEL_ID}")

    if args.dry_run:
        for _, path, lang, text in entries:
            print(f"{path} [{lang}] {text}")
        return 0

    failures = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(process, api_key, e, args.force, args.delay): e for e in entries}
        for done, fut in enumerate(as_completed(futures), 1):
            entry = futures[fut]
            try:
                path, status = fut.result()
                print(f"[{done}/{len(entries)}] {path}: {status}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"[{done}/{len(entries)}] {entry[1]}: FAILED - {exc}", file=sys.stderr)

    print(f"done, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
