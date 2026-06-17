"""In-memory background jobs for album renders.

Rendering a whole album is long-running (N tracks × candidates each), so the
album "Render all" action runs in a daemon thread and reports progress into a
small in-process registry. The UI polls a fragment endpoint (htmx) to show a
live progress bar with per-track and per-candidate state.

This is intentionally simple — a single-process, single-user local console.
State lives in memory (not the DB); it resets if the app restarts, which is
fine because the rendered audio itself is persisted by render_track as it goes.
"""

import threading
from datetime import datetime, timezone

import generation
from database import all_settings, query

_jobs = {}                 # album_id -> job dict
_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot(job):
    """A deep-enough copy for safe rendering outside the lock."""
    copy = dict(job)
    copy["tracks"] = [dict(t) for t in job["tracks"]]
    return copy


def get_job(album_id):
    with _lock:
        job = _jobs.get(int(album_id))
        return _snapshot(job) if job else None


def is_running(album_id):
    with _lock:
        job = _jobs.get(int(album_id))
        return bool(job and job["status"] == "running")


def start_album_render(album_id):
    """Start (or return the already-running) album render job. Returns a
    snapshot of the job state."""
    album_id = int(album_id)
    with _lock:
        existing = _jobs.get(album_id)
        if existing and existing["status"] == "running":
            return _snapshot(existing)

    try:
        cands = max(1, int(all_settings().get("acestep_candidates", 3)))
    except (TypeError, ValueError):
        cands = 3

    rows = query("SELECT id, title, position, lyrics, audio_path FROM track"
                 " WHERE release_id = ? ORDER BY position", (album_id,))
    plan = []
    for r in rows:
        will_render = (not r["audio_path"]) and bool((r["lyrics"] or "").strip())
        if r["audio_path"]:
            status = "rendered"
        elif will_render:
            status = "pending"
        else:
            status = "skipped"          # no lyrics/brief yet
        plan.append({
            "id": r["id"], "title": r["title"] or "Untitled",
            "position": r["position"], "will_render": will_render,
            "candidates_total": cands if will_render else 0,
            "candidates_done": 0, "status": status, "integrity": None,
        })
    total_units = sum(p["candidates_total"] for p in plan)

    job = {
        "id": f"album-{album_id}-{int(datetime.now().timestamp())}",
        "album_id": album_id, "status": "running",
        "tracks": plan, "total_units": total_units, "done_units": 0,
        "started_at": _now(), "finished_at": None, "summary": None, "message": "",
    }
    with _lock:
        _jobs[album_id] = job

    threading.Thread(target=_run, args=(album_id,), daemon=True).start()
    with _lock:
        return _snapshot(_jobs[album_id])


def _find_track(job, track_id):
    for t in job["tracks"]:
        if t["id"] == track_id:
            return t
    return None


def _run(album_id):
    def progress(event, **data):
        with _lock:
            job = _jobs.get(album_id)
            if not job:
                return
            track = _find_track(job, data.get("track_id"))
            if event == "track_start" and track:
                track["status"] = "rendering"
            elif event == "candidate" and track:
                track["candidates_done"] = (data.get("index", 0) or 0) + 1
                job["done_units"] = min(job["total_units"], job["done_units"] + 1)
            elif event == "track_done" and track:
                track["status"] = data.get("status", "rendered")
                track["integrity"] = data.get("integrity")

    try:
        summary = generation.render_album(album_id, progress=progress)
        with _lock:
            job = _jobs.get(album_id)
            if job:
                job["status"] = "done"
                job["summary"] = summary
                job["done_units"] = job["total_units"]
                job["finished_at"] = _now()
    except Exception as exc:
        with _lock:
            job = _jobs.get(album_id)
            if job:
                job["status"] = "error"
                job["message"] = str(exc)
                job["finished_at"] = _now()
