#!/usr/bin/env python3
"""Local web interface for generate_audio.py.

    python3 web_app.py            # then open http://127.0.0.1:8000
    python3 web_app.py --port 9000

Standard library only. Binds to 127.0.0.1 so it is only reachable from this
machine. The API key never leaves the server (read from .env.local).
"""

import argparse
import json
import mimetypes
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import generate_audio as ga

STATIC_DIR = ga.BASE_DIR / "web"
INPUT_FILE = ga.BASE_DIR / "AUDIO_FILES.txt"


# --------------------------------------------------------------------------
# Data helpers
# --------------------------------------------------------------------------
# Optional pasted input; when set it is used instead of the source file.
# All of this is held in memory only: restarting the server goes back to
# AUDIO_FILES.txt and the project folder.
INPUT = {"pasted": None}
CONFIG = {"source_file": INPUT_FILE, "output_root": ga.BASE_DIR}
MAX_PASTE_BYTES = 1_000_000


def source_entries():
    root = CONFIG["output_root"]
    if INPUT["pasted"] is not None:
        return ga.parse_text(INPUT["pasted"], root)[0]
    return ga.parse_entries(CONFIG["source_file"], root)


def resolve_user_path(raw):
    """Turn user-typed text into an absolute Path (~ and relative paths allowed)."""
    p = Path(str(raw).strip()).expanduser()
    if not p.is_absolute():
        p = ga.BASE_DIR / p
    return p.resolve()


def browse(raw, show_files):
    d = resolve_user_path(raw) if raw else ga.BASE_DIR
    if not d.is_dir():
        raise ValueError("not a folder: " + str(d))
    dirs, files = [], []
    try:
        for child in sorted(d.iterdir(), key=lambda c: c.name.lower()):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                dirs.append(child.name)
            elif show_files and child.suffix.lower() == ".txt":
                files.append(child.name)
    except PermissionError:
        raise ValueError("permission denied: " + str(d)) from None
    parent = str(d.parent) if d.parent != d else None
    return {"path": str(d), "parent": parent, "dirs": dirs, "files": files}


def load_entries():
    """Current entries (file re-read on every call so edits show up)."""
    out = []
    root = CONFIG["output_root"]
    for lineno, path, lang, text in source_entries():
        full = (root / path).resolve()
        exists = full.is_file() and full.stat().st_size > 0
        out.append(
            {
                "path": path,
                "lang": lang,
                "text": text,
                "exists": exists,
                "size": full.stat().st_size if exists else 0,
                "mtime": int(full.stat().st_mtime) if exists else 0,
            }
        )
    return out


def fetch_voices(api_key):
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": api_key}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    voices = [
        {"id": v["voice_id"], "name": v["name"], "category": v.get("category", "")}
        for v in data.get("voices", [])
    ]
    voices.sort(key=lambda v: (v["category"] != "premade", v["name"].lower()))
    return voices


# --------------------------------------------------------------------------
# Background job (one at a time)
# --------------------------------------------------------------------------
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.running = False
        self.stop = False
        self.total = 0
        self.done = 0
        self.current = None
        self.results = {}  # path -> {"status": "ok"|"skipped"|"failed", "message": str}
        self.started = None
        self.finished = None

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "stopping": self.running and self.stop,
                "total": self.total,
                "done": self.done,
                "current": self.current,
                "results": dict(self.results),
                "started": self.started,
                "finished": self.finished,
            }


JOB = Job()


def run_job(api_key, entries, force, delay, voice_id, root):
    try:
        for e in entries:
            with JOB.lock:
                if JOB.stop:
                    break
                JOB.current = e["path"]
            try:
                _, status = ga.process(
                    api_key,
                    (0, e["path"], e["lang"], e["text"]),
                    force,
                    delay,
                    voice_id,
                    root,
                )
                result = {
                    "status": "skipped" if status.startswith("skipped") else "ok",
                    "message": status,
                }
            except Exception as exc:  # noqa: BLE001
                result = {"status": "failed", "message": str(exc)}
            with JOB.lock:
                JOB.results[e["path"]] = result
                JOB.done += 1
    finally:
        with JOB.lock:
            JOB.running = False
            JOB.current = None
            JOB.finished = time.time()


def start_job(paths, force, delay, voice_id):
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        return None, "ELEVENLABS_API_KEY not found in environment or .env.local"
    wanted = set(paths)
    entries = [e for e in load_entries() if e["path"] in wanted]
    if not entries:
        return None, "no matching entries"
    with JOB.lock:
        if JOB.running:
            return None, "a job is already running"
        JOB.reset()
        JOB.running = True
        JOB.total = len(entries)
        JOB.started = time.time()
    threading.Thread(
        target=run_job, args=(api_key, entries, force, delay, voice_id, CONFIG["output_root"]), daemon=True
    ).start()
    return len(entries), None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "AudioGen/1.0"

    def log_message(self, fmt, *args):  # quieter logs
        if "/api/status" not in (args[0] if args else ""):
            super().log_message(fmt, *args)

    # -- helpers --
    def _host_ok(self):
        # Reject DNS-rebinding style requests: only accept local Host headers.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype=None):
        data = path.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type", ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- routes --
    def do_GET(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        route, _, query = self.path.partition("?")

        if route in ("/", "/index.html"):
            return self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")

        if route == "/api/config":
            return self._json(
                {
                    "voice_id": ga.VOICE_ID,
                    "model_id": ga.MODEL_ID,
                    "delay": 0.5,
                    "has_key": bool(os.environ.get("ELEVENLABS_API_KEY")),
                }
            )

        if route == "/api/entries":
            try:
                return self._json(
                    {
                        "entries": load_entries(),
                        "source": "pasted" if INPUT["pasted"] is not None else "file",
                        "pasted": INPUT["pasted"],
                        "source_file": str(CONFIG["source_file"]),
                        "output_root": str(CONFIG["output_root"]),
                        "output_exists": CONFIG["output_root"].is_dir(),
                    }
                )
            except (OSError, ValueError) as exc:
                return self._json({"error": str(exc)}, 500)

        if route == "/api/voices":
            key = os.environ.get("ELEVENLABS_API_KEY")
            if not key:
                return self._json({"voices": []})
            try:
                return self._json({"voices": fetch_voices(key)})
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                return self._json({"voices": [], "error": str(exc)})

        if route == "/api/browse":
            q = urllib.parse.parse_qs(query)
            try:
                return self._json(
                    browse((q.get("path") or [""])[0], (q.get("mode") or [""])[0] == "file")
                )
            except ValueError as exc:
                return self._json({"error": str(exc)}, 400)

        if route == "/api/status":
            return self._json(JOB.snapshot())

        if route.startswith("/audio/"):
            rel = urllib.request.url2pathname(route[len("/audio/"):])
            # Only serve files that are listed in the current entries.
            if rel not in {e["path"] for e in load_entries()}:
                return self._json({"error": "not found"}, 404)
            full = (CONFIG["output_root"] / rel).resolve()
            if not full.is_file():
                return self._json({"error": "not found"}, 404)
            return self._file(full, "audio/mpeg")

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        # A JSON content type forces a CORS preflight for cross-site pages.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._json({"error": "expected application/json"}, 415)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_PASTE_BYTES + 10_000:
                return self._json({"error": "request too large"}, 413)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "invalid JSON"}, 400)
        if not isinstance(payload, dict):
            return self._json({"error": "expected a JSON object"}, 400)

        if self.path == "/api/generate":
            paths = payload.get("paths")
            if not isinstance(paths, list) or not paths:
                return self._json({"error": "paths must be a non-empty list"}, 400)
            try:
                delay = max(0.0, float(payload.get("delay", 0.5)))
            except (TypeError, ValueError):
                return self._json({"error": "invalid delay"}, 400)
            voice_id = str(payload.get("voice_id") or ga.VOICE_ID)
            if not voice_id.isalnum():
                return self._json({"error": "invalid voice_id"}, 400)
            count, err = start_job(paths, bool(payload.get("force")), delay, voice_id)
            if err:
                return self._json({"error": err}, 409 if "running" in err else 400)
            return self._json({"started": count})

        if self.path == "/api/input":
            text = payload.get("text")
            if text is None or (isinstance(text, str) and not text.strip()):
                if JOB.snapshot()["running"]:
                    return self._json({"error": "a job is running"}, 409)
                INPUT["pasted"] = None  # back to AUDIO_FILES.txt
                return self._json({"source": "file", "problems": []})
            if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_PASTE_BYTES:
                return self._json({"error": "text must be a string under 1 MB"}, 400)
            entries, problems = ga.parse_text(text, CONFIG["output_root"])
            if not entries:
                return self._json({"error": "no valid entries found", "problems": problems[:20]}, 400)
            if JOB.snapshot()["running"]:
                return self._json({"error": "a job is running"}, 409)
            INPUT["pasted"] = text
            return self._json({"source": "pasted", "count": len(entries), "problems": problems[:20]})

        if self.path == "/api/source":
            if JOB.snapshot()["running"]:
                return self._json({"error": "a job is running"}, 409)
            path = payload.get("path")
            if not isinstance(path, str) or not path.strip():
                return self._json({"error": "path is required"}, 400)
            f = resolve_user_path(path)
            if f.suffix.lower() != ".txt" or not f.is_file():
                return self._json({"error": "choose an existing .txt file"}, 400)
            try:
                entries, problems = ga.parse_text(f.read_text(encoding="utf-8"), CONFIG["output_root"])
            except (OSError, ValueError) as exc:
                return self._json({"error": f"cannot read file: {exc}"}, 400)
            if not entries:
                return self._json({"error": "no valid entries found in that file", "problems": problems[:20]}, 400)
            CONFIG["source_file"] = f
            INPUT["pasted"] = None
            return self._json({"count": len(entries), "problems": problems[:20]})

        if self.path == "/api/output":
            if JOB.snapshot()["running"]:
                return self._json({"error": "a job is running"}, 409)
            path = payload.get("path")
            if not isinstance(path, str) or not path.strip():
                return self._json({"error": "path is required"}, 400)
            d = resolve_user_path(path)
            if d.exists() and not d.is_dir():
                return self._json({"error": "that path is a file, not a folder"}, 400)
            anc = d
            while not anc.exists() and anc.parent != anc:
                anc = anc.parent
            if not anc.is_dir():
                return self._json({"error": "folder cannot be created there"}, 400)
            CONFIG["output_root"] = d
            return self._json({"output_root": str(d), "exists": d.is_dir()})

        if self.path == "/api/stop":
            with JOB.lock:
                if JOB.running:
                    JOB.stop = True
            return self._json({"ok": True})

        self._json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    ga.load_env(ga.BASE_DIR / ".env.local")
    if not os.environ.get("ELEVENLABS_API_KEY"):
        print("warning: ELEVENLABS_API_KEY not found; generation will be disabled")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
