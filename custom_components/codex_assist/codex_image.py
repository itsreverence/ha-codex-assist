from __future__ import annotations

DEFAULT_IMAGE_MODEL = "gpt-image-2-medium"
DEFAULT_IMAGE_SIZE = "1024x1024"
IMAGE_MODEL_QUALITY = {
    "gpt-image-2-low": "low",
    "gpt-image-2-medium": "medium",
    "gpt-image-2-high": "high",
}
IMAGE_SIZE_OPTIONS = [
    "1024x1024",
    "1536x1024",
    "1024x1536",
]


def image_model_quality(image_model: str) -> str:
    """Return the supported OpenAI image-generation quality for an image option."""
    try:
        return IMAGE_MODEL_QUALITY[image_model]
    except KeyError as err:
        raise ValueError(f"Unsupported Codex Assist image model: {image_model}") from err


def validate_image_size(size: str) -> str:
    """Return a supported OpenAI image-generation size or raise."""
    if size not in IMAGE_SIZE_OPTIONS:
        raise ValueError(f"Unsupported Codex Assist image size: {size}")
    return size


def image_size_dimensions(size: str) -> tuple[int, int]:
    """Return width and height for a supported OpenAI image-generation size."""
    size = validate_image_size(size)
    width, height = size.split("x", maxsplit=1)
    return int(width), int(height)
