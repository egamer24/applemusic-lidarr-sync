"""Apple Music -> Lidarr sync.

An iPhone Shortcut POSTs your library's artist names to /ingest. This server
looks each one up on MusicBrainz and serves the matches at /list in the format
Lidarr's "Custom List" import list expects: [{"MusicBrainzId": "..."}].
"""
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("TOKEN", "")
DATA = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8080"))
UA = os.environ.get(
    "MB_USER_AGENT",
    "applemusic-lidarr-sync/0.1 ( https://github.com/egamer24/applemusic-lidarr-sync )",
)
STATE_FILE = os.path.join(DATA, "state.json")
SPLIT = re.compile(r"\s*(?:,|&|\+| x | feat\.? | ft\.? | featuring | with | and )\s*", re.I)

lock = threading.Lock()
wake = threading.Event()
state = {"names": [], "cache": {}, "last_ingest": None}  # cache: name -> [mbid, ...] ([] = no match)


def load():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except FileNotFoundError:
        pass


def save():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def parse_names(body: bytes, content_type: str) -> list[str]:
    """Accept a JSON list, {"artists": [...]}, or plain text with one name per line."""
    text = body.decode("utf-8", "replace").strip()
    names = None
    if "json" in content_type or text[:1] in "[{":
        try:
            data = json.loads(text)
            names = data.get("artists", []) if isinstance(data, dict) else data
        except json.JSONDecodeError:
            pass
    if names is None:
        names = text.splitlines()
    seen, out = set(), []
    for n in names:
        n = str(n).strip()
        if n and n.casefold() not in seen:
            seen.add(n.casefold())
            out.append(n)
    return out


def pick(name: str, results: list[dict]) -> str | None:
    """Exact (case-insensitive) match on name, sort name or alias; MusicBrainz's ranking breaks ties."""
    want = name.casefold()
    for r in results:
        names = {r.get("name", ""), r.get("sort-name", "")} | {a.get("name", "") for a in r.get("aliases", [])}
        if r.get("score", 0) >= 90 and want in {n.casefold() for n in names}:
            return r["id"]
    return None


_last_call = 0.0


def mb_search(name: str) -> list[dict]:
    """MusicBrainz artist search, at most one request every 1.5s, retrying 503 (rate limit)."""
    global _last_call
    q = urllib.parse.urlencode({"query": f'artist:"{name}"', "fmt": "json", "limit": 10})
    req = urllib.request.Request(f"https://musicbrainz.org/ws/2/artist?{q}", headers={"User-Agent": UA})
    for attempt in range(5):
        time.sleep(max(0.0, _last_call + 1.5 - time.time()))
        _last_call = time.time()
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r).get("artists", [])
        except urllib.error.HTTPError as e:
            if e.code != 503 or attempt == 4:
                raise
            time.sleep(5 * (attempt + 1))


def resolve(name: str) -> list[str]:
    # ponytail: same-name artists resolve to MusicBrainz's top exact match; send album too if this misfires
    hit = pick(name, mb_search(name))
    if hit:
        return [hit]
    parts = [p for p in SPLIT.split(name) if p and p.casefold() != name.casefold()]
    ids = []
    for p in parts:
        hit = pick(p, mb_search(p))
        if hit:
            ids.append(hit)
    return ids


def worker():
    while True:
        with lock:
            todo = [n for n in state["names"] if n not in state["cache"]]
        if not todo:
            wake.wait()
            wake.clear()
            continue
        for n in todo:
            try:
                ids = resolve(n)
            except Exception as e:  # network/rate-limit: back off, retry later
                print(f"lookup failed for {n!r}: {e}", flush=True)
                time.sleep(30)
                break
            with lock:
                state["cache"][n] = ids
                save()


def mbids() -> list[str]:
    with lock:
        out = []
        for n in state["names"]:
            for i in state["cache"].get(n, []):
                if i not in out:
                    out.append(i)
        return out


class Handler(BaseHTTPRequestHandler):
    def authed(self) -> bool:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = q.get("token", [""])[0] or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return bool(TOKEN) and given == TOKEN

    def send(self, code: int, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self.send(200, {"ok": True})
        if not self.authed():
            return self.send(401, {"error": "bad or missing token"})
        if path == "/list":
            return self.send(200, [{"MusicBrainzId": i} for i in mbids()])
        if path == "/":
            return self.send(200, status_page(), "text/html; charset=utf-8")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if urllib.parse.urlparse(self.path).path != "/ingest":
            return self.send(404, {"error": "not found"})
        if not self.authed():
            return self.send(401, {"error": "bad or missing token"})
        length = int(self.headers.get("Content-Length", 0))
        if length > 20_000_000:
            return self.send(413, {"error": "too large"})
        names = parse_names(self.rfile.read(length), self.headers.get("Content-Type", ""))
        if not names:
            return self.send(400, {"error": "no artist names in body"})
        with lock:
            state["names"] = names
            state["last_ingest"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save()
        wake.set()
        self.send(200, {"received": len(names)})

    def log_message(self, fmt, *args):
        if "/healthz" not in self.path:
            print(f"{self.address_string()} {fmt % args}".replace(TOKEN or "\0", "***"), flush=True)


def status_page() -> str:
    with lock:
        names, cache, last = list(state["names"]), dict(state["cache"]), state["last_ingest"]
    pending = [n for n in names if n not in cache]
    missing = sorted(n for n in names if cache.get(n) == [])
    rows = "".join(f"<li>{html.escape(n)}</li>" for n in missing)
    return f"""<!doctype html><meta name=viewport content="width=device-width">
<title>Apple Music → Lidarr</title>
<style>body{{font:15px system-ui;max-width:40em;margin:2em auto;padding:0 1em}}</style>
<h1>Apple Music → Lidarr</h1>
<p>Last upload: <b>{html.escape(last or "never")}</b><br>
Artists received: <b>{len(names)}</b><br>
Matched on MusicBrainz: <b>{len(mbids())}</b><br>
Still looking up: <b>{len(pending)}</b> (about {len(pending) * 1.5 / 60:.0f} min)<br>
No match: <b>{len(missing)}</b></p>
<details><summary>Artists with no match</summary><ul>{rows}</ul></details>"""


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the TOKEN environment variable")
    os.makedirs(DATA, exist_ok=True)
    load()
    threading.Thread(target=worker, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
