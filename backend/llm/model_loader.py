"""Loading a local checkpoint. One implementation, two callers.

This module runs under a *different* Python interpreter than the application:
one that has torch and transformers, which the app's own environment
deliberately does not. It therefore imports the standard library only and pulls
torch and transformers in lazily. **Nothing else from this application may be
imported here** - `backend/llm/worker.py` and the model lab's own worker both
import this file, and a single import of `backend.db` or `backend.store` would
break both at once.

Loading is a chain, not a single guess. Checkpoints in the wild resolve through
different Auto classes, and a class that is missing or that refuses one
checkpoint says nothing about the next one, so every name is tried in turn and
what failed is kept.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any

#: Tried in this order. The first name that both loads and can generate wins.
#: `AutoModelForVision2Seq` is gone in transformers 5 and resolves to
#: "unavailable"; it stays for older installations.
#:
#: **A newly supported architecture goes in this chain and nowhere else.** Not
#: into a second copy of the loader, and never as a model-name conditional in
#: the worker or in a caller. Task `97` exists because a second copy had drifted
#: from this one, so a checkpoint could load there and be unloadable here - the
#: comparison measured the wrong thing for weeks without anyone noticing.
MODEL_CLASSES = (
    "AutoModelForMultimodalLM",
    "AutoModelForImageTextToText",
    "AutoModelForVision2Seq",
    "MllamaForConditionalGeneration",
    "AutoModel",
    "AutoModelForCausalLM",
)

#: Placements in `hf_device_map` that mean the model did not fit the GPU.
OFFLOAD_DEVICES = {"cpu", "disk"}


class LoaderError(RuntimeError):
    """A load that cannot be retried, carrying where it gave up.

    `stage` is one of `dependency_import`, `processor`, `model_class` or
    `quantisation` - the vocabulary the model lab reports failures in.
    """

    def __init__(self, message: str, stage: str):
        super().__init__(message)
        self.stage = stage


def is_prequantized(checkpoint_path: str) -> bool:
    """Does the checkpoint bring its own quantisation? Then leave it alone.

    Quantising an AWQ, GPTQ or compressed-tensors checkpoint a second time is
    how such a download fails on load.
    """
    config = os.path.join(checkpoint_path, "config.json")
    try:
        with open(config, encoding="utf-8") as handle:
            return "quantization_config" in json.load(handle)
    except (OSError, ValueError):
        return False


def load_model(
    checkpoint_path: str,
    *,
    device_map: str = "auto",
    quantise: bool = True,
    trust_remote_code: bool = False,
    offload_folder: str | None = None,
    dtype: str = "auto",
    on_stage: Any = None,
):
    """Load a vision-language model if the checkpoint is one, else a text model.

    Returns `model, processor, tokenizer, vision, vision_error, diagnostics`.
    """
    try:
        import torch
        import transformers
    except Exception as exc:
        # A missing optional dependency degrades rather than crashing, but it
        # has to say so: importing torch can fail on a missing CUDA library too,
        # not only on a missing package.
        raise LoaderError(
            f"this Python has no PyTorch/transformers ({exc}). Point 'LLM interpreter'"
            " in the settings at an environment that has torch, transformers and"
            " bitsandbytes.",
            "dependency_import",
        ) from exc

    # Never over the network for a checkpoint: the model interpreter reads what
    # is on disk and nothing else. A constant rather than a parameter, because a
    # safeguard with a switch on it is a safeguard somebody will switch off.
    common = {"trust_remote_code": trust_remote_code, "local_files_only": True}

    processor = None
    vision = False
    vision_error = None
    started = time.perf_counter()
    try:
        processor = transformers.AutoProcessor.from_pretrained(checkpoint_path, **common)
        vision = hasattr(processor, "image_processor")
        if not vision:
            vision_error = "The processor has no image_processor."
    except Exception as exc:
        # Usually a missing torchvision/Pillow in the worker environment. Falling
        # back to text-only is right, but doing it silently would hide that the
        # images are simply not being looked at.
        vision_error = str(exc).strip().split("\n")[0] or type(exc).__name__
    processor_seconds = time.perf_counter() - started
    _stage(on_stage, "processor", processor_seconds, _class_name(processor))

    kwargs: dict[str, Any] = {**common, "device_map": device_map}
    if offload_folder:
        kwargs["offload_folder"] = offload_folder
    if dtype:
        kwargs["dtype"] = dtype
    if quantise and not is_prequantized(checkpoint_path):
        config_cls = getattr(transformers, "BitsAndBytesConfig", None)
        if config_cls is None:
            raise LoaderError(
                "4-bit loading was requested but this environment has no"
                " BitsAndBytesConfig. Install bitsandbytes into the LLM interpreter,"
                " or load the model unquantised.",
                "quantisation",
            )
        kwargs["quantization_config"] = config_cls(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            # Without this, bitsandbytes *refuses* the moment device_map="auto"
            # wants to place a module on the CPU, instead of offloading it. On an
            # 8 GB card that turns every model that does not quite fit into a
            # refusal. Loading slowly is a result; refusing to load is not.
            llm_int8_enable_fp32_cpu_offload=True,
        )

    classes = list(MODEL_CLASSES)
    failures: list[str] = []
    model = None
    effective_class = None
    started = time.perf_counter()
    for name in classes:
        model_cls = getattr(transformers, name, None)
        if model_cls is None:
            failures.append(f"{name}: unavailable in transformers {transformers.__version__}")
            continue
        try:
            candidate = _from_pretrained(model_cls, checkpoint_path, kwargs)
        except Exception as exc:
            # One class failing must not end the chain - that is the whole point
            # of having one.
            failures.append(f"{name}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            continue
        if not _can_generate(candidate):
            # transformers 5 keeps generate() on GenerationMixin, and a base
            # AutoModel does not inherit it. Such a class loads happily and then
            # has no generate(), so it is a failure here rather than a confusing
            # one at the first request.
            failures.append(f"{name}: loaded but cannot generate")
            del candidate
            _release(torch)
            continue
        model = candidate
        effective_class = name
        break
    model_seconds = time.perf_counter() - started
    if model is None:
        raise LoaderError("; ".join(failures) or "No model class was tried.", "model_class")
    _stage(on_stage, "model", model_seconds, effective_class)

    model.eval()
    tokenizer = getattr(processor, "tokenizer", None)
    if tokenizer is None:
        tokenizer = transformers.AutoTokenizer.from_pretrained(checkpoint_path, **common)

    device_map_resolved = getattr(model, "hf_device_map", None)
    diagnostics = {
        "requested_classes": classes,
        "class_failures": failures,
        "effective_model_class": type(model).__name__,
        "resolved_model_class": effective_class,
        "processor_class": _class_name(processor),
        "tokenizer_class": _class_name(tokenizer),
        "vision": vision,
        # The lab reads its whole run log out of `diagnostics`; without this a
        # text-only fallback shows up there as "vision: false" with no cause.
        "vision_error": vision_error,
        "chat_template": bool(
            getattr(processor, "chat_template", None) or getattr(tokenizer, "chat_template", None)
        ),
        "hf_device_map": device_map_resolved,
        "effective_dtype": str(getattr(model, "dtype", None)),
        "effective_quantization": _quantization(model),
        "processor_load_seconds": processor_seconds,
        "model_load_seconds": model_seconds,
        "offloaded": _offloaded(device_map_resolved),
    }
    return model, processor, tokenizer, vision, vision_error, diagnostics


def _from_pretrained(model_cls, checkpoint_path: str, kwargs: dict[str, Any]):
    """transformers 5 renamed `torch_dtype` to `dtype`; older ones want the old name."""
    try:
        return model_cls.from_pretrained(checkpoint_path, **kwargs)
    except TypeError:
        if "dtype" not in kwargs:
            raise
        legacy = {key: value for key, value in kwargs.items() if key != "dtype"}
        legacy["torch_dtype"] = kwargs["dtype"]
        return model_cls.from_pretrained(checkpoint_path, **legacy)


def _can_generate(model) -> bool:
    checker = getattr(model, "can_generate", None)
    if callable(checker):
        return bool(checker())
    return hasattr(model, "generate")


def _release(torch) -> None:
    """Give a rejected candidate's VRAM back before trying the next class."""
    cuda = getattr(torch, "cuda", None)
    if cuda is not None and cuda.is_available():
        cuda.empty_cache()


def _offloaded(device_map) -> bool:
    if not isinstance(device_map, dict):
        return False
    return any(str(value).lower() in OFFLOAD_DEVICES for value in device_map.values())


def _class_name(value) -> str | None:
    return type(value).__name__ if value is not None else None


def _quantization(model):
    value = getattr(getattr(model, "config", None), "quantization_config", None)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if value is not None and not isinstance(value, (dict, list, str, int, float, bool)):
        return str(value)
    return value


def _stage(on_stage, name: str, seconds: float, class_name: str | None) -> None:
    if on_stage is None:
        return
    # Writing a diagnostic must not break the load it describes.
    with contextlib.suppress(Exception):
        on_stage(name, seconds, class_name)
