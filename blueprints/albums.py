"""Albums — seeded with a band or artist, an ethos, and style tags."""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

from database import execute, jload, query
from backends.llm import LLMError
from backends.imagegen import ImageGenError
import generation
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


@bp.route("/<int:album_id>/brief-all", methods=["POST"])
def brief_all(album_id):
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
    res = generation.render_album(album_id)
    flash(f"Rendered {res['rendered']} track(s); {res['skipped']} skipped, "
          f"{res['failed']} failed.", "ok" if not res["failed"] else "error")
    if res["errors"]:
        flash("Details: " + "; ".join(res["errors"][:5])
              + ("…" if len(res["errors"]) > 5 else ""), "error")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/cover", methods=["POST"])
def cover(album_id):
    try:
        generation.generate_album_cover(album_id)
    except (ImageGenError, ValueError) as exc:
        flash(f"Cover generation failed: {exc}", "error")
        return redirect(url_for("albums.detail", album_id=album_id))
    flash("Album cover generated.", "ok")
    return redirect(url_for("albums.detail", album_id=album_id))


@bp.route("/<int:album_id>/publish", methods=["POST"])
def publish(album_id):
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
