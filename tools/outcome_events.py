"""Accumulate observable actions without assigning workflow success."""
import math
import re

WORDS = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)
BOUNDARIES = frozenset({"dispatch", "terminal-readback", "stop"})
PASSIVE = frozenset({"knowledge-read", "navigation", "scroll", "retry", "observer"})


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError(f"invalid {label}")
    try:
        valid = math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"invalid {label}")
    return value


def integer(value, label):
    number(value, label)
    if not isinstance(value, int):
        raise ValueError(f"integer required: {label}")
    return value


class EventCounter:
    def __init__(self):
        self.counts = dict(personSentences=0, personWords=0, clicks=0,
                           fieldActions=0, enteredCharacters=0, toolCalls=0)
        self.tokens = dict(input=None, output=None, cache=None)
        self.ids, self.responses, self.fields = set(), set(), set()
        self.unknown_tokens, self.boundaries = set(), {}
        self.last_time = -1

    def add(self, event):
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        event_id = event.get("id")
        if not isinstance(event_id, str) or not event_id or event_id in self.ids:
            raise ValueError("missing/duplicate event id")
        self.ids.add(event_id)
        at = number(event.get("seconds"), "monotonic event seconds")
        if at < self.last_time:
            raise ValueError("events must be in monotonic order")
        self.last_time = at
        kind = event.get("type")
        if not isinstance(kind, str):
            raise ValueError("event type must be a string")
        handlers = {"person": self.person, "click": self.click,
                    "field": self.field, "tokens": self.token_usage}
        if kind in BOUNDARIES:
            if kind in self.boundaries:
                raise ValueError("duplicate boundary")
            self.boundaries[kind] = at
        elif kind in handlers:
            handlers[kind](event)
        elif kind == "tool-attempt":
            self.counts["toolCalls"] += 1
        elif kind not in PASSIVE:
            raise ValueError("unknown event type")

    def person(self, event):
        text, spans = event.get("text"), event.get("sentences")
        if not isinstance(text, str) or not text.strip() or not isinstance(spans, list) or not spans:
            raise ValueError("person text and explicit sentence spans required")
        if any(not isinstance(span, str) or not span.strip() for span in spans):
            raise ValueError("empty/invalid sentence span")
        if re.sub(r"\s+", "", "".join(spans)) != re.sub(r"\s+", "", text):
            raise ValueError("sentence spans must partition visible text")
        self.counts["personSentences"] += len(spans)
        self.counts["personWords"] += len(WORDS.findall(text))

    def click(self, event):
        count = integer(event.get("count"), "click count")
        if count not in (1, 2):
            raise ValueError("click count must be 1 or 2")
        self.counts["clicks"] += count

    def field(self, event):
        field_id = event.get("fieldId")
        if not isinstance(field_id, str) or not field_id:
            raise ValueError("field identity required")
        self.fields.add(field_id)
        self.counts["fieldActions"] += 1
        self.counts["enteredCharacters"] += integer(event.get("characters"), "entered characters")

    def token_usage(self, event):
        response = event.get("responseId")
        if not isinstance(response, str) or not response or response in self.responses:
            raise ValueError("missing/duplicate token response")
        self.responses.add(response)
        for key in self.tokens:
            value = event.get(key)
            if value is None:
                self.unknown_tokens.add(key)
            else:
                self.tokens[key] = (self.tokens[key] or 0) + integer(value, "tokens")

    def result(self):
        tokens = {key: None if key in self.unknown_tokens else value
                  for key, value in self.tokens.items()}
        return dict(self.counts, distinctFields=len(self.fields), tokens=tokens)
