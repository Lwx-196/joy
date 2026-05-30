"""Phase 3: 钉住"cv2.imread 自动应用 EXIF orientation"的假设。

Phase 3 实测所有分类环境（/usr/bin/python3 生产默认 + homebrew + 主 venv，均 cv2 4.13）
cv2.imread 都自动应用 EXIF orientation，故 pose backend 不另加 EXIF 修正（加了反而因 cv2 vs PIL
JPEG 解码差异破坏 facemesh 字节一致）。若未来 cv2 降级 / 改默认行为致此假设失效，本测试 fail
提醒——届时需在图像 loader 显式修正 EXIF（且重测 facemesh 字节一致）。
"""
from __future__ import annotations

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
pytest.importorskip("PIL")


def test_cv2_imread_applies_exif_orientation(tmp_path):
    from PIL import Image

    # landscape 原始像素 W=80 × H=40；EXIF Orientation=6（显示时顺时针 90° → portrait）。
    arr = np.zeros((40, 80, 3), dtype=np.uint8)
    arr[:, :40] = 255
    img = Image.fromarray(arr)
    exif = img.getexif()
    exif[274] = 6  # 274 = Orientation tag
    path = tmp_path / "rot_exif6.jpg"
    img.save(path, exif=exif)

    out = cv2.imread(str(path))
    assert out is not None
    h, w = out.shape[:2]
    # 应用 EXIF → portrait (H=80,W=40)；不应用 → 保持 landscape (H=40,W=80)
    assert (h, w) == (80, 40), (
        f"cv2.imread 未按预期应用 EXIF orientation（得 (H,W)={(h, w)}，期望 (80,40)）—— "
        f"pose backend 依赖 cv2 自动应用 EXIF 的假设已失效，需在图像 loader 显式修正 EXIF "
        f"并重测 facemesh 字节一致性。")
