"""Structured errors every tool returns when it refuses or fails.

A refusal carries a ``code``, a one-line ``message`` and a list of ``issues``,
all found in one pass, so the caller can fix everything in a single retry.
Each issue names the ``field`` as a JSON path (``brief.keypad_entries[0].value``),
the ``problem``, and, where it is safe to show, what was ``expected`` and what
was ``received``. Never put a value in ``received`` that the brief is refused
for carrying (an SSN, a card number): say what it looked like instead.
"""

from __future__ import annotations

from typing import Any


def issue(
    field: str, problem: str, expected: str | None = None, received: Any = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"field": field, "problem": problem}
    if expected is not None:
        out["expected"] = expected
    if received is not None:
        out["received"] = received
    return out


class OnholdError(ValueError):
    """A refusal to show the caller. ``code`` is stable; ``message`` is for people."""

    code = "refused"

    def __init__(self, message: str, issues: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.message = message
        self.issues = list(issues or [])

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message, "issues": self.issues}
        if request_id:
            out["request_id"] = request_id
        return out
