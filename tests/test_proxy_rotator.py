"""Tests for ProxyRotator.pick_fresh selection logic.

Runs standalone (``python tests/test_proxy_rotator.py``) or under pytest.
The DB layer is monkeypatched, so no SQLite file is touched.
"""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core import database
from src.core.models import ProxyStateDB
from src.services import proxy_rotator
from src.services.proxy_rotator import get_proxy_rotator


def _state(url, status="benched", last_gen=None, cooldown_until=None):
    return ProxyStateDB(
        proxy_url=url,
        label=url,
        status=status,
        last_generation_at=last_gen,
        cooldown_until=cooldown_until,
    )


def _patch_states(states):
    """Monkeypatch db.get_all_proxy_states to return ``states``."""
    async def _fake():
        return states
    database.db.get_all_proxy_states = _fake  # type: ignore[assignment]


def test_picks_only_rested_benched_proxies():
    now = datetime.utcnow()
    old = now - timedelta(hours=30)     # rested > 24h window
    recent = now - timedelta(hours=2)   # too recent
    _patch_states([
        _state("http://a", status="benched", last_gen=old),
        _state("http://b", status="benched", last_gen=recent),    # too recent → skip
        _state("http://c", status="active", last_gen=old),        # active → skip
        _state("http://d", status="cooldown", cooldown_until=now + timedelta(hours=10)),  # cooling → skip
        _state("http://e", status="benched", last_gen=None),      # never used → eligible
    ])
    picked = asyncio.run(get_proxy_rotator().pick_fresh(5))
    assert set(picked) == {"http://a", "http://e"}, picked
    print("OK: only rested benched proxies picked")


def test_longest_rested_first():
    now = datetime.utcnow()
    _patch_states([
        _state("http://recent", status="benched", last_gen=now - timedelta(hours=25)),
        _state("http://oldest", status="benched", last_gen=now - timedelta(hours=80)),
        _state("http://never", status="benched", last_gen=None),
    ])
    picked = asyncio.run(get_proxy_rotator().pick_fresh(3))
    # never-used first, then oldest last_generation, then more recent.
    assert picked == ["http://never", "http://oldest", "http://recent"], picked
    print("OK: ordering is longest-rested first")


def test_respects_k_and_exclude():
    now = datetime.utcnow()
    old = now - timedelta(hours=40)
    _patch_states([
        _state("http://a", last_gen=old),
        _state("http://b", last_gen=old),
        _state("http://c", last_gen=old),
    ])
    picked = asyncio.run(get_proxy_rotator().pick_fresh(2, exclude={"http://a"}))
    assert "http://a" not in picked, picked
    assert len(picked) == 2, picked
    print("OK: k limit and exclude respected")


def test_expired_cooldown_is_eligible():
    now = datetime.utcnow()
    _patch_states([
        # cooldown timestamp in the past but status not yet ticked over — pick_fresh
        # only considers benched, so an expired-but-still-cooldown row is NOT picked.
        _state("http://stillcooldown", status="cooldown", cooldown_until=now - timedelta(hours=1)),
        _state("http://benched", status="benched", last_gen=now - timedelta(hours=30)),
    ])
    picked = asyncio.run(get_proxy_rotator().pick_fresh(5))
    assert picked == ["http://benched"], picked
    print("OK: only benched rows are candidates (tick() promotes cooldown->benched)")


if __name__ == "__main__":
    test_picks_only_rested_benched_proxies()
    test_longest_rested_first()
    test_respects_k_and_exclude()
    test_expired_cooldown_is_eligible()
    print("\nAll ProxyRotator tests passed.")
