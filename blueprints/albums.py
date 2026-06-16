"""Albums — seeded with a band or artist, an ethos, and style tags."""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

from database import execute, jload, query
from backends.llm import LLMError
import generation

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
        )
    except LLMError as exc:
        flash(f"Generation failed: {exc}", "error")
        return redirect(url_for("albums.new_album"))
    flash("Album concept and tracklist generated.", "ok")
    return redirect(url_for("albums.detail", album_id=rid))


@bp.route("/<int:album_id>")
def detail(album_id):
    album = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    if not album:
        return redirect(url_for("albums.list_albums"))
    tracks = query("SELECT * FROM track WHERE release_id=? ORDER BY position", (album_id,))
    owner_name = _owner_name(album["owner_type"], album["owner_id"])
    return render_template("albums/detail.html", album=album, tracks=tracks,
                           owner_name=owner_name, style_tags=jload(album["style_tags"]))


@bp.route("/<int:album_id>/delete", methods=["POST"])
def delete(album_id):
    execute("DELETE FROM track WHERE release_id = ?", (album_id,))
    execute("DELETE FROM release WHERE id = ?", (album_id,))
    flash("Album deleted.", "ok")
    return redirect(url_for("albums.list_albums"))
