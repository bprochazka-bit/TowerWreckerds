# Taxonomy import format

Music World can bulk-import the two taxonomies the generators draw from —
**genres** and **style tags** — from JSON. The same format is accepted by both
the Admin console (paste or upload) and the `manage.py` CLI.

Imports are **upserts keyed by `name`**: a record whose name already exists is
*updated in place*, otherwise it is *added*. This makes import files idempotent
— keep a canonical file in version control and re-apply it any time.

Name matching is **case-insensitive** (and ignores surrounding whitespace), so
`indie rock` updates an existing `Indie Rock` rather than creating a
near-duplicate. The imported record is authoritative: the stored name is set to
the casing in your file. Matching is still exact on punctuation and spacing, so
`Hip Hop`, `Hip-Hop`, and `HipHop` remain distinct entries.

---

## Top-level shape

A file is either a **JSON array** of objects:

```json
[
  { "name": "Shoegaze", "...": "..." },
  { "name": "Dub Techno", "...": "..." }
]
```

…or an **object wrapping the array** under a key (`genres` or `style_tags`):

```json
{ "genres": [ { "name": "Shoegaze" } ] }
```

Both forms are accepted by every importer. Export always emits the bare array.

### List fields

Every field documented as a *list* accepts either:

- a JSON array — `["hazy", "reverb-drenched"]`, or
- a string with comma or semicolon separators — `"hazy, reverb-drenched"`.

The string form is what the Admin text inputs send; both normalise to the same
stored list, with surrounding whitespace trimmed and empty entries dropped.

---

## Genres

| Field                 | Type        | Required | Notes                                              |
|-----------------------|-------------|----------|----------------------------------------------------|
| `name`                | string      | **yes**  | Unique. The upsert key.                            |
| `description`         | string      | no       | Short human description.                           |
| `descriptors`         | list        | no       | Adjectives/feel, e.g. `["hazy", "earnest"]`.       |
| `typical_instruments` | list        | no       | e.g. `["distorted guitar", "bass", "drums"]`.      |
| `tempo_range`         | string      | no       | Free text, e.g. `"100-140 bpm"`.                   |
| `common_regions`      | list        | no       | e.g. `["UK", "US"]`.                               |
| `base_style_tags`     | list        | no       | ACE-Step phrases seeded for this genre.            |

Records without a non-empty `name` are skipped. Any missing field defaults to an
empty string / empty list.

### Example

```json
[
  {
    "name": "Shoegaze",
    "description": "Dense, guitar-washed dream rock.",
    "descriptors": ["hazy", "wall of sound", "reverb-drenched"],
    "typical_instruments": ["distorted guitar", "bass", "drums"],
    "tempo_range": "100-140 bpm",
    "common_regions": ["UK"],
    "base_style_tags": ["shoegaze", "wall of guitar", "ethereal vocals"]
  },
  {
    "name": "Afrobeat",
    "descriptors": "polyrhythmic; horn-driven",
    "common_regions": "Nigeria, Ghana"
  }
]
```

The second record shows the string form for list fields.

---

## Style tags

| Field             | Type   | Required | Notes                                                                 |
|-------------------|--------|----------|-----------------------------------------------------------------------|
| `name`            | string | **yes**  | Unique. The upsert key.                                               |
| `category`        | string | no       | One of `mood`, `instrumentation`, `era`, `production`, `crossover`, `vocal`. Defaults to `mood`. |
| `acestep_phrases` | list   | no       | What the tag expands to at render time. Defaults to `[name]` if omitted. |

### Example

```json
[
  {
    "name": "shoegaze wash",
    "category": "production",
    "acestep_phrases": ["wall of guitar", "reverb-drenched", "blurred mix"]
  },
  {
    "name": "baritone vocal",
    "category": "vocal",
    "acestep_phrases": "deep male vocal, baritone"
  }
]
```

---

## Using it

### Admin console

In **Admin → Taxonomy**, each panel has a **Bulk import (JSON)** box: paste an
array or choose a `.json` file, then **Import**. The flash message reports how
many records were added, updated, or skipped. Click any genre or style tag in
the list to open its edit page.

### CLI (`manage.py`)

```bash
# Import (file, or - to read stdin)
python3 manage.py import-genres genres.json
cat genres.json | python3 manage.py import-genres -
python3 manage.py import-style-tags tags.json

# Export the current set (doubles as an import template)
python3 manage.py export-genres -o genres.json
python3 manage.py export-style-tags -o tags.json

# List
python3 manage.py list-genres
python3 manage.py list-style-tags
```

Export round-trips with import, so the quickest way to get a correct template is
to export what you already have, edit it, and import it back.
