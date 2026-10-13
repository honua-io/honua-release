"""Retain wire responses before applying stage-specific status assertions."""
from requests.exceptions import JSONDecodeError


def response_body(response):
    try:
        return response.json()
    except JSONDecodeError:
        # Framework authorization responses may have an empty body. Keep the
        # actual text; callers must still assert status and accepted-job shape.
        return {"rawBody": response.text}
