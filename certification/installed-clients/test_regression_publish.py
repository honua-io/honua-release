"""The SDK regression harness reads the candidate's publish acknowledgement under either handle name."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import regression  # noqa: E402

FIXTURE = {
    "lifecycle": {"database": {"host": "db"}},
    "sites": {"service": "sdkreg-sites", "table": "sites", "layerName": "sites", "geometryType": "Point"},
    "area": {"service": "sdkreg-area", "table": "area", "layerName": "area", "geometryType": "Polygon"},
    "edits": {"servicePrefix": "sdkreg-edits-", "tablePrefix": "edits_", "layerName": "edits", "geometryType": "Point"},
}


class FakeApi:
    def __init__(self, field):
        self.field, self.next_id = field, 0

    def request(self, method, path, body=None):
        if path.endswith("/connections/"):
            return {"connectionId": "c1"}
        self.next_id += 1
        return {self.field: self.next_id, "serviceLayerIndex": 0} if self.field else {"serviceLayerIndex": 0}


@pytest.mark.parametrize("field", ["storageLayerId", "layerId"])
def test_publish_harness_reads_the_current_and_the_older_handle(field):
    published = regression.publish_harness(FakeApi(field), FIXTURE, ["js"], "pw")
    assert [published["sites"]["layerId"], published["area"]["layerId"], published["edits"]["js"]["layerId"]] == [1, 2, 3]


def test_publish_harness_prefers_the_current_handle():
    api = FakeApi("storageLayerId")
    request = api.request
    api.request = lambda method, path, body=None: {**request(method, path, body), "layerId": 0}
    assert regression.publish_harness(api, FIXTURE, [], "pw")["sites"]["layerId"] == 1


def test_publish_without_a_handle_fails_the_run():
    with pytest.raises(regression.RegressionError, match="returned no layer id"):
        regression.publish_harness(FakeApi(None), FIXTURE, [], "pw")
