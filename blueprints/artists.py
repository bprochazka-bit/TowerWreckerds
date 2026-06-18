"""Artists — profiles of people in the music world."""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

from database import execute, jdump, jload, now_iso, query
from backends.llm import LLMError
import generation

bp = Blueprint("artists", __name__, url_prefix="/artists")


@bp.route("/")
def list_artists():
    artists = query("SELECT * FROM artist ORDER BY id DESC")
    return render_template("artists/list.html", artists=artists)


@bp.route("/new")
def new_artist():
    genres = query("SELECT name FROM genre ORDER BY name")
    return render_template("artists/new.html", genres=genres)


@bp.route("/", methods=["POST"])
def create_artist():
    f = request.form
    aid = execute(
        "INSERT INTO artist (name, type, persona, backstory, region, primary_genre,"
        " secondary_genres, consistency_mode, refinement, stage, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (f.get("name", "Untitled"), "solo", f.get("persona", ""), f.get("backstory", ""),
         f.get("region", ""), f.get("primary_genre", ""), jdump([]),
         f.get("consistency_mode", "prompt_only"),
         float(f.get("refinement", 0.3) or 0.3), f.get("stage", "emerging"), now_iso()),
    )
    flash("Artist created.", "ok")
    return redirect(url_for("artists.detail", artist_id=aid))


@bp.route("/generate", methods=["POST"])
def generate():
    hints = {
        "name": request.form.get("name", ""),
        "primary_genre": request.form.get("primary_genre", ""),
        "region": request.form.get("region", ""),
        "vibe": request.form.get("vibe", ""),
    }
    try:
        aid = generation.generate_artist(hints)
    except LLMError as exc:
        flash(f"Generation failed: {exc}", "error")
        return redirect(url_for("artists.new_artist"))
    flash("Artist generated.", "ok")
    return redirect(url_for("artists.detail", artist_id=aid))


@bp.route("/<int:artist_id>")
def detail(artist_id):
    artist = query("SELECT * FROM artist WHERE id = ?", (artist_id,), one=True)
    if not artist:
        return redirect(url_for("artists.list_artists"))
    bands = query(
        "SELECT b.id, b.name, m.instrument FROM membership m"
        " JOIN band b ON b.id = m.band_id WHERE m.artist_id = ?", (artist_id,))
    releases = query(
        "SELECT * FROM release WHERE owner_type='artist' AND owner_id=? ORDER BY id DESC",
        (artist_id,))
    return render_template("artists/detail.html", artist=artist, bands=bands,
                           releases=releases, secondary=jload(artist["secondary_genres"]))


@bp.route("/<int:artist_id>/influences", methods=["POST"])
def update_influences(artist_id):
    execute("UPDATE artist SET influences=? WHERE id=?",
            (request.form.get("influences", "").strip(), artist_id))
    flash("Influences saved — applied to this artist's renders.", "ok")
    return redirect(url_for("artists.detail", artist_id=artist_id))


@bp.route("/<int:artist_id>/vocal", methods=["POST"])
def update_vocal(artist_id):
    v = request.form.get("vocal", "").strip().lower()
    execute("UPDATE artist SET vocal=? WHERE id=?",
            (v if v in ("female", "male", "androgynous") else "", artist_id))
    flash("Lead vocal saved — applied to this artist's renders.", "ok")
    return redirect(url_for("artists.detail", artist_id=artist_id))


@bp.route("/<int:artist_id>/delete", methods=["POST"])
def delete(artist_id):
    execute("DELETE FROM artist WHERE id = ?", (artist_id,))
    flash("Artist deleted.", "ok")
    return redirect(url_for("artists.list_artists"))
