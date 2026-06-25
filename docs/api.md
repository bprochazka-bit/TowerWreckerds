# JSON API

Everything the web UI can do is also available as a plain-JSON REST API under
`/api`, including **one-shot pipelines** that chain the whole
create → render → publish flow in a single call.

- Request bodies are JSON (`Content-Type: application/json`); responses are JSON.
- Errors return `{"error": "..."}` with a 4xx/5xx status.
- The server is the same Flask app (`python3 app.py`); the API shares its
  settings, database, LLM/ACE-Step/sd.cpp backends, and `mock_mode`.

## Authentication

Optional and **off by default** (this is a local single-user console). To turn
it on, set the `api_token` setting — in Admin, or:

```bash
curl -X PATCH localhost:5000/api/settings -H 'Content-Type: application/json' \
     -d '{"api_token": "choose-a-secret"}'
```

Then send it on every `/api` call (the exception is `/api/health`, which stays
open for liveness checks):

```bash
curl localhost:5000/api/artists -H 'X-API-Key: choose-a-secret'
# or:  curl 'localhost:5000/api/artists?token=choose-a-secret'
```

## Long actions: background vs sync

Album-wide actions (brief/render/publish all) and one-shot pipelines are
long-running. By default they run in a **background job** and return
immediately:

- Album actions return `202` with an album job; poll `GET /api/albums/<id>/job`.
- One-shot pipelines return `202` with a pipeline job; poll `GET /api/jobs/<id>`.

Add `?sync=1` to run inline and get the final result in the response instead
(handy for scripts — make sure your HTTP client timeout is generous, since a
full render can take minutes).

---

## One-shot pipelines

`POST /api/oneshot` runs, in order: create performer → portrait → album +
tracklist → cover → write all lyrics → render all tracks → publish. Each stage
is optional; a stage you omit is reported as `skipped`. The response is a job
with a `steps` array (`pending`/`running`/`done`/`skipped`/`failed`/`cancelled`)
and a `created` map of the ids it made (`artist_id`/`band_id`, `album_id`).

Poll `GET /api/jobs/<id>`, list with `GET /api/jobs`, stop with
`POST /api/jobs/<id>/cancel` (cancellation takes effect between tracks/stages).

### Spec fields

```jsonc
{
  // EITHER create a performer …
  "performer": {
    "kind": "artist" | "band",          // default "artist"
    "mode": "generate" | "manual",      // default "generate" (artist only)
    "hints":  { "primary_genre": "...", "region": "...", "vibe": "...", "size": 4 },
    "fields": { "name": "...", "persona": "...", "primary_genre": "...",
                "vocal": "male", "language": "English" }   // mode:"manual"
  },
  // … OR point at an existing one (omit "performer" and use this instead)
  "performer_ref": { "kind": "artist" | "band", "id": 3 },

  "portrait": true,                      // generate a performer photo
  "album": {                             // REQUIRED
    "ethos": "sunny roots reggae",
    "style_tags": ["reggae", "dub"],
    "type": "album" | "ep" | "single",   // default "album"
    "track_count": 9,
    "title": "",                         // blank => the model titles it
    "self_titled": false
  },
  "cover": true,                         // generate album cover art
  "cover_use_photo": false,              // seed the cover from the performer photo
  "brief": true,                         // write lyrics for every track (default true)
  "render": true,                        // render every track       (default true)
  "publish": true                        // publish every track      (default true)
}
```

> Note: `render` needs lyrics, so leave `brief` on (the default) unless the
> tracks are all instrumentals.

### Example 1 — new artist, photo, album, cover, render, publish

```bash
curl -X POST localhost:5000/api/oneshot -H 'Content-Type: application/json' -d '{
  "performer": { "kind": "artist", "hints": { "primary_genre": "neo-psychedelic reggae", "region": "Jamaica" } },
  "portrait": true,
  "album": { "ethos": "warm island roots with a psychedelic edge", "style_tags": ["reggae", "psychedelic"], "type": "album", "track_count": 9 },
  "cover": true,
  "brief": true, "render": true, "publish": true
}'
# -> 202 {"id":"<job>", ...}; then poll:
curl localhost:5000/api/jobs/<job>
```

### Example 2 — new album for an existing performer, cover, render, publish

```bash
curl -X POST localhost:5000/api/oneshot -H 'Content-Type: application/json' -d '{
  "performer_ref": { "kind": "artist", "id": 3 },
  "album": { "ethos": "late-night dub session", "style_tags": ["dub"], "type": "ep", "track_count": 5 },
  "cover": true,
  "brief": true, "render": true, "publish": true
}'
```

Run either fully synchronously with `POST /api/oneshot?sync=1` to get the final
job (all steps `done`) back in one response.

---

## Endpoint reference

### Meta
| Method & path | Purpose |
|---|---|
| `GET /api/health` | Liveness + whether `mock_mode` is on. Always open. |
| `GET /api/settings` | All settings (secrets masked as `***`). |
| `PATCH /api/settings` | Update settings; body is `{key: value, ...}`. |
| `GET /api/encoder` | MP3 encoder availability (ffmpeg/lame). |

### Artists
| Method & path | Purpose |
|---|---|
| `GET /api/artists` | List. |
| `GET /api/artists/<id>` | One artist. |
| `POST /api/artists` | Create. `{"generate":true,"hints":{...}}` (LLM) or `{"fields":{...}}` (manual). |
| `PATCH /api/artists/<id>` | Update name/persona/region/primary_genre/influences/vocal/language/stage. |
| `POST /api/artists/<id>/portrait` | Generate portrait. Optional `{"prompt":"..."}`. |

### Bands
| Method & path | Purpose |
|---|---|
| `GET /api/bands` | List. |
| `GET /api/bands/<id>` | One band + members. |
| `POST /api/bands` | Create. `{"hints":{...}}` from scratch, or `{"members":[{"artist_id","instrument"}], "name","primary_genre"}` to assemble. |
| `POST /api/bands/<id>/portrait` | Generate band photo (collages member photos when present). |

### Albums
| Method & path | Purpose |
|---|---|
| `GET /api/albums` | List (no tracks). |
| `GET /api/albums/<id>` | One album + tracks. |
| `POST /api/albums` | Generate concept+tracklist for `{owner_type, owner_id, ethos, style_tags, type, track_count, title, self_titled}`; or `{"empty":true}` for a blank release. |
| `POST /api/albums/<id>/regenerate-tracklist` | Rebuild unlocked tracks. |
| `POST /api/albums/<id>/cover` | Generate cover. Optional `{"prompt","use_photo"}`. |
| `POST /api/albums/<id>/brief` | Write lyrics for all tracks (job; `?sync=1`). |
| `POST /api/albums/<id>/render` | Render all tracks (job; `?sync=1`). |
| `POST /api/albums/<id>/publish` | Publish all rendered tracks (job; `?sync=1`). |
| `GET /api/albums/<id>/job` | Poll the album's current job. |
| `POST /api/albums/<id>/job/cancel` | Cancel it. |

### Tracks
| Method & path | Purpose |
|---|---|
| `GET /api/tracks/<id>` | One track. |
| `PATCH /api/tracks/<id>` | Edit title/subject/summary/lyrics/lyric_notes/mood/song_key/tempo/duration/language/instrumental/cover_strength/cover_noise/render_candidates. |
| `POST /api/tracks/<id>/render` | Render one track. Optional `{"candidates","seed"}`. Runs inline. |
| `POST /api/tracks/<id>/regenerate-lyrics` | Rewrite just the lyrics. |
| `POST /api/tracks/<id>/publish` | Publish a single rendered track. |

### Pipelines / jobs
| Method & path | Purpose |
|---|---|
| `POST /api/oneshot` | Run the full chain (see above). `?sync=1` to block. |
| `GET /api/jobs` | List pipeline jobs (most recent first). |
| `GET /api/jobs/<id>` | Poll one pipeline job. |
| `POST /api/jobs/<id>/cancel` | Request cancellation. |

Job registries are in-memory and reset on restart (the work itself — DB rows,
audio, published files — is persisted as each step runs).
