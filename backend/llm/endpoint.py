"""Talking to a model served over HTTP, instead of hosting one.

The second way to reach a local model, and the one `SET-02` has named since the
beginning. An Ollama server answers here and the application starts no
subprocess at all.

**Ollama's own `/api/chat`, not the OpenAI shim on `/v1`.** Measured 2026-09-02:
the same vision request that `/api/chat` answers makes `/v1/chat/completions`
return 500, because the shim renders content parts into a form Mistral's chat
template rejects ("conversation roles must alternate"). Vision is the point of
this feature, so the working route is the one we speak - and it is the route the
promise names.

This exists because hosting the model ourselves made us the owner of a release
cadence we cannot keep. A GGUF checkpoint the maintainer downloaded refused to
load in a llama.cpp two weeks older than the file, and answered immediately
through a server that updates itself. So: **GGUF is supported through Ollama.**
Other GGUF interfaces are not built, and using one is at the user's own risk.

The class deliberately exposes the same two things `service.Worker` does -
`generate(request)` and `info` - so `service` treats a hosted model and a local
one alike and nothing above this layer has to know which is in use.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import io
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:11434"

#: Seconds to wait for a first answer. Loading a cold model into VRAM happens
#: inside this call, so it needs the same patience the local worker gets.
REQUEST_TIMEOUT = 600.0
LIST_TIMEOUT = 5.0


class EndpointError(RuntimeError):
    pass


def base_url(configured: str | None) -> str:
    url = (configured or DEFAULT_URL).strip().rstrip("/")
    # urllib is happy to open file:// and hand back its contents; an address
    # field is not a place to read local files from.
    if not url.startswith(("http://", "https://")):
        raise EndpointError(
            f"The endpoint address must start with http:// or https:// - got {url!r}."
            " Correct it in the LLM settings."
        )
    return url


def list_models(url: str | None) -> list[str] | None:
    """What the server actually has, so nobody has to type a model name.

    `None` means the server did not answer; an empty list means it answered and
    has nothing. Collapsing the two told a user with a running but empty server
    to go and start it.
    """
    try:
        payload = _get(f"{base_url(url)}/api/tags", LIST_TIMEOUT)
    except EndpointError:
        return None
    names = []
    for model in payload.get("models") or []:
        name = model.get("name") or model.get("model")
        if name:
            names.append(name)
    return sorted(names)


#: What the parser needs back. Ollama turns a schema into grammar-constrained
#: decoding, so an invalid answer stops being possible rather than being
#: repaired afterwards. Measured 2026-09-03 on Ministral-3B, the worst offender
#: in the model comparison: 4 of 20 answers parsed without this, 20 of 20 with
#: it - and faster, because a constrained model stops rambling.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["title", "description", "tags"],
}

#: Ollama's own default window, and the floor here: this may give a request more
#: room, never less.
DEFAULT_NUM_CTX = 4096

#: Measured against qwen3vl through Ollama's own `prompt_eval_count`: a
#: 1554x1554 contact sheet (2.415 MP) costs 2757 prompt tokens and a 2328x1554
#: one (3.618 MP) costs 3933. That is 978 tokens per megapixel over a text part
#: of about 395 - both points fit exactly. The rates below carry roughly 12 %
#: on top, because another model's tokenizer is not this one's.
TOKENS_PER_MEGAPIXEL = 1100
#: The text part measured at 395 tokens; 600 leaves room for a longer profile
#: prompt than the ones that were measured.
TOKENS_FOR_TEXT = 600

#: What to ask for when a picture cannot be measured at all - the window a full
#: twenty-picture post needs at the default tile size.
UNMEASURED_NUM_CTX = 16384


def _pixels(encoded: str) -> int | None:
    """Pixels in a base64 image, read from the header alone, or None.

    Pillow only reads far enough to answer `size`, so this does not decode the
    picture. `None` means "could not tell", which is not the same as zero: a
    picture counted as nothing shrinks the window, and too small a window is the
    silent mid-word truncation this whole calculation exists to prevent.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(base64.b64decode(encoded))) as picture:
            return picture.width * picture.height
    except (OSError, ValueError, binascii.Error):
        return None


def context_window(images: list[str], max_new_tokens: int) -> int:
    """How much room this one request needs, rounded up to a power of two.

    Ollama's default is 4096 tokens. A five-picture contact sheet costs 4034 of
    them, so the answer was cut off mid-word - silently, with no hint to the
    user that anything was lost. `CVT-16` allows twenty pictures, which is about
    12 megapixels and three times that again.

    Computed rather than fixed, because a window larger than the request needs
    costs time: measured at a factor of 0.9 to 1.8 on posts that already fitted.
    This way the common post keeps Ollama's default and only a big one grows.
    """
    measured = [_pixels(image) for image in images]
    if any(size is None for size in measured):
        # Guessing low loses the answer with nothing said; guessing high costs
        # about a fifth of the time. So an unreadable picture buys the largest
        # window a post can need - twenty pictures, `CVT-16`, at the default
        # tile size - rather than being treated as no picture at all.
        return UNMEASURED_NUM_CTX
    megapixels = sum(measured) / 1_000_000
    needed = int(megapixels * TOKENS_PER_MEGAPIXEL) + TOKENS_FOR_TEXT + max_new_tokens
    window = DEFAULT_NUM_CTX
    while window < needed:
        window *= 2
    return window


class Endpoint:
    """One configured hosted model."""

    def __init__(self, url: str | None, model: str):
        self.url = base_url(url)
        self.model = model
        self.vision, self.vision_error = _vision_support(self.url, self.model)
        self.info: dict[str, Any] = {
            "device": f"{self.model} on {self.url}",
            "vision": self.vision,
            "vision_error": self.vision_error,
            "offloaded": False,
            "endpoint": True,
        }

    @property
    def alive(self) -> bool:
        return True

    def generate(self, request: dict[str, Any]) -> str:
        try:
            return self._ask(request, with_images=self.vision)
        except EndpointError as exc:
            if not self.vision or not _is_image_refusal(exc):
                raise
            # A server that only discovers this at request time still gets one
            # honest answer out of the run, and the reason is recorded so the
            # settings page can say the pictures are not being looked at.
            self.vision = False
            self.vision_error = str(exc).strip()[:200]
            self.info.update({"vision": False, "vision_error": self.vision_error})
            return self._ask(request, with_images=False)

    def _ask(self, request: dict[str, Any], *, with_images: bool) -> str:
        images = list(request.get("images") or []) if with_images else []
        user: dict[str, Any] = {"role": "user", "content": request.get("user_content") or ""}
        if images:
            user["images"] = images

        temperature = float(request.get("temperature") or 0)
        options: dict[str, Any] = {
            "num_predict": int(request.get("max_new_tokens") or 384),
            "temperature": temperature,
        }
        if temperature > 0:
            options["top_p"] = float(request.get("top_p") or 0.8)
            options["top_k"] = int(request.get("top_k") or 20)
        # Read the way the local worker reads it: a caller that carries the key
        # with no value must not turn a suggestion run into a TypeError.
        if request.get("seed") is not None:
            options["seed"] = int(request["seed"])
        options["num_ctx"] = context_window(images, options["num_predict"])

        payload = _post(
            f"{self.url}/api/chat",
            {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": request.get("system_prompt") or ""},
                    user,
                ],
                "options": options,
                # Only when the caller asks for one. The same worker also serves
                # prompt shortening, which wants a plain sentence back - forcing
                # the suggestion schema on every request handed that path a JSON
                # blob as the shortened prompt.
                **({"format": schema} if (schema := request.get("response_schema")) else {}),
                "stream": False,
                # A model that reasons out loud puts its reasoning in `thinking`
                # and leaves `content` empty - so the answer never arrives, and
                # the token budget is spent thinking rather than answering.
                # MiniCPM-V 4.5 failed every request that way until this was
                # sent. Harmless for a model that does not reason; the local
                # worker asks the same of its chat template.
                "think": False,
            },
            REQUEST_TIMEOUT,
        )
        try:
            return (payload["message"]["content"] or "").strip()
        except (KeyError, TypeError) as exc:
            raise EndpointError(
                f"The server at {self.url} answered in an unexpected shape: "
                f"{json.dumps(payload)[:200]}"
            ) from exc

    def stop(self) -> None:
        """Ask the server to release the model, as the local worker does.

        The GPU is shared with image generation, so a model left resident after
        a batch is the same problem here as it is for a subprocess. Ollama frees
        it on `keep_alive: 0`; a server that does not know the field ignores it,
        which is why a failure here is not worth reporting.
        """
        # Releasing is a courtesy; failing to do it must not fail the batch.
        with contextlib.suppress(EndpointError):
            _post(f"{self.url}/api/generate", {"model": self.model, "keep_alive": 0}, LIST_TIMEOUT)


def _vision_support(url: str, model: str) -> tuple[bool, str | None]:
    """Ask the server what this model can do, before sending it pictures.

    Ollama reports `capabilities` on `/api/show`, so a text-only model is known
    up front rather than discovered as a 500 in the middle of a batch. A server
    that does not answer that question is given the benefit of the doubt - the
    request itself will settle it.
    """
    try:
        shown = _post(f"{url}/api/show", {"model": model}, LIST_TIMEOUT)
    except EndpointError:
        return True, None
    capabilities = shown.get("capabilities")
    if not isinstance(capabilities, list):
        return True, None
    if "vision" in capabilities:
        return True, None
    return False, (
        f"{model} has no vision capability on this server, so the suggestion comes"
        " from the prompts alone. A GGUF needs its mmproj file to see."
    )


def _is_image_refusal(exc: EndpointError) -> bool:
    message = str(exc).lower()
    return "image" in message and ("not supported" in message or "mmproj" in message)


def _get(url: str, timeout: float) -> dict[str, Any]:
    return _request(urllib.request.Request(url, method="GET"), timeout)


def _post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return _request(request, timeout)


def _request(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise EndpointError(
                f"{request.full_url} answered with {type(payload).__name__}, not an"
                " object. Is that address really a model server?"
            )
        return payload
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise EndpointError(
            f"{request.full_url} answered {exc.code}: {detail}. Check the endpoint"
            " address and the picked model in the LLM settings."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EndpointError(
            f"No answer from {request.full_url} ({exc}). Start the model server, or"
            " correct the endpoint address in the LLM settings."
        ) from exc
    except ValueError as exc:
        raise EndpointError(f"{request.full_url} did not answer with JSON.") from exc
