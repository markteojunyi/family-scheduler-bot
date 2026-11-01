import base64

from event_parser import interpret_image


def parse_event_from_image(image_bytes: bytes, caption: str = "") -> dict:
    b64_image = base64.b64encode(image_bytes).decode()
    blocks = [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_image}"}}]
    if caption:
        blocks.insert(0, {"type": "text", "text": f"Caption from sender: {caption}"})
    return interpret_image(blocks, source_text=caption)
