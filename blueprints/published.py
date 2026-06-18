"""Published — browse only the tracks that have been exported to MP3."""

from flask import Blueprint, render_template

from database import query

bp = Blueprint("published", __name__, url_prefix="/published")


@bp.route("/")
def index():
    rows = query(
        "SELECT t.id, t.title, t.position, t.published_path, t.source,"
        " r.id AS rid, r.title AS album, r.type AS rtype, r.cover_path,"
        " r.owner_type, r.owner_id"
        " FROM track t LEFT JOIN release r ON r.id = t.release_id"
        " WHERE t.published_path IS NOT NULL AND t.published_path != ''"
        " ORDER BY (r.id IS NULL), r.id DESC, t.position")
    groups, by_key = [], {}
    for row in rows:
        key = row["rid"]
        if key not in by_key:
            owner = None
            if row["owner_type"]:
                table = "band" if row["owner_type"] == "band" else "artist"
                o = query(f"SELECT name FROM {table} WHERE id = ?", (row["owner_id"],), one=True)
                owner = o["name"] if o else None
            g = {"rid": key, "album": row["album"], "type": row["rtype"],
                 "cover_path": row["cover_path"], "owner": owner, "tracks": []}
            by_key[key] = g
            groups.append(g)
        by_key[key]["tracks"].append(row)
    return render_template("published/index.html", groups=groups)
