# Zanzibarr engine in mpv-library-qt: plan

Status 2026-11-20: forked to `bonejay/Zanzibarr` from upstream `aedd60e`. The engine (`rust/`) builds on Windows (MSVC) and all 255 engine tests pass. Nothing is integrated yet.

## Goal

Play Usenet releases found by **Atlas** (the indexer) instantly inside **mpv-library-qt**, with seeking and no download step. Use only Zanzibarr's Rust engine; the Flutter app, Dart code, and mobile builds aren't needed.

## Why Zanzibarr's engine instead of Nuvio

| | Zanzibarr engine | Nuvio engine |
|---|---|---|
| Language | Rust: same toolchain as Atlas, and it can be linked into the player | Go: a separate executable, or cgo with a second runtime in the player |
| Seeking | Byte offsets come from the yEnc begin/end of every segment, never guessed | Works (tested by Codex: seeks in 0.45–0.69 s) |
| Archives | Direct video, RAR4/RAR5 multi-volume, split 7z, STORE and LZMA/LZMA2, AES-256, compressed RAR via vendored libunrar | Fewer formats |
| Encrypted posts | Reads `<meta type="password">` from the NZB, which Atlas already exports | n/a |
| Broken posts | PAR2 verify and Reed-Solomon repair, served from an overlay | No |
| Obfuscated sets | Orders hash-named RAR volumes by content signature | No |
| Season packs | `list_playable_entries` returns each episode; play one by key | Filename filter |
| License | MIT (vendored unrar keeps its own license: decompression only) | — |

Nuvio's measured baseline (Codex, same machine, one 4K stream with three seeks) is the bar to beat: **12 MiB idle, 62 MiB peak working set, 111 MiB private, first frame ≈ 0.7 s**.

## Architecture: in-process, no HTTP

Zanzibarr's own app plays through a localhost HTTP server (`engine/server.rs`). That costs a socket hop, an HTTP range request on every seek, a port and token, and mpv's HTTP and reconnect behaviour. Instead, mpv reads straight from the engine:

```
Atlas (indexer, Newznab API on 127.0.0.1:9090)
   │  t=get&id=…  → NZB bytes (with password meta)
   ▼
mpv-library-qt ──► zz_open(nzb, entry, provider) ──► engine session
   │                                               (NNTP pool, segment map,
   │  loadfile usenet://<session>                    archive layer, PAR2 overlay)
   ▼
libmpv ── stream_cb: read / seek / size / cancel / close ──► zz_read(offset, buf)
                                                     │
                                       segment cache + prefetch → NNTP
```

libmpv's stream-callback API (`mpv_stream_cb_add_ro`, client API `stream_cb.h`) lets the app register a `usenet://` protocol. mpv calls our functions from its own demuxer thread:

- `open_fn(uri)` looks up the session that `zz_open` created and fills in the callbacks.
- `read_fn(buf, n)` → `zz_read(session, pos, buf, n)`, then advance `pos`.
- `seek_fn(offset)` only sets `pos`. The engine fetches lazily: the next read locates the segment and kicks off prefetch from there.
- `size_fn` → `zz_size` (known before playback starts, from the segment map).
- `cancel_fn` → `zz_cancel` (unblocks a read that is waiting on the network when the user stops or switches titles).
- `close_fn` → `zz_close` (drops the session: cache, prefetch tasks, NNTP connections).

What this saves compared with HTTP: one copy and a TCP round-trip per read, and an HTTP request per seek. No port, no token, nothing listening, no second process to start or watch. The HTTP server stays available in the engine for casting or debugging.

**The trade-off is crash isolation:** a fault in the engine now takes the player down with it. Mitigation: `catch_unwind` at every FFI entry point, no `unwrap` on the FFI path, and every error returned as a code mpv treats as a read error. The sidecar remains a fallback design if this ever bites.

## Engine side (this fork)

Add a workspace crate `rust-ffi/` (`cdylib` → `zanzibarr_mpv.dll`). It depends on the existing `rust/` crate as an rlib, so upstream files stay untouched and upstream merges stay trivial. It contains:

- One tokio runtime for the DLL. `zz_read` runs `block_on` on mpv's thread; mpv's threads are never runtime threads, so that's safe.
- `ZzSession` holding the prepared `StreamSource` (built the same way `prepare_stream_source` in `api/streaming.rs` builds it) and a cancel `watch`.
- `zz_read` implemented on the source's segment API (`locate` + `segment_data`): copy out of the cached segment, no `AsyncWrite` pipe. `segment_data` already starts the prefetch wave (depth 8 for direct files, 4 for archives).
- C header (hand-written, a few functions):

```c
typedef struct zz_session zz_session;
int  zz_list_entries(const char* nzb, size_t len, char** json_out);   // season-pack picker
int  zz_open(const char* provider_json, const char* nzb, size_t len,
             const char* entry_key /*nullable*/, zz_session** out);   // blocks until playable
uint64_t zz_size(zz_session*);
int64_t  zz_read(zz_session*, uint64_t offset, char* buf, uint64_t n); // <0 = error
void zz_cancel(zz_session*);            // safe from any thread
void zz_close(zz_session*);
const char* zz_last_error(void);        // thread-local, for logs and UI
void zz_free(char*);
```

`zz_open` is blocking and cancellable by `zz_cancel`. The app calls it off the UI thread.

### Performance details

What dominates is NNTP latency and throughput; the local transport is the smaller cost. Removing HTTP gets rid of the overhead we can control, and the following keep the network side tight:

- **Direct buffer fill.** `zz_read` copies from the cached, decoded segment straight into mpv's buffer: one copy, no socket buffers, no HTTP framing. (Decoding needs its own buffer regardless: TLS, yEnc and archive reconstruction.)
- **Article-aligned cache.** The engine already caches whole decoded articles, so mpv's small reads (often 64 KiB to 1 MiB) are memcpys out of a segment held in RAM.
- **Seek-aware cancellation.** On a cache miss at a new position, the engine bumps `prefetch_gen`, and queued prefetches for the old position are dropped (`nntp_source.rs`, `spawn_prefetch_one`). **Gap:** fetches already in flight run to completion and occupy connections. Engine change: race each prefetch fetch against a generation-change notify and drop it, so a seek gets every connection immediately.
- **Prefetch depth vs connections.** Depth is 8 segments for direct files and 4 for archives. Tune it against the account's connection count while Atlas indexes on the same account. Measure seek-to-first-frame at depth 4/8/16.
- **One cache layer to size.** mpv's demuxer cache (`demuxer-max-bytes`, readahead) and the engine's segment cache both hold data. Keep mpv's cache for demuxing smoothness and the engine's small (32–64 segments), and measure.
- **Since we build our own libmpv,** deeper options are open later (a native `stream_usenet` inside mpv, or tuning stream buffer sizes). `stream_cb` needs no changes to the fork, so start there and only go deeper if profiling shows the callback layer costs anything.

Engine changes worth making (as separate commits, so they can go upstream too):
1. Make `api::streaming` take NZB **bytes** as well as a path (Atlas hands over bytes; no temp file).
2. Make the segment-cache size configurable per session. The default of 256 segments is about 180 MiB at 700 KiB articles. mpv already keeps its own demuxer cache, so start the engine at 32–64 segments and measure.
3. Expose `locate`/`segment_data` (or a `read_at(offset, &mut [u8])`) on the source for the FFI crate.

## Player side (mpv-library-qt)

- `UsenetStream` (new, ~200 lines): loads `zanzibarr_mpv.dll` (optional; Usenet playback is hidden when it's missing), registers `usenet` via `mpv_stream_cb_add_ro` on the backend's handle, keeps the session table.
- Play flow in `LibmpvBackend::load` (`src/playback/win/LibmpvBackend.cpp`, the `loadfile` at ~line 1358): fetch the NZB from Atlas (`/api?t=get&id=…`), then `zz_open` on a worker thread, then `loadfile usenet://<id>`. Show the "connecting" state while `zz_open` runs.
- Fast title switch: `zz_cancel` the pending open; an open that finishes for a stale request is closed, never loaded.
- Stop: mpv calls `close_fn` itself when it releases the stream; the backend never frees a session mpv still holds.
- Season packs: `zz_list_entries` → pick by the episode the user chose (Atlas's release meta has season/episode), else the largest entry.
- Preload (the backend already preloads the next item) can open a session early and keep it warm.
- Provider credentials: the same source the app uses for SABnzbd today; pass `max_connections` well below the account limit (Atlas indexes on the same account).

## Phases

| # | Work | Done when |
|---|---|---|
| 0 | Fork, Windows build, tests | **Done**: 255 tests pass |
| 1 | `rust-ffi` crate + a tiny C test program that plays an Atlas NZB through `libmpv-2.dll` with `stream_cb` | A 4K remux starts and seeks; numbers recorded |
| 2 | Measure against Nuvio on the same release: first frame, three seeks, idle/peak working set, private bytes; engine cache at 32/64/256 segments | Table in this doc; pick the cache default |
| 3 | mpv-library-qt `UsenetStream` + play/stop/switch/preload wiring | Plays from the library UI; switching titles mid-open never shows the old title |
| 4 | Archives and robustness: RAR5 set, split 7z, passworded release with Atlas password meta, a release with missing articles (PAR2 repair path), season pack | Each case has a known NZB that plays |
| 5 | Hardening: panic boundary tests, cancellation under load, provider errors surfaced in the UI, logging without credentials or message IDs | No player crash in a soak test |

## Keeping in sync with upstream

`origin` = `bonejay/Zanzibarr`, `upstream` = `envermeister/zanzibarr`. Our code lives in new files (`rust-ffi/`, this doc) plus small, upstreamable engine commits. Merge upstream regularly; the engine changes upstream often (archive and PAR2 fixes are exactly what we want). Upstream comments are in Turkish; the code is otherwise conventional.

## Open questions

- In-process memory vs the 62 MiB Nuvio baseline: measured in phase 2.
- Compressed RAR spools to a temp directory: put that under the app's cache dir and cap it.
- unrar's license allows decompression only. That's fine for this use; keep its notice if the player is ever distributed.
