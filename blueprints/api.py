"""JSON REST API.

Exposes everything the web UI can do — create/generate artists and bands and
portraits; generate albums, tracklists, covers; brief, render and publish — as
plain-JSON endpoints, plus one-shot *pipeline* endpoints that chain the whole
create→render→publish flow in a single call.

Conventions
-----------
* Request bodies are JSON (``Content-Type: application/json``); responses are
  JSON. Errors return ``{"error": "..."}`` with a 4xx/5xx status.
* Long album-wide actions (brief/render/publish all) default to a background
  job (HTTP 202) you poll at ``/api/albums/<id>/job``. Pass ``?sync=1`` to run
  inline and get the summary back in the response.
* One-shot pipelines run in the background by default; poll ``/api/jobs/<id>``.
  Pass ``?sync=1`` to block until the whole chain finishes.

Auth
----
Optional. Set the ``api_token`` setting (Admin or PATCH /api/settings) to a
non-empty value to require it; clients then send ``X-API-Key: <token>`` (or
``?token=<token>``). When ``api_token`` is empty the API is open — fine for a
local single-user console, which is what this app is.
"""

from flask import Blueprint, jsonify, request

from database import (
    all_settings, execute, get_setting, jload, now_iso, query, set_setting,
)
from backends.llm import LLMError
from backends.imagegen import ImageGenError
import generation
import jobs
import pipeline
from publish import PublishError, encoder_status, publish_album, publish_track

bp = Blueprint("api", __name__, url_prefix="/api")

# Track columns the PATCH /api/tracks/<id> endpoint is allowed to write.
_TRACK_PATCH_COLS = {
    "title", "subject", "summary", "lyrics", "lyric_notes", "mood", "song_key",
    "tempo", "duration", "language", "instrumental", "cover_strength",
    "cover_noise", "render_candidates",
}


# -- auth + helpers ----------------------------------------------------------

@bp.before_request
def _require_token():
    token = (get_setting("api_token", "") or "").strip()
    if not token:
        return None  # auth disabled
    if request.endpoint == "api.health":
        return None  # health stays open for liveness checks
    sent = request.headers.get("X-API-Key") or request.args.get("token") or ""
    if sent != token:
        return jsonify({"error": "unauthorized"}), 401
    return None


def _body():
    """Parsed JSON body as a dict (empty dict if none/invalid)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _row(row):
    return {k: row[k] for k in row.keys()} if row else None


def _rows(rows):
    return [_row(r) for r in rows]


def _wants_sync():
    return str(request.args.get("sync", "")).lower() in ("1", "true", "yes", "on")


def _artist_json(a):
    d = _row(a)
    if d and "secondary_genres" in d:
        d["secondary_genres"] = jload(d.get("secondary_genres"), [])
    return d


def _track_json(t):
    d = _row(t)
    if not d:
        return None
    d["style_tags"] = jload(d.get("style_tags"), [])
    d["environmentals"] = jload(d.get("environmentals"), [])
    return d


def _album_json(a, with_tracks=True):
    d = _row(a)
    if not d:
        return None
    d["style_tags"] = jload(d.get("style_tags"), [])
    d["owner_name"], d["genre"] = generation._owner_name_genre(
        a["owner_type"], a["owner_id"])
    if with_tracks:
        tracks = query("SELECT * FROM track WHERE release_id=? ORDER BY position",
                       (a["id"],))
        d["tracks"] = [_track_json(t) for t in tracks]
    return d


def _err(exc, status=400):
    return jsonify({"error": str(exc)}), status


# -- meta --------------------------------------------------------------------

@bp.get("/health")
def health():
    return jsonify({"ok": True, "service": "music-world",
                    "mock_mode": str(all_settings().get("mock_mode", "1")) in
                    ("1", "true", "True", "on")})


@bp.get("/settings")
def get_settings():
    s = dict(all_settings())
    for k in ("llm_api_key", "secret_key", "api_token"):  # don't echo secrets
        if s.get(k):
            s[k] = "***"
    return jsonify(s)


@bp.patch("/settings")
def patch_settings():
    for k, v in _body().items():
        set_setting(k, "" if v is None else str(v))
    return jsonify({"ok": True})


@bp.get("/encoder")
def encoder():
    return jsonify(encoder_status())


# -- artists -----------------------------------------------------------------

@bp.get("/artists")
def artists_list():
    return jsonify(_rows(query("SELECT * FROM artist ORDER BY id DESC")))


@bp.get("/artists/<int:artist_id>")
def artist_get(artist_id):
    a = query("SELECT * FROM artist WHERE id=?", (artist_id,), one=True)
    if not a:
        return _err("artist not found", 404)
    return jsonify(_artist_json(a))


@bp.post("/artists")
def artist_create():
    """Create an artist. Body either {"generate": true, "hints": {...}} for an
    LLM-invented artist, or fields for a manual insert (name, persona, ...)."""
    b = _body()
    try:
        if b.get("generate"):
            aid = generation.generate_artist(b.get("hints") or {})
        else:
            aid = pipeline._create_artist_manual(b.get("fields") or b)
    except LLMError as exc:
        return _err(exc, 502)
    a = query("SELECT * FROM artist WHERE id=?", (aid,), one=True)
    return jsonify(_artist_json(a)), 201


@bp.patch("/artists/<int:artist_id>")
def artist_patch(artist_id):
    if not query("SELECT 1 FROM artist WHERE id=?", (artist_id,), one=True):
        return _err("artist not found", 404)
    cols = {"name", "persona", "backstory", "region", "primary_genre",
            "influences", "vocal", "language", "stage"}
    for k, v in _body().items():
        if k in cols:
            execute(f"UPDATE artist SET {k}=? WHERE id=?", (v, artist_id))
    return jsonify(_artist_json(query(
        "SELECT * FROM artist WHERE id=?", (artist_id,), one=True)))


@bp.post("/artists/<int:artist_id>/portrait")
def artist_portrait(artist_id):
    try:
        path = generation.generate_artist_portrait(
            artist_id, prompt=_body().get("prompt"))
    except (ImageGenError, ValueError) as exc:
        return _err(exc, 502)
    return jsonify({"ok": True, "portrait_path": path})


# -- bands -------------------------------------------------------------------

@bp.get("/bands")
def bands_list():
    return jsonify(_rows(query("SELECT * FROM band ORDER BY id DESC")))


@bp.get("/bands/<int:band_id>")
def band_get(band_id):
    b = query("SELECT * FROM band WHERE id=?", (band_id,), one=True)
    if not b:
        return _err("band not found", 404)
    d = _row(b)
    d["members"] = _rows(query(
        "SELECT a.id, a.name, m.instrument FROM membership m"
        " JOIN artist a ON a.id=m.artist_id WHERE m.band_id=? AND m.left_on IS NULL",
        (band_id,)))
    return jsonify(d)


@bp.post("/bands")
def band_create():
    """Create a band. Body {"hints": {...}} invents one from scratch; body with
    "members": [{"artist_id", "instrument"}] assembles from existing artists."""
    b = _body()
    try:
        if b.get("members"):
            bid = generation.generate_band_from_members(
                b.get("name", ""), b.get("primary_genre", ""), b["members"])
        else:
            bid = generation.generate_band_from_scratch(b.get("hints") or {})
    except LLMError as exc:
        return _err(exc, 502)
    return jsonify(_row(query("SELECT * FROM band WHERE id=?", (bid,), one=True))), 201


@bp.post("/bands/<int:band_id>/portrait")
def band_portrait(band_id):
    try:
        path = generation.generate_band_portrait(band_id, prompt=_body().get("prompt"))
    except (ImageGenError, ValueError) as exc:
        return _err(exc, 502)
    return jsonify({"ok": True, "portrait_path": path})


# -- albums ------------------------------------------------------------------

@bp.get("/albums")
def albums_list():
    return jsonify([_album_json(a, with_tracks=False)
                    for a in query("SELECT * FROM release ORDER BY id DESC")])


@bp.get("/albums/<int:album_id>")
def album_get(album_id):
    a = query("SELECT * FROM release WHERE id=?", (album_id,), one=True)
    if not a:
        return _err("album not found", 404)
    return jsonify(_album_json(a))


@bp.post("/albums")
def album_create():
    """Generate an album (concept + tracklist) for a performer.

    Body: {"owner_type": "artist"|"band", "owner_id": N, "ethos": "...",
           "style_tags": [...], "type": "album"|"ep"|"single",
           "track_count": N, "title": "...", "self_titled": false}
    With {"empty": true} an empty release is created instead (no LLM)."""
    b = _body()
    owner_type = "band" if b.get("owner_type") == "band" else "artist"
    owner_id = b.get("owner_id")
    table = "band" if owner_type == "band" else "artist"
    if not owner_id or not query(f"SELECT 1 FROM {table} WHERE id=?", (owner_id,), one=True):
        return _err("owner_type/owner_id not found", 404)
    try:
        if b.get("empty"):
            rtype = b.get("type", "album")
            rtype = rtype if rtype in ("single", "ep", "album") else "album"
            rid = execute(
                "INSERT INTO release (owner_type, owner_id, type, title, status, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (owner_type, int(owner_id), rtype,
                 (b.get("title") or "Untitled").strip(), "tracklist", now_iso()))
        else:
            rid = generation.generate_album(
                owner_type, int(owner_id), b.get("ethos", ""),
                b.get("style_tags") or [], rel_type=b.get("type", "album"),
                track_count=b.get("track_count"), title=b.get("title", ""),
                self_titled=bool(b.get("self_titled")))
    except LLMError as exc:
        return _err(exc, 502)
    a = query("SELECT * FROM release WHERE id=?", (rid,), one=True)
    return jsonify(_album_json(a)), 201


@bp.post("/albums/<int:album_id>/regenerate-tracklist")
def album_regenerate(album_id):
    try:
        generation.regenerate_tracklist(album_id)
    except (LLMError, ValueError) as exc:
        return _err(exc, 502)
    a = query("SELECT * FROM release WHERE id=?", (album_id,), one=True)
    return jsonify(_album_json(a))


@bp.post("/albums/<int:album_id>/cover")
def album_cover(album_id):
    b = _body()
    try:
        path = generation.generate_album_cover(
            album_id, prompt=b.get("prompt"), use_photo=bool(b.get("use_photo")))
    except (ImageGenError, ValueError) as exc:
        return _err(exc, 502)
    return jsonify({"ok": True, "cover_path": path})


def _album_action(kind, album_id):
    """Brief/render/publish all tracks of an album. Background by default
    (poll /api/albums/<id>/job); ?sync=1 runs inline and returns the summary."""
    if not query("SELECT 1 FROM release WHERE id=?", (album_id,), one=True):
        return _err("album not found", 404)
    if _wants_sync():
        runner = {"render": generation.render_album, "brief": generation.brief_album,
                  "publish": publish_album}[kind]
        try:
            return jsonify({"ok": True, "summary": runner(album_id)})
        except Exception as exc:
            return _err(exc, 500)
    return jsonify(jobs.start(kind, album_id)), 202


@bp.post("/albums/<int:album_id>/brief")
def album_brief(album_id):
    return _album_action("brief", album_id)


@bp.post("/albums/<int:album_id>/render")
def album_render(album_id):
    return _album_action("render", album_id)


@bp.post("/albums/<int:album_id>/publish")
def album_publish(album_id):
    return _album_action("publish", album_id)


@bp.get("/albums/<int:album_id>/job")
def album_job(album_id):
    job = jobs.get_job(album_id)
    if not job:
        return _err("no job for this album", 404)
    return jsonify(job)


@bp.post("/albums/<int:album_id>/job/cancel")
def album_job_cancel(album_id):
    job = jobs.cancel_job(album_id)
    if not job:
        return _err("no job for this album", 404)
    return jsonify(job)


# -- tracks ------------------------------------------------------------------

@bp.get("/tracks/<int:track_id>")
def track_get(track_id):
    t = query("SELECT * FROM track WHERE id=?", (track_id,), one=True)
    if not t:
        return _err("track not found", 404)
    return jsonify(_track_json(t))


@bp.patch("/tracks/<int:track_id>")
def track_patch(track_id):
    if not query("SELECT 1 FROM track WHERE id=?", (track_id,), one=True):
        return _err("track not found", 404)
    for k, v in _body().items():
        if k in _TRACK_PATCH_COLS:
            execute(f"UPDATE track SET {k}=? WHERE id=?", (v, track_id))
    return jsonify(_track_json(query(
        "SELECT * FROM track WHERE id=?", (track_id,), one=True)))


@bp.post("/tracks/<int:track_id>/render")
def track_render(track_id):
    b = _body()
    if not query("SELECT 1 FROM track WHERE id=?", (track_id,), one=True):
        return _err("track not found", 404)
    try:
        result = generation.render_track(
            track_id, candidates=b.get("candidates"), seed=b.get("seed"))
    except Exception as exc:
        return _err(exc, 500)
    return jsonify(result)


@bp.post("/tracks/<int:track_id>/regenerate-lyrics")
def track_lyrics(track_id):
    try:
        generation.regenerate_lyrics(track_id)
    except (LLMError, ValueError) as exc:
        return _err(exc, 502)
    return jsonify(_track_json(query(
        "SELECT * FROM track WHERE id=?", (track_id,), one=True)))


@bp.post("/tracks/<int:track_id>/publish")
def track_publish(track_id):
    try:
        result = publish_track(track_id)
    except PublishError as exc:
        return _err(exc, 400)
    return jsonify(result)


# -- one-shot pipelines ------------------------------------------------------

@bp.post("/oneshot")
def oneshot():
    """Run the full chain in one call. See docs/api.md for the spec shape.

    Background by default (poll /api/jobs/<id>); ?sync=1 blocks until done."""
    try:
        job = pipeline.start(_body(), run_async=not _wants_sync())
    except ValueError as exc:
        return _err(exc, 400)
    status = 200 if _wants_sync() else 202
    return jsonify(job), status


@bp.get("/jobs")
def jobs_list():
    return jsonify(pipeline.list_jobs())


@bp.get("/jobs/<job_id>")
def job_get(job_id):
    job = pipeline.get(job_id)
    if not job:
        return _err("job not found", 404)
    return jsonify(job)


@bp.post("/jobs/<job_id>/cancel")
def job_cancel(job_id):
    job = pipeline.cancel(job_id)
    if not job:
        return _err("job not found", 404)
    return jsonify(job)
