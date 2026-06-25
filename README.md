# Music World

A web interface for an AI-driven "music world" — invent recording artists,
form bands, design concept albums with full tracklists, and render individual
tracks to audio. Text generation runs through **llama.cpp / Ollama / any
OpenAI-compatible endpoint**; audio runs through **ACE-Step**. Every backend is
configured from an in-app admin console.

The web app is built to run on **Debian 13 using only apt packages**
(`python3-flask`, `python3-requests`; SQLite is in the standard library).
JavaScript is vendored — a single `htmx.min.js` is the only client library and
it ships in the repo. There is **no build step and no pip dependency** for the
app itself.

---

## What it does

The app has four authoring sections plus an admin console.

1. **People** — generate a recording artist from a few hints (name, genre,
   region, vibe), or enter one by hand. Each artist gets a persona, backstory,
   genre profile, and a voice seed for consistency.
2. **Bands** — either generate a band *from scratch* (the model invents the
   members, who are saved as people with instruments) or *assemble* one from
   artists that already exist.
3. **Albums** — seed a release with a band or a solo artist, give it an ethos
   and style tags, and the model returns a titled concept plus a full
   tracklist. You can give the release a **title** (or mark it **self-titled**
   to use the performer's name), and afterwards **edit the title, inspiration,
   and ethos** and hit **Regenerate tracklist** to rebuild the sequence from the
   edited inspiration. Every track comes with a role, subject, summary, tempo,
   mood, length, stylistic cues, and a **lyric direction** (grounded in the
   concept/ethos and the artist) that pre-fills its Lyric direction field to steer
   the lyrics. You can also **compose an album by hand**:
   start an empty release, write tracks individually, then **add** them, **reorder**
   (move up/down), and **lock** a track so it survives a tracklist regeneration
   (locked tracks are kept and only the rest are replaced/filled). Removing a track
   from a release keeps it as a standalone track. Album-wide buttons **write briefs
   for all tracks**, **render audio for all tracks**, and **publish** the album — each
   runs in the background with a live, cancellable progress bar showing every
   track (and, for renders, each candidate) as it completes.
4. **Tracks** — open any album track to write its lyrics and production brief,
   or write a standalone song from a free-text prompt. Either way you then
   **render** it: the app asks ACE-Step for several candidate takes, scores each
   one for basic signal integrity, keeps the best, and lets you audition the
   rest and promote any of them to the master take. You can set the **number of
   candidates** and a **seed** per render — leave the seed blank for a fresh
   random one each time (so re-renders actually differ), or set it to reproduce a
   take. A re-render deletes the previous take's audio first, so it's always clean.

### Mock mode

The app ships with **Mock mode ON**, so the entire workflow runs with no
backends connected — render produces a short synthesized placeholder tone so
candidates, scoring, and playback all work. Turn Mock mode off in Admin once
your real backends are reachable. (The text generators still need a reachable
LLM even in mock mode; only audio is mocked.)

---

## Install & run

```bash
sudo apt update
sudo apt install python3 python3-flask python3-requests

./run.sh
#   or directly:
python3 app.py
```

Then open <http://127.0.0.1:5000>. The SQLite database (`music_world.db`) and
the audio library (`static/audio/`) are created automatically on first run.

Host and port can be overridden:

```bash
MUSIC_WORLD_HOST=0.0.0.0 MUSIC_WORLD_PORT=8000 ./run.sh
```

---

## Connecting the language model

Open **Admin** in the app and set the LLM backend. Three backend types are
supported; all are configured the same way (base URL, optional model name,
optional API key). For recommended local models (Qwen3 MoE, Hermes 4) and
download/run instructions, see **[docs/llm-setup.md](docs/llm-setup.md)**.

| Backend     | Wire format                         | Typical base URL          |
|-------------|-------------------------------------|---------------------------|
| `llamacpp`  | OpenAI-compatible `/v1/chat/...`    | `http://localhost:8080`   |
| `openai`    | OpenAI-compatible `/v1/chat/...`    | your endpoint             |
| `ollama`    | native `/api/chat`                  | `http://localhost:11434`  |

**llama.cpp** — run the bundled server, which exposes an OpenAI-compatible API:

```bash
llama-server -m your-model.gguf -c 4096 --port 8080
```

Leave the model field blank (llama.cpp serves whatever is loaded), set the base
URL to `http://localhost:8080`, and click **Test connection**.

**Ollama** — start Ollama and pull a model, then set the backend to `ollama`,
base URL `http://localhost:11434`, and the model to e.g. `llama3.1`:

```bash
ollama serve
ollama pull llama3.1
```

Any instruction-tuned model in the 7B+ range works well; the app asks for
structured JSON and tolerates extra prose around it.

### Reasoning models (Qwen3, DeepSeek-R1, etc.)

These models emit a `<think>…</think>` block before their answer, which can
eat the whole token budget and leave the JSON truncated. The app handles them:
it strips `<think>` blocks when parsing and asks the backend to disable thinking
(`chat_template_kwargs.enable_thinking=false` for llama.cpp / OpenAI, `think:
false` for Ollama). If you still see *"Could not parse JSON from model output"*:

- **Raise Max tokens** in Admin to 4096+ — a full album tracklist is large, and
  a truncated response can't be parsed. (Fresh installs now default to 4096.)
- **Make sure thinking is actually off.** With `llama-server`, the request flag
  above is the clean path; if your build ignores it, start the server with
  `--reasoning-budget 0`, or append `/no_think` to prompts. You can confirm in
  the server log — a healthy run should *not* show the response hitting the
  token limit (`n_decoded` equal to your max_tokens means it was cut off).

---

## Connecting ACE-Step (audio)

The app talks to an ACE-Step audio server over HTTP. It targets
**[acestep.cpp](https://github.com/ServeurpersoCom/acestep.cpp)** — the portable
C++/GGML build that runs as `ace-server` — which exposes an asynchronous job API.
The client (`backends/acestep.py`) implements the full flow, validated against
that server's source:

```
POST /synth  {caption, lyrics, duration, seed, output_format, task_type}  -> {"id": "N"}
GET  /job?id=N                 -> {"status": "running|done|failed|cancelled"}
GET  /job?id=N&result=1        -> result body (multipart/mixed: audio + latent)
GET  /health                   -> {"status": "ok"}
```

The app renders **text2music directly** — it does not call `/synth`'s companion
`/lm` planning stage, because in `generate` mode that stage rewrites the lyrics,
and the app wants the lyrics its own LLM authored to be rendered verbatim. The
client submits the job, polls `/job` until it's `done`, then pulls the multipart
result and extracts the audio part.

Run the server alongside your GGUF models:

```bash
./ace-server --models ./models --host 0.0.0.0 --port 8081 --keep-loaded
```

Then, in **Admin**: set the ACE-Step base URL (e.g. `http://localhost:8081`),
choose candidates-per-render and a duration ceiling, pick an **output format**
(`wav16` enables the full integrity check; `wav24`/`wav32`/`mp3` also work —
`mp3` only gets a presence/size check since the QA parses WAV), **turn Mock mode
off**, and click **Test connection** (hits `/health`).

> A reference `acestep_adapter.py` is also included for the official Python
> ACE-Step distribution, which uses a different (`/release_task`-style) API. It
> is not needed for acestep.cpp and can be ignored unless you switch backends.

### Trimming trailing noise

ACE-Step text2music often pads the tail of a render with white noise. With
**Trim trailing noise** enabled in Admin (on by default), the app removes that
trailing noise/silence from the chosen take after each render — detecting noise
by its high zero-crossing rate and low crest factor, so tonal endings and
fade-outs are left intact. It works on WAV output only. Each track page also has
a **Trim trailing noise** button to clean up tracks that were rendered earlier.

### Lyric language

Bands and solo artists have a default **lyric language** (auto-filled at
generation from their origin, editable on the detail page). Lyrics are written
in that language and it's passed to ACE-Step as `vocal_language` (mapped to a
BCP-47 code). Each track can override it in its Lyric source panel.

### Lyric style and regeneration

Every LLM request sends a **fresh random seed**, so generations vary run to run
rather than decoding to the same words each time (raise *Temperature* in Admin
for even more variation). Album concept/tracklist **naming** runs at the *Lyric
temperature* too, so titles don't come out near-greedy and repetitive. Several
levers shape every lyric prompt:

- **Lyric temperature** (Admin) — lyrics are written in a *separate* pass from the
  structured brief, so you can run the words hotter (more creative) while the brief
  JSON stays at the calmer base temperature.
- **Lyric style guidance** (Admin) — voice & tone; defaults to favouring poetic,
  image-driven lyrics over literal storytelling.
- **Lyric structure & length** (Admin) — keeps songs tight and proportional, and
  discourages tacked-on outros, filler, and repetition.
- **Per-genre lyric guidance** — each genre carries a lyric *idiom* (built-in
  defaults for the seed genres, editable per genre and importable), so country
  reads like country and hip hop like hip hop. The track's genre idiom is injected
  automatically.
- **Track length** — the duration is passed in as a proportional-length hint, so a
  short song doesn't get five verses and an outro.
- **Per-track lyric direction** — the track's **Lyric source** panel takes a
  free-text direction; **Regenerate lyrics** rewrites *only* the lyrics from the
  subject, summary, direction, genre, and length — tempo, key, and tags untouched.

### Clean lyrics

Lyric prompts deliberately exclude band-member names (the lineup is dropped from
the performer context when writing lyrics) and instruct the model to avoid
parenthetical stage directions, so names and notes like `(guitar solo)` don't
end up sung. As a backstop, **Strip parenthetical directives** (Admin, on by
default) removes any `(…)` from the lyrics sent to ACE-Step at render time while
preserving `[verse]`/`[chorus]` structure tags. Turn it off if you write backing
vocals in parentheses that you *want* sung.

### Sounds-like influences

Each band and solo artist has a **Sounds like / influences** field (on its
detail page). Whatever you put there — a reference act like *Barenaked Ladies*,
or descriptive phrases like *quirky acoustic alt-pop, tight harmonies* — is
injected into the ACE-Step caption for **every track that performer renders**,
and is also given to the language model so it steers the tracklist, style tags,
and arrangement. So you set it once at the band level rather than editing each
track. For album tracks the influence is read live at render time (change it and
re-render); standalone/cover tracks snapshot it when created. Descriptive
phrases usually steer the audio model better than a bare artist name, and
influence names are kept out of the sung lyrics.

### Lead vocal

Bands and solo artists carry a **lead vocal** gender (female / male /
androgynous), auto-filled when generated and editable on the detail page. It's
injected authoritatively into the ACE-Step caption for every track the
performer renders — overriding any default or mismatched tag, so a female artist
sings female without you adding a style tag. For bands it's read from the lead
vocalist member (the one whose instrument is vocals), with a band-level override
if you want to set it directly. Standalone/cover tracks snapshot it when created.

---

## Cover songs (reference-music repository)

Point the app at a folder of existing audio in **Admin → Reference music
(covers)** (`reference_music_path`). Then open **Tracks → Cover a song**, pick a
reference track and (optionally) a performer, and the model writes an **original
song with its own lyrics** in the reference's vibe. The render borrows the
reference's musical *style* — it is not a re-recording of the original. Choosing
a performer **associates** the cover with them (it's saved as a single under that
artist/band), and the same applies when you attribute a free-text track to a
performer.

When the cover is rendered, the reference recording is sent to ACE-Step as the
source for an **audio2audio** render (acestep.cpp `task_type="cover"`): the
source audio supplies the musical style/structure (via a lossy FSQ roundtrip, so
the model reinterprets freely) while the track's own lyrics are sung over it.
The source audio is uploaded as the multipart `audio` part so the render
follows the original's structure, recast in the new style. Two Admin controls
steer it: **Cover strength** (`acestep_cover_strength` → `audio_cover_strength`,
the fraction of diffusion steps that see the source) and **Cover noise**
(`acestep_cover_noise` → `cover_noise_strength`). The audio is sent as bytes, so
the reference library and the ACE-Step server need not be on the same host. In
Mock mode the reference is ignored and a placeholder tone is produced.

You can also attach or change a cover reference on **any existing track** from
its page (the **Cover reference** panel lists the reference-music files), or set
it back to none. There you can set **per-track cover strength/noise** (overriding
the Admin defaults) and **Fetch original lyrics** — which looks the reference song
up on [LRCLIB](https://lrclib.net) (free, no key) by its tags/filename and drops
the original words into the lyrics box, for a faithful same-words cover. And any track can be marked **Instrumental** (in its Lyric
source panel) to render with no vocals at all (`[Instrumental]`), regardless of
the lyrics it carries.

## Cover art (sd.cpp)

Album covers are generated by **[stable-diffusion.cpp](https://github.com/leejet/stable-diffusion.cpp)**,
in either of two modes (set in **Admin → sd.cpp (cover art)**):

- **Web UI (recommended)** — set **Web UI URL** (`sdcpp_url`) to an
  AUTOMATIC1111-compatible server. The app POSTs to `/sdapi/v1/txt2img` and
  reads back the base64 PNG. A URL takes precedence over the executable.
- **Local executable** — set the `sd` binary path (`sdcpp_path`) and model
  weights (`sdcpp_model`); the app shells out to
  `<sd> -m <model> -p "<prompt>" -o cover.png --steps N -W S -H S --cfg-scale C -s SEED`.

Both modes share the steps, output size, CFG scale, and negative-prompt
settings. Open any album and click **Generate cover**; the prompt is built from
the album's title, owner, genre, concept, and style tags (and is editable before
generating). With **Mock mode** on (or neither URL nor executable configured) a
deterministic placeholder image is produced instead, so the rest of the flow
still works with no backend.

### Portraits and photo-sourced art

Artist and band detail pages can **generate a portrait** (the prompt is built
from persona/genre/region/vocal and is editable). Those photos can then seed
other art via **img2img**:

- A **band photo** is built from its members' portraits — if members have
  portraits they're collaged into one reference image the band shot is generated
  from (needs `python3-pil`; without it, it falls back to one member's photo or
  text only).
- An **album cover** can be based on the performer's photo: tick *Base on the
  performer's photo* in the cover panel to seed the cover from their portrait or
  band photo.

## Publishing

Tracks and albums can be exported as a tidy, tagged MP3 library. Set an output
folder in **Admin → Publishing** (`publish_path`, defaults to `./published`),
then click **Publish MP3** on a track or **Publish album** on an album. The
layout is:

```
<publish_path>/<Artist or Band>/<Album Title>/01 - Track Title.mp3
<publish_path>/<Artist or Band>/<Album Title>/cover.png
<publish_path>/<Artist or Band>/Track Title.mp3        # standalone tracks
```

Each MP3 gets **ID3v2.3** tags (title, artist, album, track number, genre, year)
with the album cover embedded as the front-cover image. MP3 encoding shells out
to **ffmpeg** (preferred) or **lame**; install one of them for real transcoding.
If neither is present, the master take is copied verbatim (e.g. as `.wav`) and
the app tells you encoding was skipped, rather than failing the publish. Tag
writing itself is pure stdlib, so it needs no extra packages. A missing album
cover is generated automatically at publish time. The performer's portrait/band
photo is written into the artist folder as `folder.png`, and **Admin → Update
published** re-publishes everything already published to refresh tags, covers,
and artist images from the current data (handy after renames or new artwork).

A track page has a **Download MP3** button (it publishes the single track on
demand if needed, then downloads it), and the **Published** section browses every
exported track — grouped by album, with per-track download links.

A standalone (freeform) track that isn't tied to a release shows a **Performer**
panel: pick an artist or band (or create one first) to associate the track with
them. That wraps the track in a release (a single by default) owned by the
performer — so it appears under their releases, and the performer's influences
and lead vocal apply on the next render.

## JSON API

Everything above is also a plain-JSON REST API under `/api` — create/generate
artists, bands, portraits; generate albums, tracklists, covers; brief, render,
and publish. It also exposes **one-shot pipelines** that run the whole chain in
a single call:

- **New artist → photo → album → cover → tracklist → render all → publish**
- **New album (for an existing performer) → cover → tracklist → render → publish**

```bash
curl -X POST localhost:5000/api/oneshot -H 'Content-Type: application/json' -d '{
  "performer": { "kind": "artist", "hints": { "primary_genre": "reggae" } },
  "portrait": true,
  "album": { "ethos": "sunny island roots", "style_tags": ["reggae"], "type": "ep", "track_count": 5 },
  "cover": true, "brief": true, "render": true, "publish": true
}'
# -> 202 {"id": "<job>", ...}; poll: curl localhost:5000/api/jobs/<job>
```

The API is open by default (local single-user console); set the `api_token`
setting to require an `X-API-Key` header. Full endpoint reference, the pipeline
spec, and background-vs-`?sync=1` behaviour are in
**[docs/api.md](docs/api.md)**.

## Taxonomy

The Admin console also manages the **genre** and **style-tag** taxonomy the
generators draw from. Genres and tags seed the prompts and are translated into
ACE-Step style phrases at render time. The database is preloaded with a starter
set; add, edit, or remove entries to steer the world toward whatever sound you want.

Both taxonomies support **full-field add, per-entry editing, and bulk import**
(paste/upload JSON in Admin, or via the CLI). Imports upsert by `name`
(case-insensitive), so re-applying a file updates existing entries in place
instead of creating near-duplicates.

```bash
python3 manage.py import-genres genres.json       # file, or - for stdin
python3 manage.py export-genres -o genres.json     # dump current set (import template)
python3 manage.py list-genres
python3 manage.py import-style-tags tags.json
python3 manage.py export-style-tags -o tags.json
python3 manage.py list-style-tags
```

The full file layout — every field, the list-field string shorthand, and the
`{"genres": [...]}` / `{"style_tags": [...]}` wrapper forms — is documented in
**[docs/import-format.md](docs/import-format.md)**.

---

## Project layout

```
music-world/
├── app.py                 # Flask app factory, dashboard, jinja filters
├── database.py            # SQLite schema, seed data, settings store
├── generation.py          # orchestration: artists, bands, albums, tracks, render, covers, art
├── publish.py             # export tracks/albums as ID3-tagged MP3s + embedded cover art
├── jobs.py                # in-memory background jobs for album-wide brief/render/publish
├── pipeline.py            # one-shot create→render→publish pipelines for the JSON API
├── manage.py              # CLI: bulk import/export/list genres
├── acestep_adapter.py     # reference HTTP bridge to ACE-Step (runs in ACE-Step's env)
├── backends/
│   ├── llm.py             # llama.cpp / ollama / openai client + JSON extraction
│   ├── acestep.py         # ACE-Step HTTP client + mock synthesizer
│   └── imagegen.py        # sd.cpp image client + stdlib placeholder generator
├── blueprints/            # artists, bands, albums, tracks, admin, api routes
├── templates/             # server-rendered Jinja (console aesthetic)
├── static/
│   ├── css/style.css      # self-contained, system fonts, no web fonts
│   ├── js/htmx.min.js     # the only vendored JS library
│   ├── js/app.js          # small progressive-enhancement helpers
│   ├── audio/             # rendered takes land here (auto-created)
│   └── covers/            # generated cover art lands here (auto-created)
├── requirements-apt.txt
└── run.sh
```

---

## Notes on Debian 13 / Python 3.13

Debian 13 ships Python 3.13, which **removed the `audioop` module**. The
render integrity check therefore inspects WAV peak amplitude using the standard
`array` module instead of `audioop`, so the app runs cleanly on 3.13 with no
extra packages. (It also runs fine on earlier 3.x.)

Sessions use a secret key that is generated once and stored in the database on
first run, so flash messages and forms work out of the box with no
configuration. Set `MUSIC_WORLD_SECRET` in the environment to override it.
