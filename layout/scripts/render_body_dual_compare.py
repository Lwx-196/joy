#!/usr/bin/env python3
"""render_body_dual_compare.py

非面部主导案例的专用前后双视图模板：
- 支持任意 1-2 个 section
- 常见为 正面/背面 或 正面/45侧
"""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


SCRIPT_DIR = Path(__file__).resolve().parent
CASE_LAYOUT_PATH = SCRIPT_DIR / "case_layout_board.py"
CASE_LAYOUT_SPEC = importlib.util.spec_from_file_location("case_layout_board", CASE_LAYOUT_PATH)
if CASE_LAYOUT_SPEC is None or CASE_LAYOUT_SPEC.loader is None:
    raise RuntimeError(f"无法加载 case_layout_board.py: {CASE_LAYOUT_PATH}")
CASE_LAYOUT = importlib.util.module_from_spec(CASE_LAYOUT_SPEC)
CASE_LAYOUT_SPEC.loader.exec_module(CASE_LAYOUT)


def load_image(path: str) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def crop_body_frame(path: str, mode: str, size: tuple[int, int]) -> Image.Image:
    img = load_image(path)
    w, h = img.size
    if mode == "front":
        box = (
            int(w * 0.01),
            int(h * 0.05),
            int(w * 0.99),
            int(h * 0.98),
        )
    elif mode == "back":
        box = (
            int(w * 0.01),
            int(h * 0.05),
            int(w * 0.99),
            int(h * 0.98),
        )
    elif mode in {"oblique", "side", "neck"}:
        box = (
            int(w * 0.04),
            int(h * 0.04),
            int(w * 0.96),
            int(h * 0.98),
        )
    else:
        raise ValueError(f"未知 mode: {mode}")
    cropped = img.crop(box)
    contained = ImageOps.contain(cropped, size, method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", size, (248, 245, 241))
    paste_x = (size[0] - contained.width) // 2
    # 让肩部区域尽量占满画框，高度靠上放置以减少无意义留白。
    paste_y = max(0, min((size[1] - contained.height) // 2, int(size[1] * 0.04)))
    canvas.paste(contained, (paste_x, paste_y))
    return canvas


def render_section(draw, canvas, *, y, title, before_img, after_img, board_w, pad, section_title_h, panel, outline_color, label_font, section_font):
    accent = (116, 99, 84)
    ink = (56, 49, 43)
    soft_green = (226, 235, 216)
    green = (132, 154, 98)
    inner_gap = 28

    draw.rounded_rectangle((pad, y, board_w - pad, y + section_title_h), radius=18, fill=accent)
    bbox = draw.textbbox((0, 0), title, font=section_font)
    tx = pad + ((board_w - pad * 2) - (bbox[2] - bbox[0])) / 2
    ty = y + (section_title_h - (bbox[3] - bbox[1])) / 2 - 2
    draw.text((tx, ty), title, font=section_font, fill=(255, 255, 255))
    y += section_title_h + 14

    box_w = before_img.width + after_img.width + inner_gap + 76
    box_h = max(before_img.height, after_img.height) + 72
    box_x = (board_w - box_w) // 2
    draw.rounded_rectangle((box_x, y, box_x + box_w, y + box_h), radius=26, fill=panel, outline=outline_color, width=2)

    left_x = box_x + 24
    right_x = left_x + before_img.width + inner_gap
    label_y = y + 16
    draw.rounded_rectangle((left_x, label_y, left_x + before_img.width, label_y + 34), radius=12, fill=(245, 240, 234))
    draw.rounded_rectangle((right_x, label_y, right_x + after_img.width, label_y + 34), radius=12, fill=soft_green)

    for x0, width, text_label, fill in [
        (left_x, before_img.width, "术前", ink),
        (right_x, after_img.width, "术后", green),
    ]:
        bb = draw.textbbox((0, 0), text_label, font=label_font)
        tx = x0 + (width - (bb[2] - bb[0])) / 2
        ty = label_y + (34 - (bb[3] - bb[1])) / 2 - 1
        draw.text((tx, ty), text_label, font=label_font, fill=fill)

    img_y = y + 54
    canvas.paste(before_img, (left_x, img_y))
    canvas.paste(after_img, (right_x, img_y))
    return y + box_h


def render_board(args: argparse.Namespace) -> Path:
    brand = CASE_LAYOUT.resolve_brand(args.brand)

    bg = (244, 238, 231)
    panel = (253, 250, 246)
    ink = (56, 49, 43)
    accent = (116, 99, 84)
    outline_color = (227, 218, 209)
    date_fill = (236, 227, 216)

    board_w = 1600
    pad = 48
    section_gap = 34
    section_title_h = 56
    footer_h = 88
    header_h = 180
    # 身体案例更适合横向画框，避免肩峰和大臂被裁掉，同时减少上下留白。
    image_w, image_h = 560, 360

    name_font = CASE_LAYOUT.load_font(50, bold=True)
    date_font = CASE_LAYOUT.load_font(22, bold=True)
    project_font = CASE_LAYOUT.load_font(26, bold=False)
    section_font = CASE_LAYOUT.load_font(30, bold=True)
    label_font = CASE_LAYOUT.load_font(22, bold=True)

    sections = []
    section1_mode = args.section1_mode or "front"
    section1_before = crop_body_frame(args.section1_before, section1_mode, (image_w, image_h))
    section1_after = crop_body_frame(args.section1_after, section1_mode, (image_w, image_h))
    sections.append((args.section1_title, section1_before, section1_after))
    if args.section2_title and args.section2_before and args.section2_after:
        section2_mode = args.section2_mode or "back"
        section2_before = crop_body_frame(args.section2_before, section2_mode, (image_w, image_h))
        section2_after = crop_body_frame(args.section2_after, section2_mode, (image_w, image_h))
        sections.append((args.section2_title, section2_before, section2_after))

    section_body_h = image_h + 78
    board_h = header_h + len(sections) * (section_title_h + section_body_h) + section_gap * max(len(sections) - 1, 0) + footer_h + pad * 2
    canvas = Image.new("RGB", (board_w, board_h), bg)
    draw = ImageDraw.Draw(canvas)

    card_x1, card_y1 = pad, 26
    card_x2, card_y2 = board_w - pad, header_h
    draw.rounded_rectangle((card_x1, card_y1, card_x2, card_y2), radius=32, fill=panel, outline=outline_color, width=2)
    for x in range(card_x1 + 28, card_x2 - 20, 20):
        draw.rounded_rectangle((x, card_y1 + 16, x + 10, card_y1 + 22), radius=3, fill=(226, 216, 206))

    pill = (card_x1 + 28, card_y1 + 42, card_x1 + 190, card_y1 + 82)
    draw.rounded_rectangle(pill, radius=18, fill=date_fill)
    db = draw.textbbox((0, 0), args.date, font=date_font)
    draw.text(
        (
            pill[0] + ((pill[2] - pill[0]) - (db[2] - db[0])) / 2,
            pill[1] + ((pill[3] - pill[1]) - (db[3] - db[1])) / 2 - 1,
        ),
        args.date,
        font=date_font,
        fill=accent,
    )

    name_bbox = draw.textbbox((0, 0), args.customer_name, font=name_font)
    name_w = name_bbox[2] - name_bbox[0]
    name_y = card_y1 + 104
    name_x = card_x1 + 28
    draw.text((name_x, name_y), args.customer_name, font=name_font, fill=ink)

    project_x = name_x + name_w + 24
    project_y = name_y + 13
    draw.text((project_x, project_y), args.project, font=project_font, fill=ink)

    y = header_h + 8
    for idx, (section_title, before_img, after_img) in enumerate(sections):
        y = render_section(
            draw,
            canvas,
            y=y,
            title=section_title,
            before_img=before_img,
            after_img=after_img,
            board_w=board_w,
            pad=pad,
            section_title_h=section_title_h,
            panel=panel,
            outline_color=outline_color,
            label_font=label_font,
            section_font=section_font,
        )
        if idx != len(sections) - 1:
            y += section_gap

    if Path(brand["logo_path"]).exists():
        logo = Image.open(brand["logo_path"]).convert("RGBA")
        logo = ImageOps.contain(logo, (260, 88))
        logo_x = (board_w - logo.width) // 2
        logo_y = board_h - footer_h + (footer_h - logo.height) // 2 - 6
        canvas.paste(logo.convert("RGB"), (logo_x, logo_y), logo)

    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, "JPEG", quality=92)
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="渲染肩颈/身体案例前后双视图模板")
    parser.add_argument("--brand", default="fumei", choices=sorted(CASE_LAYOUT.BRANDS.keys()))
    parser.add_argument("--date", required=True)
    parser.add_argument("--customer-name", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--section1-title", required=True)
    parser.add_argument("--section1-before", required=True)
    parser.add_argument("--section1-after", required=True)
    parser.add_argument("--section1-mode", default="front", choices=["front", "back", "oblique", "side", "neck"])
    parser.add_argument("--section2-title")
    parser.add_argument("--section2-before")
    parser.add_argument("--section2-after")
    parser.add_argument("--section2-mode", choices=["front", "back", "oblique", "side", "neck"])
    parser.add_argument("--out", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = render_board(args)
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
