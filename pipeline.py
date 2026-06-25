"""One-shot orchestration pipelines for the JSON API.

A *pipeline* runs the full chain — optionally create a performer and portrait,
then generate an album + tracklist, cover art, write all lyrics, render every
track, and publish — in a daemon thread, recording per-step status into a small
in-memory registry the API polls.

This mirrors jobs.py's deliberately simple model: single process, in memory,
resets on restart. That's fine because every step persists its own work (DB
rows, audio, published files) as it goes; the registry only tracks progress.

Unlike jobs.py (which is keyed by album_id and runs one render/brief/publish
kind), a pipeline is keyed by its own id and chains many stages, so it lives
here rather than being shoehorned into that registry.
"""

import threading
import uuid
from datetime import datetime, timezone

import generation
import publish
from database import execute, jdump, now_iso, query

# Canonical step order; a step only *runs* when the spec asks for it, otherwise
# it is reported as "skipped" so a poller sees the whole plan up front.
_STEP_ORDER = ["performer", "portrait", "album", "cover", "brief", "render", "publish"]

_jobs = {}
_lock = threading.Lock()
_MAX_KEEP = 50  # cap the registry so a long-lived server doesn't grow unbounded


class _Cancelled(Exception):
    """Raised internally when a cancel was requested between steps."""


class _StepFailed(Exception):
    """Raised internally when a step errors, to abort the remaining chain."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot(job):
    c = dict(job)
    c["steps"] = [dict(s) for s in job["steps"]]
    c["created"] = dict(job["created"])
    c.pop("cancel", None)  # internal flag, not part of the public shape
    return c


# -- registry ---------------------------------------------------------------

def get(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job else None


def list_jobs():
    with _lock:
        ordered = sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)
        return [_snapshot(j) for j in ordered]


def cancel(job_id):
    with _lock:
        job = _jobs.get(job_id)
        if job and job["status"] == "running":
            job["cancel"] = True
        return _snapshot(job) if job else None


def _prune_locked():
    """Drop the oldest finished jobs once the registry exceeds the cap. Running
    jobs are never pruned. Caller must hold _lock."""
    if len(_jobs) <= _MAX_KEEP:
        return
    finished = [j for j in _jobs.values() if j["status"] != "running"]
    finished.sort(key=lambda j: j["finished_at"] or j["started_at"])
    for j in finished[: len(_jobs) - _MAX_KEEP]:
        _jobs.pop(j["id"], None)


# -- step helpers -----------------------------------------------------------

def _set_step(job_id, name, **kw):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        for s in job["steps"]:
            if s["name"] == name:
                s.update(kw)
                return


def _record(job_id, key, value):
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job["created"][key] = value


def _cancelled(job_id):
    with _lock:
        job = _jobs.get(job_id)
        return bool(job and job["cancel"])


def _track_count(album_id):
    r = query("SELECT COUNT(*) c FROM track WHERE release_id=?", (album_id,), one=True)
    return r["c"] if r else 0


def _progress_for(job_id, name, total):
    """A progress callback for brief/render/publish that advances a per-step
    counter on each finished track (coarse but enough for a progress bar)."""
    def cb(event, **data):
        if event != "track_done":
            return
        with _lock:
            job = _jobs.get(job_id)
            if not job:
                return
            for s in job["steps"]:
                if s["name"] == name:
                    s["done"] = (s.get("done") or 0) + 1
                    s["total"] = total
                    return
    return cb


def _step(job_id, name, fn):
    """Run one requested step: mark it running, execute, mark done. On error the
    step is marked failed and the chain aborts (later steps depend on it)."""
    if _cancelled(job_id):
        raise _Cancelled()
    _set_step(job_id, name, status="running", started_at=_now())
    try:
        result = fn()
    except Exception as exc:
        _set_step(job_id, name, status="failed", detail=str(exc), finished_at=_now())
        raise _StepFailed(f"{name}: {exc}") from exc
    extra = {"summary": result} if isinstance(result, dict) else {}
    _set_step(job_id, name, status="done", finished_at=_now(), **extra)
    return result


def _finish(job_id, status, error=None):
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job["status"] = status
        job["error"] = error
        job["finished_at"] = _now()
        if status == "cancelled":
            for s in job["steps"]:
                if s["status"] in ("pending", "running"):
                    s["status"] = "cancelled"
        _prune_locked()


# -- performer creation -----------------------------------------------------

def _create_artist_manual(f):
    """Insert an artist directly from supplied fields (mirrors the web UI's
    manual create), returning the new artist id."""
    return execute(
        "INSERT INTO artist (name, type, persona, backstory, region, primary_genre,"
        " secondary_genres, consistency_mode, refinement, stage, vocal, language, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f.get("name", "Untitled"), "solo", f.get("persona", ""), f.get("backstory", ""),
         f.get("region", ""), f.get("primary_genre", ""),
         jdump(f.get("secondary_genres") or []),
         f.get("consistency_mode", "prompt_only"),
         float(f.get("refinement", 0.3) or 0.3), f.get("stage", "emerging"),
         generation._norm_vocal(f.get("gender") or f.get("vocal") or ""),
         f.get("language", ""), now_iso()))


def _create_performer(p):
    """Create a performer from the spec's 'performer' block. Returns
    (owner_type, owner_id)."""
    kind = "band" if p.get("kind") == "band" else "artist"
    mode = p.get("mode", "generate")
    if kind == "band":
        return "band", generation.generate_band_from_scratch(p.get("hints") or {})
    if mode == "manual":
        return "artist", _create_artist_manual(p.get("fields") or {})
    return "artist", generation.generate_artist(p.get("hints") or {})


# -- planning + start -------------------------------------------------------

def _planned_steps(spec):
    s = []
    if spec.get("performer"):
        s.append("performer")
    if spec.get("portrait"):
        s.append("portrait")
    s.append("album")
    if spec.get("cover"):
        s.append("cover")
    if spec.get("brief", True):
        s.append("brief")
    if spec.get("render", True):
        s.append("render")
    if spec.get("publish", True):
        s.append("publish")
    return s


def validate(spec):
    """Raise ValueError if the spec can't run. Used by start() and callers."""
    spec = spec or {}
    if not spec.get("performer") and not spec.get("performer_ref"):
        raise ValueError("provide 'performer' (to create one) or 'performer_ref' (existing)")
    if spec.get("performer_ref"):
        ref = spec["performer_ref"]
        kind = "band" if ref.get("kind") == "band" else "artist"
        table = "band" if kind == "band" else "artist"
        if not ref.get("id") or not query(
                f"SELECT 1 FROM {table} WHERE id=?", (ref["id"],), one=True):
            raise ValueError("performer_ref.id not found")
    if not spec.get("album"):
        raise ValueError("'album' spec is required")
    return spec


def start(spec, run_async=True):
    """Validate and launch a pipeline. Returns the job snapshot. With
    run_async=False the chain runs inline (blocking) and the snapshot is final."""
    spec = validate(spec)
    jid = uuid.uuid4().hex[:12]
    included = set(_planned_steps(spec))
    steps = [{"name": n, "status": ("pending" if n in included else "skipped"),
              "done": 0, "total": 0} for n in _STEP_ORDER]
    job = {
        "id": jid, "kind": "oneshot", "status": "running",
        "steps": steps, "created": {}, "error": None,
        "started_at": _now(), "finished_at": None, "cancel": False,
    }
    with _lock:
        _jobs[jid] = job
        _prune_locked()
    if run_async:
        threading.Thread(target=_run, args=(jid, spec), daemon=True).start()
    else:
        _run(jid, spec)
    return get(jid)


def _run(job_id, spec):
    try:
        # 1. Performer — create a new one, or resolve an existing reference.
        if spec.get("performer"):
            owner_type, owner_id = _step(
                job_id, "performer", lambda: _create_performer(spec["performer"]))
        else:
            ref = spec["performer_ref"]
            owner_type = "band" if ref.get("kind") == "band" else "artist"
            owner_id = int(ref["id"])
        _record(job_id, "owner_type", owner_type)
        _record(job_id, f"{owner_type}_id", owner_id)
        try:
            _record(job_id, "performer_name",
                    generation._owner_name_genre(owner_type, owner_id)[0])
        except Exception:
            pass

        # 2. Portrait
        if spec.get("portrait"):
            _step(job_id, "portrait", lambda: (
                generation.generate_band_portrait(owner_id) if owner_type == "band"
                else generation.generate_artist_portrait(owner_id)))

        # 3. Album concept + tracklist
        alb = spec.get("album") or {}
        album_id = _step(job_id, "album", lambda: generation.generate_album(
            owner_type, owner_id, alb.get("ethos", ""), alb.get("style_tags") or [],
            rel_type=alb.get("type", "album"), track_count=alb.get("track_count"),
            title=alb.get("title", ""), self_titled=bool(alb.get("self_titled"))))
        _record(job_id, "album_id", album_id)

        # 4. Album cover art
        if spec.get("cover"):
            _step(job_id, "cover", lambda: generation.generate_album_cover(
                album_id, use_photo=bool(spec.get("cover_use_photo"))))

        # 5. Write lyrics/brief for every track (render needs lyrics).
        if spec.get("brief", True):
            total = _track_count(album_id)
            _step(job_id, "brief", lambda: generation.brief_album(
                album_id, progress=_progress_for(job_id, "brief", total),
                cancel=lambda: _cancelled(job_id)))

        # 6. Render every track
        if spec.get("render", True):
            total = _track_count(album_id)
            _step(job_id, "render", lambda: generation.render_album(
                album_id, progress=_progress_for(job_id, "render", total),
                cancel=lambda: _cancelled(job_id)))

        # 7. Publish every rendered track
        if spec.get("publish", True):
            total = _track_count(album_id)
            _step(job_id, "publish", lambda: publish.publish_album(
                album_id, progress=_progress_for(job_id, "publish", total),
                cancel=lambda: _cancelled(job_id)))

        _finish(job_id, "cancelled" if _cancelled(job_id) else "done")
    except _Cancelled:
        _finish(job_id, "cancelled")
    except _StepFailed as exc:
        _finish(job_id, "error", error=str(exc))
    except Exception as exc:  # pragma: no cover — safety net
        _finish(job_id, "error", error=str(exc))
