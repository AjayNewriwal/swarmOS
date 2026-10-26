"""Tag normalization helpers used by the SwarmOS sample workspace."""

from collections.abc import Iterable


def normalize_tags(tags: Iterable[str]) -> list[str]:
    """Return normalized tags.

    Expected behaviour is described by ``tests/test_target.py``.
    """
    raise NotImplementedError("normalize_tags is not implemented yet")