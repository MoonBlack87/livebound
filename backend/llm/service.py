"""Managing the model subprocess and running suggestion batches.

The worker lives under a different Python interpreter - one that has torch -
so the app's own environment stays free of a multi-gigabyte ML stack. They talk
over line-delimited JSON on stdin/stdout.

The model is loaded once per batch and released when the batch ends. That is not
an optimisation but a requirement: the GPU is shared with image generation, and
holding several gigabytes indefinitely would stop that from working.
"""

from __future__ import annotations

import json
import queue
import random
import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from .. import config, db, jobs
from ..metadata import editing as metadata_editing
from ..metadata import prompt_tools
from ..store import images as image_store
from ..store import posts as post_store
from . import endpoint as endpoint_client
from . import profiles, prompts

WORKER_PATH = Path(__file__).resolve().parent / "worker.py"

#: Where accelerate may spill a model that fits neither the GPU nor RAM. Emptied
#: before every load and removed when the worker stops - the files inside are
#: worthless once the process that mapped them is gone.
OFFLOAD_DIRNAME = "llm-offload"

STARTUP_TIMEOUT = 600.0
REQUEST_TIMEOUT = 300.0

#: Importing torch takes seconds, so the probe gets a generous budget.
_PROBE_TIMEOUT = 90
#: A short wait keeps cancellation responsive without waking the job thread constantly.
_GENERATION_WAIT_SLICE = 0.05

_detect_lock = threading.Lock()
_detected: str | None = None


class LlmError(RuntimeError):
    pass


def configured_python() -> str:
    return db.get_setting("llm_python") or ""


def configured_model_dir() -> str:
    return db.get_setting("llm_model_dir") or ""


def configured_endpoint() -> str:
    return db.get_setting("llm_endpoint") or ""


def configured_endpoint_model() -> str:
    return db.get_setting("llm_endpoint_model") or ""


def trust_remote_code() -> bool:
    """Whether the worker may run Python shipped inside the model directory."""
    return db.get_setting("llm_trust_remote_code") == "1"


def _has_torch(candidate: str) -> bool:
    try:
        result = subprocess.run(
            [candidate, "-c", "import torch"],
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            check=False,  # a non-zero exit is the expected "no torch" answer
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _own_python() -> Path:
    environment = config.project_root() / ".venv-llm"
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def detect_python() -> str | None:
    """First interpreter that can import torch. Memoised - the probe is slow.

    Three candidates, and no guessing beyond them: the environment `webui.sh
    --llm` creates next to the application, this app's own interpreter, and
    whatever `python3` resolves to. `.venv-llm` is first because it is the one
    place the project itself puts a torch environment - it is found relative to
    the checkout, so it stays correct wherever that is. An environment belonging
    to some other installation on this machine is the maintainer's to name in
    the settings, not ours to go looking for.
    """
    global _detected
    configured = configured_python()
    if configured:
        return configured
    with _detect_lock:
        if _detected is not None:
            return _detected or None
        own = _own_python()
        for candidate in (str(own) if own.exists() else None, sys.executable,
                          shutil.which("python3")):
            if not candidate:
                continue
            if _has_torch(candidate):
                _detected = candidate
                return candidate
        _detected = ""
        return None


def status() -> dict[str, Any]:
    python = configured_python() or detect_python()
    model_dir = configured_model_dir()
    python_exists = bool(python) and Path(python).exists()
    # Path("") is the current directory, which is a directory - so an unset
    # model folder must be answered before asking the filesystem anything.
    model_dir_exists = bool(model_dir) and Path(model_dir).is_dir()
    endpoint_model = configured_endpoint_model()
    # One active model, and the choice is explicit: a picked Ollama model means
    # the server answers, an empty one means the local checkpoint does. Guessing
    # from the shape of a string would turn a mistyped path into a model name.
    uses_endpoint = bool(endpoint_model)
    running = _worker is not None and _worker.alive
    if running and _worker:
        info = _worker.info
    elif uses_endpoint:
        # What a hosted model can do is only known once the server has been
        # asked, and a batch releases it again - so the answer is remembered the
        # same way the local worker's is, or "no vision" would be visible only
        # while a run is in flight.
        remembered = db.get_json_setting("llm_endpoint_last_ready") or {}
        fits = (
            remembered.get("endpoint") == (configured_endpoint() or endpoint_client.DEFAULT_URL)
            and remembered.get("model") == endpoint_model
        )
        info = remembered if fits else {}
    else:
        remembered = db.get_json_setting("llm_last_ready") or {}
        # What was remembered describes one interpreter and one model folder.
        # Once either changes it says nothing about the setup in front of the
        # user - and presenting it as the present state sent the maintainer
        # after a missing Pillow in an environment that was no longer even
        # configured.
        fits = (
            remembered.get("python") == python
            and remembered.get("model_dir") == model_dir
        )
        info = remembered if fits else {}
    return {
        "python": python,
        "python_exists": python_exists,
        "model_dir": model_dir,
        "model_dir_exists": model_dir_exists,
        "device": db.get_setting("llm_device") or "auto",
        # what the configured "auto" actually resolved to on the last load
        "resolved_device": info.get("device"),
        "load_in_4bit": db.get_setting("llm_load_in_4bit") != "0",
        "trust_remote_code": trust_remote_code(),
        "worker_running": running,
        "endpoint": configured_endpoint() or endpoint_client.DEFAULT_URL,
        "endpoint_model": endpoint_model,
        "runtime": "endpoint" if uses_endpoint else "local",
        "ready": uses_endpoint or (python_exists and model_dir_exists),
        "vision": info.get("vision"),
        "vision_error": info.get("vision_error"),
        # A model that did not fit the card runs, but slowly. The job line says
        # so while it runs; this is what still says so afterwards.
        "offloaded": info.get("offloaded"),
        # A venv made by `uv venv` has no pip, so "pip install ..." silently does
        # nothing there - or lands in a different environment entirely. Naming
        # the interpreter explicitly is the only advice that reliably works.
        "vision_fix_command": (
            f"uv pip install --python {python} pillow torchvision" if python else ""
        ),
    }


class Worker:
    """One live model process."""

    def __init__(self, python: str, options: dict[str, Any]):
        self._lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=40)
        self._next_id = 0
        self.info: dict[str, Any] = {}

        self._process = subprocess.Popen(
            [python, str(WORKER_PATH), json.dumps(options, ensure_ascii=False)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        try:
            message = self._responses.get(timeout=STARTUP_TIMEOUT)
        except queue.Empty:
            self.stop()
            raise LlmError(
                f"The worker was not ready after {STARTUP_TIMEOUT:.0f}s."
            ) from None

        if message.get("event") != "ready":
            detail = message.get("message") or "unknown error"
            self.stop()
            raise LlmError(f"The model could not be loaded: {detail}")
        self.info = message
        # Kept so the settings page can report it even after the model was
        # released - otherwise "no vision" would only ever be visible mid-run.
        db.set_json_setting(
            "llm_last_ready",
            {
                "vision": message.get("vision"),
                "vision_error": message.get("vision_error"),
                "device": message.get("device"),
                # A model that did not fit the card runs, but slowly. Remembering
                # that is what answers "why is this suddenly taking minutes".
                "offloaded": message.get("offloaded"),
                "device_map": message.get("device_map"),
                # Which setup this describes, so a later status can tell whether
                # it still applies.
                "python": python,
                "model_dir": options.get("model_dir"),
                "at": db.now_iso(),
            },
        )

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def stderr_tail(self) -> str:
        return " | ".join(list(self._stderr)[-8:])

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._responses.put(json.loads(line))
            except ValueError:
                self._stderr.append(f"unreadable worker output: {line[:200]}")
        self._responses.put({"event": "closed"})

    def _read_stderr(self) -> None:
        assert self._process.stderr is not None
        for line in self._process.stderr:
            self._stderr.append(line.rstrip())

    def generate(self, request: dict[str, Any]) -> str:
        """One request at a time - the model is a single shared resource."""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {**request, "id": request_id}
            try:
                assert self._process.stdin is not None
                self._process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LlmError(
                    f"The model worker is gone: {exc}. {self.stderr_tail()}"
                ) from exc

            while True:
                try:
                    message = self._responses.get(timeout=REQUEST_TIMEOUT)
                except queue.Empty:
                    raise LlmError(
                        f"No answer within {REQUEST_TIMEOUT:.0f}s."
                    ) from None
                if message.get("event") == "closed":
                    raise LlmError(
                        f"The model worker stopped unexpectedly. {self.stderr_tail()}"
                    )
                if message.get("event"):
                    continue
                if message.get("id") != request_id:
                    continue
                if message.get("error"):
                    raise LlmError(message["error"])
                return message.get("text") or ""

    def stop(self) -> None:
        if self._process.poll() is not None:
            return
        try:
            if self._process.stdin:
                self._process.stdin.write(json.dumps({"command": "shutdown"}) + "\n")
                self._process.stdin.flush()
                self._process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=10)


_worker: Worker | endpoint_client.Endpoint | None = None
_worker_lock = threading.Lock()


def start_worker() -> Worker | endpoint_client.Endpoint:
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.alive:
            return _worker
        state = status()
        if state["runtime"] == "endpoint":
            # No subprocess, no model load, no offload folder: the server has the
            # model already or fetches it itself.
            _worker = endpoint_client.Endpoint(state["endpoint"], state["endpoint_model"])
            _remember_endpoint(_worker)
            return _worker
        if not state["python"]:
            raise LlmError(
                "No interpreter with torch is configured or discoverable. Set the LLM"
                " interpreter in the settings to a Python that has torch."
            )
        if not state["python_exists"]:
            raise LlmError(
                f"The configured LLM interpreter does not exist: {state['python']}."
                " Correct it in the settings."
            )
        if not state["model_dir"]:
            raise LlmError(
                "No model is configured. Set the LLM model folder in the settings"
                " to a local model directory, or pick an Ollama model."
            )
        if not state["model_dir_exists"]:
            raise LlmError(f"Model folder not found: {state['model_dir']}")

        _worker = Worker(
            state["python"],
            {
                "model_dir": state["model_dir"],
                "device": state["device"],
                "load_in_4bit": state["load_in_4bit"],
                "trust_remote_code": state["trust_remote_code"],
                "offload_folder": str(_prepare_offload_dir()),
            },
        )
        return _worker


def _remember_endpoint(hosted: endpoint_client.Endpoint) -> None:
    """Keep what the server said, so the settings page can still say it later."""
    db.set_json_setting(
        "llm_endpoint_last_ready",
        {
            "endpoint": hosted.url,
            "model": hosted.model,
            "device": hosted.info.get("device"),
            "vision": hosted.vision,
            "vision_error": hosted.vision_error,
            "at": db.now_iso(),
        },
    )


def _offload_dir() -> Path:
    return config.data_dir() / OFFLOAD_DIRNAME


def _prepare_offload_dir() -> Path:
    directory = _offload_dir()
    shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def stop_worker(*, only: "Worker | endpoint_client.Endpoint | None" = None) -> None:
    global _worker
    with _worker_lock:
        if only is not None and _worker is not only:
            # A later run took the worker over. Stopping it now would pull the
            # model out from under a run that is using it.
            return
        if _worker is not None:
            if isinstance(_worker, endpoint_client.Endpoint):
                # The run may have discovered mid-batch that the model cannot
                # see; that is worth keeping, not only worth logging.
                _remember_endpoint(_worker)
            _worker.stop()
            _worker = None
        shutil.rmtree(_offload_dir(), ignore_errors=True)


def device_label(worker: Worker | endpoint_client.Endpoint) -> str:
    """What to show while the model works, including that it did not fit."""
    device = worker.info.get("device")
    if worker.info.get("offloaded"):
        return f"{device}, partly on the CPU"
    return str(device)


def suggestion_variants() -> int:
    """How many suggestions one run produces per field, 1 to 3."""
    return max(1, min(3, db.int_setting("llm_variants", 1)))


def _generate_interruptibly(
    job: jobs.Job, worker: Worker | endpoint_client.Endpoint, request: dict[str, Any]
) -> str | None:
    """Wait for one adapter generation, unless the job is cancelled."""
    answer: queue.Queue[str | Exception] = queue.Queue(maxsize=1)

    def generate() -> None:
        try:
            answer.put(worker.generate(request))
        except Exception as exc:
            # A generation is one batch item. Its exception must return to the
            # existing per-item handler, which records and classifies it.
            answer.put(exc)

    # Neither adapter can stop its current request. On cancellation this daemon
    # keeps running and its answer is dropped, so the model can remain busy and
    # a run started immediately afterwards can queue behind it.
    threading.Thread(target=generate, daemon=True).start()
    while True:
        try:
            result = answer.get(timeout=_GENERATION_WAIT_SLICE)
        except queue.Empty:
            if job.should_stop():
                return None
            continue
        if isinstance(result, Exception):
            raise result
        return result


def _release(
    worker: "Worker | endpoint_client.Endpoint", *, abandoned: bool
) -> None:
    """Give the model back - on another thread when a generation was abandoned.

    Stopping waits for the call that is still running: the local worker sends
    `shutdown` and waits up to 30 s before killing it, and the hosted one queues
    `keep_alive: 0` behind the request in flight. Doing that here would hand the
    cancel back exactly the wait it just avoided. Not doing it at all would leave
    the model resident on a GPU that image generation shares, which is what
    `stop_worker` exists to prevent. So it happens on its own thread, and only
    while no later run has taken the worker over.
    """
    if not abandoned:
        stop_worker()
        return
    threading.Thread(target=lambda: stop_worker(only=worker), daemon=True).start()


def suggest(
    job: jobs.Job,
    post_ids: list[int],
    profile_id: int | None,
    hint: str | None = None,
    seed: int | None = None,
    material: str = "auto",
) -> dict:
    """Generate title, description and tag suggestions for a batch of posts.

    One generation answers all three fields, so there is nothing to ask for
    separately. Several variants are several generations against the model that
    is already loaded - the load, not the generation, is what costs the minute.
    """
    profile = profiles.resolve(profile_id)
    # A given seed means "produce that one suggestion again". Running it three
    # times would produce the same text three times, drop two of them as
    # duplicates and charge the user three generations for one row.
    variants = 1 if seed is not None else suggestion_variants()
    job.total = len(post_ids) * variants
    results: list[dict[str, Any]] = []

    job.stage_code = "loading"
    job.stage = "Loading the model"
    worker = start_worker()
    generation_abandoned = False
    try:
        job.stage_code = "generating"
        job.stage = f"Generating suggestions on {device_label(worker)}"
        failed_posts: set[int] = set()
        for post_id in post_ids:
            # Two variants that come out word for word the same are one
            # suggestion, and offering it twice only costs the user a read.
            seen: set[tuple[str, str]] = set()
            for _variant in range(variants):
                if job.should_stop():
                    break
                try:
                    # No seed from the caller means a random one - but drawn here
                    # rather than inside torch or llama.cpp, because a number
                    # nobody chose cannot be reported back afterwards. Each
                    # variant draws its own, or the alternatives would be one
                    # answer three times.
                    result = _suggest_one(
                        job, worker, post_id, profile, hint, seen,
                        seed=seed if seed is not None else random.randrange(2**31),
                        material=material,
                    )
                    if result is None:
                        generation_abandoned = True
                        break
                    if result:
                        results.append(result)
                        job.succeeded += 1
                        job.log(post_id=post_id, status="ok", title=result.get("title"))
                except Exception as exc:
                    job.failed += 1
                    failed_posts.add(post_id)
                    job.log(post_id=post_id, status="error", message=str(exc))
                    # A model that fails on everything is broken, not unlucky -
                    # and "everything" means three posts, not three variants of
                    # the same one, which would abort a batch over one bad post.
                    if len(failed_posts) >= 3 and job.succeeded == 0:
                        job.error = str(exc)
                        break
                finally:
                    job.processed += 1
            if job.should_stop() or job.error:
                break
    finally:
        # Always give the GPU back - image generation shares it. After an
        # abandoned call that has to happen off this thread; see `_release`.
        job.stage_code = "releasing"
        job.stage = "Releasing the model"
        _release(worker, abandoned=generation_abandoned)
        job.stage_code = ""
        job.stage = "Done"

    job.result = {"items": results}
    return job.result


#: What a suggestion is built from. `auto` is the rule: a model that sees works
#: from the pictures alone, one that cannot gets the generation prompts instead.
#: Measured 2026-09-02 across eight models - **with** the prompt beside the
#: pictures, 2 of 8 named the animal in one correctly; **without** it, 6 of 8.
#: The prompt is copied, not read, so sending both makes a seeing model worse.
MATERIALS = ("auto", "images", "prompt", "both")


def _material(
    material: str, sees: bool, profile: dict, has_paths: bool
) -> tuple[bool, bool]:
    """Return `(send pictures, send prompts)` for one suggestion.

    A profile with `include_images` off never gets pictures, whatever is asked
    for: that switch is the user saying this voice works from text.
    """
    allowed = bool(profile["include_images"])
    if material == "images":
        return allowed, False
    if material == "prompt":
        return False, True
    if material == "both":
        return allowed, True
    # auto
    if allowed and sees and has_paths:
        return True, False
    return False, True


def _suggest_one(
    job: jobs.Job,
    worker: Worker | endpoint_client.Endpoint,
    post_id: int,
    profile: dict,
    hint: str | None = None,
    seen: set[tuple[str, str]] | None = None,
    seed: int | None = None,
    material: str = "auto",
) -> dict | None:
    post = post_store.detail(post_id)
    if post is None:
        raise LlmError("Post not found")

    paths: list[Path] = []
    for image in post["images"]:
        path = Path(image["source_path"])
        if path.exists():
            paths.append(path)

    sees = bool((getattr(worker, "info", None) or {}).get("vision"))
    wants_images, wants_prompt = _material(material, sees, profile, bool(paths))

    prompt_texts: list[str] = []
    if wants_prompt:
        for image in post["images"]:
            parsed = image.get("parsed") or {}
            cleaned = prompts.clean_prompt(parsed.get("prompt") or "")
            if cleaned and cleaned not in prompt_texts:
                prompt_texts.append(cleaned)

    encoded: list[str] = []
    sheet = None
    if wants_images and paths:
        # A title describes the post, not one frame of it - so the model sees the
        # whole set as a contact sheet first, then a few full-size images for
        # detail. With a single-image post there is no sheet to build.
        sheet = prompts.build_contact_sheet(paths, tile_side=profile["vision_max_side"])
        if sheet:
            encoded.append(sheet)
        singles = max(0, profile["max_images"] - (1 if sheet else 0))
        if singles:
            encoded.extend(
                prompts.encode_images(paths[:singles], max_side=profile["vision_max_side"])
            )

    raw = _generate_interruptibly(
        job, worker,
        {
            "system_prompt": profile["system_prompt"],
            "user_content": prompts.build_user_content(
                prompt_texts[:12],
                title_max_words=profile["title_max_words"],
                tag_count=profile["tag_count"],
                hint=hint,
                sheet=bool(sheet),
            ),
            "images": encoded,
            # The hosted route turns this into grammar-constrained decoding, so
            # an unreadable answer stops being possible. Asked for here rather
            # than forced in the transport, because prompt shortening shares the
            # worker and wants a sentence, not an object.
            "response_schema": endpoint_client.ANSWER_SCHEMA,
            "max_new_tokens": profile["max_new_tokens"],
            "temperature": profile["temperature"],
            "top_p": profile["top_p"],
            "top_k": profile["top_k"],
            "seed": seed,
        }
    )
    if raw is None:
        return None
    parsed = prompts.parse_result(
        raw, title_max_words=profile["title_max_words"], tag_count=profile["tag_count"]
    )

    stored = {"post_id": post_id, "seed": seed, **parsed}
    inserted = False
    with db.transaction() as conn:
        for field in ("title", "description", "tags"):
            value = ", ".join(parsed["tags"]) if field == "tags" else parsed.get(field)
            if not value:
                continue
            if seen is not None:
                if (field, value) in seen:
                    continue
                seen.add((field, value))
            conn.execute(
                "INSERT INTO llm_suggestions(post_id, field, text, reason, profile_id,"
                " seed, created_at) VALUES(?,?,?,?,?,?,?)",
                (post_id, field, value, parsed.get("reason", ""), profile["id"],
                 seed, db.now_iso()),
            )
            inserted = True
    if not inserted:
        return {}
    stored["profile_name"] = profile["name"]
    return stored


def suggestions_for(post_id: int) -> list[dict[str, Any]]:
    rows = db.get_connection().execute(
        "SELECT * FROM llm_suggestions WHERE post_id=? ORDER BY id DESC LIMIT 30", (post_id,)
    )
    return [dict(row) for row in rows]


def accept(suggestion_id: int) -> dict[str, Any]:
    """Write a suggestion into the post. Nothing is applied without this step."""
    row = db.get_connection().execute(
        "SELECT * FROM llm_suggestions WHERE id=?", (suggestion_id,)
    ).fetchone()
    if row is None:
        raise LlmError("Suggestion not found")

    field, text, post_id = row["field"], row["text"], row["post_id"]
    if field == "title":
        post_store.update(post_id, title=text)
    elif field == "description":
        post_store.update(post_id, detail=text)
    elif field == "tags":
        post_store.set_tags(post_id, [tag.strip() for tag in text.split(",") if tag.strip()])

    with db.transaction() as conn:
        conn.execute("UPDATE llm_suggestions SET accepted=1 WHERE id=?", (suggestion_id,))
        # Only the proposal that is actually in the post is worth keeping. An
        # earlier accepted one for the same field has been superseded by this
        # very click and says nothing any more - and over a year or two of use
        # they are the ones that pile up: the live database held 47 accepted
        # rows against 3 undecided. Per field, because title and description are
        # separate decisions with separate histories. Nothing undecided is
        # touched, and the post itself was written above.
        conn.execute(
            "DELETE FROM llm_suggestions"
            " WHERE post_id=? AND field=? AND accepted=1 AND id<>?",
            (post_id, field, suggestion_id),
        )
    return {"post_id": post_id, "field": field}


def dismiss(suggestion_id: int) -> dict[str, Any]:
    """Delete a machine proposal without changing the post it belongs to."""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT post_id, field FROM llm_suggestions WHERE id=?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise LlmError("Suggestion not found")
        conn.execute("DELETE FROM llm_suggestions WHERE id=?", (suggestion_id,))
    return {"post_id": row["post_id"], "field": row["field"]}


def shorten_prompts(job: jobs.Job, image_ids: list[int]) -> dict[str, Any]:
    """Generate prompt proposals without changing an image edit.

    Accepting a proposal happens later through the bulk edit planner. This is
    intentionally different from the former metadata app, which wrote drafts
    from its worker thread before the user had reviewed the result.
    """
    images = image_store.get_many(image_ids)
    job.total = len(images)
    results: list[dict[str, Any]] = []
    excluded = prompt_tools.parse_tag_list(prompt_tools.DEFAULT_EXCLUDE_TAGS)
    job.stage = "Loading the model"
    worker = start_worker()
    generation_abandoned = False
    try:
        job.stage = f"Shortening prompts on {device_label(worker)}"
        for image in images:
            if job.should_stop():
                break
            try:
                original = metadata_editing.view(image)["effective"].get("prompt") or ""
                cleaned = prompt_tools.clean_for_shortening(original, excluded)
                if not cleaned:
                    job.skipped += 1
                    continue
                raw = _generate_interruptibly(
                    job, worker,
                    {
                        "system_prompt": prompt_tools.DEFAULT_SYSTEM_PROMPT,
                        "user_content": prompt_tools.shortening_request(cleaned),
                        "max_new_tokens": 96,
                        "temperature": 0.45,
                        "top_p": 0.8,
                        "top_k": 20,
                    }
                )
                if raw is None:
                    generation_abandoned = True
                    break
                suggestion = prompt_tools.clean_shortening_result(raw)
                if not suggestion:
                    raise LlmError("The model returned an empty prompt.")
                item = {
                    "image_id": image["id"],
                    "filename": image.get("relative_path", "").rsplit("/", 1)[-1],
                    "original": original,
                    "suggestion": suggestion,
                }
                results.append(item)
                job.succeeded += 1
                job.log(image_id=image["id"], status="ok")
            except Exception as exc:
                job.failed += 1
                job.log(image_id=image["id"], status="error", message=str(exc))
            finally:
                job.processed += 1
    finally:
        job.stage = "Releasing the model"
        _release(worker, abandoned=generation_abandoned)
        job.stage_code = ""
        job.stage = "Done"
    job.result = {"items": results}
    return job.result
