"""SQLite data layer for the Music World application.

Uses only the Python standard library (sqlite3). The schema follows the
system design reference: artists, bands, memberships, genres, style tags,
releases (albums/EPs/singles), tracks, and candidates, plus a settings
table that backs the admin configuration screen.
"""

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get(
    "MUSICWORLD_DB",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "music_world.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS genre (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    descriptors TEXT,            -- json list
    typical_instruments TEXT,    -- json list
    tempo_range TEXT,
    common_regions TEXT,         -- json list
    base_style_tags TEXT,        -- json list
    lyric_guidance TEXT          -- how lyrics in this genre should read
);

CREATE TABLE IF NOT EXISTS style_tag (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    category TEXT,               -- mood | instrumentation | era | production | crossover | vocal
    acestep_phrases TEXT         -- json list
);

CREATE TABLE IF NOT EXISTS artist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'solo',    -- solo | band-member
    persona TEXT,
    backstory TEXT,
    region TEXT,
    primary_genre TEXT,
    secondary_genres TEXT,       -- json list
    consistency_mode TEXT DEFAULT 'prompt_only',  -- lora | prompt_only
    lora_ref TEXT,
    voice_seed INTEGER,
    stage TEXT DEFAULT 'emerging',
    refinement REAL DEFAULT 0.3,
    deviation REAL DEFAULT 0.2,
    popularity REAL DEFAULT 0.1,
    momentum REAL DEFAULT 0.0,
    influences TEXT,             -- "sounds like" reference artists/bands
    vocal TEXT,                  -- lead vocal: '' | female | male | androgynous
    portrait_path TEXT,          -- generated portrait image (relative path)
    portrait_prompt TEXT,        -- last prompt used/edited for the portrait
    status TEXT DEFAULT 'active',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS band (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    primary_genre TEXT,
    backstory TEXT,
    formed_on TEXT,
    influences TEXT,             -- "sounds like" reference artists/bands
    vocal TEXT,                  -- lead vocal override: '' (derive from members) | female | male | androgynous
    portrait_path TEXT,          -- generated band photo (relative path)
    portrait_prompt TEXT,        -- last prompt used/edited for the band photo
    status TEXT DEFAULT 'active',
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS membership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL,
    band_id INTEGER NOT NULL,
    instrument TEXT,
    joined_on TEXT,
    left_on TEXT,
    FOREIGN KEY (artist_id) REFERENCES artist(id) ON DELETE CASCADE,
    FOREIGN KEY (band_id) REFERENCES band(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS release (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL,    -- artist | band
    owner_id INTEGER NOT NULL,
    type TEXT DEFAULT 'album',   -- album | ep | single
    title TEXT,
    concept TEXT,
    inspiration TEXT,
    ethos TEXT,
    style_tags TEXT,             -- json list
    release_date TEXT,
    cover_path TEXT,             -- generated album cover image (relative path)
    cover_prompt TEXT,           -- last prompt used/edited for the cover image
    status TEXT DEFAULT 'draft', -- draft | tracklist | producing | published
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS track (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER,
    position INTEGER DEFAULT 1,
    role TEXT,                   -- opener | single | ballad | experimental | closer
    title TEXT,
    subject TEXT,
    summary TEXT,
    lyrics TEXT,
    style_tags TEXT,             -- json list
    tempo INTEGER,
    song_key TEXT,
    mood TEXT,
    environmentals TEXT,         -- json list
    duration INTEGER DEFAULT 180,
    seed INTEGER,
    audio_path TEXT,
    reference_audio TEXT,           -- reference track this is a cover of (relative to ref library)
    published_path TEXT,            -- where the published MP3 was last written
    influences TEXT,                -- per-track "sounds like" snapshot (freeform/cover)
    lyric_notes TEXT,               -- free-text direction for lyric (re)generation
    instrumental INTEGER DEFAULT 0, -- 1 = render with no vocals ([Instrumental])
    locked INTEGER DEFAULT 0,       -- 1 = pinned: survives album tracklist regeneration
    cover_strength REAL,            -- per-track audio_cover_strength override (NULL = use global)
    cover_noise REAL,               -- per-track cover_noise_strength override (NULL = use global)
    status TEXT DEFAULT 'briefed',  -- briefed | producing | rendered | failed | published
    source TEXT DEFAULT 'album',    -- album | freeform | cover
    created_at TEXT,
    FOREIGN KEY (release_id) REFERENCES release(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS candidate (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id INTEGER NOT NULL,
    seed INTEGER,
    integrity REAL,
    audio_path TEXT,
    selected INTEGER DEFAULT 0,
    note TEXT,
    created_at TEXT,
    FOREIGN KEY (track_id) REFERENCES track(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def query(sql, args=(), one=False):
    conn = get_db()
    try:
        cur = conn.execute(sql, args)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        conn.close()


def execute(sql, args=()):
    conn = get_db()
    try:
        cur = conn.execute(sql, args)
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def jload(value, default=None):
    if not value:
        return default if default is not None else []
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default if default is not None else []


def jdump(value):
    return json.dumps(value or [])


# --- settings helpers -------------------------------------------------------

DEFAULT_SETTINGS = {
    "llm_backend": "llamacpp",            # llamacpp | ollama | openai
    "llm_base_url": "http://localhost:8080",
    "llm_model": "default",
    "llm_api_key": "",
    "llm_temperature": "0.8",
    "lyrics_temperature": "0.95",         # creative temp for the dedicated lyric pass
    "llm_system_preamble": "",            # prepended to every system prompt (e.g. permissive framing)
    "llm_max_tokens": "4096",
    "acestep_base_url": "http://localhost:8765",
    "acestep_candidates": "3",
    "acestep_duration_ceiling": "240",
    "acestep_format": "wav16",
    "acestep_trim_noise": "1",
    # Cover (audio2audio) controls for acestep.cpp's task_type="cover".
    "acestep_cover_strength": "0.6",      # audio_cover_strength: 0..1 fraction of DiT steps
    "acestep_cover_noise": "0.0",         # cover_noise_strength: blend noise with source latents
    # Strip parenthetical directives from lyrics before rendering so the vocal
    # model doesn't sing them aloud.
    "lyrics_strip_parentheticals": "1",
    # Global lyrical-style guidance fed to every lyric prompt.
    "lyrics_style": ("Favor poetic, evocative, image-driven lyrics — lean on "
                     "metaphor, mood, and sensory detail rather than literal, "
                     "blow-by-blow storytelling."),
    # Global structure/length guidance for lyrics.
    "lyrics_structure": ("Keep it tight and proportional to the song's length. Use "
                         "only the sections the song needs (often verse / chorus / "
                         "verse / chorus / bridge). Don't tack on an outro, and "
                         "avoid filler lines, clichés, and needless repetition."),
    "mock_mode": "1",                     # 1 = synthesize placeholders, no live backends
    # Reference-music repository: a folder of existing audio used as a reference
    # when generating cover versions of songs.
    "reference_music_path": "",
    # stable-diffusion.cpp (sd.cpp) image generation of bands/artists/album covers.
    # Set sdcpp_url for a web-UI (AUTOMATIC1111-compatible) server, OR sdcpp_path
    # for the local executable. A URL takes precedence when both are set.
    "sdcpp_url": "",                      # e.g. http://localhost:7860 (A1111 /sdapi/v1)
    "sdcpp_path": "",                     # path to the sd / sd.cpp executable
    "sdcpp_model": "",                    # path to the diffusion model weights (CLI mode)
    "sdcpp_steps": "20",
    "sdcpp_size": "512",                  # square output, px
    "sdcpp_cfg": "7.0",
    "sdcpp_negative": "text, watermark, signature, blurry, low quality, deformed",
    # Where published MP3s + covers are written. Blank => <app>/published.
    "publish_path": "",
}


def get_setting(key, default=None):
    row = query("SELECT value FROM settings WHERE key = ?", (key,), one=True)
    if row is not None:
        return row["value"]
    return DEFAULT_SETTINGS.get(key, default)


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def all_settings():
    merged = dict(DEFAULT_SETTINGS)
    for row in query("SELECT key, value FROM settings"):
        merged[row["key"]] = row["value"]
    return merged


# --- genre helpers / bulk import -------------------------------------------

# The four list-valued genre columns; everything else is a scalar string.
GENRE_LIST_FIELDS = ("descriptors", "typical_instruments",
                     "common_regions", "base_style_tags")


def coerce_str_list(value):
    """Normalise a value into a clean list of strings. Accepts a JSON list, a
    comma/semicolon-separated string, or None."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [s.strip() for s in re.split(r"[;,]", value) if s.strip()]
    return [str(value).strip()] if str(value).strip() else []


def upsert_genre(data, genre_id=None):
    """Insert a genre, or update it by id (if given) or by unique name.

    `data` is a dict; list fields may be JSON lists or delimited strings.
    Returns one of 'added', 'updated', 'skipped'.
    """
    name = (data.get("name") or "").strip()
    if not name:
        return "skipped"
    cols = {
        "description": (data.get("description") or "").strip(),
        "descriptors": jdump(coerce_str_list(data.get("descriptors"))),
        "typical_instruments": jdump(coerce_str_list(data.get("typical_instruments"))),
        "tempo_range": (data.get("tempo_range") or "").strip(),
        "common_regions": jdump(coerce_str_list(data.get("common_regions"))),
        "base_style_tags": jdump(coerce_str_list(data.get("base_style_tags"))),
        "lyric_guidance": (data.get("lyric_guidance") or "").strip(),
    }
    row = None
    if genre_id is not None:
        row = query("SELECT id FROM genre WHERE id = ?", (genre_id,), one=True)
    if row is None:
        # Match case-insensitively so "indie rock" updates an existing
        # "Indie Rock" rather than creating a near-duplicate.
        row = query("SELECT id FROM genre WHERE name = ? COLLATE NOCASE", (name,), one=True)
    if row is not None:
        execute(
            "UPDATE genre SET name=?, description=?, descriptors=?, typical_instruments=?,"
            " tempo_range=?, common_regions=?, base_style_tags=?, lyric_guidance=? WHERE id=?",
            (name, cols["description"], cols["descriptors"], cols["typical_instruments"],
             cols["tempo_range"], cols["common_regions"], cols["base_style_tags"],
             cols["lyric_guidance"], row["id"]),
        )
        return "updated"
    execute(
        "INSERT INTO genre (name, description, descriptors, typical_instruments,"
        " tempo_range, common_regions, base_style_tags, lyric_guidance) VALUES (?,?,?,?,?,?,?,?)",
        (name, cols["description"], cols["descriptors"], cols["typical_instruments"],
         cols["tempo_range"], cols["common_regions"], cols["base_style_tags"],
         cols["lyric_guidance"]),
    )
    return "added"


def import_genres(items):
    """Bulk upsert genres from an iterable of dicts. Existing genres (matched by
    name) are updated. Returns {'added','updated','skipped','errors'}."""
    result = {"added": 0, "updated": 0, "skipped": 0, "errors": []}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            result["errors"].append(f"item {i}: expected an object, got {type(item).__name__}")
            continue
        if not (item.get("name") or "").strip():
            result["skipped"] += 1
            result["errors"].append(f"item {i}: missing 'name'")
            continue
        try:
            result[upsert_genre(item)] += 1
        except Exception as exc:  # one bad row shouldn't abort the batch
            result["errors"].append(f"item {i} ({item.get('name', '?')}): {exc}")
    return result


def parse_genres_payload(text):
    """Parse a JSON genre payload (a list, or an object with a 'genres' key)
    into a list of dicts. Raises ValueError on malformed input."""
    return _parse_payload(text, "genres")


def _parse_payload(text, key):
    """Parse a JSON payload that is either a list, or an object wrapping the
    list under `key` (e.g. {"genres": [...]}). Raises ValueError otherwise."""
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get(key, data)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array, or {{\"{key}\": [...]}}")
    return data


def export_genres():
    """Return all genres as a list of plain dicts (round-trips with import)."""
    out = []
    for r in query("SELECT * FROM genre ORDER BY name"):
        out.append({
            "name": r["name"],
            "description": r["description"] or "",
            "descriptors": jload(r["descriptors"], []),
            "typical_instruments": jload(r["typical_instruments"], []),
            "tempo_range": r["tempo_range"] or "",
            "common_regions": jload(r["common_regions"], []),
            "base_style_tags": jload(r["base_style_tags"], []),
            "lyric_guidance": (r["lyric_guidance"] if "lyric_guidance" in r.keys() else "") or "",
        })
    return out


# --- style-tag helpers / bulk import ---------------------------------------

STYLE_TAG_CATEGORIES = ("mood", "instrumentation", "era", "production",
                        "crossover", "vocal")


def upsert_style_tag(data, tag_id=None):
    """Insert a style tag, or update it by id (if given) or by unique name.

    `acestep_phrases` may be a JSON list or a delimited string; if omitted it
    defaults to the tag name. Returns 'added', 'updated', or 'skipped'.
    """
    name = (data.get("name") or "").strip()
    if not name:
        return "skipped"
    category = (data.get("category") or "mood").strip() or "mood"
    phrases = coerce_str_list(data.get("acestep_phrases")) or [name]
    row = None
    if tag_id is not None:
        row = query("SELECT id FROM style_tag WHERE id = ?", (tag_id,), one=True)
    if row is None:
        # Match case-insensitively so near-duplicates aren't created.
        row = query("SELECT id FROM style_tag WHERE name = ? COLLATE NOCASE", (name,), one=True)
    if row is not None:
        execute("UPDATE style_tag SET name=?, category=?, acestep_phrases=? WHERE id=?",
                (name, category, jdump(phrases), row["id"]))
        return "updated"
    execute("INSERT INTO style_tag (name, category, acestep_phrases) VALUES (?,?,?)",
            (name, category, jdump(phrases)))
    return "added"


def import_style_tags(items):
    """Bulk upsert style tags from an iterable of dicts. Existing tags (matched
    by name) are updated. Returns {'added','updated','skipped','errors'}."""
    result = {"added": 0, "updated": 0, "skipped": 0, "errors": []}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            result["errors"].append(f"item {i}: expected an object, got {type(item).__name__}")
            continue
        if not (item.get("name") or "").strip():
            result["skipped"] += 1
            result["errors"].append(f"item {i}: missing 'name'")
            continue
        try:
            result[upsert_style_tag(item)] += 1
        except Exception as exc:
            result["errors"].append(f"item {i} ({item.get('name', '?')}): {exc}")
    return result


def parse_style_tags_payload(text):
    """Parse a JSON style-tag payload (a list, or an object with a 'style_tags'
    key) into a list of dicts. Raises ValueError on malformed input."""
    return _parse_payload(text, "style_tags")


def export_style_tags():
    """Return all style tags as a list of plain dicts (round-trips with import)."""
    out = []
    for r in query("SELECT * FROM style_tag ORDER BY category, name"):
        out.append({
            "name": r["name"],
            "category": r["category"] or "mood",
            "acestep_phrases": jload(r["acestep_phrases"], []),
        })
    return out


# --- seed data --------------------------------------------------------------

SEED_GENRES = [
    ("Indie Rock", "Guitar-forward independent rock.",
     ["jangly", "earnest", "lo-fi warmth"], ["electric guitar", "bass", "drums"],
     "110-150 bpm", ["US", "UK"], ["indie rock", "jangle guitars", "warm analog"]),
    ("Synthwave", "Retro-futuristic 80s electronic.",
     ["neon", "nostalgic", "driving"], ["analog synth", "drum machine", "bass synth"],
     "100-130 bpm", ["US", "France"], ["synthwave", "retro 80s synths", "gated reverb drums"]),
    ("Country", "Storytelling roots music.",
     ["heartfelt", "rural", "narrative"], ["acoustic guitar", "pedal steel", "fiddle"],
     "80-130 bpm", ["US South"], ["country", "pedal steel", "acoustic guitar", "warm vocals"]),
    ("Hip Hop", "Beat-driven rhythmic vocal music.",
     ["confident", "rhythmic", "urban"], ["808", "sampler", "synth"],
     "80-100 bpm", ["US"], ["hip hop", "boom bap drums", "deep 808 bass"]),
    ("R&B", "Smooth soulful contemporary vocals.",
     ["smooth", "sensual", "soulful"], ["electric piano", "bass", "synth pad"],
     "70-100 bpm", ["US"], ["r&b", "smooth vocals", "lush chords", "soulful"]),
    ("Folk", "Acoustic, lyric-led traditional music.",
     ["intimate", "acoustic", "poetic"], ["acoustic guitar", "banjo", "harmonica"],
     "70-120 bpm", ["US", "UK", "Ireland"], ["folk", "fingerpicked acoustic", "intimate vocals"]),
    ("Metal", "Heavy, distorted, high-energy rock.",
     ["aggressive", "heavy", "dark"], ["distorted guitar", "double kick", "bass"],
     "120-200 bpm", ["US", "Scandinavia"], ["metal", "heavy distorted guitars", "aggressive drums"]),
    ("Jazz", "Improvisational harmonic music.",
     ["sophisticated", "improvised", "smoky"], ["upright bass", "piano", "saxophone"],
     "90-200 bpm", ["US"], ["jazz", "upright bass", "brushed drums", "improvisation"]),
    ("Electronic", "Club-oriented electronic dance.",
     ["energetic", "hypnotic", "club"], ["synth", "drum machine", "sampler"],
     "120-140 bpm", ["Germany", "UK"], ["electronic", "four on the floor", "analog synths"]),
    ("Pop", "Polished mainstream songcraft.",
     ["catchy", "bright", "polished"], ["synth", "piano", "programmed drums"],
     "100-130 bpm", ["US", "Sweden"], ["pop", "bright production", "catchy hooks", "polished vocals"]),
]

SEED_STYLE_TAGS = [
    ("melancholic", "mood", ["melancholic", "wistful", "bittersweet"]),
    ("euphoric", "mood", ["euphoric", "uplifting", "anthemic"]),
    ("aggressive", "mood", ["aggressive", "intense"]),
    ("dreamy", "mood", ["dreamy", "ethereal", "hazy"]),
    ("acoustic guitar", "instrumentation", ["acoustic guitar", "fingerpicked guitar"]),
    ("analog synth", "instrumentation", ["analog synthesizer", "warm synth pads"]),
    ("horn section", "instrumentation", ["brass section", "horns"]),
    ("80s", "era", ["1980s production", "gated reverb", "retro synths"]),
    ("90s", "era", ["1990s production", "tape saturation"]),
    ("lo-fi", "production", ["lo-fi", "tape hiss", "vinyl crackle"]),
    ("polished studio", "production", ["polished studio production", "tight arrangement", "clean mix"]),
    ("garage", "production", ["raw garage production", "rough mix", "live room"]),
    ("country-rb", "crossover", ["country r&b blend", "soulful vocals over pedal steel"]),
    ("folk-electronic", "crossover", ["folk electronic", "acoustic guitar with synth textures"]),
    ("male vocal", "vocal", ["male lead vocal"]),
    ("female vocal", "vocal", ["female lead vocal"]),
    ("harmonized", "vocal", ["layered vocal harmonies"]),
]


# Columns added after the initial release. CREATE TABLE IF NOT EXISTS won't
# alter an existing table, so add any missing columns on startup. Keep this in
# sync with the SCHEMA above.
MIGRATIONS = [
    ("track", "reference_audio", "TEXT"),
    ("track", "published_path", "TEXT"),
    ("track", "influences", "TEXT"),
    ("track", "lyric_notes", "TEXT"),
    ("track", "instrumental", "INTEGER DEFAULT 0"),
    ("track", "locked", "INTEGER DEFAULT 0"),
    ("track", "cover_strength", "REAL"),
    ("track", "cover_noise", "REAL"),
    ("release", "cover_path", "TEXT"),
    ("release", "cover_prompt", "TEXT"),
    ("artist", "influences", "TEXT"),
    ("band", "influences", "TEXT"),
    ("artist", "vocal", "TEXT"),
    ("band", "vocal", "TEXT"),
    ("artist", "portrait_path", "TEXT"),
    ("artist", "portrait_prompt", "TEXT"),
    ("band", "portrait_path", "TEXT"),
    ("band", "portrait_prompt", "TEXT"),
    ("genre", "lyric_guidance", "TEXT"),
]


def _migrate(conn):
    for table, column, coltype in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    conn.commit()


def init_db(seed=True):
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()

    if not seed:
        return

    if not query("SELECT 1 FROM genre LIMIT 1"):
        for name, desc, descr, instr, tempo, regions, base in SEED_GENRES:
            execute(
                "INSERT INTO genre (name, description, descriptors, typical_instruments,"
                " tempo_range, common_regions, base_style_tags) VALUES (?,?,?,?,?,?,?)",
                (name, desc, jdump(descr), jdump(instr), tempo, jdump(regions), jdump(base)),
            )

    if not query("SELECT 1 FROM style_tag LIMIT 1"):
        for name, cat, phrases in SEED_STYLE_TAGS:
            execute(
                "INSERT INTO style_tag (name, category, acestep_phrases) VALUES (?,?,?)",
                (name, cat, jdump(phrases)),
            )

    for key, value in DEFAULT_SETTINGS.items():
        if query("SELECT 1 FROM settings WHERE key = ?", (key,), one=True) is None:
            set_setting(key, value)


if __name__ == "__main__":
    init_db()
    print(f"Initialized database at {DB_PATH}")
