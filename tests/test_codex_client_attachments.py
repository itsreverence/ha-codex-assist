import base64

from custom_components.codex_assist.codex_client import codex_user_content_with_images


def test_codex_user_content_with_images_returns_plain_text_without_images():
    assert codex_user_content_with_images("describe this", []) == "describe this"


def test_codex_user_content_with_images_encodes_images_as_responses_input_image_items():
    content = codex_user_content_with_images(
        "describe this",
        [("image/png", b"fake-png-bytes")],
    )

    assert content == [
        {"type": "input_text", "text": "describe this"},
        {
            "type": "input_image",
            "image_url": "data:image/png;base64,"
            + base64.b64encode(b"fake-png-bytes").decode("ascii"),
            "detail": "auto",
        },
    ]
