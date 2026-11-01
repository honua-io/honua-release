"""Deterministic customer-turn policy for the preregistered Studio assistant arm."""
from __future__ import annotations


class CustomerScriptError(ValueError):
    pass


class CustomerScript:
    """Consume only the frozen replies, plus at most one content-free continuation."""

    def __init__(self, entries):
        if not isinstance(entries, list) or any(
                not isinstance(row, dict) or set(row) != {'after', 'say'}
                or row['after'] not in ('first-reply', 'question')
                or not isinstance(row['say'], str) or not row['say'].strip() for row in entries):
            raise CustomerScriptError('invalid frozen customer script')
        if sum(row['after'] == 'first-reply' for row in entries) > 1:
            raise CustomerScriptError('ambiguous first-reply script')
        self._entries = list(entries)
        self._first_seen = False
        self._continued = False

    def reply(self, event):
        """Return a send, wait, or done decision.

        An unanswerable question or repeated stall is a failure, never an improvised answer.
        The caller obtains the event from the actual Console chat and records all actions.
        """
        if event not in ('question', 'done', 'progress', 'stall'):
            raise CustomerScriptError('unrecognized assistant event')
        if event == 'done':
            return {'kind': 'done'}
        if event == 'stall':
            if self._continued:
                raise CustomerScriptError('assistant stalled after the continuation')
            self._continued = True
            return {'kind': 'send', 'text': 'continue', 'continuation': True}
        first = not self._first_seen
        self._first_seen = True
        trigger = 'first-reply' if first and any(x['after'] == 'first-reply' for x in self._entries) else event
        if trigger in ('first-reply', 'question'):
            match = next((i for i, row in enumerate(self._entries) if row['after'] == trigger), None)
            if match is None:
                raise CustomerScriptError('no frozen answer for the assistant question')
            return {'kind': 'send', 'text': self._entries.pop(match)['say'], 'continuation': False}
        return {'kind': 'wait'}
