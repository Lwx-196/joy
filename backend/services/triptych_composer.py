"""Clean before/after/recovery triptych composer."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_LABEL_BAR_HEIGHT = 64
_TEXT_FILL = (35, 35, 35)
_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)


def _load_font(size: int) -> ImageFont.ImageFont:
    for font_path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _resize_to_height(path: Path, height: int) -> Image.Image:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
    width = max(1, int(rgb.size[0] * height / rgb.size[1]))
    return rgb.resize((width, height), Image.Resampling.LANCZOS)


def _draw_label(draw: ImageDraw.ImageDraw, x: int, width: int, label: str) -> None:
    font = _load_font(28)
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    draw.text(
        (x + (width - text_w) // 2, (_LABEL_BAR_HEIGHT - text_h) // 2 - bbox[1]),
        label,
        font=font,
        fill=_TEXT_FILL,
    )


def compose_triptych(
    panels: list[Path],
    output_path: str | Path,
    *,
    height: int = 900,
    gap: int = 20,
    bg: tuple[int, int, int] = (245, 245, 245),
    labels: list[str] | None = None,
) -> Path:
    """Compose 2-3 image panels into a clean horizontal triptych."""
    if len(panels) not in (2, 3):
        raise ValueError("compose_triptych requires 2 or 3 panels")
    if height <= 0:
        raise ValueError("height must be positive")
    if gap < 0:
        raise ValueError("gap must be non-negative")
    if labels is not None and len(labels) != len(panels):
        raise ValueError("labels length must match panels length")

    resized = [_resize_to_height(Path(panel), height) for panel in panels]
    title_h = _LABEL_BAR_HEIGHT if labels is not None else 0
    total_w = sum(panel.size[0] for panel in resized) + gap * (len(resized) - 1)
    canvas = Image.new("RGB", (total_w, height + title_h), bg)
    draw = ImageDraw.Draw(canvas) if labels is not None else None

    x = 0
    for idx, panel in enumerate(resized):
        if labels is not None and draw is not None:
            _draw_label(draw, x, panel.size[0], labels[idx])
        canvas.paste(panel, (x, title_h))
        x += panel.size[0] + gap

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() in {".jpg", ".jpeg"}:
        canvas.save(output, quality=92)
    else:
        canvas.save(output)
    return output


__all__ = ["compose_triptych"]
