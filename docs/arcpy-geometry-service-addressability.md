# Stock ArcPy and remote GeometryServer operations

The 31 supported GeometryServer matrix operations remain explicit catalog
records for every existing client. Only the `arcpy` driver records are
non-addressable. Other clients and overall server support are unchanged. This
is an applicability correction, not a native pass, and does not rewrite
historical blocked/not-probed evidence.

The original generator introduced the four-client Cartesian product in
`ca80d76e` (2026-08-26), assigning all supported non-OGC Esri service cases to
REST, ArcGIS API for Python, .NET and ArcPy without checking each client's
connection API. That expansion assigned remote GeometryServer operations to
ArcPy even though the stock API does not expose a remote GeometryServer client.

[Esri's ArcPy Geometry reference](https://doc.esri.com/en/arcgis-pro/latest/arcpy/classes/geometry.html)
documents methods on geometry objects, taking geometry, distance and spatial
reference arguments. These local operations cannot prove a server request.
[ImportToolbox](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/importtoolbox.html)
and [using geoprocessing services in Python](https://pro.arcgis.com/en/pro-app/3.3/help/analysis/geoprocessing/share-analysis/using-geoprocessing-services-in-python-scripts.htm)
describe the stock remote geoprocessing-service client, which addresses a
GPServer toolbox. The separate
[Geometry Service endpoint](https://developers.arcgis.com/rest/services-reference/enterprise/geometry-service/)
is not that toolbox.

[GetPortalDescription](https://doc.esri.com/en/arcgis-pro/latest/arcpy/functions/getportaldescription.html)
returns portal information, including a geometry helper-service URL. Returning
the URL establishes discovery, not remote execution. The
[arcgis.geometry module](https://developers.arcgis.com/python/latest/api-reference/arcgis.geometry.html)
is a separate ArcGIS API for Python client and keeps its existing obligations.
Raw HTTP calls from an ArcPy-hosting Python process likewise certify the HTTP
client rather than stock ArcPy.

Read-only inspection of stock Pro 3.7.2 ArcPy Python sources found no
GeometryServer/geometryService references; geometry methods delegate local
ArcObjects. This supports the documented API distinction and is not itself a
native execution receipt. The durable policy is
`certification/sources/arcpy-geometry-addressability.v1.json`. Its scope is
exactly the ArcPy driver and GeometryServer service. A future stock ArcPy remote
API would require a reviewed policy update and real native operation evidence.
