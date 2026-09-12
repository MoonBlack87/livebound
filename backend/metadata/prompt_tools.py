"""Small, predictable transformations for generation prompts.

The language-model cleaner deliberately lives here too: shortening uses the
same syntax removal as the former metadata application, while applying a
shortening suggestion stays an explicit user action.
"""

from __future__ import annotations

import re

PLACEHOLDER_TOKEN = "CAPTION_HERE"
DEFAULT_EXCLUDE_TAGS = (
    "masterpiece, best quality, amazing quality, very aesthetic, absurdres, newest, highres, "
    "perspective, dynamic perspective, exaggerated perspective, foreshortening, "
    "ultra high textures, razor sharp execution, high resolution, HDR, best aesthetics"
)
DEFAULT_SYSTEM_PROMPT = (
    "You condense Stable Diffusion image prompts.\n"
    "Given a long, tag-heavy prompt, write ONE short English sentence describing the scene the "
    "image shows: the subject, what it is doing, and where.\n"
    "Keep it under 25 words. Drop quality boilerplate, style jargon, camera and lighting terms, "
    "artist names and technical parameters.\n"
    "Return only that one sentence - no quotation marks, no prefix, no commentary."
)


def parse_tag_list(text: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in re.split(r"[,;\n\r]+", text or ""):
        token = part.strip().lower()
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def clean_for_shortening(prompt: str, exclude_tags: list[str] | None = None) -> str:
    """Remove generator syntax and quality boilerplate before LLM shortening."""
    text = re.sub(r"\bBREAK\b", " ", prompt or "", flags=re.IGNORECASE)
    text = text.replace(PLACEHOLDER_TOKEN, " ")
    text = re.sub(r"<[^>]*>", " ", text)
    for char in "()[]{}":
        text = text.replace(char, " ")
    text = re.sub(r":\s*[0-9]+(?:\.[0-9]+)?", " ", text)
    text = re.sub(r"\b[0-9a-f]{8,16}\b", " ", text, flags=re.IGNORECASE)
    excluded = set(exclude_tags or [])
    tags = [part.strip() for part in re.split(r"[,;]+", text) if part.strip()]
    tags = [tag for tag in tags if tag.lower() not in excluded]
    return re.sub(r"\s+", " ", ", ".join(tags)).strip(" ,")


def clean_shortening_result(text: str) -> str:
    result = (text or "").strip()
    result = re.sub(
        r"^(?:here (?:is|'s)[^:]{0,80}:|(?:description|prompt|answer|sentence)\s*:)\s*",
        "",
        result,
        flags=re.IGNORECASE,
    ).strip()
    result = result.strip('"“”‘’\'').strip().split("\n\n")[0]
    return re.sub(r"\s+", " ", result).strip()


def shortening_request(prompt: str) -> str:
    return f"Prompt to condense:\n{prompt}\n\nOne-sentence description:"


def find_replace(
    text: str | None,
    find: str,
    replace: str,
    *,
    regex: bool = False,
    case_sensitive: bool = False,
) -> str | None:
    if text is None or not find:
        return text
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = find if regex else re.escape(find)
    return re.sub(pattern, lambda _match: replace, text, flags=flags)


def exclude_matches(text: str | None, expressions: list[str]) -> str | None:
    """Remove each configured regular-expression match from one prompt value."""
    result = text
    for expression in expressions:
        result = find_replace(result, expression, "", regex=True)
    return result


def join_prompt(base: str, prepend: str | None = None, append: str | None = None) -> str:
    parts = [
        (prepend or "").strip(" ,"),
        (base or "").strip(" ,"),
        (append or "").strip(" ,"),
    ]
    return ", ".join(part for part in parts if part)
