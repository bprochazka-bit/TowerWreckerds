"""In-memory background jobs for album-wide actions.

Album-wide actions — write all briefs, render all tracks, publish — are
long-running, so they run in a daemon thread and report progress into a small
in-process registry. The UI polls a fragment endpoint (htmx) for a live
progress bar with per-track (and, for renders, per-candidate) state, and can
request cancellation.

Intentionally simple: a single-process, single-user local console. State lives
in memory (not the DB) and resets on restart, which is fine — the work each job
does (briefs, audio, published files) is persisted as it goes. Only one job per
album runs at a time.
"""

import threading
from datetime import datetime, timezone

import generation
import publish
from database import all_settings, query

_jobs = {}                 # album_id -> job dict
_lock = threading.Lock()

VERB = {"render": "Rendering", "brief": "Writing briefs", "publish": "Publishing"}
_START_STATUS = {"render": "rendering", "brief": "briefing", "publish": "publishing"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot(job):
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


def cancel_job(album_id):
    """Request cancellation of the running job for this album."""
    with _lock:
        job = _jobs.get(int(album_id))
        if job and job["status"] == "running":
            job["cancel"] = True
        return _snapshot(job) if job else None


def _plan(kind, album_id):
    """Build the per-track plan and total work units for a job kind."""
    try:
        cands = max(1, int(all_settings().get("acestep_candidates", 3)))
    except (TypeError, ValueError):
        cands = 3
    rows = query("SELECT id, title, position, lyrics, audio_path FROM track"
                 " WHERE release_id = ? ORDER BY position", (album_id,))
    plan, total = [], 0
    for r in rows:
        has_lyrics = bool((r["lyrics"] or "").strip())
        has_audio = bool(r["audio_path"])
        candidates_total = 0
        if kind == "render":
            will = (not has_audio) and has_lyrics
            candidates_total = cands if will else 0
            status = "rendered" if has_audio else ("pending" if will else "skipped")
            total += candidates_total
        elif kind == "brief":
            will = not has_lyrics
            status = "pending" if will else "skipped"   # has lyrics => already briefed
            total += 1 if will else 0
        else:  # publish
            will = has_audio
            status = "pending" if will else "skipped"    # no audio => nothing to publish
            total += 1 if will else 0
        plan.append({
            "id": r["id"], "title": r["title"] or "Untitled",
            "position": r["position"], "will": will,
            "candidates_total": candidates_total, "candidates_done": 0,
            "status": status, "integrity": None,
        })
    return plan, total


def start(kind, album_id):
    """Start (or return the already-running) album job of the given kind."""
    album_id = int(album_id)
    with _lock:
        existing = _jobs.get(album_id)
        if existing and existing["status"] == "running":
            return _snapshot(existing)

    plan, total = _plan(kind, album_id)
    job = {
        "id": f"{kind}-{album_id}-{int(datetime.now().timestamp())}",
        "album_id": album_id, "kind": kind, "status": "running",
        "tracks": plan, "total_units": total, "done_units": 0,
        "started_at": _now(), "finished_at": None, "summary": None,
        "summary_line": "", "message": "", "cancel": False,
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


def _summary_line(kind, s):
    if kind == "render":
        return (f"rendered {s.get('rendered',0)} · skipped {s.get('skipped',0)} · "
                f"failed {s.get('failed',0)} · cancelled {s.get('cancelled',0)}")
    if kind == "brief":
        return (f"briefed {s.get('briefed',0)} · skipped {s.get('skipped',0)} · "
                f"failed {s.get('failed',0)} · cancelled {s.get('cancelled',0)}")
    line = f"published {s.get('count',0)} track(s)"
    if s.get("failed"):
        line += f" · failed {s['failed']}"
    if s.get("cancelled"):
        line += f" · cancelled {s['cancelled']}"
    if s.get("warning"):
        line += f" · {s['warning']}"
    return line


def _run(album_id):
    with _lock:
        kind = _jobs[album_id]["kind"]

    def cancelled():
        with _lock:
            job = _jobs.get(album_id)
            return bool(job and job["cancel"])

    def progress(event, **data):
        with _lock:
            job = _jobs.get(album_id)
            if not job:
                return
            track = _find_track(job, data.get("track_id"))
            if event == "track_start" and track:
                track["status"] = _START_STATUS[job["kind"]]
            elif event == "candidate" and track:
                track["candidates_done"] = (data.get("index", 0) or 0) + 1
                job["done_units"] = min(job["total_units"], job["done_units"] + 1)
            elif event == "track_done" and track:
                track["status"] = data.get("status", "done")
                track["integrity"] = data.get("integrity")
                if job["kind"] != "render":
                    job["done_units"] = min(job["total_units"], job["done_units"] + 1)

    runner = {"render": generation.render_album, "brief": generation.brief_album,
              "publish": publish.publish_album}[kind]
    try:
        summary = runner(album_id, progress=progress, cancel=cancelled)
        with _lock:
            job = _jobs.get(album_id)
            if job:
                was_cancelled = job["cancel"]
                job["status"] = "cancelled" if was_cancelled else "done"
                job["summary"] = summary
                job["summary_line"] = _summary_line(kind, summary)
                if not was_cancelled:
                    job["done_units"] = job["total_units"]
                else:
                    # any track that never started reads as cancelled
                    for t in job["tracks"]:
                        if t["status"] in ("pending",) + tuple(_START_STATUS.values()):
                            t["status"] = "cancelled"
                job["finished_at"] = _now()
    except Exception as exc:
        with _lock:
            job = _jobs.get(album_id)
            if job:
                job["status"] = "error"
                job["message"] = str(exc)
                job["finished_at"] = _now()
