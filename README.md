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
   tracklist. Every track comes with a role, subject, summary, tempo, mood,
   length, and stylistic cues.
4. **Tracks** — open any album track to write its lyrics and production brief,
   or write a standalone song from a free-text prompt. Either way you then
   **render** it: the app asks ACE-Step for several candidate takes, scores each
   one for basic signal integrity, keeps the best, and lets you audition the
   rest and promote any of them to the master take.

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
optional API key).

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

---

## Cover songs (reference-music repository)

Point the app at a folder of existing audio in **Admin → Reference music
(covers)** (`reference_music_path`). Then open **Tracks → Cover a song**, pick a
reference track and (optionally) a performer, and the model writes a brief — new
lyrics, mood, tempo, and style cues — that reinterprets the original in that
performer's voice.

When the cover is rendered, the reference recording is sent to ACE-Step as the
source for a real **audio2audio cover** (acestep.cpp `task_type="cover"`): the
source audio is uploaded as the multipart `src_audio` part so the render
follows the original's structure, recast in the new style. Two Admin controls
steer it: **Cover strength** (`acestep_cover_strength` → `audio_cover_strength`,
the fraction of diffusion steps that see the source) and **Cover noise**
(`acestep_cover_noise` → `cover_noise_strength`). The audio is sent as bytes, so
the reference library and the ACE-Step server need not be on the same host. In
Mock mode the reference is ignored and a placeholder tone is produced.

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
the album's title, owner, genre, concept, and style tags. With **Mock mode** on
(or neither URL nor executable configured) a deterministic placeholder image is
produced instead, so the rest of the flow still works with no backend.

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
cover is generated automatically at publish time.

## Taxonomy

The Admin console also manages the **genre** and **style-tag** taxonomy the
generators draw from. Genres and tags seed the prompts and are translated into
ACE-Step style phrases at render time. The database is preloaded with a starter
set; add, edit, or remove entries to steer the world toward whatever sound you want.

Both taxonomies support **full-field add, per-entry editing, and bulk import**
(paste/upload JSON in Admin, or via the CLI). Imports upsert by `name`, so
re-applying a file updates existing entries in place.

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
├── manage.py              # CLI: bulk import/export/list genres
├── acestep_adapter.py     # reference HTTP bridge to ACE-Step (runs in ACE-Step's env)
├── backends/
│   ├── llm.py             # llama.cpp / ollama / openai client + JSON extraction
│   ├── acestep.py         # ACE-Step HTTP client + mock synthesizer
│   └── imagegen.py        # sd.cpp image client + stdlib placeholder generator
├── blueprints/            # artists, bands, albums, tracks, admin routes
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
