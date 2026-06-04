"""YAML load/dump that prefers the libyaml C implementation when available.

The validator, reconcile, and report passes each parse every page.yaml in the
archive (tens of thousands of files); the C loader is ~5-10x faster than the pure
Python one, which matters a lot at this scale. Falls back transparently.
"""
from __future__ import annotations

import yaml

try:  # libyaml-backed (fast)
    from yaml import CSafeDumper as _Dumper, CSafeLoader as _Loader
except ImportError:  # pure-Python fallback
    from yaml import SafeDumper as _Dumper, SafeLoader as _Loader


def load(text: str):
    return yaml.load(text, Loader=_Loader)


def dump(obj, **kwargs) -> str:
    return yaml.dump(obj, Dumper=_Dumper, **kwargs)
