"""Tracks — briefed from an album row or written from a free-text prompt,
then rendered to audio through ACE-Step with candidate selection."""

from flask import (
    Blueprint, flash, jsonify, redirect, render_template, request, url_for,
)

from database import execute, jdump, jload, query
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
    tag_options = [r["name"] for r in query("SELECT name FROM style_tag ORDER BY name")]
    return render_template("tracks/detail.html", track=track, release=release,
                           candidates=candidates,
                           style_tags=jload(track["style_tags"]),
                           environmentals=jload(track["environmentals"]),
                           tag_options=tag_options)


@bp.route("/<int:track_id>/lyrics", methods=["POST"])
def save_lyrics(track_id):
    if not query("SELECT 1 FROM track WHERE id=?", (track_id,), one=True):
        return redirect(url_for("tracks.library"))
    execute("UPDATE track SET lyrics=? WHERE id=?",
            (request.form.get("lyrics", ""), track_id))
    flash("Lyrics saved.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/source", methods=["POST"])
def save_source(track_id):
    if not query("SELECT 1 FROM track WHERE id=?", (track_id,), one=True):
        return redirect(url_for("tracks.library"))
    execute("UPDATE track SET subject=?, summary=?, lyric_notes=? WHERE id=?",
            (request.form.get("subject", "").strip(),
             request.form.get("summary", "").strip(),
             request.form.get("lyric_notes", "").strip(), track_id))
    flash("Lyric source saved.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/regenerate-lyrics", methods=["POST"])
def regenerate_lyrics(track_id):
    try:
        generation.regenerate_lyrics(track_id)
    except (LLMError, ValueError) as exc:
        flash(f"Lyric regeneration failed: {exc}", "error")
        return redirect(url_for("tracks.detail", track_id=track_id))
    flash("Lyrics regenerated.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/tags/add", methods=["POST"])
def add_tag(track_id):
    t = query("SELECT style_tags FROM track WHERE id=?", (track_id,), one=True)
    if not t:
        return redirect(url_for("tracks.library"))
    tag = request.form.get("tag", "").strip()
    tags = jload(t["style_tags"], [])
    if tag and tag.lower() not in [x.lower() for x in tags]:
        tags.append(tag)
        execute("UPDATE track SET style_tags=? WHERE id=?", (jdump(tags), track_id))
        flash(f"Added style tag '{tag}'.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/tags/remove", methods=["POST"])
def remove_tag(track_id):
    t = query("SELECT style_tags FROM track WHERE id=?", (track_id,), one=True)
    if not t:
        return redirect(url_for("tracks.library"))
    tag = request.form.get("tag", "")
    tags = [x for x in jload(t["style_tags"], []) if x != tag]
    execute("UPDATE track SET style_tags=? WHERE id=?", (jdump(tags), track_id))
    flash(f"Removed style tag '{tag}'.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/env/add", methods=["POST"])
def add_env(track_id):
    t = query("SELECT environmentals FROM track WHERE id=?", (track_id,), one=True)
    if not t:
        return redirect(url_for("tracks.library"))
    item = request.form.get("env", "").strip()
    items = jload(t["environmentals"], [])
    if item and item.lower() not in [x.lower() for x in items]:
        items.append(item)
        execute("UPDATE track SET environmentals=? WHERE id=?", (jdump(items), track_id))
        flash(f"Added environment cue '{item}'.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


@bp.route("/<int:track_id>/env/remove", methods=["POST"])
def remove_env(track_id):
    t = query("SELECT environmentals FROM track WHERE id=?", (track_id,), one=True)
    if not t:
        return redirect(url_for("tracks.library"))
    item = request.form.get("env", "")
    items = [x for x in jload(t["environmentals"], []) if x != item]
    execute("UPDATE track SET environmentals=? WHERE id=?", (jdump(items), track_id))
    flash(f"Removed environment cue '{item}'.", "ok")
    return redirect(url_for("tracks.detail", track_id=track_id))


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
