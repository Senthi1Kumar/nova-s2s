"""Canonical action space + typed tool broker + capability profiles.

Extraction of `nova_hailo/tools/oem_tools.py`'s router/execute logic into
typed modules, per ROADMAP.md §2/§3 (SCENIC canonical-action-space pattern,
tool-result compressor, capability profiles). `oem_tools.OemToolGateway`
delegates here; it stays the public entry point pipeline.py imports.
"""
