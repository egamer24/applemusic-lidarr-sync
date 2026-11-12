# applemusic-lidarr-sync

Keep [Lidarr](https://lidarr.audio) following the artists in your Apple Music library, without a Mac and without an Apple Developer account.

```
iPhone Shortcut ──POST artist names──▶ this container ──▶ MusicBrainz lookup
                                              │
                         Lidarr "Custom List" ◀┘  GET /list → [{"MusicBrainzId": "..."}]
```

An iPhone Shortcut reads your library on the phone, using Apple's own Shortcuts actions, and posts the artist names to this container on a schedule. The container matches each name to a MusicBrainz artist and serves the matches as a Lidarr Custom List. Nothing ever signs in to your Apple account, so it stays within Apple's terms.

## Run it

```yaml
services:
  applemusic-lidarr-sync:
    image: ghcr.io/egamer24/applemusic-lidarr-sync:latest
    container_name: applemusic-lidarr-sync
    environment:
      TOKEN: change-me   # optional; long random string, e.g. `openssl rand -hex 24`
    ports:
      - "8099:8080"
    volumes:
      - ./data:/data     # must be writable by uid 1000
    restart: unless-stopped
```

| Variable | Default | |
|---|---|---|
| `TOKEN` | generated | API token for Lidarr's `/list` (and admin uploads). If unset, one is generated and stored in `state.json`; the GUI shows the full Lidarr URL either way |
| `RESET_ADMIN` | off | `true` deletes the admin account on start so you can run setup again (forgotten password). Remove it afterwards |
| `PORT` | `8080` | Port inside the container |
| `DATA_DIR` | `/data` | Where `state.json` (artist list + lookup cache) lives |
| `MB_USER_AGENT` | project URL | MusicBrainz asks every client to identify itself |

## Web GUI

On first visit the GUI asks you to **set up an admin account** (username + password). Setup also asks for a **setup code** so nobody else can claim the admin account first: it's printed in the container log (`docker logs applemusic-lidarr-sync`), or use your `TOKEN`. After that you sign in with the username and password; **Account** changes the password (and signs out other browsers).

From there you can:

- **Add users.** Each gets a personal token, shown once with copy buttons. Regenerate or delete it any time.
- **See reports:** totals, each user's artist count, matches and last upload (amber when it's over 36 hours old), plus per-user upload history with the artists that were added or removed, unmatched names, and matched artists linked to MusicBrainz.
- **Copy the Lidarr list URL** and **download the Shortcut** (`/shortcut`, public, it holds no secrets).

## Endpoints

| | Auth | |
|---|---|---|
| `POST /ingest` | user token, or API token + `?user=<name>` (default `default`) | Body: one artist per line, a JSON array, or `{"artists": [...]}`. Replaces that user's list |
| `GET /list` | API token (or signed in) | Lidarr Custom List JSON, every user combined |
| `GET /` | signed in | Web GUI (`/setup` on first run, `/login`, `/account`) |
| `GET /shortcut` | none | The iPhone Shortcut |
| `GET /healthz` | none | Health check |

Tokens go in `Authorization: Bearer <token>` or `?token=<token>`.

MusicBrainz allows about one lookup per second, so the first upload of a big library takes a while (the status page shows an estimate). Results are cached, so later uploads only look up new names.

## Several people, one server

Add each person in the web GUI and give them their own token. An upload only replaces that person's list, and `/list` gives Lidarr everyone's artists combined (each artist once), so one Lidarr Custom List covers the whole household.

## The iPhone Shortcut

**Quick way:** open [`shortcut/Sync to Lidarr.shortcut`](shortcut/Sync%20to%20Lidarr.shortcut) on your iPhone and tap **Add Shortcut**. It asks for your `/ingest` URL and your token (the user's own token from the web GUI). It's also served by the container at `/shortcut`. It sends every album artist in your library; edit the **Find Music** action to narrow it down.

(`shortcut/build.py` generates it; sign on a Mac with `shortcuts sign --mode anyone`.)

**Or build it yourself** in the Shortcuts app:

1. **Find Music**. Leave it as all music, or add a filter (for example a playlist, or **Is Loved** if your iOS version offers it) to send only part of your library.
2. **Get Details of Music** → **Album Artist**. Album Artist avoids most "feat." credits; **Artist** also works.
3. **Combine Text** with **New Lines**.
4. **Get Contents of URL**
   - URL: `https://your-host/ingest`
   - Method: **POST**
   - Headers: `Authorization` = `Bearer <your token>`
   - Request Body: **File** → *Combined Text*

Run it once and open the status page to check. iOS blocks Shortcuts from sending more than a few hundred items until you turn on **Settings → Apps → Shortcuts → Advanced → Allow Sharing Large Amounts of Data**.

Then in **Automation → New Automation → Time of Day**, choose daily and **Run Immediately**, and pick this Shortcut.

The phone has to reach the container when the automation runs. Either it runs while you're on home Wi-Fi, or you put the container behind a reverse proxy with HTTPS (only `/ingest` needs to be reachable from outside, and the token protects it).

## Add it to Lidarr

**Settings → Import Lists → + → Custom List**

- List URL: `http://<container-host>:8099/list?token=<your token>`
- Pick the root folder, quality profile and monitoring you want for new artists.

Lidarr refreshes custom lists every 6 hours at most. Removing an artist from your library drops it from the list but doesn't delete it from Lidarr unless you set Lidarr's "List Clean Library Level".

## Limits

- Artists are matched by name only. When several artists share a name, MusicBrainz's best match wins, which is usually the well-known one. Check the status page's "No match" list for names to fix in Lidarr by hand.
- Collaborations like `A & B` or `A feat. B` are looked up as a whole first, then split into parts.

## Development

```
docker build -t applemusic-lidarr-sync .
docker run --rm -v "$PWD:/src" -w /src --entrypoint python applemusic-lidarr-sync test_app.py
```

MIT licensed. Not affiliated with Apple, Lidarr or MusicBrainz.
