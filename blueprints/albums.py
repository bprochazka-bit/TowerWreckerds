"""Albums — seeded with a band or artist, an ethos, and style tags."""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

from database import execute, jload, now_iso, query
from backends.llm import LLMError
from backends.imagegen import ImageGenError
import generation
import jobs
from publish import publish_album, PublishError

bp = Blueprint("albums", __name__, url_prefix="/albums")


@bp.route("/")
def list_albums():
    albums = query("SELECT * FROM release ORDER BY id DESC")
    owners = {}
    for a in albums:
        owners[a["id"]] = _owner_name(a["owner_type"], a["owner_id"])
    return render_template("albums/list.html", albums=albums, owners=owners)


def _owner_name(owner_type, owner_id):
    table = "band" if owner_type == "band" else "artist"
    row = query(f"SELECT name FROM {table} WHERE id = ?", (owner_id,), one=True)
    return row["name"] if row else "(unknown)"


@bp.route("/new")
def new_album():
    artists = query("SELECT id, name, primary_genre FROM artist ORDER BY name")
    bands = query("SELECT id, name, primary_genre FROM band ORDER BY name")
    style_tags = query("SELECT name, category FROM style_tag ORDER BY category, name")
    return render_template("albums/new.html", artists=artists, bands=bands,
                           style_tags=style_tags)


@bp.route("/generate", methods=["POST"])
def generate():
    owner = request.form.get("owner", "")  # "artist:3" or "band:1"
    if ":" not in owner:
        flash("Choose a performer to seed the album.", "error")
        return redirect(url_for("albums.new_album"))
    owner_type, owner_id = owner.split(":", 1)
    style_tags = request.form.getlist("style_tags")
    extra = request.form.get("extra_tags", "").strip()
    if extra:
        style_tags += [t.strip() for t in extra.split(",") if t.strip()]
    try:
        rid = generation.generate_album(
            owner_type, int(owner_id),
            request.form.get("ethos", ""), style_tags,
            rel_type=request.form.get("type", "album"),
            track_count=request.form.get("track_count") or None,
            title=request.form.get("title", ""),
            self_titled=bool(request.form.get("self_titled")),
        )
    except LLMError as exc:
        flash(f"Generation failed: {exc}", "error")
        return redirect(url_for("albums.new_album"))
    flash("Album concept and tracklist generated.", "ok")
    return redirect(url_for("albums.detail", album_id=rid))


@bp.route("/<int:album_id>/details", methods=["POST"])
def update_details(album_id):
    if not query("SELECT 1 FROM release WHERE id=?", (album_id,), one=True):
        return redirect(url_for("albums.list_albums"))
    title = request.form.get("title", "").strip()
    execute("UPDATE release SET title=?, inspiration=?, ethos=? WHERE id=?",
            (title or "Untitled", request.form.get("inspiration", "").strip(),
             request.form.get("ethos", "").strip(), album_id))
    flash("Album details saved.", "ok")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/regenerate-tracklist", methods=["POST"])
def regenerate_tracklist(album_id):
    try:
        generation.regenerate_tracklist(album_id)
    except (LLMError, ValueError) as exc:
        flash(f"Regeneration failed: {exc}", "error")
        return redirect(url_for("albums.detail", album_id=album_id))
    flash("Tracklist regenerated.", "ok")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/create-empty", methods=["POST"])
def create_empty():
    owner = request.form.get("owner", "")
    if ":" not in owner:
        flash("Choose a performer for the release.", "error")
        return redirect(url_for("albums.new_album"))
    owner_type, owner_id = owner.split(":", 1)
    rtype = request.form.get("type", "album")
    if rtype not in ("single", "ep", "album"):
        rtype = "album"
    title = request.form.get("title", "").strip() or "Untitled"
    rid = execute(
        "INSERT INTO release (owner_type, owner_id, type, title, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (owner_type, int(owner_id), rtype, title, "tracklist", now_iso()))
    flash("Empty release created — add tracks to compose it.", "ok")
    return redirect(url_for("albums.detail", album_id=rid))


@bp.route("/<int:album_id>/add-track", methods=["POST"])
def add_track(album_id):
    try:
        generation.add_track_to_album(album_id, int(request.form.get("track_id", "0")))
    except (ValueError, TypeError) as exc:
        flash(f"Could not add track: {exc}", "error")
    else:
        flash("Track added to the release.", "ok")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/move/<int:track_id>", methods=["POST"])
def move(album_id, track_id):
    generation.move_track(album_id, track_id, request.form.get("dir", "up"))
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/track/<int:track_id>/lock", methods=["POST"])
def toggle_lock(album_id, track_id):
    t = query("SELECT locked FROM track WHERE id=? AND release_id=?",
              (track_id, album_id), one=True)
    if t is not None:
        cur = t["locked"] if "locked" in t.keys() else 0
        execute("UPDATE track SET locked=? WHERE id=?", (0 if cur else 1, track_id))
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/remove/<int:track_id>", methods=["POST"])
def remove_track(album_id, track_id):
    """Detach a track from the album — it becomes a standalone track again."""
    execute("UPDATE track SET release_id=NULL WHERE id=? AND release_id=?",
            (track_id, album_id))
    generation.renumber_tracks(album_id)
    flash("Track removed from the release (kept as a standalone track).", "ok")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>")
def detail(album_id):
    album = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    if not album:
        return redirect(url_for("albums.list_albums"))
    tracks = query("SELECT * FROM track WHERE release_id=? ORDER BY position", (album_id,))
    owner_name = _owner_name(album["owner_type"], album["owner_id"])
    cover_prompt = (album["cover_prompt"] if "cover_prompt" in album.keys() else None) \
        or generation.build_album_cover_prompt(album_id)
    # Standalone tracks that can be pulled into this release.
    addable = query("SELECT id, title FROM track WHERE release_id IS NULL ORDER BY id DESC")
    return render_template("albums/detail.html", album=album, tracks=tracks,
                           owner_name=owner_name, style_tags=jload(album["style_tags"]),
                           cover_prompt=cover_prompt, job=jobs.get_job(album_id),
                           addable=addable)


def _job_fragment(album_id, job):
    album = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    return render_template("albums/_job_progress.html", album=album, job=job)


@bp.route("/<int:album_id>/brief-all", methods=["POST"])
def brief_all(album_id):
    # htmx clients get a background job + live progress; without JS we fall back
    # to a synchronous run and a flash summary.
    if request.headers.get("HX-Request"):
        return _job_fragment(album_id, jobs.start("brief", album_id))
    try:
        res = generation.brief_album(album_id)
    except LLMError as exc:
        flash(f"Brief failed: {exc}", "error")
        return redirect(url_for("albums.detail", album_id=album_id))
    flash(f"Briefed {res['briefed']} track(s); {res['skipped']} already had lyrics.", "ok")
    if res["errors"]:
        flash("Some tracks failed: " + "; ".join(res["errors"][:5])
              + ("…" if len(res["errors"]) > 5 else ""), "error")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/render-all", methods=["POST"])
def render_all(album_id):
    if request.headers.get("HX-Request"):
        return _job_fragment(album_id, jobs.start("render", album_id))
    res = generation.render_album(album_id)
    flash(f"Rendered {res['rendered']} track(s); {res['skipped']} skipped, "
          f"{res['failed']} failed.", "ok" if not res["failed"] else "error")
    if res["errors"]:
        flash("Details: " + "; ".join(res["errors"][:5])
              + ("…" if len(res["errors"]) > 5 else ""), "error")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/job-progress")
def job_progress(album_id):
    if not query("SELECT 1 FROM release WHERE id = ?", (album_id,), one=True):
        return ""
    return _job_fragment(album_id, jobs.get_job(album_id))


@bp.route("/<int:album_id>/cancel-job", methods=["POST"])
def cancel_job(album_id):
    return _job_fragment(album_id, jobs.cancel_job(album_id))


@bp.route("/<int:album_id>/cover", methods=["POST"])
def cover(album_id):
    try:
        generation.generate_album_cover(
            album_id, prompt=request.form.get("prompt"),
            use_photo=bool(request.form.get("use_photo")))
    except (ImageGenError, ValueError) as exc:
        flash(f"Cover generation failed: {exc}", "error")
        return redirect(url_for("albums.detail", album_id=album_id))
    flash("Album cover generated.", "ok")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/publish", methods=["POST"])
def publish(album_id):
    if request.headers.get("HX-Request"):
        # The job surfaces "no rendered tracks" (etc.) as an error in the fragment.
        return _job_fragment(album_id, jobs.start("publish", album_id))
    try:
        result = publish_album(album_id)
    except PublishError as exc:
        flash(f"Publish failed: {exc}", "error")
        return redirect(url_for("albums.detail", album_id=album_id))
    msg = f"Published {result['count']} track(s) to {result['dir']}."
    if result.get("warning"):
        msg += f" ({result['warning']})"
    flash(msg, "ok")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/delete", methods=["POST"])
def delete(album_id):
    execute("DELETE FROM track WHERE release_id = ?", (album_id,))
    execute("DELETE FROM release WHERE id = ?", (album_id,))
    flash("Album deleted.", "ok")
    return redirect(url_for("albums.list_albums"))
