"""Registry of crawlable sites. Add a new site by writing an adapter and
listing it here — the engine, runner, health monitor and validator are shared."""
from __future__ import annotations

from .lostminis.adapter import LostMinisAdapter
from .solegends.adapter import SolegendsAdapter

SITES = {
    "lostminis": LostMinisAdapter,
    "solegends": SolegendsAdapter,
}
