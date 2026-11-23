#!/usr/bin/env python3
"""
Scout Finder - desktop app.

Starts a small local server on this computer and opens the app window in the
browser. Nothing is hosted online; keys stay in a file in your home folder.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scout_finder as sf  # noqa: E402

DATA_DIR = Path.home() / "Scout Finder"
RESULTS_DIR = DATA_DIR / "results"
CONFIG = DATA_DIR / "settings.json"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

JOBS: dict[str, dict] = {}
RUN_LOCK = threading.Lock()


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(new: dict) -> None:
    cur = load_settings()
    for k in ("provider", "gemini_key", "ollama_url", "ollama_model",
              "anthropic_key", "notion_token", "model"):
        if k not in new:
            continue
        v = (new.get(k) or "").strip()
        if "•" in v:                    # masked value = unchanged
            continue
        if v:
            cur[k] = v
        elif k in ("ollama_url", "ollama_model"):
            cur.pop(k, None)
    CONFIG.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG, 0o600)
    except OSError:
        pass
    apply_settings()


def apply_settings() -> None:
    s = load_settings()
    sf.PROVIDER = s.get("provider") or os.getenv("SCOUT_PROVIDER") or "gemini"
    if s.get("gemini_key"):
        os.environ["GEMINI_API_KEY"] = s["gemini_key"]
    sf.OLLAMA_URL = s.get("ollama_url") or os.getenv("OLLAMA_URL") or "http://localhost:11434"
    sf.OLLAMA_MODEL = s.get("ollama_model") or os.getenv("OLLAMA_MODEL") or "qwen3:8b"
    if s.get("anthropic_key"):
        os.environ["ANTHROPIC_API_KEY"] = s["anthropic_key"]
    if s.get("notion_token"):
        os.environ["NOTION_TOKEN"] = s["notion_token"]
    sf.MODEL = s.get("model") or os.getenv("SCOUT_MODEL") or "claude-opus-5-5"


def provider_ready(s: dict) -> bool:
    prov = s.get("provider") or sf.PROVIDER
    if prov == "claude":
        return bool(s.get("anthropic_key") or os.getenv("ANTHROPIC_API_KEY"))
    if prov == "ollama":
        return True
    return bool(s.get("gemini_key") or os.getenv("GEMINI_API_KEY"))


def mask(v: str | None) -> str:
    return (v[:6] + "••••••" + v[-4:]) if v and len(v) > 12 else ("••••" if v else "")


def notion() -> sf.Notion:
    tok = os.getenv("NOTION_TOKEN")
    if not tok:
        raise RuntimeError("Add your Notion token in Settings first.")
    return sf.Notion(tok)


# ---------------------------------------------------------------- jobs
def new_job(kind: str) -> dict:
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "status": "running", "log": [], "steps": [],
           "briefs": [], "filters": None, "error": None, "notion_urls": {},
           "file": None, "started": time.time()}
    JOBS[jid] = job
    return job


def step(job: dict, text: str, state: str = "run") -> None:
    for s in job["steps"]:
        if s["state"] == "run":
            s["state"] = "done"
    job["steps"].append({"text": text, "state": state})


def filters_from_payload(f: dict) -> sf.SearchFilters:
    def num(k, cast=float):
        try:
            return cast(f[k]) if f.get(k) not in (None, "") else None
        except (TypeError, ValueError):
            return None
    lst = lambda k: f[k] if isinstance(f.get(k), list) else sf._csv(f.get(k))
    return sf.SearchFilters(
        title=(f.get("title") or (f.get("description") or "")[:60].strip() or "Player search"),
        description=f.get("description") or "", languages=lst("languages"), custom=lst("custom"),
        only_lower_leagues=f.get("lower") == "only",
        positions=lst("positions"), age_min=num("age_min", int), age_max=num("age_max", int),
        foot=f.get("foot") or None, max_value_eur=num("max_value"), min_value_eur=num("min_value"),
        leagues=lst("leagues"), nationalities=lst("nationalities"),
        include_lower_leagues=f.get("lower") in (True, "also", "only"), min_minutes=num("min_minutes", int),
        min_height_cm=num("min_height", int), contract_ends_by=f.get("contract_by") or None,
        criteria=f.get("criteria") or "", must_haves=f.get("must_haves") or "")


def run_search(job: dict, payload: dict) -> None:
    sf.LOG_HOOK = lambda msg: job["log"].append(msg)
    try:
        with RUN_LOCK:
            if not provider_ready(load_settings()):
                raise RuntimeError("Add your free Gemini API key in Settings first "
                                   "(or pick Ollama / Claude).")
            f = filters_from_payload(payload.get("filters", {}))
            job["filters"] = f.model_dump()
            top = max(1, min(int(payload.get("top") or 10), 15))
            players = sf._csv(payload.get("players"))
            briefs = sf.run_search(sf.make_client(), f, top, players, on_step=lambda t: step(job, t))
            job["briefs"] = [b.model_dump() for b in briefs]
            step(job, "Watch list ready", "done")
            fname = RESULTS_DIR / f"{dt.datetime.now():%Y-%m-%d_%H%M}_{_slug(f.title)}.json"
            fname.write_text(json.dumps({"filters": job["filters"], "briefs": job["briefs"]},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
            job["file"] = fname.name
            job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["log"].append(traceback.format_exc())
        for s in job["steps"]:
            if s["state"] == "run":
                s["state"] = "fail"
    finally:
        sf.LOG_HOOK = None


def run_push(job: dict, briefs: list[dict]) -> None:
    try:
        n = notion()
        for d in briefs:
            b = sf.PlayerBrief(**d)
            step(job, f"Adding {b.name} to your Notion watchlist")
            job["notion_urls"][b.name] = n.add(b)
        step(job, "Notion updated", "done")
        job["status"] = "done"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        for s in job["steps"]:
            if s["state"] == "run":
                s["state"] = "fail"


def _slug(t: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in t)[:40].strip("-") or "run"


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet console
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            body = (HERE / "ui.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif p == "/api/settings":
            s = load_settings()
            self._json({"provider": s.get("provider") or sf.PROVIDER,
                        "gemini_key": mask(s.get("gemini_key")),
                        "ollama_url": s.get("ollama_url") or sf.OLLAMA_URL,
                        "ollama_model": s.get("ollama_model") or sf.OLLAMA_MODEL,
                        "anthropic_key": mask(s.get("anthropic_key")),
                        "notion_token": mask(s.get("notion_token")),
                        "model": s.get("model") or sf.MODEL,
                        "ready": provider_ready(s),
                        "notion_ready": bool(s.get("notion_token")),
                        "folder": str(RESULTS_DIR)})
        elif p == "/api/history":
            files = sorted(RESULTS_DIR.glob("*.json"), reverse=True)[:30]
            out = []
            for f in files:
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    out.append({"file": f.name, "title": (d.get("filters") or {}).get("title"),
                                "count": len(d["briefs"])})
                except Exception:
                    continue
            self._json({"history": out})
        elif p.startswith("/api/history/"):
            f = RESULTS_DIR / Path(p.rsplit("/", 1)[1]).name
            if not f.exists():
                return self._json({"error": "not found"}, 404)
            d = json.loads(f.read_text(encoding="utf-8"))
            d["file"] = f.name
            self._json(d)
        elif p.startswith("/api/job/"):
            job = JOBS.get(p.rsplit("/", 1)[1])
            if not job:
                return self._json({"error": "not found"}, 404)
            self._json({**job, "elapsed": int(time.time() - job["started"])})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?")[0]
        data = self._body()
        if p == "/api/settings":
            save_settings(data)
            self._json({"ok": True})
        elif p == "/api/run":
            if RUN_LOCK.locked():
                return self._json({"error": "A search is already running."}, 409)
            job = new_job("search")
            threading.Thread(target=run_search, args=(job, data), daemon=True).start()
            self._json({"job": job["id"]})
        elif p == "/api/push":
            briefs = data.get("briefs") or []
            if not briefs:
                return self._json({"error": "Nothing to add."}, 400)
            job = new_job("push")
            threading.Thread(target=run_push, args=(job, briefs), daemon=True).start()
            self._json({"job": job["id"]})
        else:
            self._json({"error": "not found"}, 404)


def free_port(start: int = 8765) -> int:
    for port in range(start, start + 50):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return 0


def open_window(url: str) -> None:
    """Open as a clean app window (Chrome / Edge app mode) if possible, else the browser."""
    import shutil
    import subprocess
    cands = []
    if sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                 "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"]
    elif os.name == "nt":
        for base in (os.environ.get("PROGRAMFILES", ""), os.environ.get("PROGRAMFILES(X86)", ""),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"),
                      os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")]
    else:
        cands = [shutil.which(x) or "" for x in ("google-chrome", "chromium", "microsoft-edge")]
    for exe in cands:
        if exe and os.path.exists(exe):
            try:
                subprocess.Popen([exe, f"--app={url}", "--window-size=1440,920",
                                  f"--user-data-dir={DATA_DIR / '.window'}"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                continue
    webbrowser.open(url)


def main():
    apply_settings()
    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"Scout Finder is running at {url}")
    print("Keep this window open while you use the app. Close it to quit.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: open_window(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
