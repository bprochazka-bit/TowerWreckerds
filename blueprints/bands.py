"""Bands — generated from scratch or assembled from existing artists."""

from flask import (
    Blueprint, flash, redirect, render_template, request, url_for,
)

from database import execute, now_iso, query
from backends.llm import LLMError
import generation

bp = Blueprint("bands", __name__, url_prefix="/bands")


@bp.route("/")
def list_bands():
    bands = query("SELECT * FROM band ORDER BY id DESC")
    counts = {b["id"]: query(
        "SELECT COUNT(*) c FROM membership WHERE band_id=? AND left_on IS NULL",
        (b["id"],), one=True)["c"] for b in bands}
    return render_template("bands/list.html", bands=bands, counts=counts)


@bp.route("/new")
def new_band():
    genres = query("SELECT name FROM genre ORDER BY name")
    artists = query("SELECT id, name, primary_genre FROM artist ORDER BY name")
    return render_template("bands/new.html", genres=genres, artists=artists)


@bp.route("/generate", methods=["POST"])
def generate_scratch():
    hints = {
        "name": request.form.get("name", ""),
        "primary_genre": request.form.get("primary_genre", ""),
        "vibe": request.form.get("vibe", ""),
        "size": request.form.get("size", "4"),
    }
    try:
        bid = generation.generate_band_from_scratch(hints)
    except LLMError as exc:
        flash(f"Generation failed: {exc}", "error")
        return redirect(url_for("bands.new_band"))
    flash("Band generated with full lineup.", "ok")
    return redirect(url_for("bands.detail", band_id=bid))


@bp.route("/assemble", methods=["POST"])
def assemble():
    artist_ids = request.form.getlist("artist_id")
    if not artist_ids:
        flash("Select at least one existing artist.", "error")
        return redirect(url_for("bands.new_band"))
    member_specs = [
        {"artist_id": int(aid),
         "instrument": request.form.get(f"instrument_{aid}", "")}
        for aid in artist_ids
    ]
    try:
        bid = generation.generate_band_from_members(
            request.form.get("name", ""), request.form.get("primary_genre", ""),
            member_specs)
    except LLMError as exc:
        flash(f"Generation failed: {exc}", "error")
        return redirect(url_for("bands.new_band"))
    flash("Band assembled from existing artists.", "ok")
    return redirect(url_for("bands.detail", band_id=bid))


@bp.route("/<int:band_id>")
def detail(band_id):
    band = query("SELECT * FROM band WHERE id = ?", (band_id,), one=True)
    if not band:
        return redirect(url_for("bands.list_bands"))
    members = query(
        "SELECT a.id, a.name, a.region, m.instrument, m.joined_on, m.left_on"
        " FROM membership m JOIN artist a ON a.id = m.artist_id"
        " WHERE m.band_id = ? ORDER BY m.joined_on", (band_id,))
    releases = query(
        "SELECT * FROM release WHERE owner_type='band' AND owner_id=? ORDER BY id DESC",
        (band_id,))
    return render_template("bands/detail.html", band=band, members=members,
                           releases=releases)


@bp.route("/<int:band_id>/influences", methods=["POST"])
def update_influences(band_id):
    execute("UPDATE band SET influences=? WHERE id=?",
            (request.form.get("influences", "").strip(), band_id))
    flash("Influences saved — applied to this band's renders.", "ok")
    return redirect(url_for("bands.detail", band_id=band_id))


@bp.route("/<int:band_id>/vocal", methods=["POST"])
def update_vocal(band_id):
    v = request.form.get("vocal", "").strip().lower()
    execute("UPDATE band SET vocal=? WHERE id=?",
            (v if v in ("female", "male", "androgynous") else "", band_id))
    flash("Lead vocal saved — applied to this band's renders.", "ok")
    return redirect(url_for("bands.detail", band_id=band_id))


@bp.route("/<int:band_id>/delete", methods=["POST"])
def delete(band_id):
    execute("DELETE FROM band WHERE id = ?", (band_id,))
    flash("Band deleted.", "ok")
    return redirect(url_for("bands.list_bands"))
