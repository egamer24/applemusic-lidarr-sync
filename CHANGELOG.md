# Changelog

Versions follow [semantic versioning](https://semver.org): **patch** (1.0.**1**) for fixes, docs and packaging; **minor** (1.**1**.0) for new features that don't change existing setups; **major** (**2**.0.0) for changes that need you to change your setup. Each release is published as `ghcr.io/egamer24/applemusic-lidarr-sync:<version>`, `:<major>.<minor>` and `:latest`.

## 1.2.0

- **Album sync (optional, off by default):** Lidarr monitors only the albums in your Apple Music library instead of every album by each artist. Turn it on in **Settings → Albums** with your Lidarr URL and API key, and use the new **album version** of the Shortcut (`/shortcut/albums`), which sends `Album Artist<TAB>Album` per song. Albums with files in Lidarr stay monitored; artists with no matched album are left alone. See the README section *Only the albums you have*.
- The regular Shortcut and existing setups are unchanged. With album sync off, or for people still on the regular Shortcut, nothing new happens.

## 1.1.0

- **Settings page** with an external URL (phones away from home) and an internal URL (home network).
- **Setup codes:** creating or regenerating a user's token shows a one-line setup code (`<url>/ingest <token>`) for each address, with copy buttons. The Shortcut now asks only for that code and splits it itself, so nobody types an address.
- The dashboard's Lidarr list URL uses the internal URL when set, so the API token stays on your home network.
- Shortcuts installed before 1.1.0 keep working unchanged.

## 1.0.2

- Fix the container health check: it asked `localhost`, which resolves to IPv6 `::1` in many containers while the app listens on IPv4, so Docker/CasaOS showed the container as *unhealthy* even though it worked.
- CasaOS app tested on a real CasaOS host (arm64): data folder handed to `PUID:PGID`, app runs unprivileged, setup page served.

## 1.0.1

- **CasaOS app** (`casaos/docker-compose.yml`): import it under App Store → Custom Install → Import.
- **`PUID` / `PGID`**: the container starts as root only long enough to give `/data` to that user, then drops root. Root-owned data folders (CasaOS, Unraid, plain bind mounts) now just work; `PUID=0` is refused. The Unraid template runs as `99:100` and no longer needs a manual `chown`.
- Docs: the iOS "Allow Sharing Large Amounts of Data" limit, with the setting's location on iOS 18+ and iOS 17 and earlier.
- The admin footer shows the running version.

## 1.0.0

First release: iPhone Shortcut → MusicBrainz matching → Lidarr Custom List; multi-user tokens; web admin with first-run setup, password, optional two-factor and sign-in lockout; per-user reports and upload history; Unraid template.
