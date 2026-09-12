"""Request and response models.

Pydantic is used only at the HTTP boundary; everything below works with plain
dicts and database rows.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from . import config

# --- settings ---------------------------------------------------------------


#: Bounds on the collections that reach the database. Not because a local user
#: would send a million ids on purpose, but because an unbounded list turns a
#: typo or a runaway script into a stalled process holding a write lock.
MAX_IMAGES_PER_POST = 200
MAX_TAGS_PER_POST = 30
MAX_POSTS_PER_BATCH = 200

#: CivitAI does not document a title limit; this keeps a pasted document from
#: becoming a post title.
MAX_TITLE = 255
MAX_DETAIL = 20000

ImageIds = Annotated[list[int], Field(max_length=MAX_IMAGES_PER_POST)]
Tags = Annotated[list[str], Field(max_length=MAX_TAGS_PER_POST)]
PostIds = Annotated[list[int], Field(max_length=MAX_POSTS_PER_BATCH)]

PublishMode = Literal["schedule", "now", "draft_only"]
ScheduleMode = Literal["relative", "absolute"]
UsageState = Literal["all", "unused", "planned", "used"]


class SettingsPatch(BaseModel):
    #: Which CivitAI domain is used. Defaults to .red, because .com filters to
    #: SFW in some regions and results are then incomplete.
    site_base: str | None = Field(default=None, max_length=200)
    phash_threshold: int | None = Field(default=None, ge=0, le=32)
    require_title: bool | None = None
    watch_posts_enabled: bool | None = None
    debug_logging: bool | None = None
    usage_ping_enabled: bool | None = None
    #: UI language. English is the source language; anything else is a
    #: translation the frontend applies, so an unknown value simply falls back.
    ui_language: str | None = Field(default=None, max_length=8)
    #: Library grid step, 0 = smallest. Purely a display choice, but it belongs
    #: with the state rather than in one browser's storage.
    ui_grid_size: int | None = Field(default=None, ge=0, le=2)
    ui_page_size: int | None = Field(default=None, ge=10, le=500)
    ui_discover_limit: int | None = Field(
        default=None, ge=1, le=config.DISCOVER_MAX_LIMIT
    )
    #: Zero is the persisted UI choice for "all dates"; the discovery endpoint
    #: receives no ``days`` query parameter for it.
    ui_discover_days: int | None = Field(default=None, ge=0, le=config.DISCOVER_MAX_DAYS)
    rate_limit_posts_per_day: int | None = Field(default=None, ge=1, le=1000)
    llm_python: str | None = None
    llm_model_dir: str | None = None
    llm_device: str | None = None
    llm_load_in_4bit: bool | None = None
    llm_trust_remote_code: bool | None = None
    #: Where a hosted model answers, and which one. A model name here means the
    #: server is the active model; empty means the local checkpoint is.
    llm_endpoint: str | None = Field(default=None, max_length=500)
    llm_endpoint_model: str | None = Field(default=None, max_length=200)
    #: How many suggestions one run produces per field. Three is the ceiling: a
    #: fourth costs another full generation and nobody compares four titles.
    llm_variants: int | None = Field(default=None, ge=1, le=3)
    metadata_excluded_fields: list[str] | None = Field(default=None, max_length=100)
    prompt_exclusions: list[str] | None = Field(default=None, max_length=100)
    trash_folder: str | None = Field(default=None, max_length=4096)
    adopted_folder: str | None = Field(default=None, max_length=4096)
    generation_time_patterns: list[str] | None = Field(default=None, max_length=100)


# --- sources ----------------------------------------------------------------


class SourceCreate(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    label: str = Field(default="", max_length=120)
    #: Target for archiving. Scanned like any other root, never a source for the move.
    is_archive: bool = False
    excluded_folders: Annotated[list[str], Field(max_length=50)] = Field(default_factory=list)


class ModelRootCreate(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    label: str = Field(default="", max_length=120)


class ModelFileAssignment(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


# --- posts ------------------------------------------------------------------


class PostCreate(BaseModel):
    title: str = Field(default="", max_length=MAX_TITLE)
    detail: str = Field(default="", max_length=MAX_DETAIL)
    image_ids: ImageIds = Field(default_factory=list)
    tags: Tags = Field(default_factory=list)
    model_version_id: int | None = Field(default=None, ge=1)
    collection_id: int | None = Field(default=None, ge=1)
    publish_mode: PublishMode = "schedule"
    schedule_mode: ScheduleMode = "relative"
    #: Two years is far past CivitAI's three-month ceiling, which the scheduler
    #: rejects with a clear message - this only stops absurd values.
    schedule_offset_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 730)
    scheduled_at: str | None = None


class PostPatch(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE)
    detail: str | None = Field(default=None, max_length=MAX_DETAIL)
    model_version_id: int | None = Field(default=None, ge=1)
    model_name: str | None = Field(default=None, max_length=MAX_TITLE)
    version_name: str | None = Field(default=None, max_length=MAX_TITLE)
    collection_id: int | None = Field(default=None, ge=1)
    collection_tag_id: int | None = Field(default=None, ge=1)
    publish_mode: PublishMode | None = None
    schedule_mode: ScheduleMode | None = None
    schedule_offset_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 730)
    scheduled_at: str | None = None
    notes: str | None = Field(default=None, max_length=MAX_DETAIL)
    #: Sent as ``null`` explicitly to clear the binding, which the plain
    #: ``model_version_id=None`` default cannot express.
    clear_binding: bool = False


class ArchivePreview(BaseModel):
    """One position-preserving image option in the archived-post list."""

    position: int = Field(ge=0)
    image_id: int | None = Field(default=None, ge=1)
    thumbnail_path: str | None = None
    remote_url: str | None = None


class ImageList(BaseModel):
    image_ids: ImageIds


class ImageOrder(BaseModel):
    post_image_ids: ImageIds


class TagList(BaseModel):
    tags: Tags


class RescheduleRequest(BaseModel):
    scheduled_at: str | None = None
    offset_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 730)


class SpreadRequest(BaseModel):
    post_ids: PostIds
    start: str | None = None
    start_offset_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 730)
    interval_minutes: int = Field(default=60, ge=1, le=60 * 24 * 30)
    apply: bool = False


class PushRequest(BaseModel):
    post_ids: PostIds
    dry_run: bool = False


class DeleteImages(BaseModel):
    """Files to remove from disk for good."""

    image_ids: Annotated[list[int], Field(min_length=1, max_length=500)]
    #: Take the prompt .txt and friends with it. On by default: leaving them
    #: behind creates orphans nobody will ever look at again.
    with_sidecars: bool = True


class TrashImages(BaseModel):
    """Library files to move into the recoverable trash."""

    image_ids: Annotated[list[int], Field(min_length=1, max_length=500)]
    with_sidecars: bool = True


class RestoreImages(BaseModel):
    """Trashed library rows to put back at their original paths."""

    image_ids: Annotated[list[int], Field(min_length=1, max_length=500)]


class ImageSelection(BaseModel):
    """A selection the user has made in the library, to be asked about."""

    image_ids: Annotated[list[int], Field(min_length=1, max_length=500)]


class MoveDataDir(BaseModel):
    """Where the database, thumbnails and backups should live from now on."""

    path: Annotated[str, Field(min_length=1, max_length=1000)]


class DuplicateDismissal(BaseModel):
    image_ids: Annotated[list[int], Field(min_length=2, max_length=20)]

    @field_validator("image_ids")
    @classmethod
    def distinct_image_ids(cls, values: list[int]) -> list[int]:
        unique = list(dict.fromkeys(values))
        if len(unique) < 2:
            raise ValueError("Choose at least two different images.")
        return unique


class ImageEditPut(BaseModel):
    draft: dict[str, Any] = Field(default_factory=dict)
    touched: Annotated[list[str], Field(max_length=500)] = Field(default_factory=list)
    deleted: Annotated[list[str], Field(max_length=500)] = Field(default_factory=list)


class PromptFindReplace(BaseModel):
    find: str = Field(min_length=1, max_length=5000)
    replace: str = Field(default="", max_length=5000)
    target: Literal["prompt", "negative", "both"] = "prompt"
    regex: bool = False
    case_sensitive: bool = False


class BulkEditOperations(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)
    delete_fields: Annotated[list[str], Field(max_length=200)] = Field(default_factory=list)
    restore_fields: Annotated[list[str], Field(max_length=200)] = Field(default_factory=list)
    find_replace: PromptFindReplace | None = None
    prompt_prepend: str | None = Field(default=None, max_length=10000)
    prompt_append: str | None = Field(default=None, max_length=10000)
    negative_append: str | None = Field(default=None, max_length=10000)
    #: LLM shortening proposals, keyed by image id. Applying these is the
    #: explicit accept step; generating suggestions never writes an edit.
    prompt_values: dict[int, str] = Field(default_factory=dict)


class BulkEditRequest(BaseModel):
    image_ids: Annotated[list[int], Field(max_length=500)] = Field(default_factory=list)
    post_id: int | None = Field(default=None, ge=1)
    operations: BulkEditOperations = Field(default_factory=BulkEditOperations)
    apply: bool = False


class ResourceAdd(BaseModel):
    resource_type: Literal["checkpoint", "lora", "embedding"] = "lora"
    name: str = Field(min_length=1, max_length=500)
    weight: float | None = None
    #: Set when the name was picked from the suggestion list, which carries the
    #: hash of the file or the known identity it came from. A name alone is a
    #: guess; a hash is what the resolver can actually act on.
    hash: str | None = Field(default=None, max_length=64, pattern=r"^[0-9a-fA-F]{8,64}$")


class ResourcePatch(BaseModel):
    name: str | None = Field(default=None, max_length=500)
    weight: float | None = None


class ResourceAttach(BaseModel):
    model: dict[str, Any]
    version: dict[str, Any]


class ArchiveRequest(BaseModel):
    #: Empty means "everything the plan yields". Otherwise exactly these images.
    image_ids: ImageIds = Field(default_factory=list)


class AdoptRequest(BaseModel):
    remote_post_id: int = Field(ge=1)


class AdoptBatchRequest(BaseModel):
    remote_post_ids: Annotated[
        list[Annotated[int, Field(ge=1)]],
        Field(min_length=1, max_length=MAX_POSTS_PER_BATCH),
    ]

    @field_validator("remote_post_ids")
    @classmethod
    def remote_post_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("Remote post ids must be unique.")
        return value


DiscoveryState = Literal["draft", "scheduled", "published"]


class DiscoveryItem(BaseModel):
    remote_post_id: int
    title: str | None
    state: DiscoveryState
    published_at: str | None
    image_count: int
    url: str
    cover_url: str | None


class DiscoverySummary(BaseModel):
    examined: int
    outside_period: int
    already_known: int
    adoptable: int


class DiscoveryResponse(BaseModel):
    items: list[DiscoveryItem]
    summary: DiscoverySummary
    reason: str = ""
    since: str | None = None
    next_cursor: str | int | None = None
    range_complete: bool = False
    drafts_not_listable: bool = True
    drafts_url: str | None = None


# --- llm --------------------------------------------------------------------


class SuggestRequest(BaseModel):
    #: No field selection: one generation answers title, description and tags at
    #: once, so asking for a subset only threw two thirds of it away and paid for
    #: another model load to ask again.
    post_ids: PostIds
    profile_id: int | None = Field(default=None, ge=1)
    #: Free-text steer for this one run ("darker", "mention the rain"). Bounded
    #: because it is pasted straight into the prompt.
    hint: str | None = Field(default=None, max_length=1000)
    #: Empty means a fresh random seed per generation, which is the normal case.
    #: A number repeats one particular suggestion - same model, same profile.
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    #: What the suggestion is built from. `auto` follows the rule: a model that
    #: sees works from the pictures alone, one that cannot gets the generation
    #: prompts. The other three override it for one run.
    material: Literal["auto", "images", "prompt", "both"] = "auto"


class ShortenRequest(BaseModel):
    image_ids: Annotated[list[int], Field(min_length=1, max_length=500)]


class ProfileUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    system_prompt: str = Field(min_length=1, max_length=8000)
    title_max_words: int = Field(default=7, ge=1, le=30)
    tag_count: int = Field(default=7, ge=0, le=30)
    include_images: bool = True
    max_images: int = Field(default=1, ge=0, le=8)
    vision_max_side: int = Field(default=768, ge=128, le=2048)
    max_new_tokens: int = Field(default=700, ge=32, le=4096)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.8, ge=0.0, le=1.0)
    top_k: int = Field(default=20, ge=0, le=200)


class ProfilePatch(BaseModel):
    """The same bounds as ``ProfileUpsert``, but every field optional.

    PATCH is partial by definition, so reusing ``ProfileUpsert`` here would have
    demanded a full body and broken every partial edit. What was missing was the
    bounds: an untyped dict let a stray ``{"temperature": "hot"}`` through to the
    REAL column, which ``create`` had been protected from all along.
    """

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    system_prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    title_max_words: int | None = Field(default=None, ge=1, le=30)
    tag_count: int | None = Field(default=None, ge=0, le=30)
    include_images: bool | None = None
    max_images: int | None = Field(default=None, ge=0, le=8)
    vision_max_side: int | None = Field(default=None, ge=128, le=2048)
    max_new_tokens: int | None = Field(default=None, ge=32, le=4096)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0, le=200)
    #: Every key in ``profiles._PATCHABLE`` needs a field here, or a PATCH that
    #: used to work becomes a silent no-op.
    sort_order: int | None = Field(default=None, ge=0)


class Ok(BaseModel):
    ok: bool = True
    detail: dict[str, Any] | None = None


class ArchiveSelection(BaseModel):
    """Explicit post ids to move to ``archived``. State only, no files move."""

    post_ids: list[int] = Field(default_factory=list)


class WatchToggle(BaseModel):
    """Turn a per-folder periodic check on or off."""

    enabled: bool
