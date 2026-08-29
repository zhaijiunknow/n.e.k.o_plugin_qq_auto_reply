"""Test bootstrap: make the plugin's ``lib/`` and the N.E.K.O app root importable.

The plugin ships vendored runtime deps in ``lib/`` (e.g. ``utils.connection``) and
relies on the N.E.K.O root (``plugin`` / ``config`` / ``utils`` / ``main_logic``).
pytest's default sys.path may not cover either at collection time, so prepend them
so the plugin's own tests can ``from plugin...`` and ``from utils.connection...``.
"""
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent           # qq_auto_reply/
_LIB = _HERE / "lib"                               # vendored deps
_APP_ROOT = Path(__file__).resolve().parents[3]    # N.E.K.O root (plugin/utils/config)

for _root in (_LIB, _APP_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
