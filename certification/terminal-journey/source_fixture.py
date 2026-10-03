"""Authored GeoServices fixture source, never an implementation of Honua APIs."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "build-source.json").read_text())
FEATURES = [{"attributes": {"OBJECTID": f["properties"]["journey_id"], **f["properties"]},
             "geometry": {"x": f["geometry"]["coordinates"][0], "y": f["geometry"]["coordinates"][1],
                          "spatialReference": {"wkid": 4326}}} for f in FIXTURE["features"]]
FIELDS = [{"name": "OBJECTID", "type": "esriFieldTypeOID", "alias": "OBJECTID"},
          {"name": "journey_id", "type": "esriFieldTypeInteger", "alias": "journey_id"}]


def respond(path, params):
    if path.endswith("/query"):
        if params.get("returnCountOnly", [""])[0].lower() == "true":
            return {"count": len(FEATURES)}
        if params.get("returnIdsOnly", [""])[0].lower() == "true":
            return {"objectIdFieldName": "OBJECTID", "objectIds": [f["attributes"]["OBJECTID"] for f in FEATURES]}
        offset = int(params.get("resultOffset", [0])[0])
        count = int(params.get("resultRecordCount", [len(FEATURES)])[0])
        rows = FEATURES[offset:offset + count]
        requested = params.get("objectIds", [None])[0]
        if requested:
            ids = set(map(int, requested.split(",")))
            rows = [f for f in rows if f["attributes"]["OBJECTID"] in ids]
        return {"objectIdFieldName": "OBJECTID", "geometryType": "esriGeometryPoint", "fields": FIELDS,
                "spatialReference": {"wkid": 4326}, "features": rows,
                "exceededTransferLimit": offset + count < len(FEATURES)}
    if path.endswith("/0"):
        return {"id": 0, "name": "journey_source", "type": "Feature Layer", "geometryType": "esriGeometryPoint",
                "objectIdField": "OBJECTID", "fields": FIELDS, "hasAttachments": False,
                "extent": {"xmin": -1, "ymin": -1, "xmax": 1, "ymax": 1, "spatialReference": {"wkid": 4326}},
                "advancedQueryCapabilities": {"supportsPagination": True}, "capabilities": "Query", "maxRecordCount": 1000}
    return {"currentVersion": 11.2, "serviceDescription": "Authored journey import source",
            "layers": [{"id": 0, "name": "journey_source"}], "spatialReference": {"wkid": 4326},
            "capabilities": "Query", "maxRecordCount": 1000}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        self.write_response(parsed.path, parse_qs(parsed.query))

    def do_POST(self):
        parsed = urlsplit(self.path)
        size = int(self.headers.get("Content-Length", 0))
        if size > 65536:
            self.send_error(413)
            return
        self.write_response(parsed.path, parse_qs(self.rfile.read(size).decode()))

    def write_response(self, path, params):
        try:
            data = json.dumps(respond(path.rstrip("/"), params)).encode()
        except (ValueError, TypeError):
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
