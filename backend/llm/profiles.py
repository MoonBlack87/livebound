"""Prompt profiles.

The split is deliberate: a profile holds the *creative* choices - system prompt,
how long a title may be, how many tags, the sampling parameters. Infrastructure
(interpreter, model directory, device) stays in the global settings, because it
is a property of the machine rather than of the style you want.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from .. import db

#: Identical in every profile, word for word. CivitAI shows a post on two
#: levels: the direct image view, where title, description and tags are not
#: visible at all, and the post page, where the prose is read. Tags are the only
#: part that works on both, because they carry discovery - so the voice varies
#: and the tag rules do not.
_TAG_RULES = (
    "Supply exactly the number of distinct lowercase tags the request asks for. "
    "They are search terms, not decoration: visible subjects, named characters or "
    "franchises the pictures actually support, setting, medium, mood, and "
    "prominent motifs. Prefer names already established as CivitAI tags over "
    "newly coined wording. Never use a checkpoint name, LoRA name, hash, version "
    "label, trigger token, username, or an empty quality word such as masterpiece "
    "or best quality. Separate words with spaces, never underscores. Never assign "
    "a character identity the pictures do not support."
)

#: The same sentence in every voice. Nothing downstream fixes casing - a model
#: told only "sentence case" lowercases "Mortal Kombat", and a model told
#: nothing writes Every Word In Title Case.
_CASE_RULE = (
    "Use ordinary sentence case: begin with a capital letter, keep the "
    "established capitals in names and franchises, and never capitalise every "
    "word as Title Case."
)

#: A post is one thing, not a stack of pictures. Every profile says so, because
#: a model given a contact sheet otherwise walks it frame by frame.
_CLOSING = (
    "Record mature material plainly and without moralising.\n\n"
    "Write one description for the whole post, never a picture-by-picture list "
    "and never frames joined with 'while' or 'and'."
)

#: The four voices the application ships. They differ in what they do with a
#: title and a description; the tag block is shared.
#:
#: The temperature spread is deliberate and measured against Qwen3.5-4B: Plain
#: trades variation for JSON reliability at 0.35, Storyteller accepts the most
#: at 0.9. `max_new_tokens` follows the length each voice is asked for. Do not
#: flatten either.
#:
#: The UUID is the identity and the only thing that must not change: it survives
#: a rename, a reorder, a delete-and-restore, and one day an import from someone
#: else's installation. The names are data, not interface - they are not
#: translated, the user may rename them, and `restore_builtins` matches on the
#: UUID, never on the name.
BUILTINS: list[dict[str, Any]] = [
    {
        "name": "Plain",
        "uuid": "0ee3c247-3107-40b6-b58e-65f56be81a25",
        "description": "Names what is visibly there. No interpretation.",
        "system_prompt": (
            "You catalogue image posts for viewers who want to know what a post "
            "contains at a glance.\n\n"
            "TITLE: name the visible subject or action, factually. No metaphor, no "
            "mood, no invented drama. Respect the maximum word count and count "
            "before answering. " + _CASE_RULE + "\n\n"
            "DESCRIPTION: one to three restrained sentences stating what is "
            "visibly present across the set. Only adjectives that add observable "
            "information.\n\n" + _TAG_RULES + "\n\n" + _CLOSING
        ),
        "temperature": 0.35,
        "max_new_tokens": 500,
        "sort_order": 0,
    },
    {
        "name": "Standard",
        "uuid": "23cde510-5f59-4040-8b7d-17f39db3e786",
        "description": "Plain, with one pointed hook. The everyday choice.",
        "system_prompt": (
            "You write the headline for an image post: accurate first, "
            "interesting second.\n\n"
            "TITLE: name the subject, then sharpen it with one concrete visible "
            "detail - a colour, a gesture, a material, a light. The detail is "
            "required, not optional. A title that is only a list of names or "
            "nouns is wrong here; that is what the plain catalogue does. One "
            "hook, not a mood piece. Respect the maximum word count and count "
            "before answering. " + _CASE_RULE + "\n\n"
            "DESCRIPTION: one to three clear sentences. Say what the set is and "
            "what holds it together. You may name the atmosphere in a single "
            "clause, but every sentence must still carry something a viewer can "
            "see.\n\n" + _TAG_RULES + "\n\n" + _CLOSING
        ),
        "temperature": 0.55,
        "max_new_tokens": 500,
        "sort_order": 1,
    },
    {
        "name": "Narrative",
        "uuid": "8a9bfa8f-77ef-4fe7-99f8-d241d4f360a8",
        "description": "Reads between the lines: what the pictures imply.",
        "system_prompt": (
            "You write about what a set of pictures implies - the situation "
            "around the visible moment, not a retelling of it.\n\n"
            "TITLE: a scene title with an active verb, three to five words, "
            "naming the tension or transformation the set shares. Never a noun "
            "list. Never end on and, or, of, to, with. Count before answering. "
            + _CASE_RULE + "\n\n"
            "DESCRIPTION: two to four sentences. Say what the pictures suggest "
            "beyond their surface - what has just happened, what is held back, "
            "what the composition is withholding. Every claim must be anchored in "
            "something visible: name the detail that carries the implication. "
            "Suggest; do not narrate events that are not there, and do not simply "
            "list what is present.\n\n" + _TAG_RULES + "\n\n" + _CLOSING
        ),
        "temperature": 0.85,
        "max_new_tokens": 700,
        "sort_order": 2,
    },
    {
        "name": "Storyteller",
        "uuid": "c9507ba1-0ea6-4c46-9e21-fca09074557c",
        "description": "A short scene with a before and an after.",
        "system_prompt": (
            "You write a very short story from what a set of pictures shows - one "
            "scene, with a moment before it and a moment after it.\n\n"
            "TITLE: title it the way a short story is titled - three to six words, "
            "evocative and specific to this set. Count before answering. "
            + _CASE_RULE + "\n\n"
            "DESCRIPTION: at least four sentences and at most seven - count them "
            "before you answer, and if you have fewer than four, keep writing. "
            "Continuous prose. Open in the moment the pictures show, give it a "
            "cause and a consequence, and close on an image rather than a "
            "summary. Write it as one passage, not as notes. Everything you "
            "invent must be consistent with what is visible: the setting, the "
            "figures, the light and the mood are given, the story between them is "
            "yours. Do not invent a named character, a franchise or a place the "
            "pictures do not support.\n\n" + _TAG_RULES + "\n\n" + _CLOSING
        ),
        "temperature": 0.9,
        "max_new_tokens": 900,
        "sort_order": 3,
    },
]

#: Every column a seed owns, so "restore" means every shipped value and the
#: confirmation can say so. What `BUILTINS` does not name comes from here, and
#: `create` uses the same dict for its fallbacks - one copy of the measured
#: numbers, not a second one that can drift away from it (task `104`).
PROFILE_DEFAULTS: dict[str, Any] = {
    "description": "",
    "title_max_words": 7,
    "tag_count": 7,
    "include_images": 1,
    "max_images": 1,
    "vision_max_side": 768,
    "max_new_tokens": 700,
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "sort_order": 0,
}

_SEED_COLUMNS = ("uuid", "name", "system_prompt", *PROFILE_DEFAULTS)

_BUILTIN_UUIDS = frozenset(seed["uuid"] for seed in BUILTINS)


def is_builtin(profile_uuid: str) -> bool:
    """Whether a row is one of the four the application ships.

    Derived rather than stored. A column beside the UUID would be a second
    answer to the same question, and task `105` is what two answers cost: the
    name said one thing, the identity another.
    """
    return profile_uuid in _BUILTIN_UUIDS


def ensure_builtins() -> None:
    """Seed the built-in profiles once.

    Guarded on the row count rather than on identity, so later edits and
    deletions are never silently undone on the next start. Getting them back is
    deliberate: `restore_builtins`.
    """
    conn = db.get_connection()
    if conn.execute("SELECT COUNT(*) AS n FROM llm_profiles").fetchone()["n"]:
        return
    with db.transaction() as tx:
        for profile in BUILTINS:
            _write_seed(tx, profile, row_id=None)


def restore_builtins() -> list[str]:
    """Put the four shipped profiles back, and return the names touched.

    The row is found by UUID, so a renamed, reordered or re-created built-in is
    still the same profile and a profile the user made can never be mistaken for
    one. A seed with no row is inserted; a seed with one is reset to every value
    it ships with, its name included.

    Nothing can collide: since the name stopped being unique it is only a label,
    and the predecessor's clash check refused restores in which no user profile
    was involved - two built-ins whose names had been swapped were enough.
    """
    with db.transaction() as tx:
        rows = {
            row["uuid"]: row["id"]
            for row in tx.execute("SELECT id, uuid FROM llm_profiles")
        }
        for seed in BUILTINS:
            _write_seed(tx, seed, row_id=rows.get(seed["uuid"]))
    return [seed["name"] for seed in BUILTINS]


def _write_seed(tx: sqlite3.Connection, seed: dict[str, Any], *, row_id: int | None) -> None:
    """Insert a seed, or reset an existing row to it. Same columns either way."""
    values = [seed.get(column, PROFILE_DEFAULTS.get(column)) for column in _SEED_COLUMNS]
    if row_id is None:
        columns = ", ".join(_SEED_COLUMNS)
        marks = ", ".join("?" for _ in _SEED_COLUMNS)
        tx.execute(
            f"INSERT INTO llm_profiles({columns}, created_at) VALUES({marks}, ?)",
            [*values, db.now_iso()],
        )
        return
    sets = ", ".join(f"{column}=?" for column in _SEED_COLUMNS)
    tx.execute(f"UPDATE llm_profiles SET {sets} WHERE id=?", [*values, row_id])


def list_profiles() -> list[dict[str, Any]]:
    ensure_builtins()
    rows = db.get_connection().execute(
        "SELECT * FROM llm_profiles ORDER BY sort_order, id"
    )
    return [_row(row) for row in rows]


def get(profile_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM llm_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    return _row(row) if row else None


def seed_system_prompt(profile_id: int) -> str | None:
    """Return a built-in profile's own seed without changing the stored row.

    Matched on the UUID. The predecessor tried the name, then fell back to
    `sort_order` - a column the user can patch, whose meaning shifted the moment
    a fourth voice was added, so a renamed built-in silently got its neighbour's
    prompt.
    """
    row = db.get_connection().execute(
        "SELECT uuid FROM llm_profiles WHERE id=?", (profile_id,)
    ).fetchone()
    if row is None:
        return None
    for seed in BUILTINS:
        if seed["uuid"] == row["uuid"]:
            return str(seed["system_prompt"])
    return None


def resolve(profile_id: int | None) -> dict[str, Any]:
    """The profile to use: the one asked for, the default, or the first."""
    ensure_builtins()
    if profile_id:
        found = get(profile_id)
        if found:
            return found
    stored = db.get_setting("llm_default_profile_id")
    if stored:
        found = get(int(stored))
        if found:
            return found
    profiles = list_profiles()
    if not profiles:
        raise RuntimeError("No model profile exists.")
    return profiles[0]


class LastProfileError(Exception):
    """Deleting the only remaining profile would disable suggestions."""


def create(payload: dict[str, Any]) -> dict[str, Any]:
    """A profile the user makes. It gets a UUID like any other."""
    columns = (
        "uuid",
        "name",
        "system_prompt",
        *(key for key in PROFILE_DEFAULTS if key != "sort_order"),
    )
    values = [str(uuid.uuid4()), payload["name"], payload["system_prompt"]]
    for column in PROFILE_DEFAULTS:
        if column == "sort_order":
            continue
        given = payload.get(column, PROFILE_DEFAULTS[column])
        # The only column the caller hands over as a bool; SQLite wants 0/1.
        values.append((1 if given else 0) if column == "include_images" else given)

    marks = ", ".join("?" for _ in columns)
    with db.transaction() as conn:
        cursor = conn.execute(
            f"INSERT INTO llm_profiles({', '.join(columns)}, sort_order, created_at)"
            f" VALUES({marks},"
            " (SELECT COALESCE(MAX(sort_order) + 1, 0) FROM llm_profiles), ?)",
            [*values, db.now_iso()],
        )
        profile_id = int(cursor.lastrowid)
    return get(profile_id) or {}


_PATCHABLE = {
    "name",
    "description",
    "system_prompt",
    "title_max_words",
    "tag_count",
    "include_images",
    "max_images",
    "vision_max_side",
    "max_new_tokens",
    "temperature",
    "top_p",
    "top_k",
    "sort_order",
}


def update(profile_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    fields = {
        key: value
        for key, value in payload.items()
        if key in _PATCHABLE and value is not None
    }
    if "include_images" in fields:
        fields["include_images"] = 1 if fields["include_images"] else 0
    if not fields:
        return get(profile_id)
    sets = ", ".join(f"{key}=?" for key in fields)
    with db.transaction() as conn:
        conn.execute(
            f"UPDATE llm_profiles SET {sets} WHERE id=?", [*fields.values(), profile_id]
        )
    return get(profile_id)


def delete(profile_id: int) -> bool:
    """Delete a profile, but never the last usable one."""
    with db.transaction() as conn:
        found = conn.execute(
            "SELECT 1 FROM llm_profiles WHERE id=?", (profile_id,)
        ).fetchone()
        if not found:
            return False
        count = conn.execute("SELECT COUNT(*) AS n FROM llm_profiles").fetchone()["n"]
        if count == 1:
            raise LastProfileError
        conn.execute("DELETE FROM llm_profiles WHERE id=?", (profile_id,))
        conn.execute(
            "DELETE FROM settings WHERE key='llm_default_profile_id' AND value=?",
            (str(profile_id),),
        )
    return True


def set_default(profile_id: int) -> bool:
    if get(profile_id) is None:
        return False
    db.set_setting("llm_default_profile_id", str(profile_id))
    return True


def _row(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["include_images"] = bool(value.get("include_images"))
    value["is_builtin"] = is_builtin(str(value.get("uuid") or ""))
    return value
