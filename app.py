"""Apple Music -> Lidarr sync.

An iPhone Shortcut POSTs your library's artist names to /ingest. This server
looks each one up on MusicBrainz and serves the matches at /list in the format
Lidarr's "Custom List" import list expects: [{"MusicBrainzId": "..."}].
Several people can share one server: each uploads to /ingest?user=<name> and
/list serves everyone's artists combined.
"""
import html
import json
import os
import re
import threading
import unicodedata
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
USER = re.compile(r"^[a-z0-9_-]{1,32}$")
# users: name -> {"names": [...], "last_ingest": str}; cache: artist name -> [mbid, ...] ([] = no match)
state = {"users": {}, "cache": {}}


def load():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
        if "names" in state:  # single-list state from v0.1
            state["users"]["default"] = {"names": state.pop("names"), "last_ingest": state.pop("last_ingest", None)}
        state["cache"] = {n: ids for n, ids in state["cache"].items() if ids}  # retry misses after an update
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


def key(name: str) -> str:
    """Loose form for comparing names: no case, accents, punctuation or spaces; `$` reads as `s` (Ke$ha)."""
    n = unicodedata.normalize("NFKD", name.casefold().replace("$", "s"))
    k = "".join(c for c in n if c.isalnum())
    return k or name.casefold()


def pick(name: str, results: list[dict]) -> str | None:
    """Match on name, sort name or alias (loosely); MusicBrainz's ranking breaks ties."""
    want = key(name)
    for r in results:
        names = {r.get("name", ""), r.get("sort-name", "")} | {a.get("name", "") for a in r.get("aliases", [])}
        if want in {key(n) for n in names}:
            return r["id"]
    return None


_last_call = 0.0


def mb_search(name: str, exact: bool = True) -> list[dict]:
    """MusicBrainz artist search, at most one request every 1.5s, retrying 503 (rate limit)."""
    global _last_call
    query = f'artist:"{name}"' if exact else re.sub(r'[+\-&|!(){}\[\]^"~*?:\\/]', " ", name)
    q = urllib.parse.urlencode({"query": query, "fmt": "json", "limit": 25})
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
    hit = pick(name, mb_search(name)) or pick(name, mb_search(name, exact=False))
    if hit:
        return [hit]
    parts = [p for p in SPLIT.split(name) if p and p.casefold() != name.casefold()]
    ids = []
    for p in parts:
        hit = pick(p, mb_search(p))
        if hit:
            ids.append(hit)
    return ids


def all_names() -> list[str]:
    """Every user's artist names, deduplicated, in upload order. Call with lock held."""
    return list(dict.fromkeys(n for u in state["users"].values() for n in u["names"]))


def worker():
    while True:
        with lock:
            todo = [n for n in all_names() if n not in state["cache"]]
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
        for n in all_names():
            for i in state["cache"].get(n, []):
                if i not in out:
                    out.append(i)
        return out


class Handler(BaseHTTPRequestHandler):
    def authed(self) -> bool:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        given = q.get("token", [""])[0].strip() or self.headers.get("Authorization", "").strip()
        given = re.sub(r"^bearer\s+", "", given, flags=re.I).strip()  # pasted tokens often carry spaces/newlines
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
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        user = q.get("user", ["default"])[0].strip().casefold()
        if not USER.match(user):
            return self.send(400, {"error": "user must be 1-32 letters, digits, - or _"})
        length = int(self.headers.get("Content-Length", 0))
        if length > 20_000_000:
            return self.send(413, {"error": "too large"})
        body, ctype = self.rfile.read(length), self.headers.get("Content-Type", "")
        names = parse_names(body, ctype)
        if not names:
            print(f"empty ingest: {length} bytes, {ctype!r}, starts {body[:80]!r}", flush=True)
            return self.send(400, {"error": "no artist names in body", "bytes": length, "content_type": ctype})
        with lock:
            state["users"][user] = {"names": names, "last_ingest": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())}
            save()
        wake.set()
        self.send(200, {"user": user, "received": len(names)})

    def log_message(self, fmt, *args):
        if "/healthz" not in self.path:
            print(re.sub(r"token=[^ &]*", "token=***", f"{self.address_string()} {fmt % args}"), flush=True)


def status_page() -> str:
    with lock:
        names, cache = all_names(), dict(state["cache"])
        users = {u: (len(d["names"]), d["last_ingest"]) for u, d in sorted(state["users"].items())}
    pending = [n for n in names if n not in cache]
    missing = sorted(n for n in names if cache.get(n) == [])
    rows = "".join(f"<li>{html.escape(n)}</li>" for n in missing)
    people = "".join(f"<tr><td>{html.escape(u)}</td><td>{c}</td><td>{html.escape(t or 'never')}</td></tr>" for u, (c, t) in users.items())
    return f"""<!doctype html><meta name=viewport content="width=device-width">
<title>Apple Music → Lidarr</title>
<style>body{{font:15px system-ui;max-width:40em;margin:2em auto;padding:0 1em}}</style>
<h1>Apple Music → Lidarr</h1>
<table><tr><th align=left>User</th><th align=left>Artists</th><th align=left>Last upload</th></tr>{people or "<tr><td colspan=3>No uploads yet</td></tr>"}</table>
<p>Artists, all users combined: <b>{len(names)}</b><br>
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
