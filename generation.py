"""Generation orchestration.

Bridges the data model to the two backends. Builds the prompts for each
authoring stage (artist, band, album concept + tracklist, track brief), and
runs the render-N-candidates / pick-best loop against ACE-Step.
"""

import os
import wave

from database import (
    all_settings, execute, jdump, jload, now_iso, query,
)
from backends.llm import LLMClient
from backends.acestep import ACEStepClient
from backends.imagegen import ImageGenClient

ROOT = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(ROOT, "static", "audio")
COVER_DIR = os.path.join(ROOT, "static", "covers")

# Audio extensions we recognise in the reference-music repository.
REFERENCE_AUDIO_EXTS = (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac",
                        ".opus", ".wma", ".aiff", ".aif")


# ---------------------------------------------------------------------------
# Reference material pulled from the DB to ground prompts
# ---------------------------------------------------------------------------

def _genre_names():
    return [r["name"] for r in query("SELECT name FROM genre ORDER BY name")]


def _style_tag_lines():
    rows = query("SELECT name, category FROM style_tag ORDER BY category, name")
    return ", ".join(f"{r['name']} ({r['category']})" for r in rows)


def _tags_to_phrases(tags):
    """Translate StyleTag names into ACE-Step phrases, falling back to the raw name."""
    phrases = []
    for tag in tags:
        row = query("SELECT acestep_phrases FROM style_tag WHERE name = ?", (tag,), one=True)
        if row:
            phrases.extend(jload(row["acestep_phrases"], [tag]))
        else:
            phrases.append(tag)
    # de-dupe, preserve order
    seen, out = set(), []
    for p in phrases:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _refinement_tags(refinement):
    try:
        r = float(refinement)
    except (TypeError, ValueError):
        r = 0.3
    if r < 0.34:
        return ["rough mix", "raw vocals", "garage production", "live room feel"]
    if r < 0.67:
        return ["balanced mix", "semi-polished production"]
    return ["polished studio production", "tight arrangement", "clean professional mix"]


# ---------------------------------------------------------------------------
# Artist
# ---------------------------------------------------------------------------

def generate_artist(hints):
    """hints: dict with optional name, primary_genre, region, vibe."""
    llm = LLMClient()
    genres = ", ".join(_genre_names())
    asked = {k: v for k, v in hints.items() if v}
    system = (
        "You are a music-world worldbuilder. Invent a believable recording artist "
        "with a distinct identity. Keep the persona vivid but grounded."
    )
    user = f"""Create one musical artist as JSON with exactly these keys:
  name, persona, backstory, region, primary_genre, secondary_genres (array of 0-2),
  stage (one of: emerging, rising, established, veteran),
  refinement (0.0-1.0 number reflecting how polished their production is).

Choose primary_genre from this list when possible: {genres}.
region drives accent and language, so pick a real place.
{("Constraints from the user: " + str(asked)) if asked else "No constraints; surprise me."}
"""
    data = llm.generate_json(user, system=system)
    return _persist_artist(data)


def _persist_artist(data, artist_type="solo"):
    aid = execute(
        "INSERT INTO artist (name, type, persona, backstory, region, primary_genre,"
        " secondary_genres, stage, refinement, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            data.get("name", "Untitled Artist"),
            artist_type,
            data.get("persona", ""),
            data.get("backstory", ""),
            data.get("region", ""),
            data.get("primary_genre", ""),
            jdump(data.get("secondary_genres", [])),
            data.get("stage", "emerging"),
            float(data.get("refinement", 0.3) or 0.3),
            now_iso(),
        ),
    )
    return aid


# ---------------------------------------------------------------------------
# Band
# ---------------------------------------------------------------------------

def generate_band_from_scratch(hints):
    """Invent a band and its members from nothing. Returns band_id."""
    llm = LLMClient()
    genres = ", ".join(_genre_names())
    size = int(hints.get("size") or 4)
    asked = {k: v for k, v in hints.items() if v and k != "size"}
    system = "You are a music-world worldbuilder inventing a coherent band and its lineup."
    user = f"""Invent a band as JSON with keys:
  name, primary_genre, backstory,
  members: array of exactly {size} objects, each with keys
     name, persona, region, instrument, refinement (0.0-1.0).
Choose primary_genre from: {genres}.
Members should feel like real people with chemistry and tension.
{("Constraints: " + str(asked)) if asked else ""}
"""
    data = llm.generate_json(user, system=system)
    band_id = execute(
        "INSERT INTO band (name, primary_genre, backstory, formed_on, created_at)"
        " VALUES (?,?,?,?,?)",
        (data.get("name", "Untitled Band"), data.get("primary_genre", ""),
         data.get("backstory", ""), now_iso()[:10], now_iso()),
    )
    for m in data.get("members", []):
        aid = _persist_artist(
            {
                "name": m.get("name", "Member"),
                "persona": m.get("persona", ""),
                "region": m.get("region", ""),
                "primary_genre": data.get("primary_genre", ""),
                "refinement": m.get("refinement", 0.3),
            },
            artist_type="band-member",
        )
        execute(
            "INSERT INTO membership (artist_id, band_id, instrument, joined_on)"
            " VALUES (?,?,?,?)",
            (aid, band_id, m.get("instrument", ""), now_iso()[:10]),
        )
    return band_id


def generate_band_from_members(name_hint, genre_hint, member_specs):
    """Seed a band from existing artists.

    member_specs: list of {artist_id, instrument}. The LLM is asked only to
    name the band and write its backstory, grounded in the real members.
    """
    llm = LLMClient()
    members = []
    for spec in member_specs:
        a = query("SELECT * FROM artist WHERE id = ?", (spec["artist_id"],), one=True)
        if a:
            members.append({
                "name": a["name"], "persona": a["persona"],
                "region": a["region"], "instrument": spec.get("instrument", ""),
            })
    system = "You name and frame a band based on its real, existing members."
    user = f"""These musicians are forming a band:
{members}

Return JSON with keys: name, primary_genre, backstory.
{f'Prefer the name "{name_hint}".' if name_hint else ''}
{f'Genre leans {genre_hint}.' if genre_hint else ''}
The backstory should reference how these specific people came together.
"""
    data = llm.generate_json(user, system=system)
    band_id = execute(
        "INSERT INTO band (name, primary_genre, backstory, formed_on, created_at)"
        " VALUES (?,?,?,?,?)",
        (data.get("name", name_hint or "Untitled Band"),
         data.get("primary_genre", genre_hint or ""),
         data.get("backstory", ""), now_iso()[:10], now_iso()),
    )
    for spec in member_specs:
        execute(
            "INSERT INTO membership (artist_id, band_id, instrument, joined_on)"
            " VALUES (?,?,?,?)",
            (spec["artist_id"], band_id, spec.get("instrument", ""), now_iso()[:10]),
        )
        execute("UPDATE artist SET type = 'band-member' WHERE id = ?", (spec["artist_id"],))
    return band_id


# ---------------------------------------------------------------------------
# Album: concept + tracklist
# ---------------------------------------------------------------------------

def _owner_context(owner_type, owner_id):
    if owner_type == "band":
        b = query("SELECT * FROM band WHERE id = ?", (owner_id,), one=True)
        members = query(
            "SELECT a.name, m.instrument FROM membership m JOIN artist a ON a.id = m.artist_id"
            " WHERE m.band_id = ? AND m.left_on IS NULL", (owner_id,))
        lineup = ", ".join(f"{m['name']} ({m['instrument']})" for m in members)
        return (f"Band: {b['name']} | genre: {b['primary_genre']} | lineup: {lineup}\n"
                f"Backstory: {b['backstory']}"), b["primary_genre"]
    a = query("SELECT * FROM artist WHERE id = ?", (owner_id,), one=True)
    return (f"Artist: {a['name']} | genre: {a['primary_genre']} | region: {a['region']}\n"
            f"Persona: {a['persona']}\nRefinement: {a['refinement']}"), a["primary_genre"]


def generate_album(owner_type, owner_id, ethos, style_tags, rel_type="album", track_count=None):
    llm = LLMClient()
    ctx, genre = _owner_context(owner_type, owner_id)
    default_counts = {"album": 9, "ep": 5, "single": 1}
    n = int(track_count or default_counts.get(rel_type, 9))
    tag_str = ", ".join(style_tags) if style_tags else "(none specified)"
    system = (
        "You are an A&R producer shaping a cohesive release. Build a tracklist with "
        "a real emotional arc, not a random list. Each track has a clear job."
    )
    user = f"""Design a {rel_type} for:
{ctx}

Ethos / concept brief from the user: {ethos or "(none given — derive from the artist)"}
Requested style tags: {tag_str}
Primary genre: {genre}

Return JSON with keys:
  title, concept, inspiration,
  tracks: array of exactly {n} objects, each with keys:
     position (int, 1-based),
     title,
     role (one of: opener, single, ballad, experimental, interlude, closer),
     subject (what the song is about),
     summary (2-3 sentences: the feel, what happens musically and lyrically),
     style_cues (array of short production/style descriptors),
     tempo (bpm int),
     mood,
     length_seconds (int, 90-300).
Sequence the roles sensibly (opener first, closer last).
"""
    data = llm.generate_json(user, system=system)
    rid = execute(
        "INSERT INTO release (owner_type, owner_id, type, title, concept, inspiration,"
        " ethos, style_tags, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (owner_type, owner_id, rel_type, data.get("title", "Untitled"),
         data.get("concept", ""), data.get("inspiration", ""), ethos or "",
         jdump(style_tags), "tracklist", now_iso()),
    )
    for t in data.get("tracks", []):
        cues = t.get("style_cues", [])
        execute(
            "INSERT INTO track (release_id, position, role, title, subject, summary,"
            " style_tags, tempo, mood, duration, status, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, t.get("position", 1), t.get("role", ""), t.get("title", "Untitled"),
             t.get("subject", ""), t.get("summary", ""), jdump(cues),
             t.get("tempo"), t.get("mood", ""), t.get("length_seconds", 180),
             "briefed", "album", now_iso()),
        )
    return rid


# ---------------------------------------------------------------------------
# Track: brief (lyrics + final tags) then render
# ---------------------------------------------------------------------------

def brief_track_from_album(track_id):
    """Expand an album tracklist row into a full brief with lyrics + tags."""
    llm = LLMClient()
    t = query("SELECT * FROM track WHERE id = ?", (track_id,), one=True)
    rel = query("SELECT * FROM release WHERE id = ?", (t["release_id"],), one=True)
    ctx, genre = _owner_context(rel["owner_type"], rel["owner_id"])
    refinement = _owner_refinement(rel["owner_type"], rel["owner_id"])
    system = "You are a songwriter and producer turning a track concept into a recordable brief."
    user = f"""{ctx}

Album: {rel['title']} — {rel['concept']}
Track {t['position']}: "{t['title']}" (role: {t['role']})
Subject: {t['subject']}
Summary so far: {t['summary']}
Existing style cues: {jload(t['style_tags'])}
Genre: {genre}

Return JSON with keys:
  lyrics (full lyrics with [verse]/[chorus] section tags suitable for a singer),
  style_tags (array of concise production/style descriptors),
  tempo (bpm int), key (musical key), mood,
  environmentals (array, e.g. room, reverb, tape, vinyl crackle),
  duration_seconds (int).
Lyrics must fit the subject and the artist's voice.
"""
    data = llm.generate_json(user, system=system)
    base_cues = jload(t["style_tags"], [])
    final_tags = base_cues + data.get("style_tags", []) + _refinement_tags(refinement)
    execute(
        "UPDATE track SET lyrics=?, style_tags=?, tempo=?, song_key=?, mood=?,"
        " environmentals=?, duration=?, status='briefed' WHERE id=?",
        (data.get("lyrics", ""), jdump(final_tags), data.get("tempo", t["tempo"]),
         data.get("key", ""), data.get("mood", t["mood"]),
         jdump(data.get("environmentals", [])),
         data.get("duration_seconds", t["duration"]), track_id),
    )
    return track_id


def brief_album(album_id, progress=None, cancel=None):
    """Write lyrics + full brief for every album track that doesn't have lyrics
    yet. Already-briefed tracks (with lyrics) are left untouched so manual edits
    aren't clobbered. Returns {'briefed','skipped','failed','cancelled','errors'}.

    `progress(event, **data)` fires ("track_start"/"track_done") per track;
    `cancel()` is polled between tracks to stop early.
    """
    tracks = query(
        "SELECT id, lyrics FROM track WHERE release_id = ? ORDER BY position",
        (album_id,))
    to_brief = [t for t in tracks if not (t["lyrics"] or "").strip()]
    res = {"briefed": 0, "skipped": len(tracks) - len(to_brief),
           "failed": 0, "cancelled": 0, "errors": []}
    for idx, t in enumerate(to_brief):
        if cancel and cancel():
            res["cancelled"] = len(to_brief) - idx
            break
        if progress:
            try:
                progress("track_start", track_id=t["id"])
            except Exception:
                pass
        try:
            brief_track_from_album(t["id"])
            res["briefed"] += 1
            status = "briefed"
        except Exception as exc:  # one bad track shouldn't abort the batch
            res["failed"] += 1
            res["errors"].append(f"track {t['id']}: {exc}")
            status = "failed"
        if progress:
            try:
                progress("track_done", track_id=t["id"], status=status)
            except Exception:
                pass
    return res


def render_album(album_id, progress=None, cancel=None):
    """Render every album track that has a brief (lyrics) but isn't rendered yet.
    Tracks already rendered are skipped; tracks without lyrics are skipped with a
    note. Returns {'rendered','skipped','failed','cancelled','errors'}.

    `progress(event, **data)` fires ("track_start"/"candidate"/"track_done");
    `cancel()` is polled between tracks (and between candidates) to stop early.
    """
    tracks = query(
        "SELECT id, lyrics, audio_path FROM track WHERE release_id = ? ORDER BY position",
        (album_id,))
    to_render = [t for t in tracks
                 if not t["audio_path"] and (t["lyrics"] or "").strip()]
    res = {"rendered": 0, "skipped": 0, "failed": 0, "cancelled": 0, "errors": []}
    for t in tracks:
        if t["audio_path"] or not (t["lyrics"] or "").strip():
            res["skipped"] += 1
            if not (t["lyrics"] or "").strip() and not t["audio_path"]:
                res["errors"].append(f"track {t['id']}: no brief/lyrics yet")

    for idx, t in enumerate(to_render):
        if cancel and cancel():
            res["cancelled"] = len(to_render) - idx
            break
        if progress:
            try:
                progress("track_start", track_id=t["id"], index=idx, total=len(to_render))
            except Exception:
                pass

        def cand_cb(event, **data):
            if progress:
                progress(event, track_id=t["id"], **data)

        try:
            result = render_track(t["id"], progress=cand_cb, cancel=cancel)
            if result.get("ok"):
                res["rendered"] += 1
                status, integrity = "rendered", result.get("integrity")
            elif result.get("cancelled"):
                res["cancelled"] += 1
                status, integrity = "pending", None
            else:
                res["failed"] += 1
                res["errors"].append(f"track {t['id']}: {result.get('reason')}")
                status, integrity = "failed", None
        except Exception as exc:
            res["failed"] += 1
            res["errors"].append(f"track {t['id']}: {exc}")
            status, integrity = "failed", None
        if progress:
            try:
                progress("track_done", track_id=t["id"], status=status, integrity=integrity)
            except Exception:
                pass
    return res


def create_freeform_track(prompt, owner_type=None, owner_id=None):
    """Build a standalone track from a free-text prompt."""
    llm = LLMClient()
    ctx = ""
    refinement = 0.5
    if owner_type and owner_id:
        ctx, _ = _owner_context(owner_type, owner_id)
        refinement = _owner_refinement(owner_type, owner_id)
    system = "You turn a loose idea into a complete, recordable song brief."
    ctx_block = f"Performer context:\n{ctx}\n" if ctx else ""
    user = f"""{ctx_block}User prompt: {prompt}

Return JSON with keys:
  title, subject, summary,
  lyrics (with [verse]/[chorus] tags),
  style_tags (array), tempo (bpm int), key, mood,
  environmentals (array), duration_seconds (int).
"""
    data = llm.generate_json(user, system=system)
    final_tags = data.get("style_tags", []) + _refinement_tags(refinement)
    tid = execute(
        "INSERT INTO track (position, role, title, subject, summary, lyrics, style_tags,"
        " tempo, song_key, mood, environmentals, duration, status, source, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "single", data.get("title", "Untitled"), data.get("subject", ""),
         data.get("summary", ""), data.get("lyrics", ""), jdump(final_tags),
         data.get("tempo"), data.get("key", ""), data.get("mood", ""),
         jdump(data.get("environmentals", [])), data.get("duration_seconds", 180),
         "briefed", "freeform", now_iso()),
    )
    return tid


def _owner_refinement(owner_type, owner_id):
    if owner_type == "artist":
        a = query("SELECT refinement FROM artist WHERE id = ?", (owner_id,), one=True)
        return a["refinement"] if a else 0.5
    members = query(
        "SELECT a.refinement FROM membership m JOIN artist a ON a.id = m.artist_id"
        " WHERE m.band_id = ?", (owner_id,))
    vals = [m["refinement"] for m in members if m["refinement"] is not None]
    return sum(vals) / len(vals) if vals else 0.5


def _owner_name_genre(owner_type, owner_id):
    if owner_type == "band":
        b = query("SELECT name, primary_genre FROM band WHERE id = ?", (owner_id,), one=True)
        return (b["name"], b["primary_genre"]) if b else ("Unknown Artist", "")
    a = query("SELECT name, primary_genre FROM artist WHERE id = ?", (owner_id,), one=True)
    return (a["name"], a["primary_genre"]) if a else ("Unknown Artist", "")


# ---------------------------------------------------------------------------
# Reference music repository / cover songs
# ---------------------------------------------------------------------------

def list_reference_music():
    """Enumerate audio files under the configured reference-music repository.

    Returns a list of {"rel": <path relative to the repo>, "name": <filename>}.
    Empty if no path is configured or the folder doesn't exist.
    """
    root = (all_settings().get("reference_music_path") or "").strip()
    if not root or not os.path.isdir(root):
        return []
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(REFERENCE_AUDIO_EXTS):
                full = os.path.join(dirpath, f)
                out.append({"rel": os.path.relpath(full, root), "name": f})
        if len(out) > 2000:  # keep the picker bounded on huge libraries
            break
    return sorted(out, key=lambda r: r["rel"].lower())


def reference_music_abspath(rel):
    """Resolve a reference-library-relative path to an absolute path, guarding
    against escaping the configured repository."""
    root = (all_settings().get("reference_music_path") or "").strip()
    if not root or not rel:
        return None
    full = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([os.path.abspath(root), os.path.abspath(full)]) != os.path.abspath(root):
        return None
    return full if os.path.exists(full) else None


def create_cover_track(reference_rel, owner_type=None, owner_id=None, notes=""):
    """Write a cover/reinterpretation of a reference track in a performer's style.

    The reference is grounded by its title (filename); the performer's persona
    and genre steer the reimagining. The reference path is stored for provenance
    and shown on the track page.
    """
    llm = LLMClient()
    ref_title = os.path.splitext(os.path.basename(reference_rel))[0]
    ctx = ""
    refinement = 0.5
    if owner_type and owner_id:
        ctx, _ = _owner_context(owner_type, owner_id)
        refinement = _owner_refinement(owner_type, owner_id)
    system = ("You reinterpret an existing song as a cover, recast in a new "
              "performer's voice and style.")
    ctx_block = f"Performer context:\n{ctx}\n" if ctx else ""
    user = f"""{ctx_block}Original song to cover: "{ref_title}"
{('Direction from the user: ' + notes) if notes else ''}

Reimagine this as a cover. Return JSON with keys:
  title (keep or lightly adapt the original title),
  subject, summary (how this cover reinterprets the original),
  lyrics (with [verse]/[chorus] tags; write fresh lyrics fitting the title/theme),
  style_tags (array), tempo (bpm int), key, mood,
  environmentals (array), duration_seconds (int).
"""
    data = llm.generate_json(user, system=system)
    final_tags = data.get("style_tags", []) + _refinement_tags(refinement)
    tid = execute(
        "INSERT INTO track (position, role, title, subject, summary, lyrics, style_tags,"
        " tempo, song_key, mood, environmentals, duration, reference_audio,"
        " status, source, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "cover", data.get("title", ref_title), data.get("subject", ""),
         data.get("summary", ""), data.get("lyrics", ""), jdump(final_tags),
         data.get("tempo"), data.get("key", ""), data.get("mood", ""),
         jdump(data.get("environmentals", [])), data.get("duration_seconds", 180),
         reference_rel, "briefed", "cover", now_iso()),
    )
    return tid


# ---------------------------------------------------------------------------
# Cover art (sd.cpp)
# ---------------------------------------------------------------------------

def _art_prompt(subject, descriptor, genre, tags, concept=""):
    bits = [subject, descriptor]
    if genre:
        bits.append(f"{genre} aesthetic")
    if concept:
        bits.append(concept)
    if tags:
        bits.append("style: " + ", ".join(tags[:6]))
    bits.append("highly detailed, professional artwork, dramatic lighting, no text, no watermark")
    return ", ".join(b for b in bits if b)


def generate_album_cover(album_id):
    """Render an album-cover image via sd.cpp (or a placeholder in mock mode).
    Stores the relative path on the release and returns it."""
    album = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    if not album:
        raise ValueError("album not found")
    owner_name, genre = _owner_name_genre(album["owner_type"], album["owner_id"])
    tags = jload(album["style_tags"], [])
    prompt = _art_prompt(
        f'album cover art for "{album["title"]}" by {owner_name}',
        f'a {album["type"]} release', genre, tags, album["concept"] or "")
    os.makedirs(COVER_DIR, exist_ok=True)
    out = os.path.join(COVER_DIR, f"album{album_id}.png")
    ImageGenClient().generate(prompt, out, seed=album_id * 7 + 13)
    rel = os.path.relpath(out, ROOT)
    execute("UPDATE release SET cover_path=? WHERE id=?", (rel, album_id))
    return rel


def generate_track_cover(track_id):
    """Render single/standalone cover art for a track. Returns the relative path."""
    t = query("SELECT * FROM track WHERE id = ?", (track_id,), one=True)
    if not t:
        raise ValueError("track not found")
    tags = jload(t["style_tags"], [])
    prompt = _art_prompt(
        f'single cover art for "{t["title"]}"',
        f'a {t["mood"] or "moody"} song', "", tags, t["subject"] or "")
    os.makedirs(COVER_DIR, exist_ok=True)
    out = os.path.join(COVER_DIR, f"track{track_id}.png")
    ImageGenClient().generate(prompt, out, seed=track_id * 5 + 3)
    return os.path.relpath(out, ROOT)


# ---------------------------------------------------------------------------
# Render: N candidates -> integrity check -> select best
# ---------------------------------------------------------------------------

def render_track(track_id, progress=None, cancel=None):
    """Render N candidates and keep the best.

    `progress`, if given, is called as `progress(event, **data)`:
      - ("candidate", index=i, total=n, score=float, note=str) per candidate
    `cancel`, if given, is polled between candidates; when it returns True the
    candidate loop stops early and the best take rendered so far (if any) is kept.
    """
    settings = all_settings()
    ace = ACEStepClient(settings)
    t = query("SELECT * FROM track WHERE id = ?", (track_id,), one=True)
    if not t:
        raise ValueError("track not found")

    tags = ", ".join(_tags_to_phrases(jload(t["style_tags"], [])))
    env = ", ".join(jload(t["environmentals"], []))
    if env:
        tags = f"{tags}, {env}" if tags else env
    lyrics = t["lyrics"] or ""
    duration = t["duration"] or 180

    try:
        n = int(settings.get("acestep_candidates", 3))
    except (TypeError, ValueError):
        n = 3
    n = max(1, n)

    execute("UPDATE track SET status='producing' WHERE id=?", (track_id,))
    execute("DELETE FROM candidate WHERE track_id=?", (track_id,))

    # If this track is a cover of a reference recording, resolve the source
    # audio so the render runs as acestep.cpp audio2audio ("cover" task).
    ref_abs = reference_music_abspath(t["reference_audio"]) if t["reference_audio"] else None
    cover_strength = settings.get("acestep_cover_strength")
    cover_noise = settings.get("acestep_cover_noise")

    base_seed = t["seed"] or (track_id * 1000)
    best = None
    for i in range(n):
        if cancel and cancel():
            break
        seed = base_seed + i
        out_path = os.path.join(AUDIO_DIR, f"track{track_id}_cand{i}.{ace.fmt}")
        try:
            ace.generate(tags, lyrics, duration, seed, out_path,
                         reference_audio=ref_abs, cover_strength=cover_strength,
                         cover_noise=cover_noise)
            score, note = integrity_score(out_path, duration)
        except Exception as exc:  # keep going; a bad candidate shouldn't kill the batch
            score, note = 0.0, f"render error: {exc}"
            out_path = None
        cid = execute(
            "INSERT INTO candidate (track_id, seed, integrity, audio_path, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (track_id, seed, score, out_path, note, now_iso()),
        )
        if out_path and (best is None or score > best[1]):
            best = (cid, score, out_path, seed)
        if progress:
            try:
                progress("candidate", index=i, total=n, score=score, note=note)
            except Exception:
                pass  # progress reporting must never break a render

    if best is None:
        if cancel and cancel():
            # Cancelled before any candidate finished — leave the track briefed
            # so it can be retried rather than marking it failed.
            execute("UPDATE track SET status='briefed' WHERE id=?", (track_id,))
            return {"ok": False, "cancelled": True, "reason": "cancelled"}
        execute("UPDATE track SET status='failed' WHERE id=?", (track_id,))
        return {"ok": False, "reason": "all candidates failed"}

    cid, score, path, seed = best
    execute("UPDATE candidate SET selected=1 WHERE id=?", (cid,))

    # Trim trailing white noise / silence off the chosen master (best take only,
    # so the QA length scores above were computed on the untrimmed candidates).
    if str(settings.get("acestep_trim_noise", "1")) in ("1", "true", "True", "on"):
        try:
            trim_trailing_noise(path)
        except Exception:
            pass  # never let trimming fail a render

    rel = os.path.relpath(path, os.path.dirname(os.path.abspath(__file__)))
    execute("UPDATE track SET audio_path=?, seed=?, status='rendered' WHERE id=?",
            (rel, seed, track_id))
    return {"ok": True, "candidate_id": cid, "integrity": score, "audio_path": rel}


def integrity_score(path, intended_duration):
    """Cheap signal QA: file present, non-empty, plausible length, not dead air.

    Returns (score 0-1, note). WAV inspected via stdlib; other formats get a
    presence/size check only.
    """
    if not path or not os.path.exists(path):
        return 0.0, "missing file"
    size = os.path.getsize(path)
    if size < 1024:
        return 0.0, "empty/truncated"
    if not path.lower().endswith(".wav"):
        return 0.7, "non-wav: size check only"
    try:
        with wave.open(path, "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate() or 1
            dur = frames / rate
            peak = _peak_amplitude(wf)
    except (wave.Error, EOFError) as exc:
        return 0.0, f"unreadable wav: {exc}"

    score, notes = 1.0, []
    if peak < 500:
        score -= 0.6
        notes.append("near-silent")
    if intended_duration:
        ratio = dur / intended_duration
        if ratio < 0.5 or ratio > 1.8:
            score -= 0.3
            notes.append(f"length off ({dur:.0f}s vs {intended_duration}s)")
    return max(0.0, score), "; ".join(notes) or "ok"


def _peak_amplitude(wf, max_frames=220500):
    """Peak sample magnitude using stdlib `array` (audioop is gone in 3.13)."""
    import array
    pos = wf.tell()
    raw = wf.readframes(min(wf.getnframes(), max_frames))
    wf.setpos(pos)
    width = wf.getsampwidth()
    try:
        if width == 2:
            samples = array.array("h")
            samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
            return max((abs(s) for s in samples), default=0)
        if width == 1:
            # 8-bit WAV is unsigned, centered on 128
            return max((abs(b - 128) * 256 for b in raw), default=0)
    except Exception:
        pass
    return 1000  # don't penalize formats we can't cheaply measure


# ---------------------------------------------------------------------------
# Trailing-noise trim
#
# ACE-Step text2music often pads the tail of a render with white noise. It is
# not silence (amplitude is high), so it's detected by a high zero-crossing
# rate together with a low crest factor (peak/RMS); near-silence is detected by
# low RMS. Musical endings (tonal, transient-rich, or fading) survive untouched.
# Pure stdlib so the deploy stays apt-only.
# ---------------------------------------------------------------------------

def trim_trailing_noise(path, frame=4096, hop=2048, fade_ms=15,
                        min_keep_ratio=0.35, min_trim_sec=0.30,
                        zcr_thresh=0.28, crest_thresh=4.5, silence_rms=180,
                        analyze_sec=60):
    """Trim a trailing run of white noise / silence from a 16-bit WAV in place.
    Returns (trimmed: bool, removed_seconds: float)."""
    import array
    import math

    if not path or not path.lower().endswith(".wav") or not os.path.exists(path):
        return False, 0.0
    with wave.open(path, "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        fr = wf.getframerate() or 44100
        n = wf.getnframes()
        raw = wf.readframes(n)
    if sw != 2 or n == 0:
        return False, 0.0
    data = array.array("h")
    data.frombytes(raw[: n * nch * 2])
    total = n
    if total < fr // 2:
        return False, 0.0

    def mono(i):
        base = i * nch
        if nch == 1:
            return data[base]
        s = 0
        for c in range(nch):
            s += data[base + c]
        return s // nch

    def removable(start):
        end = min(start + frame, total)
        m = end - start
        if m < 8:
            return True
        sq = 0
        peak = 0
        zc = 0
        prev = None
        for i in range(start, end):
            s = mono(i)
            sq += s * s
            a = s if s >= 0 else -s
            if a > peak:
                peak = a
            sign = s >= 0
            if prev is not None and sign != prev:
                zc += 1
            prev = sign
        rms = math.sqrt(sq / m)
        if rms < silence_rms:
            return True
        zcr = zc / (m - 1)
        crest = peak / rms if rms > 0 else 99.0
        return zcr >= zcr_thresh and crest <= crest_thresh

    floor = max(0, total - fr * analyze_sec)
    cut = total
    pos = total - frame
    while pos >= floor:
        if removable(pos):
            cut = pos
            pos -= hop
        else:
            break
    if cut >= total:
        return False, 0.0
    removed = (total - cut) / fr
    if removed < min_trim_sec or (cut / total) < min_keep_ratio:
        return False, 0.0

    out = data[: cut * nch]
    fade = min(int(fr * fade_ms / 1000), cut)
    for i in range(fade):
        g = (fade - 1 - i) / fade
        idx = (cut - fade + i) * nch
        for c in range(nch):
            out[idx + c] = int(out[idx + c] * g)

    with wave.open(path, "wb") as wf:
        wf.setnchannels(nch)
        wf.setsampwidth(2)
        wf.setframerate(fr)
        wf.writeframes(out.tobytes())
    return True, removed
