"""Apple Music -> Lidarr sync.

iPhone Shortcuts POST their library's artist names to /ingest. This server looks
each one up on MusicBrainz and serves the matches at /list in the format Lidarr's
"Custom List" import list expects: [{"MusicBrainzId": "..."}].

Several people can share one server. On first visit the web GUI asks for a setup
code and creates the admin account (username + password); the admin then adds
users, issues their tokens and sees reports. Each user's phone uploads with its
own token; /list (Lidarr) uses the API token and combines everyone.
"""
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import struct
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.2.0"  # MAJOR.MINOR.PATCH; the release tag must be v + this (CI checks)
TOKEN = os.environ.get("TOKEN", "")  # API token (Lidarr /list); generated and kept in state.json when unset
RESET_ADMIN = os.environ.get("RESET_ADMIN", "").lower() in ("1", "true", "yes")
SETUP_CODE = secrets.token_hex(4)  # printed to the log while no admin exists
DATA = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8080"))
UA = os.environ.get(
    "MB_USER_AGENT",
    f"applemusic-lidarr-sync/{VERSION} ( https://github.com/egamer24/applemusic-lidarr-sync )",
)
STATE_FILE = os.path.join(DATA, "state.json")
HERE = os.path.dirname(os.path.abspath(__file__))
SHORTCUTS = {"/shortcut": "Sync to Lidarr.shortcut", "/shortcut/albums": "Sync to Lidarr (albums).shortcut"}
SPLIT = re.compile(r"\s*(?:,|&|\+| x | feat\.? | ft\.? | featuring | with | and )\s*", re.I)
USER = re.compile(r"^[a-z0-9_-]{1,32}$")
HISTORY = 30  # uploads kept per user

lock = threading.Lock()
wake = threading.Event()
album_wake = threading.Event()
# users: name -> {"names": [...], "albums": [[artist, album], ...], "last_ingest": str|None, "token_hash": str|None, "history": [...]}
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
        u.setdefault("albums", [])
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


def hash_password(pw: str, salt: str | None = None) -> dict:
    salt = salt or secrets.token_hex(16)
    return {"salt": salt, "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 600_000).hex()}


def check_password(user: str, pw: str) -> bool:
    a = state.get("admin")
    if not a or not hmac.compare_digest(user.casefold().encode(), a["user"].encode()):
        hash_password(pw)  # same work either way
        return False
    return hmac.compare_digest(hash_password(pw, a["salt"])["hash"].encode(), a["hash"].encode())


def sign(msg: str) -> str:
    return hmac.new(bytes.fromhex(state["secret"]), msg.encode(), hashlib.sha256).hexdigest()


def session_for(a: dict) -> str:
    """Full sign-in cookie. Changes when the password or 2FA secret does, which signs out other browsers."""
    user, pw, totp = a["user"], a["hash"], a.get("totp") or "-"
    return f"{user}:{sign(f'session|{user}|{pw}|{totp}')}"


def pending_for(a: dict, exp: int) -> str:
    """Half-signed-in cookie between password and 2FA code. Only opens /login/2fa, for 5 minutes."""
    user, pw = a["user"], a["hash"]
    return f"{exp}:{sign(f'2fa|{user}|{pw}|{exp}')}"


# ---------- two-factor (TOTP, RFC 6238: 6 digits, 30 s, SHA-1, what authenticator apps use) ----------

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, step: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    h = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    o = h[-1] & 15
    return f"{(int.from_bytes(h[o:o + 4], 'big') & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_step(secret: str, code: str, after: int = 0) -> int | None:
    """The time step `code` is valid for (now ±30 s), if later than `after` (stops replays)."""
    code = re.sub(r"\s", "", code)
    if not secret or not re.fullmatch(r"\d{6}", code):
        return None
    now_step = int(time.time() // 30)
    for step in (now_step - 1, now_step, now_step + 1):
        if step > after and hmac.compare_digest(totp_code(secret, step), code):
            return step
    return None


def check_second_factor(a: dict, code: str) -> bool:
    """TOTP code or an unused recovery code. Consumes what it accepts. Call with lock held."""
    step = totp_step(a.get("totp"), code, a.get("totp_last", 0))
    if step:
        a["totp_last"] = step
        save()
        return True
    h = hash_token(re.sub(r"[\s-]", "", code).lower())
    for i, c in enumerate(a.get("recovery", [])):
        if hmac.compare_digest(c, h):
            a["recovery"].pop(i)
            save()
            return True
    return False


def new_recovery_codes(a: dict) -> list[str]:
    codes = [secrets.token_hex(5) for _ in range(8)]
    a["recovery"] = [hash_token(c) for c in codes]
    return [f"{c[:5]}-{c[5:]}" for c in codes]


# ---------- sign-in throttling ----------

_fails: list[float] = []


def locked_out() -> bool:
    """10 failed sign-in steps in 15 minutes locks sign-in for everyone until they age out."""
    cutoff = time.time() - 900
    _fails[:] = [t for t in _fails if t > cutoff]
    return len(_fails) >= 10


def record_failure():
    _fails.append(time.time())


# ---------- matching ----------

def parse_upload(body: bytes, content_type: str) -> tuple[list[str], list[list[str]]]:
    """Artist names, plus [artist, album] pairs from "Artist<TAB>Album" lines (the album Shortcut)."""
    names, albums, seen = [], [], set()
    for line in parse_names(body, content_type):
        artist, tab, album = line.partition("\t")
        artist, album = artist.strip(), album.strip()
        if not artist:
            continue
        names.append(artist)
        if tab and album and (artist.casefold(), album.casefold()) not in seen:
            seen.add((artist.casefold(), album.casefold()))
            albums.append([artist, album])
    return list(dict.fromkeys(names)), albums


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
            album_wake.set()  # lookups done: albums can map to artists now
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


def ingest(user: str, names: list[str], albums: list[list[str]] | None = None) -> dict:
    with lock:
        u = state["users"].setdefault(user, {"names": [], "last_ingest": None, "token_hash": None, "history": []})
        u["albums"] = albums or []
        old, new = set(u["names"]), set(names)
        added = [n for n in names if n not in old]
        removed = sorted(old - new)
        u["names"], u["last_ingest"] = names, now()
        u["history"] = ([{"at": u["last_ingest"], "count": len(names), "added": added[:50], "added_count": len(added),
                          "removed": removed[:50], "removed_count": len(removed)}] + u["history"])[:HISTORY]
        save()
    wake.set()
    return {"user": user, "received": len(names), "added": len(added), "removed": len(removed)}


# ---------- albums (optional): monitor only the albums in the library ----------

EDITION = re.compile(r"\s*(?:[(\[][^)\]]*[)\]]|-\s*(?:single|ep)\s*$)", re.I)


def album_key(title: str) -> str:
    """Compare album titles without "(Deluxe Edition)", "[Remastered]", " - Single" and the like."""
    return key(EDITION.sub("", title)) if EDITION.sub("", title).strip() else key(title)


def wanted_albums() -> dict:
    """MusicBrainz artist id -> {"Artist — Album": album title}, from every user's album upload. Call with lock held."""
    out = {}
    for u in state["users"].values():
        for artist, album in u.get("albums", []):
            for mbid in state["cache"].get(artist, []):
                out.setdefault(mbid, {})[f"{artist} — {album}"] = album
    return out


def album_plan(artists: list[dict], albums: list[dict], wanted: dict) -> dict:
    """What to change in Lidarr. Only artists with at least one matched album are touched; for them,
    library albums get monitored and other albums unmonitored, unless Lidarr already has files for them."""
    by_artist = {}
    for a in albums:
        by_artist.setdefault(a["artistId"], []).append(a)
    plan = {"artists": [], "monitor": [], "unmonitor": [], "managed": 0, "matched": set()}
    for ar in artists:
        own = by_artist.get(ar["id"], [])
        exact, loose, mine = {}, {}, set()
        for a in own:
            exact.setdefault(key(a["title"]), []).append(a["id"])
            loose.setdefault(album_key(a["title"]), []).append(a["id"])
        for label, title in (wanted.get(ar.get("foreignArtistId")) or {}).items():
            ids = exact.get(key(title)) or loose.get(album_key(title))  # "X" shouldn't also pick "X (Live)"
            if ids:
                mine.update(ids)
                plan["matched"].add(label)
        if not mine:
            continue  # nothing matched: leave this artist as it is rather than unmonitor everything
        plan["managed"] += 1
        if not ar.get("monitored"):
            plan["artists"].append(ar["id"])
        for a in own:
            if a["id"] in mine and not a.get("monitored"):
                plan["monitor"].append(a["id"])
            elif a["id"] not in mine and a.get("monitored") and not (a.get("statistics") or {}).get("trackFileCount"):
                plan["unmonitor"].append(a["id"])
    labels = {label for w in wanted.values() for label in w}
    plan["unmatched"] = sorted(labels - plan["matched"])
    plan["matched"] = len(plan["matched"])
    return plan


def lidarr(cfg: dict, method: str, path: str, body=None):
    req = urllib.request.Request(cfg["lidarr_url"] + "/api/v1" + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Api-Key": cfg["lidarr_key"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    return json.loads(data) if data else None


def sync_albums():
    with lock:
        cfg = dict(state.get("settings", {}))
        if not (cfg.get("albums_only") and cfg.get("lidarr_url") and cfg.get("lidarr_key")):
            return
        wanted = wanted_albums()
    if not wanted:
        return  # nobody has uploaded albums yet (old Shortcut): change nothing
    plan = album_plan(lidarr(cfg, "GET", "/artist"), lidarr(cfg, "GET", "/album"), wanted)
    if plan["artists"]:
        lidarr(cfg, "PUT", "/artist/editor", {"artistIds": plan["artists"], "monitored": True})
    if plan["unmonitor"]:
        lidarr(cfg, "PUT", "/album/monitor", {"albumIds": plan["unmonitor"], "monitored": False})
    if plan["monitor"]:
        lidarr(cfg, "PUT", "/album/monitor", {"albumIds": plan["monitor"], "monitored": True})
        lidarr(cfg, "POST", "/command", {"name": "AlbumSearch", "albumIds": plan["monitor"]})
    with lock:
        state["album_sync"] = {"at": now(), "error": "", **{k: plan[k] for k in ("managed", "matched", "unmatched")},
                               "monitored": len(plan["monitor"]), "unmonitored": len(plan["unmonitor"])}
        save()
    print(f"album sync: {plan['managed']} artists, +{len(plan['monitor'])} -{len(plan['unmonitor'])} albums", flush=True)


def album_worker():
    while True:
        album_wake.wait(3600)  # hourly, so artists Lidarr adds on its own list sync get picked up
        album_wake.clear()
        try:
            sync_albums()
        except Exception as e:
            print(f"album sync failed: {e}", flush=True)
            with lock:
                state.setdefault("album_sync", {})["error"] = f"{now()}: {e}"
                save()


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
@media (max-width:640px){td,th{padding:8px 4px}.pill{white-space:normal;display:inline-block}}
"""

COPY_JS = """<script>document.addEventListener('click',e=>{const b=e.target.closest('[data-copy]');if(!b)return;
navigator.clipboard.writeText(b.dataset.copy).then(()=>{const t=b.textContent;b.textContent='Copied';setTimeout(()=>b.textContent=t,1200)})})</script>"""


def esc(s) -> str:
    return html.escape(str(s))


def page(title: str, body: str, nav: bool = True) -> str:
    head = ('<header><a href="/">Apple Music → Lidarr</a><div class=row><a href="/settings" class=mute>Settings</a><a href="/account" class=mute>Account</a>'
            '<form class=inline method=post action=/logout><button class=ghost>Sign out</button></form></div></header>') if nav else ""
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style>{head}<main>{body}<footer class=mute style='margin:24px 0 8px;text-align:center'>"
            f"<a href='https://github.com/egamer24/applemusic-lidarr-sync/releases' style='color:inherit'>v{VERSION}</a></footer></main>{COPY_JS}</html>")


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


URL_RE = re.compile(r"https?://[^\s/?#]+(/[^\s?#]*)?")


def site_urls(h) -> tuple[str, str]:
    """(external, internal) base URLs from Settings; external falls back to the address in use."""
    with lock:
        cfg = dict(state.get("settings", {}))
    return cfg.get("external_url") or base_url(h), cfg.get("internal_url", "")


def setup_code(base: str, token: str) -> str:
    """What the Shortcut asks for on import: the /ingest URL and the token, separated by a space."""
    return f"{base}/ingest {token}"


def settings_page(msg: str = "", ok: bool = False) -> str:
    with lock:
        cfg = dict(state.get("settings", {}))
    note = f"<p class={'ok' if ok else 'warn'}>{esc(msg)}</p>" if msg else ""
    return page("Settings", f"""{FORM_CSS}<p><a href="/">← Overview</a></p>{note}
<div class=card style="max-width:620px"><h2>Addresses</h2>
<p class=mute>Used to build each user's <b>setup code</b> for the iPhone Shortcut and the Lidarr list URL,
so nobody has to type an address.</p>
<form method=post action=/settings class=stack>
<label>External URL, for phones away from home (e.g. <code>https://music.example.com</code>)
<input name=external_url type=url value="{esc(cfg.get('external_url', ''))}" placeholder="https://music.example.com" autocapitalize=none></label>
<label>Internal URL, on your home network (e.g. <code>http://192.168.1.10:8099</code>). Lidarr uses this one.
<input name=internal_url type=url value="{esc(cfg.get('internal_url', ''))}" placeholder="http://192.168.1.10:8099" autocapitalize=none></label>
<button>Save</button></form>
<p class=mute>Leave one empty if you don't use it. Without an external URL, the address you're using right now is used.</p></div>
<div class=card style="max-width:620px"><h2>Albums (optional)</h2>
<p class=mute>Off by default: Lidarr adds each artist with whatever its import list's <b>Monitor</b> setting says.
On: for each artist, Lidarr monitors only the albums in your Apple Music library (albums it already has files for stay monitored),
and searches for them. Needs the <b>album version of the Shortcut</b>; uploads from the regular Shortcut change nothing.
Checked hourly. In Lidarr, set the Apple Music list's <b>Monitor</b> to <b>None</b> so new artists don't start with every album.</p>
<form method=post action=/settings class=stack>
<input type=hidden name=albums_form value=1>
<label class=row style="display:flex"><input type=checkbox name=albums_only {'checked' if cfg.get('albums_only') else ''}> Only monitor albums in the library</label>
<label>Lidarr URL (e.g. <code>http://192.168.1.10:8686</code>)
<input name=lidarr_url type=url value="{esc(cfg.get('lidarr_url', ''))}" placeholder="http://192.168.1.10:8686" autocapitalize=none></label>
<label>Lidarr API key (Lidarr → Settings → General){' — saved; leave empty to keep it' if cfg.get('lidarr_key') else ''}
<input name=lidarr_key type=password autocomplete=off></label>
<button>Save</button></form>{album_report(cfg)}</div>""")


def album_report(cfg: dict) -> str:
    with lock:
        r = dict(state.get("album_sync", {}))
    if not cfg.get("albums_only") or not r:
        return ""
    err = f"<p class=warn>Last error: {esc(r['error'])}</p>" if r.get("error") else ""
    if "at" not in r:
        return err
    miss = "".join(f"<li>{esc(n)}</li>" for n in r.get("unmatched", [])[:500])
    return f"""{err}<p class=mute>Last check {esc(r['at'])}: {r['managed']} artists, {r['matched']} albums matched
(+{r['monitored']} monitored, −{r['unmonitored']} unmonitored).</p>
<details><summary class=mute>{len(r.get('unmatched', []))} library albums not found in Lidarr</summary>
<p class=mute>Usually singles and EPs (only listed if Lidarr's metadata profile includes them), compilations, or titles that differ.
Artists Lidarr hasn't added yet show up here until it does.</p><ul class=cols>{miss}</ul></details>"""


def dashboard(h) -> str:
    with lock:
        cache = dict(state["cache"])
        users = {u: (list(d["names"]), d["last_ingest"]) for u, d in sorted(state["users"].items())}
        names = all_names()
    tot = stats(names, cache)
    external, internal = site_urls(h)
    lidarr_url = f"{internal or external}/list?token={TOKEN}"
    lidarr_note = ("Uses your internal URL, so the token stays on your home network." if internal else
                   "The URL contains the API token. Set your <b>internal URL</b> in <a href='/settings'>Settings</a> "
                   "so Lidarr uses your home network and the token never passes through a reverse proxy's logs.")
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
<p class=mute>Settings → Import Lists → + → Custom List, with this as the List URL. It combines every user.
{lidarr_note}</p>
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
        n_albums = len(d.get("albums", []))
        cache = dict(state["cache"])
    s = stats(names, cache)
    external, internal = site_urls(h)
    ingest_url = f"{external}/ingest"
    tok = ""
    if new_token:
        codes = [("Away from home", external)] + ([("Home network only", internal)] if internal else [])
        rows = "".join(f"""<p style="margin:12px 0 4px"><b>{label}</b></p><div class=secret>{esc(setup_code(base, new_token))}</div>
<div class=row style="margin-top:6px"><button data-copy="{esc(setup_code(base, new_token))}">Copy setup code</button></div>"""
                       for label, base in codes)
        hint = "" if internal or state.get("settings", {}).get("external_url") else \
            "<p class=mute>Set your addresses in <a href='/settings'>Settings</a> to get the right URL in here.</p>"
        tok = f"""<div class=card><h2>Setup code for {esc(user)}</h2>
<p class=warn>Shown only once. Send it to {esc(user)} privately; you can always generate a new one.</p>
<p class=mute>Paste it when the Shortcut asks on import. It holds the upload address and {esc(user)}'s token.</p>
{rows}{hint}<details style="margin-top:10px"><summary class=mute>Token on its own</summary>
<div class=secret>{esc(new_token)}</div></details></div>"""
    tok_state = ("Has a personal token." if has_tok else
                 "Uses the API token (TOKEN) with no ?user. Generate a personal token to switch." if user == "default" else "No token yet.")
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
<ol><li><a href="/shortcut">Download the Shortcut</a> on the iPhone and tap <b>Add Shortcut</b>.
Using album sync (<a href="/settings">Settings</a>)? Get the <a href="/shortcut/albums">album version</a> instead.{f" Last upload had {n_albums} albums." if n_albums else ""}</li>
<li>When it asks, paste this user's <b>setup code</b> (shown once when you generate a token below). {esc(tok_state)}</li>
<li>iPhone Settings → Apps → Shortcuts → Advanced → <b>Allow Sharing Large Amounts of Data</b> (iOS 17 and earlier: Settings → Shortcuts → Advanced). Without it iOS blocks big libraries.</li>
<li>Shortcuts → Automation → Time of Day → daily → run <b>Sync to Lidarr</b>.</li></ol>
<div class=row><form class=inline method=post action="/u/{esc(user)}/token"><button>{'Regenerate' if has_tok else 'Generate'} token</button></form>
<form class=inline method=post action="/u/{esc(user)}/delete" onsubmit="return confirm('Delete {esc(user)} and their list? Artists already in Lidarr stay.')"><button class=danger>Delete user</button></form></div>
<p class=mute>Regenerating stops the old token working straight away.</p></div>
<div class=card><h2>Upload history</h2><div class=wrap><table>
<tr><th>When</th><th>Artists</th><th>New</th><th>Gone</th></tr>{hist_rows}</table></div></div>
<div class=card><h2>No match ({len(s['unmatched'])})</h2><ul class=cols>{unmatched}</ul></div>
<div class=card><details><summary><b>Matched artists ({s['matched']})</b></summary><ul class=cols>{matched}</ul></details></div>""")


FORM_CSS = "<style>.stack{display:grid;gap:10px}.stack label{display:grid;gap:4px;font-size:13px;color:var(--mute)}</style>"


def auth_page(title: str, inner: str, error: str = "") -> str:
    err = f"<p class=warn>{esc(error)}</p>" if error else ""
    return page(title, f"""{FORM_CSS}<div class=card style="max-width:420px;margin:8vh auto">
<h1>Apple Music → Lidarr</h1>{err}{inner}</div>""", nav=False)


def login_page(error: str = "") -> str:
    return auth_page("Sign in", """<form method=post action=/login class=stack>
<label>Username<input name=user autocomplete=username required autofocus autocapitalize=none></label>
<label>Password<input type=password name=password autocomplete=current-password required></label>
<button>Sign in</button></form>""", error)


def setup_page(error: str = "") -> str:
    return auth_page("Set up", """<h2>First-time setup</h2>
<p class=mute>Create the admin account. The setup code is printed in the container log
(<code>docker logs</code>); the <code>TOKEN</code> from the container's environment also works.</p>
<form method=post action=/setup class=stack>
<label>Setup code<input name=code required autocomplete=off></label>
<label>Admin username<input name=user autocomplete=username required autocapitalize=none pattern="[A-Za-z0-9_.@\\-]{1,64}"></label>
<label>Password (at least 10 characters)<input type=password name=password autocomplete=new-password minlength=10 required></label>
<label>Repeat password<input type=password name=password2 autocomplete=new-password minlength=10 required></label>
<button>Create admin</button></form>""", error)


def two_factor_page(error: str = "") -> str:
    return auth_page("Two-factor code", """<h2>Two-factor code</h2>
<p class=mute>Enter the 6-digit code from your authenticator app, or one of your recovery codes.</p>
<form method=post action=/login/2fa class=stack>
<label>Code<input name=code inputmode=numeric autocomplete=one-time-code required autofocus autocapitalize=none></label>
<button>Verify</button></form><p><a href="/login">Start over</a></p>""", error)


def codes_card(codes: list[str]) -> str:
    items = "".join(f"<li><code>{esc(c)}</code></li>" for c in codes)
    return f"""<div class=card><h2>Recovery codes</h2>
<p class=warn>Shown only once. Save them somewhere safe; each one signs you in once if you lose your phone.</p>
<ul class=cols>{items}</ul><div class=row style="margin-top:10px">
<button class=ghost data-copy="{esc(chr(10).join(codes))}">Copy all</button></div></div>"""


def account_page(msg: str = "", ok: bool = False, extra: str = "") -> str:
    a = state["admin"]
    note = f"<p class={'ok' if ok else 'warn'}>{esc(msg)}</p>" if msg else ""
    if a.get("totp"):
        tfa = f"""<p><span class="pill ok">On</span> {len(a.get('recovery', []))} recovery codes left.</p>
<form method=post action=/account/2fa/recovery class=stack>
<label>Current code<input name=code inputmode=numeric autocomplete=one-time-code required></label>
<button class=ghost>New recovery codes</button></form>
<hr style="border:0;border-top:1px solid var(--line);margin:16px 0">
<form method=post action=/account/2fa/disable class=stack>
<label>Password<input type=password name=password autocomplete=current-password required></label>
<label>Code or recovery code<input name=code autocomplete=one-time-code required></label>
<button class=danger>Turn off two-factor</button></form>"""
    else:
        tfa = """<p><span class="pill warn">Off</span> Add a 6-digit code from an authenticator app
(iPhone Passwords, Google Authenticator, 1Password, …) to every sign-in.</p>
<form method=post action=/account/2fa/start><button>Set up two-factor</button></form>"""
    return page("Account", f"""{FORM_CSS}<p><a href="/">← Overview</a></p>{note}{extra}
<div class=card style="max-width:520px"><h2>Two-factor sign-in</h2>{tfa}</div>
<div class=card style="max-width:520px"><h2>Change password</h2>
<p class=mute>Signed in as <b>{esc(a['user'])}</b>. Changing the password signs out every other browser.</p>
<form method=post action=/account class=stack>
<label>Current password<input type=password name=current autocomplete=current-password required></label>
<label>New password (at least 10 characters)<input type=password name=password autocomplete=new-password minlength=10 required></label>
<label>Repeat new password<input type=password name=password2 autocomplete=new-password minlength=10 required></label>
<button>Change password</button></form></div>""")


def totp_setup_card(user: str, secret: str, error: str = "") -> str:
    issuer = "AppleMusic-Lidarr"
    uri = f"otpauth://totp/{issuer}:{urllib.parse.quote(user)}?" + urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": 6, "period": 30})
    grouped = " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
    err = f"<p class=warn>{esc(error)}</p>" if error else ""
    return f"""<div class=card style="max-width:520px"><h2>Set up two-factor</h2>{err}
<ol><li>On this iPhone: <a href="{esc(uri)}">add to Passwords / authenticator app</a>.<br>
Anywhere else: add an account in your authenticator app and enter this key:
<div class=secret style="margin:6px 0">{esc(grouped)}</div>
<button class=ghost data-copy="{esc(secret)}">Copy key</button></li>
<li>Enter the 6-digit code it shows.</li></ol>
<form method=post action=/account/2fa/enable class=stack>
<label>Code<input name=code inputmode=numeric autocomplete=one-time-code required autofocus></label>
<button>Turn on two-factor</button></form></div>"""


class Handler(BaseHTTPRequestHandler):
    def query(self) -> dict:
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def given_token(self) -> str:
        t = self.query().get("token", [""])[0].strip() or self.headers.get("Authorization", "").strip()
        return re.sub(r"^bearer\s+", "", t, flags=re.I).strip()  # pasted tokens often carry spaces/newlines

    def is_admin_token(self, t: str) -> bool:
        return bool(TOKEN) and hmac.compare_digest(t.encode(), TOKEN.encode())

    def cookie(self, name: str) -> str:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return ""

    def set_cookie(self, name: str, value: str, max_age: int) -> str:
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return f"{name}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def is_admin(self) -> bool:
        """Fully signed in to the web GUI (password and, when on, 2FA)."""
        with lock:
            a = state.get("admin")
            want = session_for(a) if a else ""
        return bool(want) and hmac.compare_digest(self.cookie("amls").encode(), want.encode())

    def is_pending(self) -> bool:
        """Password accepted, waiting for the 2FA code (5 minutes)."""
        exp, _, _ = self.cookie("amls2fa").partition(":")
        with lock:
            a = state.get("admin")
            if not (a and a.get("totp") and exp.isdigit() and int(exp) > time.time()):
                return False
            want = pending_for(a, int(exp))
        return hmac.compare_digest(self.cookie("amls2fa").encode(), want.encode())

    def session_cookies(self) -> list[str]:
        return [self.set_cookie("amls", session_for(state["admin"]), 2592000), self.set_cookie("amls2fa", "", 0)]

    def sign_in(self, to: str = "/"):
        self.redirect(to, {"Set-Cookie": self.session_cookies()})

    def failed(self, body: str):
        record_failure()
        time.sleep(1)  # slow down guessing
        self.html(body, 401)

    def send(self, code: int, body, ctype="application/json", headers: dict | None = None):
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            for one in v if isinstance(v, list) else [v]:
                self.send_header(k, one)
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
        if path in SHORTCUTS:  # public: the files hold no secrets, they ask for URL + token on import
            try:
                with open(os.path.join(HERE, SHORTCUTS[path]), "rb") as f:
                    data = f.read()
            except FileNotFoundError:
                return self.send(404, {"error": "shortcut not bundled"})
            return self.send(200, data, "application/octet-stream",
                             {"Content-Disposition": f'attachment; filename="{SHORTCUTS[path]}"'})
        if path == "/list":  # Lidarr: API token
            if not (self.is_admin_token(self.given_token()) or self.is_admin()):
                return self.send(401, {"error": "bad or missing token"})
            return self.send(200, [{"MusicBrainzId": i} for i in mbids()])
        if "admin" not in state:
            return self.html(setup_page()) if path == "/setup" else self.redirect("/setup")
        if path == "/login/2fa":
            return self.html(two_factor_page()) if self.is_pending() else self.redirect("/login")
        if path in ("/login", "/setup"):
            return self.redirect("/") if self.is_admin() else self.html(login_page())
        if not self.is_admin():
            return self.redirect("/login")
        if path == "/settings":
            return self.html(settings_page())
        if path == "/account":
            return self.html(account_page("Password changed." if "changed" in self.query() else "", ok=True))
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
        if "admin" not in state and path != "/setup":
            return self.redirect("/setup")
        if path in ("/setup", "/login", "/login/2fa") and locked_out():
            return self.html(auth_page("Locked", "<p>Too many failed attempts. Try again in 15 minutes.</p>"), 429)
        if path == "/setup":
            f = self.form()
            with lock:
                if "admin" in state:
                    return self.redirect("/login")
                code = f.get("code", "").strip()
                user = f.get("user", "").strip().casefold()
                pw = f.get("password", "")
                err = ("Wrong setup code." if not (hmac.compare_digest(code.encode(), SETUP_CODE.encode()) or self.is_admin_token(code))
                       else "Username: 1–64 letters, digits, . _ - or @." if not re.fullmatch(r"[a-z0-9_.@-]{1,64}", user)
                       else "Password must be at least 10 characters." if len(pw) < 10
                       else "Passwords don't match." if pw != f.get("password2", "") else "")
                if err == "Wrong setup code.":
                    record_failure()
            if err:
                time.sleep(1)
                return self.html(setup_page(err), 400)
            with lock:
                if "admin" in state:
                    return self.redirect("/login")
                state["admin"] = {"user": user, **hash_password(pw)}
                save()
            print(f"admin account '{user}' created", flush=True)
            return self.sign_in()
        if path == "/login":
            f = self.form()
            with lock:
                ok = check_password(f.get("user", "").strip(), f.get("password", ""))
                a = state["admin"]
                pending = pending_for(a, int(time.time()) + 300) if ok and a.get("totp") else ""
            if not ok:
                return self.failed(login_page("Wrong username or password."))
            if pending:
                return self.redirect("/login/2fa", {"Set-Cookie": self.set_cookie("amls2fa", pending, 300)})
            return self.sign_in()
        if path == "/login/2fa":
            if not self.is_pending():
                return self.redirect("/login")
            with lock:
                ok = check_second_factor(state["admin"], self.form().get("code", ""))
            return self.sign_in() if ok else self.failed(two_factor_page("That code didn't work."))
        if path == "/logout":
            return self.redirect("/login", {"Set-Cookie": "amls=; Path=/; Max-Age=0"})
        if not self.is_admin():
            return self.redirect("/login")
        if path == "/account":
            f = self.form()
            with lock:
                a = state["admin"]
                pw = f.get("password", "")
                if not check_password(a["user"], f.get("current", "")):
                    return self.html(account_page("Current password is wrong."), 400)
                if len(pw) < 10 or pw != f.get("password2", ""):
                    return self.html(account_page("New passwords must match and be at least 10 characters."), 400)
                a.update(hash_password(pw))
                save()
            return self.sign_in("/account?changed=1")
        if path.startswith("/account/2fa/"):
            return self.two_factor_settings(path.rsplit("/", 1)[1], self.form())
        if path == "/settings":
            f = self.form()  # two forms post here; each changes only its own fields
            with lock:
                new = dict(state.get("settings", {}))
            for k in ("lidarr_url",) if "albums_form" in f else ("external_url", "internal_url"):
                v = f.get(k, "").strip().rstrip("/")
                if v and not URL_RE.fullmatch(v):
                    return self.html(settings_page(f"Not a valid http(s) address: {v}"), 400)
                new[k] = v
            if "albums_form" in f:
                new["albums_only"] = f.get("albums_only") == "on"
                new["lidarr_key"] = f.get("lidarr_key", "").strip() or new.get("lidarr_key", "")
            with lock:
                if new.get("albums_only") and not (new.get("lidarr_url") and new.get("lidarr_key")):
                    return self.html(settings_page("Album sync needs the Lidarr URL and API key."), 400)
                state["settings"] = new
                save()
            album_wake.set()
            return self.html(settings_page("Saved.", ok=True))
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

    def two_factor_settings(self, action: str, f: dict):
        with lock:
            a = state["admin"]
            code = f.get("code", "")
            if action == "start":
                a["totp_pending"] = new_totp_secret()
                save()
                return self.html(account_page(extra=totp_setup_card(a["user"], a["totp_pending"])))
            if action == "enable":
                step = totp_step(a.get("totp_pending"), code)
                if not step:
                    return self.html(account_page(extra=totp_setup_card(a["user"], a.get("totp_pending") or new_totp_secret(),
                                                                       "That code didn't match. Check the phone's clock and try the next code.")), 400)
                a["totp"], a["totp_last"] = a.pop("totp_pending"), step
                codes = new_recovery_codes(a)
                save()
                return self.html(account_page("Two-factor is on.", True, codes_card(codes)), headers={"Set-Cookie": self.session_cookies()})
            if action == "recovery" and a.get("totp"):
                step = totp_step(a["totp"], code, a.get("totp_last", 0))
                if not step:
                    return self.html(account_page("That code didn't work."), 400)
                a["totp_last"] = step
                codes = new_recovery_codes(a)
                save()
                return self.html(account_page("New recovery codes made; the old ones no longer work.", True, codes_card(codes)))
            if action == "disable" and a.get("totp"):
                if not (check_password(a["user"], f.get("password", "")) and check_second_factor(a, code)):
                    return self.html(account_page("Password or code is wrong."), 400)
                for k in ("totp", "totp_last", "recovery", "totp_pending"):
                    a.pop(k, None)
                save()
                return self.html(account_page("Two-factor is off."), headers={"Set-Cookie": self.session_cookies()})
        self.redirect("/account")

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
        names, albums = parse_upload(body, ctype)
        if not names:
            print(f"empty ingest for {user}: {length} bytes, {ctype!r}", flush=True)
            return self.send(400, {"error": "no artist names in body", "bytes": length, "content_type": ctype})
        self.send(200, ingest(user, names, albums))

    def log_message(self, fmt, *args):
        if "/healthz" not in self.path:
            print(re.sub(r"token=[^ &]*", "token=***", f"{self.address_string()} {fmt % args}"), flush=True)


def drop_privileges():
    """Started as root (CasaOS, Unraid, plain `docker run`): give DATA to PUID:PGID, then run as them."""
    if os.getuid() != 0:
        return  # already unprivileged, e.g. `user: 1000:1000`
    uid, gid = int(os.environ.get("PUID", "1000")), int(os.environ.get("PGID", "1000"))
    if uid == 0:
        raise SystemExit("PUID=0 would run the web server as root; pick an unprivileged user")
    os.makedirs(DATA, exist_ok=True)
    for root, dirs, files in os.walk(DATA):
        for name in [root, *(os.path.join(root, n) for n in dirs + files)]:
            os.lchown(name, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    print(f"running as {uid}:{gid}", flush=True)


if __name__ == "__main__":
    drop_privileges()
    os.umask(0o077)  # state.json holds password hash, 2FA secret and signing key
    os.makedirs(DATA, exist_ok=True)
    try:
        os.chmod(STATE_FILE, 0o600)
    except FileNotFoundError:
        pass
    except OSError as e:  # e.g. restored by root; the next save rewrites it as ours with 600
        print(f"warning: can't chmod {STATE_FILE}: {e}", flush=True)
    load()
    state.setdefault("secret", secrets.token_hex(32))
    if not TOKEN:
        TOKEN = state.setdefault("token", secrets.token_hex(24))
    if RESET_ADMIN and state.pop("admin", None):
        print("RESET_ADMIN: admin account removed; remove RESET_ADMIN and open the GUI to set up again", flush=True)
    save()
    if "admin" not in state:
        print(f"No admin account yet. Open the web GUI and use setup code: {SETUP_CODE}", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=album_worker, daemon=True).start()
    print(f"listening on :{PORT}", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
