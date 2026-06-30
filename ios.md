# Native iOS app — guidelines for a future gapless client

Notes for when we build a dedicated iOS app to get **true gapless playback** on iPhone
(something the web `<audio>` backend can't do — see the mobile-backend comments in
[static/player.js](static/player.js#L1-L15)). This is a *future* effort, not a current
priority. Nothing here changes the existing web app; the server stays the single backend
and the iOS app is just another client.

## Why native (recap)

The web limits we hit on iOS all disappear natively:

- **Silent-switch muting / background suspension** — solved by `AVAudioSession` `.playback`
  category + the background-audio entitlement.
- **No sample-level trim** — the reason even double-buffered `<audio>` is only *near*-gapless.
  Native decodes to PCM and lets us schedule buffers sample-accurately and trim the MP3
  encoder delay/padding ourselves. This is the direct equivalent of the desktop Web Audio
  engine ([parseGapless](static/player.js#L324-L364), [scheduleNext](static/player.js#L400-L416)).

Tradeoff to keep in mind: it's a second client (Swift), iOS-only payoff (Android Chrome
already does true gapless via the existing Web Audio engine), and App Store / signing
overhead. Build it only if mobile listening becomes a primary use case.

## Use the right audio API

- ✅ **`AVAudioEngine` + `AVAudioPlayerNode`** — schedule decoded segments back-to-back with
  sample accuracy. The only reliable gapless path. This is what to build.
- ❌ **`AVQueuePlayer` / `AVPlayer`** — convenient but transitions are **not** reliably
  gapless. Don't use it for the queue.
- ❌ **Wrapping the web UI** (Capacitor / WKWebView) — no gapless benefit; still the browser
  audio stack.

## Architecture: reuse the existing server

The FastAPI server ([server.py](server.py)) already exposes everything a client needs — the
iOS app is a pure consumer, no server changes required to start:

| Endpoint | Use |
|----------|-----|
| `GET /api/album?path=…` | Track list JSON (each track has `path`, `title`, `artist`, `track`, `bitrate`) — [server.py:688](server.py#L688) |
| `GET /api/track?path=…&cid=…` | MP3 bytes, `audio/mpeg`, **HTTP Range supported** (`FileResponse`) — [server.py:719-726](server.py#L719-L726) |
| `GET /api/cover?path=…` | Album art for Now Playing |
| `GET /api/whoami` | Role (owner/lan/member/…) — drives what the client may do |

Pass a stable `cid` (client id) on `/api/track` like the web player does
([trackUrl](static/player.js#L18-L22)) so the server's Devices view can attribute
"now playing" to the iPhone.

## Audio pipeline (the core)

1. **Fetch** the MP3 for the next track over HTTP (the Range support means you can stream;
   simplest first cut is to download the whole file to a temp location, matching the
   desktop engine's "decode the full track" approach).
2. **Decode to PCM** — open with `AVAudioFile` (or `AVAudioConverter`) to get
   `AVAudioPCMBuffer`s.
3. **Trim encoder delay/padding for gapless.** Port the Xing/Info+LAME header parse from
   [parseGapless](static/player.js#L324-L364): read the 12-bit delay + 12-bit padding, then
   when scheduling, drop `delay` frames from the start and `padding` frames from the end of
   the decoded buffer. ⚠️ **Verify the decoder-priming offset on iOS.** The web code adds
   `528 + 1` samples to the delay and subtracts it from padding
   ([player.js:359-360](static/player.js#L359-L360)) because of libmpg123's priming. Core
   Audio's MP3 decoder may prime differently — confirm against a known-gapless album (a live
   set or DJ mix) and adjust the constant rather than assuming 529.
4. **Schedule gaplessly.** Keep one `AVAudioPlayerNode`; use
   `scheduleBuffer`/`scheduleSegment` to append the next track's trimmed buffer immediately
   after the current one, with the completion handler advancing the queue and pre-decoding
   track N+1 — the native analog of [scheduleNext / onSourceEnded](static/player.js#L400-L440).
   Pre-decode one track ahead so the handoff buffer is always ready.

## Background playback & lock screen

- `AVAudioSession.setCategory(.playback)` + `setActive(true)`.
- Enable the **Audio** background mode in the target capabilities.
- `MPNowPlayingInfoCenter` for title/artist/artwork/elapsed (richer than MediaSession was).
- `MPRemoteCommandCenter` for play/pause/next/prev. Note: the web code deliberately omitted
  seek/position to keep the lock screen showing track-skip buttons on iOS
  ([applyMediaHandlers](static/player.js#L139-L152)) — native gives finer control, so you
  can offer real seeking here if wanted.

## Auth / remote access

- On the **LAN** (`--lan`), the app talks to the server as the `lan` (read-only) role —
  fine for a playback client (GET-only).
- For **internet** use (`--remote`), the app must do the session login flow
  ([webauth.py](webauth.py)): POST `/api/auth/login`, then send the session cookie.
  Audio fetches are plain GETs through the proxy. Do **not** try to look like the owner —
  owner is loopback-only by construction (see the remote-access security model in
  [CLAUDE.md](CLAUDE.md)).

## Suggested phasing

1. **MVP:** browse → `/api/album` → play a queue gaplessly with `AVAudioEngine`, full-file
   download + decode, lock-screen controls, LAN-only.
2. Streaming/progressive download to cut start latency and memory.
3. Remote auth flow.
4. Polish: artwork, accent theming, "now playing → show in library", offline cache.

## Distribution

Apple Developer account ($99/yr); TestFlight or sideload is enough for a personal tool —
no App Store review needed unless you want it public.
