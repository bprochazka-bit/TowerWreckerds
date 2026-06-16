"""Publishing — export rendered tracks and albums as tagged MP3 files.

Produces a clean, portable library on disk:

    <publish_path>/<Artist or Band>/<Album Title>/NN - Track Title.mp3
    <publish_path>/<Artist or Band>/<Album Title>/cover.png
    <publish_path>/<Owner or "Singles">/Track Title.mp3        (standalone)

MP3 encoding shells out to ffmpeg (preferred) or lame; the app itself stays
apt-only. ID3v2.3 tags — including the embedded front-cover image — are written
by hand with the standard library, so tagging needs no extra packages. If no
encoder is installed the master audio is copied verbatim (e.g. as .wav) and the
caller is told encoding was skipped, rather than failing the publish outright.
"""

import os
import re
import shutil
import struct
import subprocess

from database import all_settings, execute, jload, query
import generation

ROOT = os.path.dirname(os.path.abspath(__file__))


class PublishError(RuntimeError):
    pass


# --- paths / naming ---------------------------------------------------------

def _publish_root():
    p = (all_settings().get("publish_path") or "").strip()
    return p or os.path.join(ROOT, "published")


def _sanitize(name, fallback="Untitled"):
    """Make a string safe to use as a single path segment."""
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = name.rstrip(". ").strip()
    return name[:120] or fallback


def _abs_audio(stored_path):
    """Resolve a stored audio_path (relative to the app root, or absolute)."""
    if not stored_path:
        return None
    return stored_path if os.path.isabs(stored_path) else os.path.join(ROOT, stored_path)


def _year(*candidates):
    for c in candidates:
        if c:
            m = re.search(r"(\d{4})", str(c))
            if m:
                return m.group(1)
    return ""


# --- MP3 encoding -----------------------------------------------------------

def _encode_mp3(src, dst):
    """Write a tagless MP3 at dst from src (wav or mp3). Returns True on success.

    Prefers ffmpeg, falls back to lame for WAV input; if neither is present but
    the source is already an MP3, copies its frames as-is.
    """
    ext = src.lower().rsplit(".", 1)[-1]
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        cmd = [ffmpeg, "-y", "-i", src, "-map_metadata", "-1", "-vn",
               "-codec:a", "libmp3lame", "-qscale:a", "2", dst]
        try:
            p = subprocess.run(cmd, capture_output=True, timeout=900)
            if p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 128:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    lame = shutil.which("lame")
    if lame and ext == "wav":
        try:
            p = subprocess.run([lame, "-V2", "--silent", src, dst],
                               capture_output=True, timeout=900)
            if p.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 128:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    if ext == "mp3":
        shutil.copyfile(src, dst)
        return True
    return False


# --- ID3v2.3 tagging (stdlib) -----------------------------------------------

def _synchsafe(n):
    return bytes([(n >> 21) & 0x7f, (n >> 14) & 0x7f, (n >> 7) & 0x7f, n & 0x7f])


def _text_frame(frame_id, text):
    if not text:
        return b""
    # Encoding 0x01 = UTF-16 with BOM (Python's "utf-16" prepends the BOM).
    data = b"\x01" + str(text).encode("utf-16") + b"\x00\x00"
    return frame_id + struct.pack(">I", len(data)) + b"\x00\x00" + data


def _apic_frame(image_bytes, mime="image/png"):
    if not image_bytes:
        return b""
    data = (b"\x00"                              # text encoding for the strings below
            + mime.encode("latin-1") + b"\x00"   # MIME type, terminated
            + b"\x03"                            # picture type: front cover
            + b"\x00"                            # empty description, terminated
            + image_bytes)
    return b"APIC" + struct.pack(">I", len(data)) + b"\x00\x00" + data


def _strip_leading_id3(raw):
    """Drop a leading ID3v2 tag if present, so we don't stack two tags."""
    if raw[:3] == b"ID3" and len(raw) >= 10:
        size = ((raw[6] & 0x7f) << 21 | (raw[7] & 0x7f) << 14
                | (raw[8] & 0x7f) << 7 | (raw[9] & 0x7f))
        return raw[10 + size:]
    return raw


def _id3v2_tag(title, artist, album, track_no, genre, year, cover_bytes, cover_mime):
    frames = b"".join([
        _text_frame(b"TIT2", title),
        _text_frame(b"TPE1", artist),
        _text_frame(b"TALB", album),
        _text_frame(b"TRCK", str(track_no) if track_no else ""),
        _text_frame(b"TCON", genre),
        _text_frame(b"TYER", year),
        _apic_frame(cover_bytes, cover_mime),
    ])
    return b"ID3" + b"\x03\x00" + b"\x00" + _synchsafe(len(frames)) + frames


def _write_tagged_mp3(src, dst, *, title, artist, album, track_no, genre, year,
                      cover_bytes, cover_mime):
    """Encode src -> dst as MP3 and prepend our ID3v2 tag.

    Returns (final_path, warning_or_None). On encoder absence with a non-MP3
    source, copies the source verbatim (keeping its extension) and warns.
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    if _encode_mp3(src, tmp):
        with open(tmp, "rb") as fh:
            body = _strip_leading_id3(fh.read())
        os.remove(tmp)
        tag = _id3v2_tag(title, artist, album, track_no, genre, year,
                         cover_bytes, cover_mime)
        with open(dst, "wb") as fh:
            fh.write(tag + body)
        return dst, None
    # No encoder available and source isn't MP3 — copy verbatim, no ID3.
    fallback = os.path.splitext(dst)[0] + os.path.splitext(src)[1]
    shutil.copyfile(src, fallback)
    return fallback, ("no MP3 encoder (ffmpeg/lame) found — copied the master "
                      "audio verbatim instead of transcoding")


# --- cover resolution -------------------------------------------------------

def _cover_for_release(album):
    """Absolute path to a release's cover image, generating one if missing."""
    cover = album["cover_path"] if "cover_path" in album.keys() else None
    if cover and os.path.exists(_abs_audio(cover)):
        return _abs_audio(cover)
    rel = generation.generate_album_cover(album["id"])
    return _abs_audio(rel)


def _read_image(path):
    if not path or not os.path.exists(path):
        return None, None
    with open(path, "rb") as fh:
        data = fh.read()
    mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
    return data, mime


# --- public API -------------------------------------------------------------

def publish_track(track_id, *, album=None, dest_dir=None, cover_path=None,
                  owner_override=None):
    """Publish a single rendered track. Returns a dict describing the result.

    When called for an album track, the caller passes the shared album, dest_dir
    and cover so every track lands in one folder with one cover.
    """
    t = query("SELECT * FROM track WHERE id = ?", (track_id,), one=True)
    if not t:
        raise PublishError("track not found")
    src = _abs_audio(t["audio_path"])
    if not src or not os.path.exists(src):
        raise PublishError("track has no rendered audio to publish")

    release = album
    if release is None and t["release_id"]:
        release = query("SELECT * FROM release WHERE id = ?", (t["release_id"],), one=True)

    # Owner / artist name + genre.
    if owner_override:
        artist_name, genre = owner_override
    elif release:
        artist_name, genre = generation._owner_name_genre(
            release["owner_type"], release["owner_id"])
    else:
        artist_name, genre = "Unknown Artist", ""

    album_title = release["title"] if release else ""
    year = _year(release["release_date"] if release else None,
                 release["created_at"] if release else None, t["created_at"])

    # Folder + filename.
    if dest_dir is None:
        base = _publish_root()
        if release:
            dest_dir = os.path.join(base, _sanitize(artist_name),
                                    _sanitize(album_title, "Album"))
        else:
            dest_dir = os.path.join(base, _sanitize(artist_name, "Singles"))
    if release:
        filename = f"{int(t['position'] or 1):02d} - {_sanitize(t['title'], 'Track')}.mp3"
    else:
        filename = f"{_sanitize(t['title'], 'Track')}.mp3"
    dst = os.path.join(dest_dir, filename)

    # Cover art.
    if cover_path is None:
        if release:
            cover_path = _cover_for_release(release)
        else:
            cover_path = _abs_audio(generation.generate_track_cover(track_id))
    cover_bytes, cover_mime = _read_image(cover_path)

    final, warning = _write_tagged_mp3(
        src, dst, title=t["title"] or "Untitled", artist=artist_name,
        album=album_title, track_no=(t["position"] if release else None),
        genre=genre, year=year, cover_bytes=cover_bytes, cover_mime=cover_mime)

    rel = os.path.relpath(final, ROOT)
    execute("UPDATE track SET status='published', published_path=? WHERE id=?",
            (rel, track_id))
    return {"ok": True, "path": final, "dir": dest_dir, "warning": warning,
            "cover": cover_path}


def publish_album(album_id):
    """Publish every rendered track of an album into a single folder, with one
    shared cover written alongside as cover.png. Returns a summary dict."""
    album = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    if not album:
        raise PublishError("album not found")
    tracks = query(
        "SELECT * FROM track WHERE release_id = ? AND audio_path IS NOT NULL"
        " ORDER BY position", (album_id,))
    if not tracks:
        raise PublishError("no rendered tracks to publish")

    artist_name, genre = generation._owner_name_genre(
        album["owner_type"], album["owner_id"])
    dest_dir = os.path.join(_publish_root(), _sanitize(artist_name),
                            _sanitize(album["title"], "Album"))
    os.makedirs(dest_dir, exist_ok=True)

    cover_path = _cover_for_release(album)
    if cover_path and os.path.exists(cover_path):
        shutil.copyfile(cover_path, os.path.join(dest_dir, "cover.png"))

    published, warnings = [], set()
    for t in tracks:
        res = publish_track(t["id"], album=album, dest_dir=dest_dir,
                            cover_path=cover_path,
                            owner_override=(artist_name, genre))
        published.append(res["path"])
        if res["warning"]:
            warnings.add(res["warning"])

    execute("UPDATE release SET status='published' WHERE id=?", (album_id,))
    return {"ok": True, "count": len(published), "dir": dest_dir,
            "warning": "; ".join(sorted(warnings)) or None}
