"""Tracks — briefed from an album row or written from a free-text prompt,
then rendered to audio through ACE-Step with candidate selection."""

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for,
)

from database import execute, jload, query
from backends.llm import LLMError
from backends.acestep import ACEStepError
import generation
from publish import publish_track, PublishError

bp = Blueprint("tracks", __name__, url_prefix="/tracks")


@bp.route("/")
def library():
    tracks = query(
        "SELECT t.*, r.title AS release_title FROM track t"
        " LEFT JOIN release r ON r.id = t.release_id ORDER BY t.id DESC")
    return render_template("tracks/library.html", tracks=tracks)


@bp.route("/new")
def new_track():
    artists = query("SELECT id, name FROM artist ORDER BY name")
    bands = query("SELECT id, name FROM band ORDER BY name")
    return render_template("tracks/new.html", artists=artists, bands=bands)


@bp.route("/freeform", methods=["POST"])
def freeform():
    prompt = request.form.get("prompt", "").strip()
    if not prompt:
        flash("Enter a prompt for the song.", "error")
        return redirect(url_for("tracks.new_track"))
    owner = request.form.get("owner", "")
    owner_type = owner_id = None
    if ":" in owner:
        owner_type, oid = owner.split(":", 1)
        owner_id = int(oid)
    try:
        tid = generation.create_freeform_track(prompt, owner_type, owner_id)
    except LLMError as exc:
        flash(f"Generation failed: {exc}", "error")
        return redirect(url_for("tracks.new_track"))
    flash("Track brief written from prompt.", "ok")
    return redirect(url_for("tracks.detail", track_id=tid))


@bp.route("/cover")
def new_cover():
    references = generation.list_reference_music()
    artists = query("SELECT id, name FROM artist ORDER BY name")
    bands = query("SELECT id, name FROM band ORDER BY name")
    ref_path = generation.all_settings().get("reference_music_path", "")
    return render_template("tracks/cover.html", references=references,
                           artists=artists, bands=bands, ref_path=ref_path)


@bp.route("/cover", methods=["POST"])
def cover():
    reference = request.form.get("reference", "").strip()
    if not reference:
        flash("Choose a reference track from the music repository.", "error")
        return redirect(url_for("tracks.new_cover"))
    owner = request.form.get("owner", "")
    owner_type = owner_id = None
    if ":" in owner:
        owner_type, oid = owner.split(":", 1)
        owner_id = int(oid)
    try:
        tid = generation.create_cover_track(
            reference, owner_type, owner_id, request.form.get("notes", "").strip())
    except LLMError as exc:
        flash(f"Generation failed: {exc}", "error")
        return redirect(url_for("tracks.new_cover"))
    flash("Cover brief written from the reference track.", "ok")
    return redirect(url_for("tracks.detail", track_id=tid))


@bp.route("/<int:track_id>/publish", methods=["POST"])
def publish(track_id):
    try:
        result = publish_track(track_id)
    except PublishError as exc:
        flash(f"Publish failed: {exc}", "error")
        return redirect(url_for("tracks.detail", track_id=track_id))
    msg = f"Published to {result['path']}."
    if result.get("warning"):
        msg += f" ({result['warning']})"
    flash(msg, "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/brief", methods=["POST"])
def brief(track_id):
    try:
        generation.brief_track_from_album(track_id)
    except LLMError as exc:
        flash(f"Brief failed: {exc}", "error")
        return redirect(url_for("tracks.detail", track_id=track_id))
    flash("Lyrics and production brief written.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/render", methods=["POST"])
def render(track_id):
    try:
        result = generation.render_track(track_id)
    except (ACEStepError, ValueError) as exc:
        flash(f"Render failed: {exc}", "error")
        return redirect(url_for("tracks.detail", track_id=track_id))
    if result.get("ok"):
        flash(f"Rendered. Best candidate integrity {result['integrity']:.2f}.", "ok")
    else:
        flash(f"Render produced no usable candidate: {result.get('reason')}", "error")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>")
def detail(track_id):
    track = query("SELECT * FROM track WHERE id = ?", (track_id,), one=True)
    if not track:
        return redirect(url_for("tracks.library"))
    release = None
    if track["release_id"]:
        release = query("SELECT * FROM release WHERE id = ?", (track["release_id"],), one=True)
    candidates = query(
        "SELECT * FROM candidate WHERE track_id=? ORDER BY integrity DESC, id", (track_id,))
    return render_template("tracks/detail.html", track=track, release=release,
                           candidates=candidates,
                           style_tags=jload(track["style_tags"]),
                           environmentals=jload(track["environmentals"]))


@bp.route("/<int:track_id>/select/<int:candidate_id>", methods=["POST"])
def select_candidate(track_id, candidate_id):
    cand = query("SELECT * FROM candidate WHERE id=? AND track_id=?",
                 (candidate_id, track_id), one=True)
    if cand and cand["audio_path"]:
        import os
        execute("UPDATE candidate SET selected=0 WHERE track_id=?", (track_id,))
        execute("UPDATE candidate SET selected=1 WHERE id=?", (candidate_id,))
        rel = os.path.relpath(
            cand["audio_path"],
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        execute("UPDATE track SET audio_path=?, seed=?, status='rendered' WHERE id=?",
                (rel, cand["seed"], track_id))
        flash("Selected candidate set as the master take.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/trim", methods=["POST"])
def trim(track_id):
    t = query("SELECT audio_path FROM track WHERE id=?", (track_id,), one=True)
    if not t or not t["audio_path"]:
        flash("No rendered audio to trim.", "error")
        return redirect(url_for("tracks.detail", track_id=track_id))
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    abs_path = os.path.join(root, t["audio_path"])
    try:
        trimmed, removed = generation.trim_trailing_noise(abs_path)
    except Exception as exc:
        flash(f"Trim failed: {exc}", "error")
        return redirect(url_for("tracks.detail", track_id=track_id))
    if trimmed:
        flash(f"Trimmed {removed:.1f}s of trailing noise.", "ok")
    else:
        flash("Nothing to trim — no trailing noise detected (or not a WAV).", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/delete", methods=["POST"])
def delete(track_id):
    execute("DELETE FROM track WHERE id = ?", (track_id,))
    flash("Track deleted.", "ok")
    return redirect(url_for("tracks.library"))
