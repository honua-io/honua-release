"""Decode a retained local-docker PNG. A screenshot path is not a render.

Pixel size comes from the image bytes. Layer ids, labels, regions and browser
errors must already be an independent capture. This module does not browse.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from agent_e2e_pack import PackError


def require(value, reason):
    if not value:
        raise PackError(reason)


_path = Path(__file__).resolve().parents[1]/'certification/installed-clients/render_png.py'
_spec = importlib.util.spec_from_file_location('honua_agent_render_png', _path)
_png = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_png)


def observe(*, png, http_status, browser):
    require(type(http_status) is int and http_status == 200, 'render response was not HTTP 200')
    require(isinstance(png, bytes) and png.startswith(b'\x89PNG'), 'render body is not a PNG')
    require(isinstance(browser, dict) and 'screenshotPath' not in browser and 'screenshotOnly' not in browser,
            'a screenshot path is not an independent render')
    fields = {'loadedLayerIds', 'browserErrors', 'regions', 'visibleLabelSourceIds'}
    require(set(browser) == fields, 'render is missing independent browser observations')
    require(isinstance(browser['loadedLayerIds'], list) and isinstance(browser['browserErrors'], list) and
            isinstance(browser['visibleLabelSourceIds'], list) and isinstance(browser['regions'], dict),
            'render browser observations have the wrong shape')
    width, height, pixel = _png.decode_png(png)
    counted = 0
    for y in range(height):
        for x in range(width):
            red, green, blue, alpha = pixel(x, y)
            if alpha and (red, green, blue) != (255, 255, 255):
                counted += 1
    return {'qualification': False, 'httpStatus': 200, 'decodedWidth': width, 'decodedHeight': height,
            'nonBackgroundPixels': counted, 'loadedLayerIds': list(browser['loadedLayerIds']),
            'browserErrors': list(browser['browserErrors']), 'regions': browser['regions'],
            'visibleLabelSourceIds': list(browser['visibleLabelSourceIds'])}


def handoff_observations(http):
    """Candidate API reads for the customer handoff. The model transcript is not an input."""
    from agent_e2e_scenario_handoff import observe_handoff
    return observe_handoff(http)
