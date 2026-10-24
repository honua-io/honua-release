"""Authenticate raw OCI image inputs; image identity alone grants no admission."""
from __future__ import annotations

import hashlib
import json
import re


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate OCI input field")
        result[key] = value
    return result


def verify_image_chain(index_bytes: bytes, manifest_bytes: bytes, config_bytes: bytes,
                       *, admitted_index: str) -> dict:
    """Join an independently admitted index to its unique Linux amd64 config.

    The caller authenticates admitted_index from governance. A containerd index
    Id and a classic Docker config Id remain distinct identities in the result.
    No registry fallback, runtime execution or producer receipt is inferred.
    """
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", admitted_index):
        raise ValueError("immutable admitted OCI index required")
    def sha(data):
        if type(data) is not bytes:
            raise ValueError("original OCI bytes required")
        return "sha256:" + hashlib.sha256(data).hexdigest()

    if sha(index_bytes) != admitted_index:
        raise ValueError("OCI index bytes differ from admission")
    index = json.loads(index_bytes, object_pairs_hook=_object)
    if index.get("schemaVersion") != 2 or index.get("mediaType") != "application/vnd.oci.image.index.v1+json":
        raise ValueError("OCI index contract differs")
    selected = [row for row in index["manifests"]
                if row.get("platform", {}).get("os") == "linux"
                and row.get("platform", {}).get("architecture") == "amd64"]
    if len(selected) != 1:
        raise ValueError("OCI index must select exactly one Linux amd64 manifest")
    descriptor = selected[0]
    manifest_type = "application/vnd.oci.image.manifest.v1+json"
    if (descriptor.get("mediaType") != manifest_type
            or descriptor.get("digest") != sha(manifest_bytes)
            or type(descriptor.get("size")) is not int
            or descriptor["size"] != len(manifest_bytes)):
        raise ValueError("OCI platform manifest bytes differ")
    manifest = json.loads(manifest_bytes, object_pairs_hook=_object)
    if manifest.get("schemaVersion") != 2 or manifest.get("mediaType") != manifest_type:
        raise ValueError("OCI platform manifest contract differs")
    config_descriptor = manifest["config"]
    if (config_descriptor.get("mediaType") != "application/vnd.oci.image.config.v1+json"
            or config_descriptor.get("digest") != sha(config_bytes)
            or type(config_descriptor.get("size")) is not int
            or config_descriptor["size"] != len(config_bytes)):
        raise ValueError("OCI config bytes differ")
    config = json.loads(config_bytes, object_pairs_hook=_object)
    if config.get("os") != "linux" or config.get("architecture") != "amd64":
        raise ValueError("OCI config platform differs")
    return {"indexDigest": admitted_index, "platformManifestDigest": descriptor["digest"],
            "configDigest": config_descriptor["digest"], "os": "linux", "architecture": "amd64",
            "sourceAdmitted": False, "candidateQualified": False}
