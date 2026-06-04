import pytest

from custom_components.codex_assist.codex_image import (
    image_size_dimensions,
    validate_image_size,
)


def test_image_size_dimensions_returns_width_and_height():
    assert image_size_dimensions("1536x1024") == (1536, 1024)
    assert image_size_dimensions("1024x1536") == (1024, 1536)


def test_image_size_dimensions_rejects_unsupported_size():
    with pytest.raises(ValueError, match="Unsupported Codex Assist image size"):
        image_size_dimensions("2048x2048")


def test_validate_image_size_returns_supported_size():
    assert validate_image_size("1024x1024") == "1024x1024"
