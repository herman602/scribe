"""Deterministic parser registry."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.domain.parsing import ParserEmail, TransactionParser


class DefaultParserRegistry:
    """Order supported parsers by explicit priority and stable name."""

    def __init__(self, parsers: Iterable[TransactionParser]) -> None:
        ordered = tuple(sorted(parsers, key=lambda item: (item.priority, item.name)))
        if len({item.name for item in ordered}) != len(ordered):
            raise ValueError("parser names must be unique")
        self._parsers = ordered

    def candidates_for(self, email: ParserEmail) -> Sequence[TransactionParser]:
        """Return supported parsers in deterministic order."""
        return tuple(parser for parser in self._parsers if parser.supports(email))
