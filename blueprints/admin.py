"""Admin — configure the LLM and ACE-Step backends, manage taxonomy."""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

import os

from database import all_settings, execute, jdump, query, set_setting
from backends.llm import LLMClient
from backends.acestep import ACEStepClient
from backends.imagegen import ImageGenClient

bp = Blueprint("admin", __name__, url_prefix="/admin")

SETTING_KEYS = [
    "llm_backend", "llm_base_url", "llm_model", "llm_api_key",
    "llm_temperature", "llm_max_tokens",
    "acestep_base_url", "acestep_candidates", "acestep_duration_ceiling",
    "acestep_format", "acestep_trim_noise",
    "acestep_cover_strength", "acestep_cover_noise", "mock_mode",
    "reference_music_path",
    "sdcpp_url", "sdcpp_path", "sdcpp_model", "sdcpp_steps", "sdcpp_size",
    "sdcpp_cfg", "sdcpp_negative",
    "publish_path",
]
CHECKBOX_KEYS = {"mock_mode", "acestep_trim_noise"}


@bp.route("/")
def admin_home():
    settings = all_settings()
    genres = query("SELECT * FROM genre ORDER BY name")
    style_tags = query("SELECT * FROM style_tag ORDER BY category, name")
    return render_template("admin/index.html", settings=settings,
                           genres=genres, style_tags=style_tags)


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


@bp.route("/genres", methods=["POST"])
def add_genre():
    name = request.form.get("name", "").strip()
    if name:
        try:
            execute(
                "INSERT INTO genre (name, description, base_style_tags) VALUES (?,?,?)",
                (name, request.form.get("description", ""),
                 jdump([t.strip() for t in request.form.get("base_style_tags", "").split(",") if t.strip()])),
            )
            flash(f"Genre '{name}' added.", "ok")
        except Exception:
            flash("Genre already exists.", "error")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/genres/<int:genre_id>/delete", methods=["POST"])
def delete_genre(genre_id):
    execute("DELETE FROM genre WHERE id = ?", (genre_id,))
    flash("Genre removed.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/style-tags", methods=["POST"])
def add_style_tag():
    name = request.form.get("name", "").strip()
    if name:
        try:
            execute(
                "INSERT INTO style_tag (name, category, acestep_phrases) VALUES (?,?,?)",
                (name, request.form.get("category", "mood"),
                 jdump([p.strip() for p in request.form.get("acestep_phrases", name).split(",") if p.strip()])),
            )
            flash(f"Style tag '{name}' added.", "ok")
        except Exception:
            flash("Style tag already exists.", "error")
    return redirect(url_for("admin.admin_home") + "#taxonomy")


@bp.route("/style-tags/<int:tag_id>/delete", methods=["POST"])
def delete_style_tag(tag_id):
    execute("DELETE FROM style_tag WHERE id = ?", (tag_id,))
    flash("Style tag removed.", "ok")
    return redirect(url_for("admin.admin_home") + "#taxonomy")
