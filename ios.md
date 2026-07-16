# Native iOS App — Project Plan

A dedicated Swift/SwiftUI client for the mp3tools library. This is a **committed
Path A effort**: a fully custom app (not a web wrapper, not an off-the-shelf
Subsonic client), for maximum control over playback and UX.

The app is a pure client of the existing FastAPI server ([server.py](server.py)) —
no changes to the library, tagging, standardization, ripping, or DAP sync. The
server-side API contract the app needs is **already built** (bearer-token auth +
library manifest; see below).

> Environment note: the app is a separate Xcode project built on a Mac. This
> repo holds only the server half. Swift written here can't be compiled/run in
> this repo's environment.

## Why native (the three goals)

Everything painful about the web player on iOS disappears in a native client:

1. **True gapless with no re-encoding.** The web `<audio>` element can't trim MP3
   encoder delay/padding, which is why the whole server-side transcode/stream
   saga exists ([static/player.js](static/player.js) header comments). Native
   decodes to PCM and schedules sample-accurately, so albums play gapless from
   the **original MP3 bytes** — no transcode, bandwidth = the file's own bitrate.
2. **Offline download + playback.** Download tracks to local storage, keep a
   local index, play with no network. The web app can't do durable offline.
3. **Robust background/lock-screen playback.** `AVAudioSession` + the audio
   background entitlement + `MPNowPlayingInfoCenter`/`MPRemoteCommandCenter` give
   reliable locked-screen playback, real duration, and scrubbing — the exact
   things we fought the browser for across multiple sessions.

Tradeoffs: a second codebase in Swift, iOS-only payoff (Android Chrome already
does true gapless via the web Web Audio engine), and Apple Developer signing
overhead.

## Architecture (six layers, one is hard)

1. **API client** — `URLSession` + async/await; typed models over the JSON
   endpoints below.
2. **Local store** — SwiftData (iOS 17+): `Artist` / `Album` / `Track`, plus a
   `DownloadedAsset` (local file URL, `sig`, state). Mirrors the manifest.
3. **Sync service** — fetch manifest, diff `sig` against the local store,
   download deltas with `URLSession` **background** download tasks (survive app
   suspension), prune orphans.
4. **Audio engine** — `AVAudioEngine` + `AVAudioPlayerNode`, scheduling decoded
   PCM buffers back-to-back. **The hard 20%** — see below.
5. **Now-playing integration** — `MPNowPlayingInfoCenter` +
   `MPRemoteCommandCenter`; `AVAudioSession` interruption/route-change handling.
6. **UI** — SwiftUI: browse (artists/albums/genres), album detail, player,
   downloads, settings.

Layers 1–3 and 6 are standard app work. Layer 4 is the only real technical risk.

## Server API contract (already built)

All read-only; the app never mutates the library.

| Endpoint | Use |
|----------|-----|
| `POST /api/auth/login` `{password, cid, device_name}` → `{role, token}` | Remote login. Store `token` in the Keychain. `cid` = a stable per-install UUID (Keychain). |
| `GET /api/app/manifest` → `{root, count, tracks:[…]}` | Whole-library sync manifest — `browse.library_manifest`. One flat entry per track. |
| `GET /api/track?path=<abs>&cid=<cid>` | Raw MP3 bytes, `audio/mpeg`, **HTTP Range supported**. The gapless audio source. |
| `GET /api/cover?path=<albumAbs>` | Album art (derive `albumAbs` = dirname of a track's `path`). |
| `GET /api/whoami` → `{role, …}` | Role check on launch (`owner`/`lan`/`member`/`pending`/`anonymous`). |

**Manifest track entry:**
```json
{
  "rel": "Artist/2024 - Album/01. Artist - Title.mp3",  // stable local id
  "path": "/abs/.../01. Artist - Title.mp3",            // for /api/track, /api/cover
  "album_rel": "Artist/2024 - Album",                    // client-side album grouping
  "size": 8123456,
  "sig": "8123456-1699999999000000000",                  // opaque size+mtime change token
  "artist": "...", "albumartist": "...", "album": "...",
  "title": "...", "track": "3/12", "year": "2024", "genre": "Rock",
  "duration": 251, "bitrate": "320"
}
```

### Auth model (important)

The server's session system is a bearer-token system; the browser just delivers
the token via cookie. The app uses the **same** flow with an
`Authorization: Bearer <token>` header instead:

- **On the LAN** (server run plain or `--lan`): the app is the `lan` role
  automatically — read-only, **no login needed**. Just hit the endpoints.
- **Over the internet** (`--remote`, behind the Caddy TLS proxy): `POST
  /api/auth/login` once → store `token` → send `Authorization: Bearer <token>`
  on every request. A new device lands **pending**; the owner approves its `cid`
  in the web **Access** view, and it becomes a read-only **member**.
- The app can **never** be `owner` (loopback-only by construction) and members
  are read-only, so nothing the app can do is destructive — by design. See the
  remote-access security model in [CLAUDE.md](CLAUDE.md).

Pass `cid` on `/api/track` like the web player does so the server's Devices view
attributes "now playing" to the iPhone.

## Offline sync (layer 3 design)

The single source of truth is the manifest; the local store is a cache.

1. **Fetch** `GET /api/app/manifest`.
2. **Diff** each entry's `sig` against the local `DownloadedAsset`:
   - unknown `rel`, or `sig` changed → (re)download via `/api/track?path=<path>`.
   - `rel` gone from the manifest → delete the local file + row.
   - `sig` unchanged → keep.
3. **Download** deltas with a background `URLSessionDownloadTask`; on completion,
   move the file into Application Support and record `{rel, sig, localURL}`.

`sig` = `size + mtime`, the same change signal `sync_library` trusts for DAP
mirroring ([sync_library.py](sync_library.py) `file_matches`). Re-tagging a track
(standardize) bumps its mtime → the app re-downloads it. Treat `sig` as opaque
(equality only) so the server can switch to a real content hash later without a
client change.

Let the user pick what to keep offline (whole library, chosen artists/albums, or
"downloaded on play"). A large library's manifest is a multi-MB payload — fetch
it periodically, not per-navigation.

## Audio engine (layer 4 — the core)

Use **`AVAudioEngine` + `AVAudioPlayerNode`** and schedule decoded segments
back-to-back with sample accuracy. This is the only reliable gapless path.
Do **not** use `AVQueuePlayer`/`AVPlayer` (transitions are not reliably gapless),
and do not wrap the web UI (no gapless benefit).

Per track:

1. **Source** the MP3 — the local file when downloaded, else stream from
   `/api/track` (Range support means progressive; simplest first cut is
   full-file-to-temp, matching the desktop engine's whole-track decode).
2. **Decode to PCM** — `AVAudioFile` → `AVAudioPCMBuffer`.
3. **Trim encoder delay/padding for gapless.** Port the Xing/Info+LAME parse from
   `parseGapless` in [static/player.js](static/player.js) (12-bit delay + 12-bit
   padding); drop `delay` frames from the buffer head and `padding` from the tail
   when scheduling. The reference parse also lives in
   [mp3header.py](mp3header.py) (`lame_delay_padding`, `DUMMY_DELAY_PADDING`) —
   the server already depends on it, so the algorithm is proven.
   ⚠️ **Verify the decoder-priming offset on iOS.** The web code adds `528 + 1`
   samples to the delay (and subtracts from padding) for libmpg123's priming.
   Core Audio's MP3 decoder may prime differently — confirm against a
   known-gapless album (a live set or DJ mix) and adjust the constant rather than
   assuming 529.
4. **Schedule gaplessly.** One `AVAudioPlayerNode`;
   `scheduleBuffer`/`scheduleSegment` appends the next track's trimmed buffer
   immediately after the current; the completion handler advances the queue and
   pre-decodes track N+1 (always stay one track ahead). Native analog of
   `scheduleNext`/`onSourceEnded` in the desktop engine.

## Background playback & lock screen (layer 5)

- `AVAudioSession.setCategory(.playback)` + `setActive(true)`.
- Enable the **Audio** background mode in target capabilities.
- `MPNowPlayingInfoCenter` — title/artist/artwork/**duration**/elapsed.
- `MPRemoteCommandCenter` — play/pause/next/prev, and (unlike the web, which
  suppressed it to keep track-skip buttons) real **seek**, since native gives
  finer control.
- Handle `AVAudioSession` interruptions (calls) and route changes (headphones
  unplugged → pause). Known checklist, not research.

## Data model sketch (SwiftData)

```
Artist  { name, albums: [Album] }
Album   { rel, title, year, genre, artist, coverURL, tracks: [Track] }
Track   { rel, path, title, trackNo, duration, bitrate, sig, asset: DownloadedAsset? }
DownloadedAsset { rel, sig, localURL, downloadedAt, state }   // state: queued|downloading|ready|failed
Settings { serverBaseURL, token(Keychain), cid(Keychain), offlinePolicy, streamQuality }
```

## Milestone plan

Build the **walking skeleton first** — it de-risks the only hard part before any
UI breadth. Do not build screens first.

- **M1 — Prove gapless + background (the whole risk).** Ugly single screen: fetch
  one album (`/api/app/manifest` or `/api/album`), play it gaplessly via
  `AVAudioEngine` from the server's MP3s, working with the **screen locked** and
  lock-screen controls live. If a locked iPhone plays a gapless album, the
  project is proven; everything else is standard work.
- **M2 — Library browse.** Manifest → SwiftData → SwiftUI artist/album/genre
  navigation, album detail, search. Play-from-anywhere into the M1 engine.
- **M3 — Offline.** Download manager (background tasks), local store, the
  sig-diff sync loop, offline playback, per-artist/album download toggles.
- **M4 — Remote auth.** Login flow, Keychain token, `Authorization: Bearer`,
  pending/approved handling, LAN-vs-remote base URL.
- **M5 — Polish.** Artwork, now-playing → "show in library", queue editing,
  gapless verification across odd albums, interruption/route-change edge cases.

## Reuse from this repo

- **Gapless header parse** — [mp3header.py](mp3header.py) and `parseGapless` in
  [static/player.js](static/player.js). Port the algorithm; re-verify the iOS
  priming constant.
- **Change signal for sync** — `sync_library.file_matches` (size + mtime) is the
  same idea as the manifest `sig`.
- **Auth/roles** — [CLAUDE.md](CLAUDE.md) remote-access security model; the app is
  a read-only member, never owner.

## Distribution

Apple Developer account ($99/yr). TestFlight or sideload is enough for a personal
tool — no App Store review unless you want it public. Free 7-day provisioning
works for early M1 experiments without the paid account.
