import hashlib
import json

import pytest

from python_image_binding import verify_image_chain


def encode(value):
    return json.dumps(value, sort_keys=True).encode()


def sha(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def chain(*, duplicate=False, config_platform="amd64"):
    config = encode({"os": "linux", "architecture": config_platform})
    manifest = encode({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                       "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                                  "digest": sha(config), "size": len(config)}})
    descriptor = {"mediaType": "application/vnd.oci.image.manifest.v1+json", "digest": sha(manifest),
                  "size": len(manifest), "platform": {"os": "linux", "architecture": "amd64"}}
    index = encode({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": [descriptor] * (2 if duplicate else 1)})
    return index, manifest, config


def test_index_and_config_ids_remain_distinct_but_authenticated():
    index, manifest, config = chain()
    actual = verify_image_chain(index, manifest, config, admitted_index=sha(index))
    assert actual["indexDigest"] == sha(index) != actual["configDigest"] == sha(config)
    assert actual["platformManifestDigest"] == sha(manifest)
    assert actual["sourceAdmitted"] is False
    assert actual["candidateQualified"] is False


@pytest.mark.parametrize("mutation", ["index-bytes", "manifest-bytes", "config-bytes", "ambiguous-platform", "coherent-wrong-platform", "duplicate-key"])
def test_untrusted_or_ambiguous_image_chain_is_refused(mutation):
    index, manifest, config = chain(duplicate=mutation == "ambiguous-platform",
                                    config_platform="arm64" if mutation == "coherent-wrong-platform" else "amd64")
    admitted = sha(index)
    if mutation == "index-bytes": index += b" "
    if mutation == "manifest-bytes": manifest += b" "
    if mutation == "config-bytes": config += b" "
    if mutation == "duplicate-key":
        index = index.replace(b'"schemaVersion": 2', b'"schemaVersion": 1, "schemaVersion": 2')
        admitted = sha(index)
    with pytest.raises(ValueError):
        verify_image_chain(index, manifest, config, admitted_index=admitted)
