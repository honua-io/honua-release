"""Retain wire responses before applying stage-specific status assertions."""
import json


def response_body(response):
    try:
        return response.json()
    except json.JSONDecodeError:
        # Framework authorization responses may have an empty body. Keep the
        # actual text; callers must still assert status and accepted-job shape.
        return {"rawBody": response.text}
