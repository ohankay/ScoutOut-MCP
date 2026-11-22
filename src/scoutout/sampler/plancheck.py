"""Estimated-plan inspection: find operators that read a whole index/heap (spec §12)."""

from __future__ import annotations

import re

SCAN_OPS = {"Table Scan", "Clustered Index Scan", "Index Scan"}


def physical_ops(plan_xml: str) -> list[str]:
    return re.findall(r'PhysicalOp="([^"]+)"', plan_xml)


def scan_operators(plan_xml: str) -> list[str]:
    return [op for op in physical_ops(plan_xml) if op in SCAN_OPS]
