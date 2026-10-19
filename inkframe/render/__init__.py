"""Rendering: everything between an RGB image and a 600 x 400 palette-index frame.

``dither`` is the ctypes wrapper around the native engine (``dither.c``) and
the registry of dither methods the UI offers.  The photo pipeline (fit,
pre-adjust, dither) lands here in Phase 3.
"""

from __future__ import annotations
