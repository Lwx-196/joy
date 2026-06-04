"""Compatibility facade for backend.services.ai_generation.adapter."""
from __future__ import annotations

import sys as _sys

from .services.ai_generation import adapter as _adapter

_FACADE_NAME = __name__
_PARENT_NAME, _ATTR_NAME = _FACADE_NAME.rsplit(".", 1)
globals().update(_adapter.__dict__)
setattr(_sys.modules[_PARENT_NAME], _ATTR_NAME, _adapter)
_sys.modules[_FACADE_NAME] = _adapter
