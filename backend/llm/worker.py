#!/usr/bin/env python3
"""Model host, run under a *different* Python interpreter.

This file imports one module from the app - `backend.llm.model_loader`, which
holds the loading itself so the model lab can run exactly the same code - and
nothing else, on purpose: it runs in an environment that has torch and
transformers, which the app's own environment deliberately does not. That is
also why the loader module has to stay free of app imports. The two processes
talk over line-delimited JSON.

A model served over HTTP never reaches this file: `backend/llm/endpoint.py`
answers those without a subprocess at all.

Protocol::

    argv[1]  {"model_dir": "...", "device": "auto|cuda|cpu", "load_in_4bit": true,
              "trust_remote_code": false, "offload_folder": "..."}
    stdin    {"id": 1, "system_prompt": "...", "user_content": "...",
              "images": ["<base64 jpeg>", ...], "max_new_tokens": 384,
              "temperature": 0.7, "top_p": 0.8, "top_k": 20, "seed": 12345}
             {"command": "shutdown"}
    stdout   {"event": "ready", "device": "cuda", "model": "...", "vision": true,
              "device_map": {...}, "offloaded": false}
             {"id": 1, "text": "..."}   |   {"id": 1, "error": "..."}
             {"event": "fatal", "message": "...", "failure_stage": "model_class"}

stdout carries protocol JSON only; every log line goes to stderr. The parent
parses stdout strictly, so a stray print here would break the channel.

`trust_remote_code` is off unless the parent sends it. With it on, transformers
imports and runs Python that ships inside the model directory, in this process,
as the maintainer - so it stays an explicit choice per installation, never a
default that a freshly downloaded checkpoint can make for them.

The model is loaded once and then serves requests until told to shut down -
processing a batch of posts is one model load, not one per post. It is released
as soon as the batch ends, because the GPU is shared with image generation.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.llm import model_loader

# Qwen3.5's recommended non-thinking default. This is a mode setting that
# reduces repetition, not a profile's creative choice.
NON_THINKING_PRESENCE_PENALTY = 1.5


class _PresencePenaltyLogitsProcessor:
    """Apply the server-style penalty that transformers does not expose."""

    def __init__(self, prompt_length: int, penalty: float, torch):
        self.prompt_length = prompt_length
        self.penalty = penalty
        self.torch = torch

    def __call__(self, input_ids, scores):
        generated = input_ids[:, self.prompt_length :]
        if generated.shape[-1] == 0:
            return scores
        present = self.torch.zeros_like(scores, dtype=self.torch.bool)
        present.scatter_(1, generated, True)
        return scores - present.to(scores.dtype) * self.penalty


def emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()


def main() -> int:
    try:
        options = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    except ValueError as exc:
        emit({"event": "fatal", "message": f"invalid options: {exc}"})
        return 2

    model_dir = options.get("model_dir") or ""
    device = options.get("device") or "auto"
    load_in_4bit = bool(options.get("load_in_4bit", True))
    trust_remote_code = bool(options.get("trust_remote_code", False))
    offload_folder = options.get("offload_folder") or None

    try:
        import torch
    except Exception as exc:
        emit(
            {
                "event": "fatal",
                "message": (
                    f"this Python has no PyTorch/transformers ({exc}). "
                    "Point 'LLM interpreter' in the settings at an environment that has "
                    "torch, transformers and bitsandbytes."
                ),
            }
        )
        return 2

    try:
        model, processor, tokenizer, vision, vision_error, diagnostics = (
            model_loader.load_model(
                model_dir,
                device_map=device,
                quantise=load_in_4bit,
                trust_remote_code=trust_remote_code,
                offload_folder=offload_folder,
                on_stage=_log_stage,
            )
        )
    except model_loader.LoaderError as exc:
        log(traceback.format_exc())
        emit(
            {
                "event": "fatal",
                "message": f"The model could not be loaded: {exc}",
                "failure_stage": exc.stage,
            }
        )
        return 2
    except Exception as exc:
        log(traceback.format_exc())
        emit({"event": "fatal", "message": f"The model could not be loaded: {exc}"})
        return 2

    resolved = str(getattr(model, "device", device))
    emit(
        {
            "event": "ready",
            "device": resolved,
            "model": model_dir,
            "vision": vision,
            "vision_error": vision_error,
            "device_map": diagnostics["hf_device_map"],
            "offloaded": diagnostics["offloaded"],
        }
    )

    return _serve(lambda request: generate(request, model, processor, tokenizer, vision, torch))


def _serve(answer) -> int:
    """The request loop, shared by both runtimes."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as exc:
            log(f"unreadable request: {exc}")
            continue

        if request.get("command") == "shutdown":
            break

        request_id = request.get("id")
        try:
            emit({"id": request_id, "text": answer(request)})
        except Exception as exc:
            # one bad request must not kill the worker
            log(traceback.format_exc())
            emit({"id": request_id, "error": f"{type(exc).__name__}: {exc}"})

    emit({"event": "closed"})
    return 0


def _log_stage(stage: str, seconds: float, class_name: str | None) -> None:
    """Which class actually answered, on stderr - stdout carries protocol only."""
    log(f"{stage}: {class_name or 'none'} in {seconds:.1f}s")


def generate(request, model, processor, tokenizer, vision, torch) -> str:
    system_prompt = request.get("system_prompt") or ""
    user_content = request.get("user_content") or ""
    images_b64 = request.get("images") or []

    images = []
    if vision and images_b64:
        from PIL import Image

        for encoded in images_b64:
            try:
                images.append(
                    Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")
                )
            except Exception as exc:
                log(f"Image could not be read: {exc}")

    gen_kwargs = {
        "max_new_tokens": int(request.get("max_new_tokens") or 384),
        "do_sample": float(request.get("temperature") or 0) > 0,
        "temperature": float(request.get("temperature") or 0.7),
        "top_p": float(request.get("top_p") or 0.8),
        "top_k": int(request.get("top_k") or 20),
    }
    if not gen_kwargs["do_sample"]:
        gen_kwargs.pop("temperature")
        gen_kwargs.pop("top_p")
        gen_kwargs.pop("top_k")

    seed = request.get("seed")
    if seed is not None:
        # torch seeds its default generator from system entropy per process and
        # advances it between requests, so nothing is repeatable and nothing can
        # be reported afterwards. The parent draws the number instead and hands
        # it over, which makes one answer reproducible without making every
        # answer the same.
        torch.manual_seed(int(seed))

    if images and processor is not None:
        content: list[dict[str, Any]] = [{"type": "image"} for _ in images]
        content.append({"type": "text", "text": user_content})
        messages = [
            {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
            {"role": "user", "content": content},
        ]
        prompt = _apply_template(processor, messages)
        inputs = processor(text=[prompt], images=images, return_tensors="pt").to(model.device)
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        if hasattr(tokenizer, "apply_chat_template") and getattr(
            tokenizer, "chat_template", None
        ):
            prompt = _apply_template(tokenizer, messages)
        else:
            prompt = f"{system_prompt}\n\n{user_content}"
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)

    if gen_kwargs["do_sample"]:
        gen_kwargs["logits_processor"] = [
            _PresencePenaltyLogitsProcessor(
                inputs["input_ids"].shape[-1], NON_THINKING_PRESENCE_PENALTY, torch
            )
        ]

    with torch.inference_mode():
        output = model.generate(**inputs, **gen_kwargs)

    prompt_length = inputs["input_ids"].shape[-1]
    decoder = processor if (images and processor is not None) else tokenizer
    return decoder.decode(output[0][prompt_length:], skip_special_tokens=True).strip()


def _apply_template(owner, messages) -> str:
    """Render the chat template with reasoning turned off where supported.

    Qwen3-family templates think out loud before answering. That prose is not
    useful here and it eats the token budget - a short answer limit produced
    pages of "Thinking Process:" and never reached the JSON. Templates that do
    not know the flag simply reject the keyword, hence the fallback.
    """
    try:
        return owner.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False, enable_thinking=False
        )
    except TypeError:
        return owner.apply_chat_template(messages, add_generation_prompt=True, tokenize=False)


if __name__ == "__main__":
    raise SystemExit(main())
