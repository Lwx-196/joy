from pathlib import Path

from PIL import Image

from backend.routes import cases


def test_existing_mpo_simulation_input_is_materialized_as_rgb_jpeg(tmp_path: Path) -> None:
    source = tmp_path / "source.JPG"
    first = Image.new("RGB", (12, 10), "red")
    second = Image.new("RGB", (12, 10), "blue")
    first.save(source, format="MPO", save_all=True, append_images=[second])

    resolved = cases._resolve_simulation_image_input(
        tmp_path,
        path="source.JPG",
        image=None,
        role="after",
        stamp="stamp-1",
        required=True,
    )

    assert resolved == tmp_path / ".case-workbench-simulation-inputs" / "stamp-1" / "after-normalized.jpg"
    assert resolved.is_file()
    with Image.open(resolved) as normalized:
        assert normalized.format == "JPEG"
        assert normalized.mode == "RGB"


def test_existing_regular_jpeg_simulation_input_keeps_original_path(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    Image.new("RGB", (12, 10), "red").save(source, format="JPEG")

    resolved = cases._resolve_simulation_image_input(
        tmp_path,
        path="source.jpg",
        image=None,
        role="after",
        stamp="stamp-2",
        required=True,
    )

    assert resolved == source.resolve()
