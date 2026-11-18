# Audio generator (ElevenLabs)

`generate_audio.py` reads `AUDIO_FILES.txt` and creates an mp3 for each line using the ElevenLabs text-to-speech API.

## Requirements

- Python 3.8+ (standard library only, nothing to install)
- An ElevenLabs API key

## Setup

Create `.env.local` in this folder:

```
ELEVENLABS_API_KEY=your_key_here
```

A real environment variable `ELEVENLABS_API_KEY` takes precedence over the file. Keep `.env.local` out of version control.

## Input format

`AUDIO_FILES.txt` has one entry per line, three fields separated by ` | `:

```
<output file path> | <language code> | <text>
```

Example:

```
assets/audio/en/apple.mp3 | en | Apple
assets/audio/bn/apple.mp3 | bn | আপেল
```

- Output paths are relative to this folder. Missing folders are created.
- The language code (`en`, `bn`, ...) is sent to ElevenLabs as `language_code`.
- Blank lines are ignored. Malformed lines are skipped with a warning.

## Usage

```
python3 generate_audio.py                 # generate all missing files
python3 generate_audio.py --dry-run       # list entries without calling the API
python3 generate_audio.py --limit 3       # only the first 3 entries (good for testing)
python3 generate_audio.py --force         # regenerate files that already exist
python3 generate_audio.py --delay 1       # wait 1 second between generations
python3 generate_audio.py --input other.txt
```

| Option | Default | Description |
|---|---|---|
| `--input` | `AUDIO_FILES.txt` | Input file |
| `--force` | off | Regenerate existing files |
| `--limit N` | all | Only process the first N entries |
| `--delay S` | `0.5` | Seconds to wait after each generation |
| `--workers N` | `1` | Parallel requests (1 = one at a time) |
| `--dry-run` | off | Print entries, make no API calls |

## Behaviour

- **Existing files are skipped.** If the output file exists and isn't empty, no API call is made and no credits are used. Re-running after an interruption only generates what's missing.
- **Safe writes.** Audio is written to a `.part` file and renamed when complete, so a crash never leaves a half-written mp3.
- **Retries.** Rate limits (429) and server errors (5xx) are retried up to 5 times with increasing waits.
- **Exit code.** Failures are printed and the script exits with code 1 if any entry failed.

## Configuration

Voice and model can be changed in `.env.local` (or the environment) without editing code:

```
ELEVENLABS_VOICE_ID=5WbDBNLwAg4h26Km0GgQ
ELEVENLABS_MODEL_ID=eleven_v3
```

Both are optional. If unset, the built-in defaults in `generate_audio.py` are used: voice Sarah (`EXAVITQu4vr4xnSDxMaL`) and model `eleven_v4`. See `.env.local.example`.

`OUTPUT_FORMAT` (`mp3_44100_128`) is set at the top of `generate_audio.py`.

## Free Voices

| Voice | ID | Notes |
|---|---|---|
| Sarah | `EXAVITQu4vr4xnSDxMaL` | Female, clear and confident. Verified for Hindi, which is the closest language. |
| Alice | `Xb7hH8MSUJpSbSDYk0k2` | Female, an educator style that suits kids' vocabulary words. Verified for Hindi. |
| Jessica | `cgSgspJ2msm6clMCkdW9` | Female, playful and warm. Verified for Hindi. |
| Brian | `nPczCjzI2devNBz1zQrb` | Male, calm. Verified for Hindi. |


## Web interface

```
python3 web_app.py             # then open http://127.0.0.1:8000
python3 web_app.py --port 9000
```

- Lists every entry from `AUDIO_FILES.txt` with its status (missing / exists / failed); filter by language, status or text.
- Play existing audio in the browser.
- Generate all missing files (in the current filter), only the selected rows, or regenerate with "Regenerate existing files". Live progress, and a Stop button.
- Pick any voice on your account from a dropdown, and set the delay between generations.
- Uses the same logic as the command line script, so existing files are skipped the same way.

The server only listens on 127.0.0.1 and the API key stays on the server. Only one job runs at a time.

## Troubleshooting

- **HTTP 402 `paid_plan_required`**: free plans can't use library voices through the API. Use a premade voice, or upgrade your plan.
- **HTTP 401**: the API key is missing or wrong.
- **Mixed voices**: if you change `VOICE_ID` after generating some files, run with `--force` to regenerate them all in the new voice.
