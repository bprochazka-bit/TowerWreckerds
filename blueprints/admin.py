"""Admin — configure the LLM and ACE-Step backends, manage taxonomy."""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

import os

from database import (
    all_settings, execute, import_genres, import_style_tags,
    parse_genres_payload, parse_style_tags_payload, query, set_setting,
    upsert_genre, upsert_style_tag,
)
from backends.llm import LLMClient
from backends.acestep import ACEStepClient
from backends.imagegen import ImageGenClient
import publish

bp = Blueprint("admin", __name__, url_prefix="/admin")

SETTING_KEYS = [
    "llm_backend", "llm_base_url", "llm_model", "llm_api_key",
    "llm_temperature", "llm_max_tokens", "lyrics_style", "lyrics_structure",
    "lyrics_temperature",
    "acestep_base_url", "acestep_candidates", "acestep_duration_ceiling",
    "acestep_format", "acestep_trim_noise",
    "acestep_cover_strength", "acestep_cover_noise", "lyrics_strip_parentheticals",
    "mock_mode",
    "reference_music_path",
    "sdcpp_url", "sdcpp_path", "sdcpp_model", "sdcpp_steps", "sdcpp_size",
    "sdcpp_cfg", "sdcpp_negative",
    "publish_path",
]
CHECKBOX_KEYS = {"mock_mode", "acestep_trim_noise", "lyrics_strip_parentheticals"}


@bp.route("/")
def admin_home():
    settings = all_settings()
    genres = query("SELECT * FROM genre ORDER BY name")
    style_tags = query("SELECT * FROM style_tag ORDER BY category, name")
    enc_ok, enc_msg = publish.encoder_status()
    return render_template("admin/index.html", settings=settings,
                           genres=genres, style_tags=style_tags,
                           encoder_ok=enc_ok, encoder_msg=enc_msg)


@bp.route("/settings", methods=["POST"])
def save_settings():
    for key in SETTING_KEYS:
        if key in CHECKBOX_KEYS:
            set_setting(key, "1" if request.form.get(key) else "0")
        else:
            set_setting(key, request.form.get(key, ""))
    flash("Settings saved.", "ok")
    return redirect(url_for("admin.admin_home"))


@bp.route("/test/llm", methods=["POST"])
def test_llm():
    ok, msg = LLMClient().ping()
    cls = "ok" if ok else "error"
    label = "LLM reachable" if ok else "LLM unreachable"
    return f'<span class="status {cls}">{label}: {msg}</span>'


@bp.route("/test/acestep", methods=["POST"])
def test_acestep():
    ok, msg = ACEStepClient().ping()
    cls = "ok" if ok else "error"
    label = "ACE-Step reachable" if ok else "ACE-Step unreachable"
    return f'<span class="status {cls}">{label}: {msg}</span>'


@bp.route("/test/sdcpp", methods=["POST"])
def test_sdcpp():
    ok, msg = ImageGenClient().ping()
    cls = "ok" if ok else "error"
    label = "sd.cpp ready" if ok else "sd.cpp unavailable"
    return f'<span class="status {cls}">{label}: {msg}</span>'


@bp.route("/test/reference", methods=["POST"])
def test_reference():
    path = (request.form.get("reference_music_path", "") or "").strip()
    if not path:
        return '<span class="status error">No reference-music path set</span>'
    if not os.path.isdir(path):
        return f'<span class="status error">Not a folder: {path}</span>'
    # Count audio files in the field's path directly (it may be unsaved).
    import generation
    exts = generation.REFERENCE_AUDIO_EXTS
    count = 0
    for _root, _dirs, files in os.walk(path):
        count += sum(1 for f in files if f.lower().endswith(exts))
        if count > 2000:
            break
    return f'<span class="status ok">Folder OK — {count} audio file(s) found</span>'


def _genre_form_data():
    """Pull the full set of genre fields out of a submitted form."""
    return {
        "name": request.form.get("name", ""),
        "description": request.form.get("description", ""),
        "descriptors": request.form.get("descriptors", ""),
        "typical_instruments": request.form.get("typical_instruments", ""),
        "tempo_range": request.form.get("tempo_range", ""),
        "common_regions": request.form.get("common_regions", ""),
        "base_style_tags": request.form.get("base_style_tags", ""),
        "lyric_guidance": request.form.get("lyric_guidance", ""),
    }


@bp.route("/genres", methods=["POST"])
def add_genre():
    data = _genre_form_data()
    if not data["name"].strip():
        flash("Genre needs a name.", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    outcome = upsert_genre(data)
    flash(f"Genre '{data['name'].strip()}' {outcome}.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/genres/<int:genre_id>/edit")
def edit_genre(genre_id):
    genre = query("SELECT * FROM genre WHERE id = ?", (genre_id,), one=True)
    if not genre:
        flash("Genre not found.", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    return render_template("admin/genre_edit.html", genre=genre)


@bp.route("/genres/<int:genre_id>/edit", methods=["POST"])
def update_genre(genre_id):
    data = _genre_form_data()
    if not data["name"].strip():
        flash("Genre needs a name.", "error")
        return redirect(url_for("admin.edit_genre", genre_id=genre_id))
    upsert_genre(data, genre_id=genre_id)
    flash(f"Genre '{data['name'].strip()}' updated.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/genres/import", methods=["POST"])
def import_genres_route():
    text = request.form.get("genres_json", "").strip()
    upload = request.files.get("genres_file")
    if upload and upload.filename:
        text = upload.read().decode("utf-8", "replace")
    if not text:
        flash("Paste JSON or choose a file to import.", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    try:
        items = parse_genres_payload(text)
    except ValueError as exc:
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    res = import_genres(items)
    msg = (f"Imported genres: {res['added']} added, {res['updated']} updated, "
           f"{res['skipped']} skipped.")
    flash(msg, "ok")
    if res["errors"]:
        flash("Some rows had issues: " + "; ".join(res["errors"][:5])
              + ("…" if len(res["errors"]) > 5 else ""), "error")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/genres/<int:genre_id>/delete", methods=["POST"])
def delete_genre(genre_id):
    execute("DELETE FROM genre WHERE id = ?", (genre_id,))
    flash("Genre removed.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


def _style_tag_form_data():
    return {
        "name": request.form.get("name", ""),
        "category": request.form.get("category", "mood"),
        "acestep_phrases": request.form.get("acestep_phrases", ""),
    }


@bp.route("/style-tags", methods=["POST"])
def add_style_tag():
    data = _style_tag_form_data()
    if not data["name"].strip():
        flash("Style tag needs a name.", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    outcome = upsert_style_tag(data)
    flash(f"Style tag '{data['name'].strip()}' {outcome}.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/style-tags/<int:tag_id>/edit")
def edit_style_tag(tag_id):
    tag = query("SELECT * FROM style_tag WHERE id = ?", (tag_id,), one=True)
    if not tag:
        flash("Style tag not found.", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    return render_template("admin/style_tag_edit.html", tag=tag)


@bp.route("/style-tags/<int:tag_id>/edit", methods=["POST"])
def update_style_tag(tag_id):
    data = _style_tag_form_data()
    if not data["name"].strip():
        flash("Style tag needs a name.", "error")
        return redirect(url_for("admin.edit_style_tag", tag_id=tag_id))
    upsert_style_tag(data, tag_id=tag_id)
    flash(f"Style tag '{data['name'].strip()}' updated.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/style-tags/import", methods=["POST"])
def import_style_tags_route():
    text = request.form.get("style_tags_json", "").strip()
    upload = request.files.get("style_tags_file")
    if upload and upload.filename:
        text = upload.read().decode("utf-8", "replace")
    if not text:
        flash("Paste JSON or choose a file to import.", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    try:
        items = parse_style_tags_payload(text)
    except ValueError as exc:
        flash(f"Import failed: {exc}", "error")
        return redirect(url_for("admin.admin_home") + "#taxonomy")
    res = import_style_tags(items)
    flash(f"Imported style tags: {res['added']} added, {res['updated']} updated, "
          f"{res['skipped']} skipped.", "ok")
    if res["errors"]:
        flash("Some rows had issues: " + "; ".join(res["errors"][:5])
              + ("…" if len(res["errors"]) > 5 else ""), "error")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/style-tags/<int:tag_id>/delete", methods=["POST"])
def delete_style_tag(tag_id):
    execute("DELETE FROM style_tag WHERE id = ?", (tag_id,))
    flash("Style tag removed.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")
