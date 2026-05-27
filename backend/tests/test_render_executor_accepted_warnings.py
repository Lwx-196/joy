"""P0.2-C — render_executor._accepted_warning_match 作用域 bug 回归测试。

旧代码在 r''' 内嵌子进程脚本里隐式引用 `selection_plan` 自由变量；P0.2-C 改为
显式入参，让函数纯粹、可测试、不依赖函数定义顺序。

测试包括：
- 结构性：函数签名含 3 个参数 + 4 个 caller 都显式传 accepted_warnings
- 行为性：在 controlled namespace exec 函数定义 + 调用验证匹配逻辑
"""
from __future__ import annotations

import re

from backend import render_executor


def _runner_src() -> str:
    return render_executor._build_render_runner()


# ---------- Structural ----------


def test_accepted_warning_match_signature_takes_accepted_warnings_param() -> None:
    """函数签名必须显式接收 accepted_warnings 第 3 参数。"""
    src = _runner_src()
    assert re.search(
        r"def _accepted_warning_match\(\s*text\s*,\s*selected_files_by_slot\s*,\s*accepted_warnings\s*\)",
        src,
    ), "_accepted_warning_match 必须显式接 accepted_warnings 参数，不可隐式依赖 selection_plan"


def test_accepted_warning_match_body_no_longer_reads_selection_plan() -> None:
    """函数体不应再用 `selection_plan.get('accepted_warnings')` 自由变量查找。"""
    src = _runner_src()
    func_match = re.search(
        r"def _accepted_warning_match\([^)]*\):(.*?)\n(?:def |\nclass )",
        src,
        re.DOTALL,
    )
    assert func_match, "找不到 _accepted_warning_match 函数体"
    body = func_match.group(1)
    assert "selection_plan.get" not in body, (
        "_accepted_warning_match 函数体仍引用 selection_plan 自由变量；"
        "应使用 accepted_warnings 入参"
    )


def test_all_callers_pass_accepted_warnings_explicitly() -> None:
    """4 个 caller 必须显式传 accepted_warnings（不靠隐式 closure）。"""
    src = _runner_src()
    # 找到所有调用点，跳过签名行（含 `def `）。允许 arg 中有嵌套括号。
    lines = src.splitlines()
    call_lines = [
        ln for ln in lines
        if "_accepted_warning_match(" in ln and "def _accepted_warning_match" not in ln
    ]
    assert len(call_lines) >= 4, f"应有至少 4 个调用点行，实测 {len(call_lines)}"
    for line in call_lines:
        # 每个调用点都必须传 accepted_warnings 风格的第三参数（容忍命名 _acc_warnings / accepted_warnings）。
        assert (
            "_acc_warnings" in line
            or "accepted_warnings" in line
            or "selection_plan.get" in line
        ), f"caller 未显式传 accepted_warnings: {line!r}"


# ---------- Behavioral (exec the function in a controlled namespace) ----------


def _extract_func_src(src: str, func_name: str) -> str:
    """提取 def `func_name` 到下一个 def/class 之前的完整源码。"""
    m = re.search(
        rf"\ndef {func_name}\([^)]*\):.*?(?=\n(?:def |class ))",
        src,
        re.DOTALL,
    )
    assert m, f"can't extract {func_name}"
    return m.group(0)


class _FakeCaseLayout:
    """Stub for `_warning_slot` to enable isolated exec testing."""
    ANGLE_LABELS = {"front": "正面", "oblique": "45°", "side": "侧面"}
    ANGLE_SLOTS = ["front", "oblique", "side"]


def test_accepted_warning_match_behavior_matches_slot_code_files() -> None:
    """exec 函数在隔离 namespace，验证 slot+code+files 全匹配时返回 accepted dict。"""
    src = _runner_src()
    namespace: dict = {"re": re, "case_layout": _FakeCaseLayout()}
    exec(_extract_func_src(src, "_warning_slot"), namespace)
    exec(_extract_func_src(src, "_accepted_warning_match"), namespace)

    selected_files_by_slot = {"front": {"a.jpg", "b.jpg"}}
    accepted_warnings = [{
        "slot": "front",
        "code": "crop_touches_frame",
        "selected_files": ["a.jpg"],
        "message_contains": "裁切贴边",
    }]
    text = "正式正面图存在面部/主体裁切贴边 (a.jpg)"

    matched = namespace["_accepted_warning_match"](text, selected_files_by_slot, accepted_warnings)
    assert matched is not None
    assert matched["code"] == "crop_touches_frame"
    assert matched["slot"] == "front"
    assert matched["message"] == text


def test_accepted_warning_match_behavior_returns_none_when_no_accept() -> None:
    """无 accepted_warnings 时返回 None（不再访问 free var）。"""
    src = _runner_src()
    namespace: dict = {"re": re, "case_layout": _FakeCaseLayout()}
    exec(_extract_func_src(src, "_warning_slot"), namespace)
    exec(_extract_func_src(src, "_accepted_warning_match"), namespace)

    matched = namespace["_accepted_warning_match"]("warning text", {"front": {"a.jpg"}}, [])
    assert matched is None
    matched = namespace["_accepted_warning_match"]("warning text", {"front": {"a.jpg"}}, None)
    assert matched is None
