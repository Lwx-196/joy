"""Regression test: export_delivery_batch.py must wire SimulationDeliveryGate.

Plan §P2.2 + interface-auditor B.3 (Wave 2 hardening):
The CLI `python -m backend.scripts.export_delivery_batch` is the only
non-test caller of `DeliveryGate.list_deliverables()`. If it forgets to
pass `simulation_gate=`, the SimulationDeliveryGate becomes orphan and
no simulation candidate ever surfaces in delivery — half-baked wire-up
per dev-spec §1.3 红线.

This test is a structural regression guard: it imports the script module
and asserts (a) the SimulationDeliveryGate symbol is imported, and (b)
the `export()` function source code references `simulation_gate=`.
"""
from __future__ import annotations

import inspect

from backend.scripts import export_delivery_batch


def test_export_delivery_batch_imports_simulation_delivery_gate():
    assert hasattr(export_delivery_batch, "SimulationDeliveryGate"), (
        "export_delivery_batch.py must import SimulationDeliveryGate so "
        "simulation candidates surface in delivery (plan §P2.2)."
    )


def test_export_function_wires_simulation_gate():
    source = inspect.getsource(export_delivery_batch.export)
    assert "simulation_gate=" in source, (
        "export() must call list_deliverables(simulation_gate=...) — "
        "otherwise SimulationDeliveryGate is orphan dead code."
    )
