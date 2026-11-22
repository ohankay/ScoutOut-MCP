"""Detector registry. ``scan_cell`` is the single entry point used at run time (deterministic)."""

from __future__ import annotations

from .generic import Detector, DetectorMeta, Finding, Span, build_generic
from .normalize import digit_candidates, fold, word_tokens

_GENERIC: list[Detector] | None = None


def generic_detectors() -> list[Detector]:
    global _GENERIC
    if _GENERIC is None:
        _GENERIC = build_generic()
    return _GENERIC


def scan_cell(text: str, detectors: list[Detector] | None = None, ctx: dict | None = None) -> Finding:
    finding = Finding()
    if not text:
        return finding
    cands = digit_candidates(text)
    cell_ctx = dict(ctx or {})
    cell_ctx["words"] = word_tokens(text)  # shared by name, address and reference-set detectors
    for det in detectors if detectors is not None else generic_detectors():
        det.scan(text, cands, finding, cell_ctx)
    return finding


__all__ = ["Detector", "DetectorMeta", "Finding", "Span", "fold", "generic_detectors", "scan_cell"]
