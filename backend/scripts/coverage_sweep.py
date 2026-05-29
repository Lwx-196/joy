"""零成本覆盖率 sweep：N 个真实 case → 部位→角度板路由统计（无 AI 生图）.

回答 Stage B ROI 问题：front+45° 覆盖多少？profile 板真正被用到几次？
只跑 FaceMesh 角度分类 + 纯路由（不渲染、不付费）。
"""
from __future__ import annotations

import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from backend.services import case_material_coverage as cov  # noqa: E402
from backend.services import facial_region_atlas as atlas  # noqa: E402
from backend.services import treatment_panel_triptych as tri  # noqa: E402

MODEL = "/tmp/focal-p4-asset/face_landmarker.task"
ROOT = os.path.expanduser("~/Desktop/案例生成器/incoming/无创案例库/无创注射案例库")

# (患者, 术式目录) — 跨术式多样样本
CASES = [
    ("林真呈", "2026.3.30 玻尿酸注射面颊，下巴"),
    ("陈小玲", "2026.4.1菲林普利 1组注射隆鼻术前"),
    ("蔡伟玲", "2025.9.11保妥适350U下颌线、颈阔肌 海魅云境_1组山根+下巴"),
    ("吕碧英", "2025.9.12乔雅登极致_2下巴 乔雅登雅致_1苹果肌 塑公主_4太阳穴"),
    ("赵建芬", "2025.12.23塑妍萃法令纹，印第安纹，卧蚕"),
    ("甘浩文", "2025.9.11 保妥适300U注射面部皱纹、下颌线、颈阔肌、咬肌"),
    ("江佳慧", "2025.12.27缇颜3支唇，法令纹，口角、保妥适20U颏肌、保妥适20U川字纹"),
    ("胡志超", "2025.11.11乔雅登丰颜1下巴，普丽妍+海魅2额结节、面颊、太阳穴"),
    ("黄婧", "2026.3.3玻尿酸注射下巴"),
    ("曾瑜勤", "2025.11.26珂芮绮 泪沟填充"),
]

_VIEW_CN = {"front": "正面", "oblique": "45°", "profile": "侧面"}


def main() -> int:
    panel_region_hits: Counter = Counter()   # panel → 落该板的(region 次数)
    status_hits: Counter = Counter()
    photo_view_hits: Counter = Counter()
    profile_landings: list[tuple[str, str, str]] = []  # (患者, region, 原因)
    case_rows = []

    for patient, proc in CASES:
        case_dir = os.path.join(ROOT, patient, proc)
        if not os.path.isdir(case_dir):
            print(f"!! 缺目录 {patient}/{proc}", file=sys.stderr)
            continue
        cc = cov.analyze_case(case_dir, MODEL, focus_text=proc)
        for p in cc.photos:
            photo_view_hits[p.view if p.has_face else "no_face"] += 1
        regions_detail = []
        for rc in cc.regions:
            status_hits[rc.status] += 1
            if rc.status == cov.STATUS_MISSING:
                regions_detail.append(f"{rc.region}→缺")
                continue
            panel = tri._panel_for(rc)
            panel_region_hits[panel] += 1
            tag = "" if rc.status == cov.STATUS_COVERED else "*"  # *=降级
            regions_detail.append(f"{rc.region}→{_VIEW_CN[panel]}{tag}")
            if panel == atlas.VIEW_PROFILE:
                why = "no_face降级2D" if not (rc.chosen and rc.chosen.has_face) else "真侧面照"
                profile_landings.append((patient, rc.region, why))
        nfaces = sum(1 for p in cc.photos if p.has_face)
        case_rows.append((patient, len(cc.photos), nfaces, regions_detail))

    print("\n" + "=" * 78)
    print("逐 case 部位→角度板路由（* = 降级）")
    print("=" * 78)
    for patient, ntot, nface, detail in case_rows:
        print(f"\n▸ {patient}  ({nface}/{ntot} 张有脸)")
        print(f"    {'  '.join(detail)}")

    print("\n" + "=" * 78)
    print("聚合统计")
    print("=" * 78)
    tot_region = sum(panel_region_hits.values()) + status_hits.get(cov.STATUS_MISSING, 0)
    print(f"\n部位总数（去重后 per-case）: {tot_region}")
    print(f"  状态: covered={status_hits.get(cov.STATUS_COVERED,0)}  "
          f"degraded={status_hits.get(cov.STATUS_DEGRADED,0)}  "
          f"missing={status_hits.get(cov.STATUS_MISSING,0)}")
    print("\n部位落板分布:")
    for v in ("front", "oblique", "profile"):
        n = panel_region_hits.get(v, 0)
        pct = 100.0 * n / tot_region if tot_region else 0
        print(f"  {_VIEW_CN[v]:4s}: {n:3d}  ({pct:.1f}%)")
    front_oblique = panel_region_hits.get("front", 0) + panel_region_hits.get("oblique", 0)
    print(f"\n  ▶ front+45° 覆盖: {front_oblique}/{tot_region} = "
          f"{100.0*front_oblique/tot_region if tot_region else 0:.1f}%")
    print(f"  ▶ 落到 profile 板: {panel_region_hits.get('profile',0)} 次")
    if profile_landings:
        print("    profile 落点明细:")
        for patient, region, why in profile_landings:
            print(f"      {patient} · {region} · {why}")

    print("\n照片角度分布（FaceMesh 实测）:")
    for v, n in photo_view_hits.most_common():
        print(f"  {_VIEW_CN.get(v, v):6s}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
