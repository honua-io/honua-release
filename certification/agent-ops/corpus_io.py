"""Load town layers from the agent corpus. The corpus files stay the source of feature identity."""

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


def load_layer(town_key: str, layer: str) -> list[dict]:
    path = CORPUS / f"town-{town_key}" / f"{layer}.4326.geojson"
    document = json.loads(path.read_text(encoding="utf-8"))
    features = document["features"]
    if not isinstance(features, list) or not features:
        raise ValueError(f"{path} has no features")
    return features


def geometry_hash(feature: dict) -> str:
    blob = json.dumps(feature["geometry"], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def signature(features: list[dict]) -> dict:
    rows = []
    for feature in features:
        source_id = feature["properties"]["source_id"]
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("feature is missing source_id")
        rows.append((source_id, geometry_hash(feature)))
    rows.sort()
    ids = [row[0] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate source_id")
    return {"ids": ids, "count": len(ids), "geometry": {row[0]: row[1] for row in rows}}


def town_signatures(town_key: str) -> dict[str, dict]:
    return {layer: signature(load_layer(town_key, layer)) for layer in LAYERS}
