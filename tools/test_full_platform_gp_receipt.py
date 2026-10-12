import importlib.util
import json
from pathlib import Path

import pytest

DRILL = Path(__file__).resolve().parents[1] / "e2e" / "dr-drill" / "full_platform.py"


class DrillStarted(Exception):
    pass


@pytest.fixture
def drill(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("full_platform_under_test", DRILL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    started = []

    def refuse_to_start(output):
        # Standing in for the destructive stack: reaching it means every precondition passed.
        started.append(output)
        raise DrillStarted

    monkeypatch.setattr(module, "Stack", refuse_to_start)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("HONUA_GP_DR_RECEIPT", raising=False)
    module.started = started
    return module


def gp_receipt(path, value=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value if value is not None else {"schema": "gp", "passed": 12}), encoding="utf-8")
    return path


def test_readme_standalone_invocation_without_gp_receipt_fails_before_the_drill(drill, tmp_path):
    output = tmp_path / "out"
    with pytest.raises(SystemExit) as exit_info:
        drill.main(["--output", str(output)])
    message = str(exit_info.value.code)
    assert "GP restore and crash receipt not found" in message
    assert str(tmp_path / "artifacts" / "gp-candidate" / "receipt.json") in message
    assert "nothing was started or destroyed" in message
    assert drill.started == []
    assert not output.exists()


@pytest.mark.parametrize("raw", ["{not json", "[]", "{}", '"receipt"'])
def test_unusable_gp_receipt_fails_before_the_drill(drill, tmp_path, raw):
    path = tmp_path / "gp.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(SystemExit, match="nothing was started or destroyed"):
        drill.main(["--output", str(tmp_path / "out"), "--gp-receipt", str(path)])
    assert drill.started == []


def test_default_gp_receipt_is_the_gp_outputs_artifact(drill, tmp_path):
    gp_receipt(tmp_path / "artifacts" / "gp-candidate" / "receipt.json")
    with pytest.raises(DrillStarted):
        drill.main(["--output", str(tmp_path / "out")])
    assert drill.started == [tmp_path / "out"]


def test_environment_and_flag_select_the_gp_receipt(drill, tmp_path, monkeypatch):
    monkeypatch.setenv("HONUA_GP_DR_RECEIPT", str(gp_receipt(tmp_path / "env" / "receipt.json")))
    with pytest.raises(DrillStarted):
        drill.main(["--output", str(tmp_path / "out")])

    monkeypatch.setenv("HONUA_GP_DR_RECEIPT", str(tmp_path / "missing.json"))
    with pytest.raises(DrillStarted):
        drill.main(["--output", str(tmp_path / "out"), "--gp-receipt", str(gp_receipt(tmp_path / "flag.json"))])
    assert len(drill.started) == 2


def test_signed_receipt_embeds_the_receipt_loaded_before_the_drill():
    text = DRILL.read_text(encoding="utf-8")
    assert 'os.environ["HONUA_GP_DR_RECEIPT"]' not in text
    assert text.index("load_gp_receipt(args.gp_receipt)") < text.index("stack = Stack(args.output)")
    assert 'receipt["geoprocessingOutputs"] = geoprocessing_outputs' in text


class ServedStack:
    """The two read-back responses the drill sees, in the shapes the pinned server answers."""

    def __init__(self, esri_properties):
        self.esri_properties = esri_properties

    def request(self, method, path, *args, **kwargs):
        if path.startswith("/ogc/features/"):
            return {"type": "Feature", "id": 3,
                    "geometry": {"type": "Point", "coordinates": [-156.5, 20.9]},
                    "properties": {"properties": {"name": "dr-inserted-charlie"}}}
        return {"objectIdFieldName": "objectid", "features": [
            {"attributes": {"objectid": 1, "properties": '{"name": "dr-imported-alpha"}'},
             "geometry": {"x": -156.33, "y": 20.75}},
            {"attributes": {"objectid": 3, "properties": self.esri_properties},
             "geometry": {"x": -156.5, "y": 20.9}}]}


@pytest.mark.parametrize("esri_properties", [
    '{"name": "dr-inserted-charlie"}',  # JSON column as text (honua-server#5147)
    {"name": "dr-inserted-charlie"},    # JSON column as an object (servers before #5147)
])
def test_inserted_feature_reads_back_through_featureserver_json_text(drill, esri_properties):
    drill.assert_inserted_feature_served(ServedStack(esri_properties), 0, 3)


@pytest.mark.parametrize("esri_properties", ['{"name": "other"}', "not json", '["dr-inserted-charlie"]', None])
def test_featureserver_row_without_the_inserted_marker_is_refused(drill, esri_properties):
    with pytest.raises(RuntimeError, match="served 0 rows"):
        drill.assert_inserted_feature_served(ServedStack(esri_properties), 0, 3)
