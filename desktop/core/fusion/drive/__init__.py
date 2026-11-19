"""Fusion Drive: the local store the main agent and its experts share, separate from the long-term memory.

Two layers (docs/fusion-route-design-2026-11-18.md, section 8): L0 entries are short and may be injected; L1 records
hold the process detail and are read back one at a time through a pointer. The program writes; agents only read.
"""
