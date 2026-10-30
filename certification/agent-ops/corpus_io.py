"""Load town layers from the agent corpus. Geometry identity is computed here, from geometry only."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from functools import lru_cache
from pathlib import Path

from constants import LAYERS

CORPUS = Path(__file__).resolve().parents[1] / "agent-e2e" / "corpus"


def load_corpus_oracle():
    """Import certification/agent-e2e/corpus/oracle.py without editing it."""
    path = CORPUS / "oracle.py"
    spec = importlib.util.spec_from_file_location("honua_corpus_oracle", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load corpus oracle at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def corpus_oracle():
    return load_corpus_oracle()


def corpus_exact():
    return corpus_oracle().exact


def geometry_canonical(geometry: dict) -> str:
    return json.dumps(
        [geometry.get("type"), geometry.get("coordinates")],
        separators=(",", ":"),
    )


def geometry_digest(geometry: dict) -> str:
    """Identity of a geometry. Called only while reading a corpus file."""
    return hashlib.sha256(geometry_canonical(geometry).encode("utf-8")).hexdigest()


def load_layer(town_key: str, layer: str) -> dict:
    path = CORPUS / f"town-{town_key}" / f"{layer}.4326.geojson"
    document = json.loads(path.read_text(encoding="utf-8"))
    features = document["features"]
    if not isinstance(features, list) or not features:
        raise ValueError(f"{path} has no features")
    digests = {}
    canonical = {}
    for feature in features:
        source_id = feature["properties"]["source_id"]
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("feature is missing source_id")
        digests[source_id] = geometry_digest(feature["geometry"])
        canonical[source_id] = geometry_canonical(feature["geometry"])
    return {"features": features, "digests": digests, "canonical": canonical}
