"""Generation orchestration.

Bridges the data model to the two backends. Builds the prompts for each
authoring stage (artist, band, album concept + tracklist, track brief), and
runs the render-N-candidates / pick-best loop against ACE-Step.
"""

import glob
import os
import random
import re
import wave

from database import (
    all_settings, execute, jdump, jload, now_iso, query,
)
from backends.llm import LLMClient, LLMError
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


# --- coercion of model output (LLMs sometimes return the wrong JSON type) ----

def _text(v):
    """Coerce a model value to a scalar text string for a TEXT column."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v if x is not None)
    return str(v)


def _lyrics_text(v):
    """Lyrics may come back as a list of lines; join them with newlines."""
    if isinstance(v, (list, tuple)):
        return "\n".join(str(x) for x in v)
    return v or ""


def _intval(v, default=None):
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _aslist(v):
    """Coerce a model value to a list of strings (accepts a list or a delimited string)."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [s.strip() for s in re.split(r"[;,]", v) if s.strip()]
    return [str(v)]


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
  gender (the lead vocal: one of female, male, androgynous),
  language (the language they sing in, based on region — e.g. English, Spanish),
  stage (one of: emerging, rising, established, veteran),
  refinement (0.0-1.0 number reflecting how polished their production is).

Choose primary_genre from this list when possible: {genres}.
region drives accent and language, so pick a real place.
{("Constraints from the user: " + str(asked)) if asked else "No constraints; surprise me."}
"""
    data = llm.generate_json(user, system=system)
    return _persist_artist(data)


VOCAL_VALUES = ("female", "male", "androgynous")


def _norm_vocal(value):
    """Normalise a freeform gender/vocal string to '', female, male or androgynous."""
    v = (value or "").strip().lower()
    if v in ("female", "f", "woman", "women", "feminine"):
        return "female"
    if v in ("male", "m", "man", "men", "masculine"):
        return "male"
    if v in ("androgynous", "nonbinary", "non-binary", "neutral", "ambiguous"):
        return "androgynous"
    return v if v in VOCAL_VALUES else ""


def _persist_artist(data, artist_type="solo"):
    aid = execute(
        "INSERT INTO artist (name, type, persona, backstory, region, primary_genre,"
        " secondary_genres, stage, refinement, vocal, language, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            _text(data.get("name")) or "Untitled Artist",
            artist_type,
            _text(data.get("persona", "")),
            _text(data.get("backstory", "")),
            _text(data.get("region", "")),
            _text(data.get("primary_genre", "")),
            jdump(_aslist(data.get("secondary_genres"))),
            _text(data.get("stage", "emerging")) or "emerging",
            float(data.get("refinement", 0.3) or 0.3) if not isinstance(data.get("refinement"), (list, dict)) else 0.3,
            _norm_vocal(_text(data.get("gender") or data.get("vocal"))),
            _text(data.get("language", "")),
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
  language (the language they sing in, based on origin — e.g. English, Spanish),
  members: array of exactly {size} objects, each with keys
     name, persona, region, instrument, gender (female, male, or androgynous),
     refinement (0.0-1.0).
Mark the lead singer's instrument as "lead vocals".
Choose primary_genre from: {genres}.
Members should feel like real people with chemistry and tension.
{("Constraints: " + str(asked)) if asked else ""}
"""
    data = llm.generate_json(user, system=system)
    band_id = execute(
        "INSERT INTO band (name, primary_genre, backstory, language, formed_on, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (_text(data.get("name")) or "Untitled Band", _text(data.get("primary_genre", "")),
         _text(data.get("backstory", "")), _text(data.get("language", "")),
         now_iso()[:10], now_iso()),
    )
    for m in data.get("members", []):
        aid = _persist_artist(
            {
                "name": m.get("name", "Member"),
                "persona": m.get("persona", ""),
                "region": m.get("region", ""),
                "primary_genre": data.get("primary_genre", ""),
                "refinement": m.get("refinement", 0.3),
                "gender": m.get("gender", ""),
            },
            artist_type="band-member",
        )
        execute(
            "INSERT INTO membership (artist_id, band_id, instrument, joined_on)"
            " VALUES (?,?,?,?)",
            (aid, band_id, _text(m.get("instrument", "")), now_iso()[:10]),
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
        (_text(data.get("name")) or name_hint or "Untitled Band",
         _text(data.get("primary_genre")) or genre_hint or "",
         _text(data.get("backstory", "")), now_iso()[:10], now_iso()),
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

def _infl_line(influences):
    influences = (influences or "").strip()
    return f"\nSounds like / influences: {influences}" if influences else ""


def _owner_influences(owner_type, owner_id):
    """The performer's 'sounds like' influence text, or ''."""
    table = "band" if owner_type == "band" else "artist"
    r = query(f"SELECT influences FROM {table} WHERE id = ?", (owner_id,), one=True)
    return (r["influences"] or "").strip() if r and "influences" in r.keys() else ""


# --- lead vocal -------------------------------------------------------------

_VOCAL_GENDER_RE = re.compile(r"\b(?:fe)?male\b|\bandrogynous\b", re.I)


def _is_vocal_directive(text):
    """True if a caption/tag segment is a gendered vocal directive (so it can be
    overridden by the performer's actual vocal). 'layered vocal harmonies' (no
    gender) is left alone."""
    low = text.lower()
    return "vocal" in low and _VOCAL_GENDER_RE.search(low) is not None


def _band_lead_vocal(band_id):
    """The lead vocalist's gender for a band: the vocal of the member whose
    instrument mentions vocals (preferring 'lead'). '' if none set."""
    rows = query(
        "SELECT a.vocal AS vocal, m.instrument AS instrument FROM membership m"
        " JOIN artist a ON a.id = m.artist_id"
        " WHERE m.band_id = ? AND m.left_on IS NULL", (band_id,))
    singers = [r for r in rows if (r["instrument"] or "").lower().find("vocal") >= 0]
    singers.sort(key=lambda r: 0 if "lead" in (r["instrument"] or "").lower() else 1)
    for r in singers:
        if (r["vocal"] or "").strip():
            return r["vocal"].strip()
    return ""


def _owner_vocal(owner_type, owner_id):
    """The lead-vocal gender for a performer: '', female, male or androgynous.
    Bands use their override if set, else the lead vocalist member's gender."""
    if not owner_type or owner_id is None:
        return ""
    if owner_type == "band":
        b = query("SELECT vocal FROM band WHERE id = ?", (owner_id,), one=True)
        override = (b["vocal"] or "").strip() if b and "vocal" in b.keys() else ""
        return override or _band_lead_vocal(owner_id)
    a = query("SELECT vocal FROM artist WHERE id = ?", (owner_id,), one=True)
    return (a["vocal"] or "").strip() if a and "vocal" in a.keys() else ""


def _owner_language(owner_type, owner_id):
    """The default lyric language for a performer, or ''."""
    if not owner_type or owner_id is None:
        return ""
    table = "band" if owner_type == "band" else "artist"
    r = query(f"SELECT language FROM {table} WHERE id = ?", (owner_id,), one=True)
    return (r["language"] or "").strip() if r and "language" in r.keys() else ""


_LANG_CODES = {
    "english": "en", "spanish": "es", "español": "es", "french": "fr",
    "français": "fr", "german": "de", "deutsch": "de", "italian": "it",
    "portuguese": "pt", "português": "pt", "dutch": "nl", "russian": "ru",
    "japanese": "ja", "korean": "ko", "chinese": "zh", "mandarin": "zh",
    "arabic": "ar", "hindi": "hi", "swedish": "sv", "norwegian": "no",
    "danish": "da", "finnish": "fi", "polish": "pl", "turkish": "tr",
}


def _lang_code(name):
    """Map a language name to a BCP-47 code for ACE-Step's vocal_language; pass a
    short code or unknown value through as-is."""
    n = (name or "").strip().lower()
    if not n:
        return ""
    return _LANG_CODES.get(n, n)


def _apply_vocal_to_tags(tags, vocal):
    """Drop any gendered-vocal tags and append the performer's actual vocal."""
    if not vocal:
        return tags
    kept = [t for t in tags if not _is_vocal_directive(t)]
    kept.append(f"{vocal} vocal")
    return kept


def _owner_context(owner_type, owner_id, for_lyrics=False):
    """Performer context for prompts. When `for_lyrics` is set, the band lineup
    (member names/instruments) is omitted so member names don't leak into the
    sung lyrics; genre, backstory, and influences are kept for grounding."""
    voc = _owner_vocal(owner_type, owner_id)
    voc_line = f"\nLead vocal: {voc}" if voc else ""
    if owner_type == "band":
        b = query("SELECT * FROM band WHERE id = ?", (owner_id,), one=True)
        infl = _infl_line(b["influences"] if "influences" in b.keys() else "")
        if for_lyrics:
            return (f"Band: {b['name']} | genre: {b['primary_genre']}\n"
                    f"Backstory: {b['backstory']}{infl}{voc_line}"), b["primary_genre"]
        members = query(
            "SELECT a.name, m.instrument FROM membership m JOIN artist a ON a.id = m.artist_id"
            " WHERE m.band_id = ? AND m.left_on IS NULL", (owner_id,))
        lineup = ", ".join(f"{m['name']} ({m['instrument']})" for m in members)
        return (f"Band: {b['name']} | genre: {b['primary_genre']} | lineup: {lineup}\n"
                f"Backstory: {b['backstory']}{infl}{voc_line}"), b["primary_genre"]
    a = query("SELECT * FROM artist WHERE id = ?", (owner_id,), one=True)
    infl = _infl_line(a["influences"] if "influences" in a.keys() else "")
    return (f"Artist: {a['name']} | genre: {a['primary_genre']} | region: {a['region']}\n"
            f"Persona: {a['persona']}\nRefinement: {a['refinement']}{infl}{voc_line}"), a["primary_genre"]


# Shared lyric-writing rules appended to every prompt that authors lyrics, to
# (1) keep performer/band-member names out of the sung words and (2) stop the
# model emitting parenthetical stage directions that ACE-Step vocalizes.
LYRIC_RULES = (
    "Lyric rules: write only words meant to be sung. Use [section] tags on their "
    "own lines for structure (e.g. [verse], [chorus], [bridge]). Do NOT include "
    "stage directions, ad-libs, or production notes in parentheses — for example "
    "'(guitar solo)', '(whisper)', '(x2)', '(instrumental)'. Anything inside "
    "parentheses gets sung aloud by the vocal model, so leave it out entirely. "
    "Never mention the performer's or any band member's name in the lyrics, and "
    "never name any reference or influence artist in the lyrics either."
)


def _lyric_style_line():
    s = (all_settings().get("lyrics_style") or "").strip()
    return f"Lyrical style: {s}\n" if s else ""


# Built-in per-genre lyric idiom, used when a genre has no custom lyric_guidance.
_GENRE_LYRIC_DEFAULTS = {
    "indie rock": "earnest and conversational, specific personal images, wry and understated.",
    "synthwave": "neon nightscapes, motion and longing, sleek and nostalgic; spare.",
    "country": "plainspoken narrative with specific, vivid rural detail and a clear story or character.",
    "hip hop": "rhythmic, dense wordplay and internal rhyme, swagger or sharp social observation.",
    "r&b": "intimate, sensual and emotionally direct; smooth, repeatable hooks.",
    "folk": "poetic and imagistic, restrained, a story or parable; few words, well chosen.",
    "metal": "intense and visceral, dark or mythic themes, forceful imagery.",
    "jazz": "sophisticated and allusive, mood over plot, loose phrasing.",
    "electronic": "minimal and hypnotic; repeated phrases used as texture more than narrative.",
    "pop": "catchy and direct — one clear hook and feeling, economical.",
}


def _genre_lyric_guidance(name):
    """Per-genre lyric idiom: the genre's own `lyric_guidance`, else a built-in
    default for known genres, else ''."""
    name = (name or "").strip()
    if not name:
        return ""
    r = query("SELECT lyric_guidance FROM genre WHERE name = ? COLLATE NOCASE", (name,), one=True)
    g = (r["lyric_guidance"] if r and "lyric_guidance" in r.keys() else "") or ""
    return g.strip() or _GENRE_LYRIC_DEFAULTS.get(name.lower(), "")


def _lyric_guidance_block(genre_name="", duration=None):
    """The tunable lyric guidance appended to lyric prompts: genre idiom, the
    global voice/tone and structure settings, and a proportional-length hint."""
    parts = []
    gi = _genre_lyric_guidance(genre_name)
    if gi:
        parts.append(f"Genre idiom — {genre_name}: {gi}")
    s = all_settings()
    style = (s.get("lyrics_style") or "").strip()
    if style:
        parts.append(f"Voice & tone: {style}")
    struct = (s.get("lyrics_structure") or "").strip()
    if struct:
        parts.append(f"Structure & length: {struct}")
    if duration:
        parts.append(f"Target length: about {int(duration)} seconds of music — keep the "
                     "lyric proportional to that; don't overwrite or pad.")
    return ("\n".join(parts) + "\n") if parts else ""


def _lyrics_temperature():
    """Temperature for the dedicated lyric pass (separate from the structured
    brief, which uses the global LLM temperature)."""
    try:
        return float(all_settings().get("lyrics_temperature", 0.95))
    except (TypeError, ValueError):
        return 0.95


def _write_lyrics(ctx, genre, title, role="", subject="", summary="", mood="",
                  tempo=None, style_cues=None, notes="", duration=None, language=""):
    """Generate just the lyrics in a focused call at the lyric temperature, so
    the words can run creative while the structured brief stays calm and parses."""
    llm = LLMClient()
    if isinstance(style_cues, str):
        cues = style_cues
    else:
        cues = ", ".join(style_cues or [])
    lang_line = (f"Write the lyrics entirely in {language}.\n") if language else ""
    system = "You are a songwriter writing lyrics to fit a track brief."
    user = f"""{ctx}
Track: "{title}"{f" (role: {role})" if role else ''}
Subject: {subject or '(none)'}
Summary: {summary or '(none)'}
Mood: {mood or '(unspecified)'} | tempo: {tempo or '?'} bpm
Style cues: {cues or '(none)'}
{('Lyric direction: ' + notes) if notes else ''}

{lang_line}Write the lyrics with [verse]/[chorus] section tags, in this performer's voice
and the {genre or 'song'}'s idiom — specific, not generic. Return JSON with a
single key "lyrics" whose value is the lyric text.

{_lyric_guidance_block(genre, duration)}{LYRIC_RULES}
"""
    data = llm.generate_json(user, system=system, temperature=_lyrics_temperature())
    if isinstance(data, dict):
        return _lyrics_text(data.get("lyrics") or data.get("text") or "")
    return data if isinstance(data, str) else ""


def _strip_lrc_timestamps(lyrics):
    """Remove LRC `[mm:ss.xx]` timestamps (and a leading space) from synced lyrics
    so they render cleanly — ACE-Step's lyric field is structure-based, not timed,
    and would otherwise sing the timestamps."""
    if not lyrics:
        return lyrics
    text = re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]\s?", "", lyrics)
    return text


def _strip_lyric_directives(lyrics):
    """Remove parenthetical stage directions/ad-libs from lyrics before they go
    to ACE-Step, which otherwise sings them. Square-bracket [section] structure
    tags are preserved. Whitespace/blank lines are tidied."""
    if not lyrics:
        return lyrics
    text = re.sub(r"\([^()]*\)", "", lyrics)   # drop (parenthetical) groups
    text = "\n".join(re.sub(r"[ \t]{2,}", " ", ln).strip()
                     for ln in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)     # collapse runs of blank lines
    return text.strip()


_TRACK_KEYS_DOC = """     position (int, 1-based),
     title,
     role (one of: opener, single, ballad, experimental, interlude, closer),
     subject (what the song is about),
     summary (2-3 sentences: the feel, what happens musically and lyrically),
     style_cues (array of short production/style descriptors),
     tempo (bpm int),
     mood,
     length_seconds (int, 90-300),
     lyric_direction (one or two sentences guiding the lyrics for this track —
        angle, perspective, recurring image/refrain, structure or tone — grounded
        in the album's concept/ethos and the artist; not the lyrics themselves)."""


# Appended to album-concept/tracklist prompts. Without strong steering, models
# (a) decode to the same stock words, and (b) over-literally mash the brief's
# keywords and the album title into a themed-pun set (e.g. "Roots" -> Soil
# Sample, Mycelium Network). This pushes them toward real, human song titles.
_NAMING_GUIDANCE = (
    "Naming: title the album and each track like real songs in this genre — "
    "human, evocative, and varied, each rooted in that song's own emotion or "
    "story. Do NOT apply one gimmick or motif across the whole tracklist, and do "
    "NOT turn the brief's literal keywords or the album title into the track names "
    "(e.g. don't make a run of gardening puns from the word 'roots', or tech puns "
    "from 'electric/glitchy'). Read themes figuratively, in their musical sense — "
    "'roots' means heritage and origins, not soil or plants. Avoid stock title "
    "words (neon, concrete, echoes, ghosts, shadows, midnight, static, velvet) and "
    "tech jargon (circuit, signal, protocol, network, upload, decay) unless a song "
    "is truly about that. When unsure, simpler and more emotional beats clever."
)


def _insert_tracklist(rid, tracks, position_from=None):
    for i, t in enumerate(tracks):
        cues = _aslist(t.get("style_cues"))
        pos = (position_from + i) if position_from is not None else _intval(t.get("position"), i + 1)
        execute(
            "INSERT INTO track (release_id, position, role, title, subject, summary,"
            " style_tags, tempo, mood, duration, lyric_notes, status, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, pos, _text(t.get("role", "")), _text(t.get("title", "Untitled")),
             _text(t.get("subject", "")), _text(t.get("summary", "")), jdump(cues),
             _intval(t.get("tempo")), _text(t.get("mood", "")),
             _intval(t.get("length_seconds"), 180),
             _text(t.get("lyric_direction", "")),
             "briefed", "album", now_iso()),
        )


def generate_album(owner_type, owner_id, ethos, style_tags, rel_type="album",
                   track_count=None, title=None, self_titled=False):
    llm = LLMClient()
    ctx, genre = _owner_context(owner_type, owner_id)
    owner_name, _ = _owner_name_genre(owner_type, owner_id)
    default_counts = {"album": 9, "ep": 5, "single": 1}
    n = int(track_count or default_counts.get(rel_type, 9))
    tag_str = ", ".join(style_tags) if style_tags else "(none specified)"

    # A fixed title (explicit or self-titled) is told to the model and overrides
    # whatever it returns.
    fixed_title = owner_name if self_titled else (title or "").strip()
    if self_titled:
        title_line = f'This is a SELF-TITLED release; its title is "{owner_name}".'
    elif fixed_title:
        title_line = f'The release title is fixed: "{fixed_title}".'
    else:
        title_line = "Invent a fitting title."

    system = (
        "You are an A&R producer shaping a cohesive release. Build a tracklist with "
        "a real emotional arc, not a random list. Each track has a clear job."
    )
    language = _owner_language(owner_type, owner_id)
    lang_line = (f"\nWrite the title, concept, inspiration, and every track's "
                 f"title/subject/summary in {language}.") if language else ""
    user = f"""Design a {rel_type} for:
{ctx}

Ethos / concept brief from the user: {ethos or "(none given — derive from the artist)"}
Requested style tags: {tag_str}
Primary genre: {genre}
{title_line}{lang_line}

Return JSON with keys:
  title, concept, inspiration,
  tracks: array of exactly {n} objects, each with keys:
{_TRACK_KEYS_DOC}
Sequence the roles sensibly (opener first, closer last).

{_NAMING_GUIDANCE}
"""
    data = llm.generate_json(user, system=system, temperature=_lyrics_temperature())
    final_title = fixed_title or _text(data.get("title")) or "Untitled"
    rid = execute(
        "INSERT INTO release (owner_type, owner_id, type, title, concept, inspiration,"
        " ethos, style_tags, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (owner_type, owner_id, rel_type, final_title,
         _text(data.get("concept", "")), _text(data.get("inspiration", "")), ethos or "",
         jdump(style_tags), "tracklist", now_iso()),
    )
    _insert_tracklist(rid, data.get("tracks", []))
    return rid


def regenerate_tracklist(album_id):
    """Rebuild a release's tracklist from its current title/concept/inspiration/
    ethos/genre/tags (e.g. after editing the inspiration). Locked tracks are kept
    untouched; only unlocked tracks are replaced, and the model is asked to
    generate complementary tracks to fill out the album. Returns the album_id."""
    rel = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    if not rel:
        raise ValueError("album not found")
    llm = LLMClient()
    ctx, genre = _owner_context(rel["owner_type"], rel["owner_id"])
    default_counts = {"album": 9, "ep": 5, "single": 1}
    locked = query(
        "SELECT * FROM track WHERE release_id=? AND locked=1 ORDER BY position",
        (album_id,))
    total = query("SELECT COUNT(*) c FROM track WHERE release_id=?", (album_id,), one=True)["c"]
    target = total or default_counts.get(rel["type"], 9)
    n_new = max(0, target - len(locked))

    if n_new:
        tags = jload(rel["style_tags"], [])
        tag_str = ", ".join(tags) if tags else "(none specified)"
        locked_block = ""
        if locked:
            lines = "\n".join(
                f"- {t['title']} ({t['role'] or 'track'}): {t['subject'] or ''}"
                for t in locked)
            locked_block = (f"\nThese tracks are FIXED — keep them, do not repeat or "
                            f"duplicate them; write new tracks that complement them:\n{lines}\n")
        system = (
            "You are an A&R producer rebuilding the tracklist for an existing release, "
            "keeping its title, concept, and inspiration intact while giving it a fresh, "
            "coherent sequence with a real emotional arc."
        )
        language = _owner_language(rel["owner_type"], rel["owner_id"])
        lang_line = (f"\nWrite every track's title/subject/summary in {language}."
                     if language else "")
        user = f"""Rebuild the tracklist for this {rel['type']}:
{ctx}

Title: {rel['title']}
Concept: {rel['concept'] or '(none)'}
Inspiration: {rel['inspiration'] or '(none)'}
Ethos: {rel['ethos'] or '(none)'}
Style tags: {tag_str}
Primary genre: {genre}{lang_line}
{locked_block}
Return JSON with key:
  tracks: array of exactly {n_new} objects, each with keys:
{_TRACK_KEYS_DOC}
Sequence the roles sensibly.

{_NAMING_GUIDANCE}
"""
        data = llm.generate_json(user, system=system, temperature=_lyrics_temperature())
        new_tracks = (data.get("tracks", []) or [])[:n_new]  # never exceed the target
    else:
        new_tracks = []

    # Replace only the unlocked tracks; append the new ones after the locked ones.
    execute("DELETE FROM track WHERE release_id=? AND COALESCE(locked,0)=0", (album_id,))
    maxpos = max((t["position"] or 0 for t in locked), default=0)
    _insert_tracklist(album_id, new_tracks, position_from=maxpos + 1)
    execute("UPDATE release SET status='tracklist' WHERE id=?", (album_id,))
    return album_id


def renumber_tracks(album_id):
    """Renumber a release's tracks to 1..N by current (position, id) order."""
    rows = query("SELECT id FROM track WHERE release_id=? ORDER BY position, id", (album_id,))
    for i, r in enumerate(rows, start=1):
        execute("UPDATE track SET position=? WHERE id=?", (i, r["id"]))


def add_track_to_album(album_id, track_id):
    """Attach a standalone track to an album at the end of the tracklist."""
    rel = query("SELECT 1 FROM release WHERE id=?", (album_id,), one=True)
    t = query("SELECT release_id FROM track WHERE id=?", (track_id,), one=True)
    if not rel or not t:
        raise ValueError("album or track not found")
    if t["release_id"]:
        raise ValueError("track already belongs to a release")
    maxpos = query("SELECT MAX(position) m FROM track WHERE release_id=?", (album_id,), one=True)["m"] or 0
    execute("UPDATE track SET release_id=?, position=? WHERE id=?",
            (album_id, maxpos + 1, track_id))
    return track_id


def move_track(album_id, track_id, direction):
    """Move a track up/down within its album, then renumber 1..N."""
    rows = query("SELECT id FROM track WHERE release_id=? ORDER BY position, id", (album_id,))
    ids = [r["id"] for r in rows]
    if track_id not in ids:
        return
    i = ids.index(track_id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for pos, tid in enumerate(ids, start=1):
        execute("UPDATE track SET position=? WHERE id=?", (pos, tid))


# ---------------------------------------------------------------------------
# Track: brief (lyrics + final tags) then render
# ---------------------------------------------------------------------------

def brief_track_from_album(track_id):
    """Expand an album tracklist row into a full brief with lyrics + tags."""
    llm = LLMClient()
    t = query("SELECT * FROM track WHERE id = ?", (track_id,), one=True)
    rel = query("SELECT * FROM release WHERE id = ?", (t["release_id"],), one=True)
    ctx, genre = _owner_context(rel["owner_type"], rel["owner_id"], for_lyrics=True)
    refinement = _owner_refinement(rel["owner_type"], rel["owner_id"])
    system = "You are a songwriter and producer turning a track concept into a recordable brief."
    user = f"""{ctx}

Album: {rel['title']} — {rel['concept']}
Track {t['position']}: "{t['title']}" (role: {t['role']})
Subject: {t['subject']}
Summary so far: {t['summary']}
Existing style cues: {jload(t['style_tags'])}
Genre: {genre}

Return JSON with keys (no lyrics — those are written separately):
  style_tags (array of concise production/style descriptors),
  tempo (bpm int), key (musical key), mood,
  environmentals (array, e.g. room, reverb, tape, vinyl crackle),
  duration_seconds (int).
"""
    data = llm.generate_json(user, system=system)
    base_cues = jload(t["style_tags"], [])
    final_tags = base_cues + _aslist(data.get("style_tags")) + _refinement_tags(refinement)
    mood = _text(data.get("mood", t["mood"]))
    tempo = _intval(data.get("tempo"), t["tempo"])
    duration = _intval(data.get("duration_seconds"), t["duration"])
    # Instrumentals carry no written lyrics; otherwise write them in a focused,
    # creative-temperature pass.
    no_lyrics = bool(t["instrumental"]) if "instrumental" in t.keys() else False
    notes = (t["lyric_notes"] or "").strip() if "lyric_notes" in t.keys() else ""
    language = (t["language"] or "").strip() if "language" in t.keys() else ""
    language = language or _owner_language(rel["owner_type"], rel["owner_id"])
    lyrics_out = "" if no_lyrics else _write_lyrics(
        ctx, genre, t["title"], role=t["role"], subject=t["subject"],
        summary=t["summary"], mood=mood, tempo=tempo, style_cues=final_tags,
        notes=notes, duration=duration, language=language)
    execute(
        "UPDATE track SET lyrics=?, style_tags=?, tempo=?, song_key=?, mood=?,"
        " environmentals=?, duration=?, status='briefed' WHERE id=?",
        (lyrics_out, jdump(final_tags), tempo,
         _text(data.get("key", "")), mood,
         jdump(_aslist(data.get("environmentals"))),
         duration, track_id),
    )
    return track_id


def regenerate_lyrics(track_id):
    """Rewrite ONLY the lyrics for a track, from its subject/summary/style and an
    optional lyric direction — leaving tempo, key, style tags, etc. untouched.
    Runs at the lyric temperature."""
    t = query("SELECT * FROM track WHERE id = ?", (track_id,), one=True)
    if not t:
        raise ValueError("track not found")
    if "instrumental" in t.keys() and t["instrumental"]:
        raise LLMError("This track is an instrumental — it has no lyrics.")
    ctx = ""
    genre = ""
    owner_type = owner_id = None
    if t["release_id"]:
        rel = query("SELECT owner_type, owner_id FROM release WHERE id = ?",
                    (t["release_id"],), one=True)
        if rel:
            owner_type, owner_id = rel["owner_type"], rel["owner_id"]
            ctx, genre = _owner_context(owner_type, owner_id, for_lyrics=True)
    notes = (t["lyric_notes"] or "").strip() if "lyric_notes" in t.keys() else ""
    language = (t["language"] or "").strip() if "language" in t.keys() else ""
    if not language and owner_type:
        language = _owner_language(owner_type, owner_id)
    lyrics = _write_lyrics(
        ctx, genre, t["title"], role=t["role"], subject=t["subject"],
        summary=t["summary"], mood=t["mood"], tempo=t["tempo"],
        style_cues=jload(t["style_tags"], []), notes=notes, duration=t["duration"],
        language=language)
    if not (lyrics or "").strip():
        raise LLMError("model returned no lyrics")
    execute("UPDATE track SET lyrics=? WHERE id=?", (lyrics, track_id))
    return track_id


def brief_album(album_id, progress=None, cancel=None):
    """Write lyrics + full brief for every album track that doesn't have lyrics
    yet. Already-briefed tracks (with lyrics) are left untouched so manual edits
    aren't clobbered. Returns {'briefed','skipped','failed','cancelled','errors'}.

    `progress(event, **data)` fires ("track_start"/"track_done") per track;
    `cancel()` is polled between tracks to stop early.
    """
    tracks = query(
        "SELECT id, lyrics, instrumental FROM track"
        " WHERE release_id = ? ORDER BY position", (album_id,))
    # Skip instrumentals (no written lyrics); covers still get their own lyrics.
    to_brief = [t for t in tracks if not (t["lyrics"] or "").strip()
                and not t["instrumental"]]
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
        "SELECT id, lyrics, audio_path, instrumental"
        " FROM track WHERE release_id = ? ORDER BY position", (album_id,))

    def _renderable(t):
        # Has a brief (lyrics), or is an instrumental. Covers carry their own lyrics.
        return bool((t["lyrics"] or "").strip() or t["instrumental"])

    to_render = [t for t in tracks if not t["audio_path"] and _renderable(t)]
    res = {"rendered": 0, "skipped": 0, "failed": 0, "cancelled": 0, "errors": []}
    for t in tracks:
        if t["audio_path"] or not _renderable(t):
            res["skipped"] += 1
            if not _renderable(t) and not t["audio_path"]:
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
    genre = ""
    refinement = 0.5
    if owner_type and owner_id:
        ctx, genre = _owner_context(owner_type, owner_id, for_lyrics=True)
        refinement = _owner_refinement(owner_type, owner_id)
    system = "You turn a loose idea into a complete, recordable song brief."
    ctx_block = f"Performer context:\n{ctx}\n" if ctx else ""
    user = f"""{ctx_block}User prompt: {prompt}

Return JSON with keys (no lyrics — those are written separately):
  title, subject, summary,
  style_tags (array), tempo (bpm int), key, mood,
  environmentals (array), duration_seconds (int).
"""
    data = llm.generate_json(user, system=system)
    final_tags = _aslist(data.get("style_tags")) + _refinement_tags(refinement)
    influence = _owner_influences(owner_type, owner_id) if (owner_type and owner_id) else ""
    if owner_type and owner_id:
        final_tags = _apply_vocal_to_tags(final_tags, _owner_vocal(owner_type, owner_id))
    title = _text(data.get("title")) or "Untitled"
    subject = _text(data.get("subject", ""))
    summary = _text(data.get("summary", ""))
    mood = _text(data.get("mood", ""))
    tempo = _intval(data.get("tempo"))
    duration = _intval(data.get("duration_seconds"), 180)
    language = _owner_language(owner_type, owner_id) if (owner_type and owner_id) else ""
    lyrics = _write_lyrics(ctx, genre, title, subject=subject, summary=summary,
                           mood=mood, tempo=tempo, style_cues=final_tags,
                           duration=duration, language=language)
    tid = execute(
        "INSERT INTO track (position, role, title, subject, summary, lyrics, style_tags,"
        " tempo, song_key, mood, environmentals, duration, influences, status, source, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "single", title, subject, summary, lyrics, jdump(final_tags),
         tempo, _text(data.get("key", "")), mood,
         jdump(_aslist(data.get("environmentals"))), duration,
         influence, "briefed", "freeform", now_iso()),
    )
    # Attributing to a performer associates the track with them (as a single).
    if owner_type and owner_id:
        associate_track_with_owner(tid, owner_type, owner_id, title)
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


def associate_track_with_owner(track_id, owner_type, owner_id, title, rtype="single"):
    """Attach a standalone track to a performer by wrapping it in a release (a
    single by default) owned by that artist/band. Returns the release id."""
    rid = execute(
        "INSERT INTO release (owner_type, owner_id, type, title, status, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (owner_type, owner_id, rtype, title or "Single", "tracklist", now_iso()))
    execute("UPDATE track SET release_id=?, position=1 WHERE id=?", (rid, track_id))
    return rid


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


def _decode_id3_text(frame):
    if not frame:
        return ""
    enc, raw = frame[0], frame[1:]
    try:
        if enc == 1:
            return raw.decode("utf-16", "replace").split("\x00", 1)[0].strip()
        if enc == 2:
            return raw.decode("utf-16-be", "replace").split("\x00", 1)[0].strip()
        codec = "utf-8" if enc == 3 else "latin-1"
        return raw.split(b"\x00", 1)[0].decode(codec, "replace").strip()
    except Exception:
        return ""


def _read_id3_basic(path):
    """Best-effort artist/title from an MP3's ID3v2 tag (TPE1/TIT2). Stdlib only."""
    out = {}
    try:
        with open(path, "rb") as f:
            head = f.read(10)
            if head[:3] != b"ID3":
                return out
            ver = head[3]
            size = ((head[6] & 0x7f) << 21 | (head[7] & 0x7f) << 14
                    | (head[8] & 0x7f) << 7 | (head[9] & 0x7f))
            data = f.read(size)
    except OSError:
        return out
    i, n = 0, len(data)
    while i + 10 <= n:
        fid = data[i:i + 4]
        if fid == b"\x00\x00\x00\x00":
            break
        if ver >= 4:   # v2.4 frame sizes are synchsafe
            fsize = ((data[i + 4] & 0x7f) << 21 | (data[i + 5] & 0x7f) << 14
                     | (data[i + 6] & 0x7f) << 7 | (data[i + 7] & 0x7f))
        else:          # v2.3 plain big-endian
            fsize = int.from_bytes(data[i + 4:i + 8], "big")
        frame = data[i + 10:i + 10 + fsize]
        i += 10 + fsize
        if fid == b"TIT2":
            out["title"] = _decode_id3_text(frame)
        elif fid == b"TPE1":
            out["artist"] = _decode_id3_text(frame)
    return out


def _reference_artist_title(reference_rel):
    """Derive (artist, title) for a reference file from its ID3 tags, else its
    filename ("Artist - Title")."""
    abs_path = reference_music_abspath(reference_rel)
    artist = title = ""
    if abs_path and abs_path.lower().endswith(".mp3"):
        tags = _read_id3_basic(abs_path)
        artist, title = tags.get("artist", ""), tags.get("title", "")
    if not title:
        name = os.path.splitext(os.path.basename(reference_rel))[0]
        if " - " in name:
            a, t = name.split(" - ", 1)
            artist = artist or a.strip()
            title = t.strip()
        else:
            title = name.strip()
    return artist.strip(), title.strip()


def fetch_reference_lyrics(reference_rel, synced=False):
    """Look up the original song's lyrics from LRCLIB (free, no API key) using
    the reference's artist/title. Returns plain lyrics, or — when `synced` and
    LRCLIB has them — the timestamped LRC (`[mm:ss.xx]` lines). Raises ValueError
    if nothing is found or the lookup fails."""
    import requests
    artist, title = _reference_artist_title(reference_rel)
    if not title:
        raise ValueError("couldn't determine the song title from the reference")
    field = "syncedLyrics" if synced else "plainLyrics"
    headers = {"User-Agent": "MusicWorld (local music-world app)"}

    def _pick(d):
        # Prefer the requested field; fall back to plain if synced is missing.
        return (d.get(field) or (d.get("plainLyrics") if synced else "") or "").strip()

    try:
        if artist:
            r = requests.get("https://lrclib.net/api/get",
                             params={"artist_name": artist, "track_name": title},
                             headers=headers, timeout=15)
            if r.status_code == 200:
                lyr = _pick(r.json())
                if lyr:
                    return lyr
        q = (artist + " " + title).strip()
        r = requests.get("https://lrclib.net/api/search", params={"q": q},
                         headers=headers, timeout=15)
        r.raise_for_status()
        for item in r.json() or []:
            lyr = _pick(item)
            if lyr:
                return lyr
    except requests.RequestException as exc:
        raise ValueError(f"lyrics lookup failed: {exc}") from exc
    except ValueError as exc:  # bad JSON
        raise ValueError(f"lyrics lookup returned an unexpected response: {exc}") from exc
    raise ValueError(f"no lyrics found for \"{title}\"" + (f" by {artist}" if artist else ""))


def create_cover_track(reference_rel, owner_type=None, owner_id=None, notes=""):
    """Create a cover of a reference track. Tries to fetch the original song's
    lyrics first (LRCLIB); only if that fails does it generate new lyrics. Either
    way the render borrows the reference's musical style (audio2audio). Returns
    (track_id, lyrics_source) where lyrics_source is 'fetched' or 'generated'.
    """
    llm = LLMClient()
    ref_title = os.path.splitext(os.path.basename(reference_rel))[0]
    ctx = ""
    genre = ""
    refinement = 0.5
    if owner_type and owner_id:
        ctx, genre = _owner_context(owner_type, owner_id, for_lyrics=True)
        refinement = _owner_refinement(owner_type, owner_id)

    # Prefer the original song's real lyrics; only generate when none are found.
    fetched = None
    try:
        fetched = fetch_reference_lyrics(reference_rel)
    except Exception:
        fetched = None

    ctx_block = f"Performer context:\n{ctx}\n" if ctx else ""
    system = ("You write a production/style brief for an original song to be "
              "performed in the musical STYLE of a reference track (audio2audio).")
    user = f"""{ctx_block}Style reference (for vibe/production feel only): "{ref_title}"
{('Direction: ' + notes) if notes else ''}

Return JSON with keys (no lyrics — those are handled separately):
  title, subject, summary,
  style_tags (array), tempo (bpm int), key, mood,
  environmentals (array), duration_seconds (int).
"""
    data = llm.generate_json(user, system=system)
    final_tags = _aslist(data.get("style_tags")) + _refinement_tags(refinement)
    influence = _owner_influences(owner_type, owner_id) if (owner_type and owner_id) else ""
    if owner_type and owner_id:
        final_tags = _apply_vocal_to_tags(final_tags, _owner_vocal(owner_type, owner_id))
    # Name the cover after the original song: "Original Title (Cover)".
    _, orig_title = _reference_artist_title(reference_rel)
    title = f"{(orig_title or ref_title).strip()} (Cover)"
    subject = _text(data.get("subject", ""))
    summary = _text(data.get("summary", ""))
    mood = _text(data.get("mood", ""))
    tempo = _intval(data.get("tempo"))
    duration = _intval(data.get("duration_seconds"), 180)
    # Use the original's real lyrics if found, else write fresh ones in a pass.
    language = _owner_language(owner_type, owner_id) if (owner_type and owner_id) else ""
    lyrics_out = fetched if fetched else _write_lyrics(
        ctx, genre, title, subject=subject, summary=summary, mood=mood,
        tempo=tempo, style_cues=final_tags, notes=notes, duration=duration,
        language=language)
    tid = execute(
        "INSERT INTO track (position, role, title, subject, summary, lyrics, style_tags,"
        " tempo, song_key, mood, environmentals, duration, reference_audio, influences,"
        " status, source, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "cover", title, subject, summary, lyrics_out, jdump(final_tags),
         tempo, _text(data.get("key", "")), mood,
         jdump(_aslist(data.get("environmentals"))), duration,
         reference_rel, influence, "briefed", "cover", now_iso()),
    )
    # Attributing to a performer associates the cover with them (as a single).
    if owner_type and owner_id:
        associate_track_with_owner(tid, owner_type, owner_id, title)
    return tid, ("fetched" if fetched else "generated")


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


def build_album_cover_prompt(album_id):
    """The auto-built cover-art prompt for an album (from title/owner/genre/
    concept/style tags). Shown in the UI so it can be tweaked before generating."""
    album = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    if not album:
        raise ValueError("album not found")
    owner_name, genre = _owner_name_genre(album["owner_type"], album["owner_id"])
    tags = jload(album["style_tags"], [])
    return _art_prompt(
        f'album cover art for "{album["title"]}" by {owner_name}',
        f'a {album["type"]} release', genre, tags, album["concept"] or "")


def _abs_cover(rel):
    if not rel:
        return None
    p = rel if os.path.isabs(rel) else os.path.join(ROOT, rel)
    return p if os.path.exists(p) else None


def _owner_portrait_path(owner_type, owner_id):
    """Absolute path to a performer's portrait image, or None."""
    table = "band" if owner_type == "band" else "artist"
    r = query(f"SELECT portrait_path FROM {table} WHERE id = ?", (owner_id,), one=True)
    return _abs_cover(r["portrait_path"]) if r and "portrait_path" in r.keys() else None


def generate_album_cover(album_id, prompt=None, use_photo=False):
    """Render an album-cover image via sd.cpp (or a placeholder in mock mode).
    Uses `prompt` if given (the user's edited text), else the auto-built one.
    When `use_photo` is set and the performer has a portrait, that image seeds the
    cover via img2img. Stores the cover path and prompt on the release."""
    album = query("SELECT * FROM release WHERE id = ?", (album_id,), one=True)
    if not album:
        raise ValueError("album not found")
    prompt = (prompt or "").strip() or build_album_cover_prompt(album_id)
    init = _owner_portrait_path(album["owner_type"], album["owner_id"]) if use_photo else None
    os.makedirs(COVER_DIR, exist_ok=True)
    out = os.path.join(COVER_DIR, f"album{album_id}.png")
    ImageGenClient().generate(prompt, out, seed=album_id * 7 + 13,
                              init_image=init, strength=0.55)
    rel = os.path.relpath(out, ROOT)
    execute("UPDATE release SET cover_path=?, cover_prompt=? WHERE id=?",
            (rel, prompt, album_id))
    return rel


# ---------------------------------------------------------------------------
# Portraits (artists & bands)
# ---------------------------------------------------------------------------

def _portrait_prompt(subject, gender, genre, influences, extra=""):
    bits = ["professional promotional portrait photograph", subject]
    if gender:
        bits.append({"female": "a woman", "male": "a man",
                     "androgynous": "an androgynous person"}.get(gender, ""))
    if genre:
        bits.append(f"{genre} musician")
    if extra:
        bits.append(extra)
    if influences:
        bits.append(f"styled like {influences}")
    bits.append("realistic, detailed, studio lighting, sharp focus, no text, no watermark")
    return ", ".join(b for b in bits if b)


def build_artist_portrait_prompt(artist_id):
    a = query("SELECT * FROM artist WHERE id = ?", (artist_id,), one=True)
    if not a:
        raise ValueError("artist not found")
    extra = a["region"] and f"from {a['region']}" or ""
    if a["persona"]:
        extra = (extra + ", " if extra else "") + a["persona"][:160]
    return _portrait_prompt(f'of {a["name"]}', a["vocal"] or "", a["primary_genre"] or "",
                            (a["influences"] or "").strip(), extra)


def build_band_portrait_prompt(band_id):
    b = query("SELECT * FROM band WHERE id = ?", (band_id,), one=True)
    if not b:
        raise ValueError("band not found")
    n = query("SELECT COUNT(*) c FROM membership WHERE band_id=? AND left_on IS NULL",
              (band_id,), one=True)["c"]
    subject = f'group portrait of the band {b["name"]}'
    subject += f', {n} musicians' if n else ''
    return _portrait_prompt(subject, "", b["primary_genre"] or "",
                            (b["influences"] or "").strip())


def generate_artist_portrait(artist_id, prompt=None):
    """Render an artist portrait (txt2img). Stores path + prompt; returns path."""
    if not query("SELECT 1 FROM artist WHERE id=?", (artist_id,), one=True):
        raise ValueError("artist not found")
    prompt = (prompt or "").strip() or build_artist_portrait_prompt(artist_id)
    os.makedirs(COVER_DIR, exist_ok=True)
    out = os.path.join(COVER_DIR, f"artist{artist_id}.png")
    ImageGenClient().generate(prompt, out, seed=artist_id * 13 + 5)
    rel = os.path.relpath(out, ROOT)
    execute("UPDATE artist SET portrait_path=?, portrait_prompt=? WHERE id=?",
            (rel, prompt, artist_id))
    return rel


def _build_member_collage(paths, out_path, cell=384):
    """Tile member portraits into one image (img2img reference). Needs Pillow
    (python3-pil); returns the path, or None if PIL is missing or no images."""
    try:
        from PIL import Image
    except ImportError:
        return None
    import math
    imgs = []
    for p in paths:
        try:
            imgs.append(Image.open(p).convert("RGB"))
        except Exception:
            pass
    if not imgs:
        return None
    cols = math.ceil(math.sqrt(len(imgs)))
    rows = math.ceil(len(imgs) / cols)
    canvas = Image.new("RGB", (cols * cell, rows * cell), (18, 18, 18))
    for i, im in enumerate(imgs):
        r, c = divmod(i, cols)
        canvas.paste(im.resize((cell, cell)), (c * cell, r * cell))
    canvas.save(out_path)
    return out_path


def generate_band_portrait(band_id, prompt=None):
    """Render a band photo. If members have portraits, they're collaged into one
    img2img reference (Pillow) so the band shot is built from their photos."""
    if not query("SELECT 1 FROM band WHERE id=?", (band_id,), one=True):
        raise ValueError("band not found")
    prompt = (prompt or "").strip() or build_band_portrait_prompt(band_id)
    rows = query(
        "SELECT a.portrait_path AS p FROM membership m JOIN artist a ON a.id = m.artist_id"
        " WHERE m.band_id = ? AND m.left_on IS NULL", (band_id,))
    portraits = [pp for pp in (_abs_cover(r["p"]) for r in rows) if pp]
    os.makedirs(COVER_DIR, exist_ok=True)
    init = None
    if len(portraits) >= 2:
        init = _build_member_collage(
            portraits, os.path.join(COVER_DIR, f"band{band_id}_ref.png"))
    if init is None and portraits:
        init = portraits[0]
    out = os.path.join(COVER_DIR, f"band{band_id}.png")
    ImageGenClient().generate(prompt, out, seed=band_id * 11 + 7,
                              init_image=init, strength=0.6)
    rel = os.path.relpath(out, ROOT)
    execute("UPDATE band SET portrait_path=?, portrait_prompt=? WHERE id=?",
            (rel, prompt, band_id))
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

def render_track(track_id, progress=None, cancel=None, candidates=None, seed=None):
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

    # Resolve the performer behind this track (album tracks own via the release;
    # freeform/cover snapshot their influence/vocal at creation instead).
    owner_type = owner_id = None
    if t["release_id"]:
        rel = query("SELECT owner_type, owner_id FROM release WHERE id = ?",
                    (t["release_id"],), one=True)
        if rel:
            owner_type, owner_id = rel["owner_type"], rel["owner_id"]

    # "Sounds like" influence: per-track snapshot (freeform/cover) or the album
    # owner's current influence — injected into the ACE-Step caption.
    influence = (t["influences"] or "").strip() if "influences" in t.keys() else ""
    if not influence and owner_type:
        influence = _owner_influences(owner_type, owner_id)
    if influence:
        tags = f"{tags}, {influence}" if tags else influence

    instrumental = bool(t["instrumental"]) if "instrumental" in t.keys() else False
    # A cover renders audio2audio: the source recording supplies the musical
    # STYLE/structure, but the track's own lyrics are still sung over it. Only an
    # instrumental suppresses lyrics/vocals. If a reference is set but can't be
    # found, fail loudly rather than silently rendering a plain (non-cover) take.
    ref_abs = None
    if t["reference_audio"]:
        ref_abs = reference_music_abspath(t["reference_audio"])
        if not ref_abs:
            raise ValueError(
                f"cover reference not found: {t['reference_audio']} — check the "
                "reference-music path in Admin (rendered as a non-cover otherwise).")

    # Lead-vocal gender wins over any default/LLM guess, except for instrumentals.
    vocal = "" if instrumental else _owner_vocal(owner_type, owner_id)
    if vocal:
        segs = [s for s in (tags.split(", ") if tags else []) if s and not _is_vocal_directive(s)]
        segs.append(f"{vocal} vocal")
        tags = ", ".join(segs)

    if instrumental:
        lyrics = "[Instrumental]"   # no vocals
    else:
        lyrics = _strip_lrc_timestamps(t["lyrics"] or "")   # render-safe if LRC-timed
        if str(settings.get("lyrics_strip_parentheticals", "1")) in ("1", "true", "True", "on"):
            lyrics = _strip_lyric_directives(lyrics)
    duration = t["duration"] or 180

    # Candidate count: explicit arg > per-track override > global setting.
    n_src = candidates
    if n_src is None and "render_candidates" in t.keys():
        n_src = t["render_candidates"]
    if n_src is None:
        n_src = settings.get("acestep_candidates", 3)
    try:
        n = int(n_src)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 8))

    execute("UPDATE track SET status='producing', audio_path=NULL WHERE id=?", (track_id,))
    execute("DELETE FROM candidate WHERE track_id=?", (track_id,))
    # Delete the previous render's audio files so a re-render is verifiably clean
    # (no stale wavs lingering) — the candidate filenames are reused per render.
    for f in glob.glob(os.path.join(AUDIO_DIR, f"track{track_id}_cand*.*")):
        try:
            os.remove(f)
        except OSError:
            pass

    # Cover (audio2audio) controls; ref_abs was resolved above. Per-track values
    # override the global defaults when set.
    def _track_or_setting(col, key):
        v = t[col] if col in t.keys() else None
        return v if v is not None else settings.get(key)
    cover_strength = _track_or_setting("cover_strength", "acestep_cover_strength")
    cover_noise = _track_or_setting("cover_noise", "acestep_cover_noise")

    # Lyric language → ACE-Step vocal_language (track override, else owner default).
    language = (t["language"] or "").strip() if "language" in t.keys() else ""
    language = language or _owner_language(owner_type, owner_id)
    vocal_language = _lang_code(language)

    # Seed: an explicit seed is reproducible; otherwise use a fresh random base
    # each render so re-renders actually vary (ACE-Step is deterministic per seed).
    if seed is not None and str(seed).strip() != "":
        base_seed = int(seed)
    else:
        base_seed = random.randint(1, 2_000_000_000)
    best = None
    for i in range(n):
        if cancel and cancel():
            break
        cand_seed = base_seed + i
        out_path = os.path.join(AUDIO_DIR, f"track{track_id}_cand{i}.{ace.fmt}")
        try:
            ace.generate(tags, lyrics, duration, cand_seed, out_path,
                         reference_audio=ref_abs, cover_strength=cover_strength,
                         cover_noise=cover_noise, vocal_language=vocal_language)
            score, note = integrity_score(out_path, duration)
        except Exception as exc:  # keep going; a bad candidate shouldn't kill the batch
            score, note = 0.0, f"render error: {exc}"
            out_path = None
        cid = execute(
            "INSERT INTO candidate (track_id, seed, integrity, audio_path, note, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (track_id, cand_seed, score, out_path, note, now_iso()),
        )
        if out_path and (best is None or score > best[1]):
            best = (cid, score, out_path, cand_seed)
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
