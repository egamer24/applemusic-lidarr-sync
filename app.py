"""Apple Music -> Lidarr sync.

iPhone Shortcuts POST their library's artist names to /ingest. This server looks
each one up on MusicBrainz and serves the matches at /list in the format Lidarr's
"Custom List" import list expects: [{"MusicBrainzId": "..."}].

Several people can share one server. The admin (env TOKEN) signs in to the web
GUI at / to add users, issue their tokens and see reports. Each user's phone
uploads with its own token; /list combines everyone.
"""
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("TOKEN", "")
DATA = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8080"))
UA = os.environ.get(
    "MB_USER_AGENT",
    "applemusic-lidarr-sync/0.3 ( https://github.com/egamer24/applemusic-lidarr-sync )",
)
STATE_FILE = os.path.join(DATA, "state.json")
SHORTCUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Sync to Lidarr.shortcut")
SPLIT = re.compile(r"\s*(?:,|&|\+| x | feat\.? | ft\.? | featuring | with | and )\s*", re.I)
USER = re.compile(r"^[a-z0-9_-]{1,32}$")
HISTORY = 30  # uploads kept per user

lock = threading.Lock()
wake = threading.Event()
# users: name -> {"names": [...], "last_ingest": str|None, "token_hash": str|None, "history": [...]}
# cache: artist name -> [mbid, ...]  ([] = no match)
state = {"users": {}, "cache": {}}


# ---------- storage ----------

def load():
    try:
        with open(STATE_FILE) as f:
            state.update(json.load(f))
    except FileNotFoundError:
        return
    if "names" in state:  # single-list state from v0.1
        state["users"]["default"] = {"names": state.pop("names"), "last_ingest": state.pop("last_ingest", None)}
    for u in state["users"].values():
        u.setdefault("token_hash", None)
        u.setdefault("history", [])
    state["cache"] = {n: ids for n, ids in state["cache"].items() if ids}  # retry misses after an update


def save():
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


# ---------- matching ----------

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
        return list(dict.fromkeys(i for n in all_names() for i in state["cache"].get(n, [])))


def ingest(user: str, names: list[str]) -> dict:
    with lock:
        u = state["users"].setdefault(user, {"names": [], "last_ingest": None, "token_hash": None, "history": []})
        old, new = set(u["names"]), set(names)
        added = [n for n in names if n not in old]
        removed = sorted(old - new)
        u["names"], u["last_ingest"] = names, now()
        u["history"] = ([{"at": u["last_ingest"], "count": len(names), "added": added[:50], "added_count": len(added),
                          "removed": removed[:50], "removed_count": len(removed)}] + u["history"])[:HISTORY]
        save()
    wake.set()
    return {"user": user, "received": len(names), "added": len(added), "removed": len(removed)}


def stats(names: list[str], cache: dict) -> dict:
    return {"artists": len(names), "matched": sum(1 for n in names if cache.get(n)),
            "pending": sum(1 for n in names if n not in cache),
            "unmatched": sorted(n for n in names if cache.get(n) == [])}


# ---------- web ----------

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mute:#5f6b7a;--line:#e3e6ea;--acc:#d6304f;--ok:#1f8a4c;--warn:#b26a00}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e8eaed;--mute:#9aa4b1;--line:#2a2f37;--acc:#ff5a77;--ok:#4cc27e;--warn:#e6a23c}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,sans-serif}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--card)}
header a{color:var(--fg);text-decoration:none;font-weight:650}main{max-width:960px;margin:0 auto;padding:16px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px}
.cards .card{margin:0}.big{font-size:26px;font-weight:700}.mute{color:var(--mute);font-size:13px}
h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:0 0 10px}
table{width:100%;border-collapse:collapse}td,th{padding:8px 6px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:12px;color:var(--mute);font-weight:600;text-transform:uppercase}
.wrap{overflow-x:auto}a{color:var(--acc)}code{word-break:break-all}
input,button{font:inherit;border-radius:8px;border:1px solid var(--line);padding:8px 10px;background:var(--bg);color:var(--fg)}
button{background:var(--acc);border-color:var(--acc);color:#fff;cursor:pointer}button.ghost{background:none;color:var(--fg);border-color:var(--line)}
button.danger{background:none;color:var(--acc)}form.inline{display:inline}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.secret{font-family:ui-monospace,monospace;word-break:break-all;background:var(--bg);border:1px dashed var(--line);border-radius:8px;padding:10px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}
ul.cols{columns:2 220px;padding-left:18px;margin:0}
"""

COPY_JS = """<script>document.addEventListener('click',e=>{const b=e.target.closest('[data-copy]');if(!b)return;
navigator.clipboard.writeText(b.dataset.copy).then(()=>{const t=b.textContent;b.textContent='Copied';setTimeout(()=>b.textContent=t,1200)})})</script>"""


def esc(s) -> str:
    return html.escape(str(s))


def page(title: str, body: str, nav: bool = True) -> str:
    head = ('<header><a href="/">Apple Music → Lidarr</a><form class=inline method=post action=/logout>'
            '<button class=ghost>Sign out</button></form></header>') if nav else ""
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style>{head}<main>{body}</main>{COPY_JS}</html>")


def age_badge(ts: str | None) -> str:
    if not ts:
        return '<span class="pill warn">no uploads yet</span>'
    try:
        t = time.strptime(ts.replace(" UTC", "")[:16], "%Y-%m-%d %H:%M")
        hours = (time.time() - (time.mktime(t) - time.timezone)) / 3600
    except ValueError:
        return f"<span class=pill>{esc(ts)}</span>"
    return f'<span class="pill {"ok" if hours < 36 else "warn"}">{esc(ts)}</span>'


def base_url(h) -> str:
    return f"{h.headers.get('X-Forwarded-Proto', 'http')}://{h.headers.get('Host', 'localhost')}"


def dashboard(h) -> str:
    with lock:
        cache = dict(state["cache"])
        users = {u: (list(d["names"]), d["last_ingest"]) for u, d in sorted(state["users"].items())}
        names = all_names()
    tot = stats(names, cache)
    lidarr_url = f"{base_url(h)}/list?token={TOKEN}"
    rows = ""
    for u, (n, last) in users.items():
        s = stats(n, cache)
        rows += (f"<tr><td><a href='/u/{esc(u)}'>{esc(u)}</a></td><td>{s['artists']}</td><td>{s['matched']}</td>"
                 f"<td>{len(s['unmatched'])}</td><td>{age_badge(last)}</td></tr>")
    rows = rows or "<tr><td colspan=5 class=mute>No users yet. Add one below.</td></tr>"
    pend = f"<div class=mute>{tot['pending']} still looking up (~{tot['pending'] * 1.5 / 60:.0f} min)</div>" if tot["pending"] else ""
    unmatched = "".join(f"<li>{esc(n)}</li>" for n in tot["unmatched"]) or "<li class=mute>None</li>"
    return page("Apple Music → Lidarr", f"""
<h1>Overview</h1>
<div class=cards>
 <div class=card><div class=mute>Users</div><div class=big>{len(users)}</div></div>
 <div class=card><div class=mute>Artists (all users)</div><div class=big>{tot['artists']}</div></div>
 <div class=card><div class=mute>Sent to Lidarr</div><div class=big>{len(mbids())}</div><div class=mute>MusicBrainz artists</div></div>
 <div class=card><div class=mute>No match</div><div class=big>{len(tot['unmatched'])}</div>{pend}</div>
</div>
<div class=card><h2>Users</h2><div class=wrap><table>
<tr><th>User</th><th>Artists</th><th>Matched</th><th>No match</th><th>Last upload</th></tr>{rows}</table></div>
<form method=post action=/users class=row style="margin-top:12px">
 <input name=user placeholder="new user, e.g. alex" pattern="[A-Za-z0-9_\\-]{{1,32}}" required>
 <button>Add user</button></form>
<div class=mute style="margin-top:6px">Letters, digits, - and _. Each user gets their own token for their phone.</div></div>
<div class=card><h2>Lidarr</h2>
<p class=mute>Settings → Import Lists → + → Custom List, with this as the List URL. It combines every user.</p>
<div class=row><button class=ghost data-copy="{esc(lidarr_url)}">Copy Lidarr list URL</button>
<a href="/list">View list JSON</a></div></div>
<div class=card><h2>No match on MusicBrainz ({len(tot['unmatched'])})</h2>
<p class=mute>Usually small artists, podcasts or audiobooks. Add real ones in Lidarr by hand.</p>
<ul class=cols>{unmatched}</ul></div>""")


def history_cell(names: list, count: int, sign: str) -> str:
    more = f"<details><summary class=mute>show</summary>{esc(', '.join(names))}</details>" if names else ""
    return f"<td>{sign}{count}{more}</td>"


def user_page(h, user: str, new_token: str | None = None) -> str:
    with lock:
        d = state["users"].get(user)
        if d is None:
            return ""
        names, last, hist, has_tok = list(d["names"]), d["last_ingest"], list(d["history"]), bool(d["token_hash"])
        cache = dict(state["cache"])
    s = stats(names, cache)
    ingest_url = f"{base_url(h)}/ingest"
    tok = ""
    if new_token:
        tok = f"""<div class=card><h2>Token for {esc(user)}</h2>
<p class=warn>Shown only once. Copy it now; you can always generate a new one.</p>
<div class=secret>{esc(new_token)}</div>
<div class=row style="margin-top:10px"><button data-copy="{esc(new_token)}">Copy token</button>
<button class=ghost data-copy="{esc(ingest_url)}">Copy Shortcut URL</button></div></div>"""
    tok_state = ("Has a personal token." if has_tok else
                 "Uses the admin token with no ?user (generate a personal one to switch)." if user == "default" else "No token yet.")
    hist_rows = "".join(
        f"<tr><td>{esc(e['at'])}</td><td>{e['count']}</td>{history_cell(e['added'], e['added_count'], '+')}"
        f"{history_cell(e['removed'], e['removed_count'], '−')}</tr>" for e in hist
    ) or "<tr><td colspan=4 class=mute>No uploads recorded yet</td></tr>"
    matched = "".join(
        f"<li>{esc(n)} " + " ".join(f"<a href='https://musicbrainz.org/artist/{esc(i)}' rel=noreferrer target=_blank>↗</a>" for i in cache[n]) + "</li>"
        for n in sorted(names, key=str.casefold) if cache.get(n))
    unmatched = "".join(f"<li>{esc(n)}</li>" for n in s["unmatched"]) or "<li class=mute>None</li>"
    return page(f"{user} · Apple Music → Lidarr", f"""
<p><a href="/">← Overview</a></p><h1>{esc(user)}</h1>
<div class=cards>
 <div class=card><div class=mute>Artists</div><div class=big>{s['artists']}</div></div>
 <div class=card><div class=mute>Matched</div><div class=big>{s['matched']}</div></div>
 <div class=card><div class=mute>No match</div><div class=big>{len(s['unmatched'])}</div></div>
 <div class=card><div class=mute>Last upload</div><div style="margin-top:6px">{age_badge(last)}</div></div>
</div>{tok}
<div class=card><h2>Phone setup</h2>
<ol><li><a href="/shortcut">Download the Shortcut</a> on the iPhone and tap <b>Add Shortcut</b>.</li>
<li>URL: <code>{esc(ingest_url)}</code> <button class=ghost data-copy="{esc(ingest_url)}">Copy</button></li>
<li>Token: this user's token. {esc(tok_state)}</li>
<li>iPhone Settings → Apps → Shortcuts → Advanced → <b>Allow Sharing Large Amounts of Data</b>.</li>
<li>Shortcuts → Automation → Time of Day → daily → run <b>Sync to Lidarr</b>.</li></ol>
<div class=row><form class=inline method=post action="/u/{esc(user)}/token"><button>{'Regenerate' if has_tok else 'Generate'} token</button></form>
<form class=inline method=post action="/u/{esc(user)}/delete" onsubmit="return confirm('Delete {esc(user)} and their list? Artists already in Lidarr stay.')"><button class=danger>Delete user</button></form></div>
<p class=mute>Regenerating stops the old token working straight away.</p></div>
<div class=card><h2>Upload history</h2><div class=wrap><table>
<tr><th>When</th><th>Artists</th><th>New</th><th>Gone</th></tr>{hist_rows}</table></div></div>
<div class=card><h2>No match ({len(s['unmatched'])})</h2><ul class=cols>{unmatched}</ul></div>
<div class=card><details><summary><b>Matched artists ({s['matched']})</b></summary><ul class=cols>{matched}</ul></details></div>""")


def login_page(error: str = "") -> str:
    err = f"<p class=warn>{esc(error)}</p>" if error else ""
    return page("Sign in", f"""<div class=card style="max-width:420px;margin:10vh auto">
<h1>Apple Music → Lidarr</h1>{err}
<form method=post action=/login class=row><input type=password name=token placeholder="Admin token" required autofocus style="flex:1">
<button>Sign in</button></form><p class=mute>The <code>TOKEN</code> from the container's environment.</p></div>""", nav=False)


class Handler(BaseHTTPRequestHandler):
    def query(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def given_token(self) -> str:
        t = self.query().get("token", [""])[0].strip() or self.headers.get("Authorization", "").strip()
        return re.sub(r"^bearer\s+", "", t, flags=re.I).strip()  # pasted tokens often carry spaces/newlines

    def is_admin_token(self, t: str) -> bool:
        return bool(TOKEN) and hmac.compare_digest(t.encode(), TOKEN.encode())

    def session_value(self) -> str:
        return hmac.new(TOKEN.encode(), b"admin-session", hashlib.sha256).hexdigest()

    def is_admin(self) -> bool:
        if self.is_admin_token(self.given_token()):
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "amls" and hmac.compare_digest(v.encode(), self.session_value().encode()):
                return True
        return False

    def send(self, code: int, body, ctype="application/json", headers: dict | None = None):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def html(self, body: str, code: int = 200, headers: dict | None = None):
        self.send(code, body, "text/html; charset=utf-8", {"X-Frame-Options": "DENY", **(headers or {})})

    def redirect(self, to: str, headers: dict | None = None):
        self.send(303, b"", "text/plain", {"Location": to, **(headers or {})})

    def form(self) -> dict:
        length = min(int(self.headers.get("Content-Length", 0)), 10_000)
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/healthz":
            return self.send(200, {"ok": True})
        if path == "/shortcut":  # public: the file holds no secrets, it asks for URL + token on import
            try:
                with open(SHORTCUT_FILE, "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                return self.send(404, {"error": "shortcut not bundled"})
            return self.send(200, data, "application/octet-stream",
                             {"Content-Disposition": 'attachment; filename="Sync to Lidarr.shortcut"'})
        if path == "/login":
            return self.html(login_page())
        if not self.is_admin():
            if path == "/list":
                return self.send(401, {"error": "bad or missing token"})
            return self.redirect("/login")
        if path == "/list":
            return self.send(200, [{"MusicBrainzId": i} for i in mbids()])
        if path == "/":
            return self.html(dashboard(self))
        m = re.fullmatch(r"/u/([a-z0-9_-]{1,32})", path)
        if m and (body := user_page(self, m[1])):
            return self.html(body)
        self.html(page("Not found", "<p>Not found. <a href='/'>Overview</a></p>"), 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/ingest":
            return self.do_ingest()
        if path == "/login":
            if self.is_admin_token(self.form().get("token", "").strip()):
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"amls={self.session_value()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000{secure}"
                return self.redirect("/", {"Set-Cookie": cookie})
            time.sleep(1)  # slow down guessing
            return self.html(login_page("Wrong token."), 401)
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": "amls=; Path=/; Max-Age=0"})
        if not self.is_admin():
            return self.redirect("/login")
        if path == "/users":
            user = self.form().get("user", "").strip().casefold()
            if not USER.match(user):
                return self.html(page("Bad name", "<p>Use 1–32 letters, digits, - or _. <a href='/'>Back</a></p>"), 400)
            with lock:
                exists = user in state["users"]
                if not exists:
                    state["users"][user] = {"names": [], "last_ingest": None, "token_hash": None, "history": []}
            return self.redirect(f"/u/{user}") if exists else self.new_token(user)
        m = re.fullmatch(r"/u/([a-z0-9_-]{1,32})/(token|delete)", path)
        if m:
            user, action = m[1], m[2]
            with lock:
                if user not in state["users"]:
                    return self.redirect("/")
                if action == "delete":
                    del state["users"][user]
                    save()
            return self.redirect("/") if action == "delete" else self.new_token(user)
        self.send(404, {"error": "not found"})

    def new_token(self, user: str):
        t = secrets.token_hex(24)
        with lock:
            state["users"][user]["token_hash"] = hash_token(t)
            save()
        self.html(user_page(self, user, new_token=t))

    def do_ingest(self):
        given = self.given_token()
        if self.is_admin_token(given):  # admin token picks the list with ?user= (default "default")
            user = self.query().get("user", ["default"])[0].strip().casefold()
            if not USER.match(user):
                return self.send(400, {"error": "user must be 1-32 letters, digits, - or _"})
        else:
            h = hash_token(given) if given else ""
            with lock:
                user = next((u for u, d in state["users"].items()
                             if d["token_hash"] and hmac.compare_digest(d["token_hash"], h)), None)
            if not user:
                return self.send(401, {"error": "bad or missing token"})
        length = int(self.headers.get("Content-Length", 0))
        if length > 20_000_000:
            return self.send(413, {"error": "too large"})
        body, ctype = self.rfile.read(length), self.headers.get("Content-Type", "")
        names = parse_names(body, ctype)
        if not names:
            print(f"empty ingest for {user}: {length} bytes, {ctype!r}", flush=True)
            return self.send(400, {"error": "no artist names in body", "bytes": length, "content_type": ctype})
        self.send(200, ingest(user, names))

    def log_message(self, fmt, *args):
        if "/healthz" not in self.path:
            print(re.sub(r"token=[^ &]*", "token=***", f"{self.address_string()} {fmt % args}"), flush=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set the TOKEN environment variable")
    os.makedirs(DATA, exist_ok=True)
    load()
    threading.Thread(target=worker, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
