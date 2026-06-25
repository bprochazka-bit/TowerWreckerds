"""Music World — Flask application entry point.

Run locally:  python3 app.py   (or:  flask --app app run)
Apt deps only: python3-flask, python3-requests  (sqlite3 is stdlib).
"""

from flask import Flask, render_template

from database import init_db, jload, query
from blueprints.artists import bp as artists_bp
from blueprints.bands import bp as bands_bp
from blueprints.albums import bp as albums_bp
from blueprints.tracks import bp as tracks_bp
from blueprints.published import bp as published_bp
from blueprints.admin import bp as admin_bp
from blueprints.api import bp as api_bp


def create_app():
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False
    init_db()

    # Sessions/flash need a secret key. Prefer an env override; otherwise
    # generate one once and persist it in the settings table so it survives
    # restarts without any manual configuration.
    import os
    import secrets
    from database import get_setting, set_setting
    secret = os.environ.get("MUSIC_WORLD_SECRET") or get_setting("secret_key")
    if not secret:
        secret = secrets.token_hex(32)
        set_setting("secret_key", secret)
    app.secret_key = secret

    app.register_blueprint(artists_bp)
    app.register_blueprint(bands_bp)
    app.register_blueprint(albums_bp)
    app.register_blueprint(tracks_bp)
    app.register_blueprint(published_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)

    # Make json-decoding available inside templates.
    app.jinja_env.filters["jload"] = lambda v: jload(v, [])

    # Audio files always live in static/audio/. Stored paths may be relative
    # ("static/audio/x.wav") or absolute; normalise to a servable URL.
    import os

    def _audio_url(path):
        if not path:
            return None
        return "/static/audio/" + os.path.basename(path)

    app.jinja_env.filters["audio_url"] = _audio_url

    # Cover images live in static/covers/. Normalise a stored path to a URL.
    def _image_url(path):
        if not path:
            return None
        return "/static/covers/" + os.path.basename(path)

    app.jinja_env.filters["image_url"] = _image_url

    @app.route("/")
    def index():
        stats = {
            "artists": query("SELECT COUNT(*) c FROM artist", one=True)["c"],
            "bands": query("SELECT COUNT(*) c FROM band", one=True)["c"],
            "albums": query("SELECT COUNT(*) c FROM release", one=True)["c"],
            "tracks": query("SELECT COUNT(*) c FROM track", one=True)["c"],
            "rendered": query("SELECT COUNT(*) c FROM track WHERE status='rendered'", one=True)["c"],
        }
        recent_tracks = query(
            "SELECT id, title, status, audio_path FROM track ORDER BY id DESC LIMIT 8")
        recent_albums = query(
            "SELECT id, title, type, status FROM release ORDER BY id DESC LIMIT 6")
        return render_template("index.html", stats=stats,
                               recent_tracks=recent_tracks, recent_albums=recent_albums)

    return app


app = create_app()


if __name__ == "__main__":
    import os
    host = os.environ.get("MUSIC_WORLD_HOST", "127.0.0.1")
    port = int(os.environ.get("MUSIC_WORLD_PORT", "5000"))
    # threaded so progress-poll requests are served while a background album
    # render runs in its daemon thread.
    app.run(host=host, port=port, debug=True, threaded=True)
