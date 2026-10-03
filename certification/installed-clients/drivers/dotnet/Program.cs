// .NET SDK driver for the installed-client regression suite.
//
// Runs against the manifest-pinned Honua.Sdk restored from nuget.org only and calls its client
// interfaces, never raw HTTP. Prints one JSON observation per contract step; the runner evaluates
// the oracles.
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Honua.Sdk;
using Honua.Sdk.Abstractions;
using Honua.Sdk.Abstractions.Features;
using Honua.Sdk.Admin;
using Honua.Sdk.Admin.Models;
using Honua.Sdk.Catalogs.Stac;
using Honua.Sdk.Catalogs.Stac.Models;
using Honua.Sdk.GeoServices.FeatureServer;
using Honua.Sdk.GeoServices.FeatureServer.Exceptions;
using Honua.Sdk.GeoServices.FeatureServer.Models;
using Honua.Sdk.OgcFeatures;
using Honua.Sdk.OgcFeatures.Models;
using Honua.Sdk.Processes;
using Microsoft.Extensions.DependencyInjection;

var plan = JsonNode.Parse(File.ReadAllText(Environment.GetEnvironmentVariable("SDKREG_PLAN")!))!.AsObject();
var baseUrl = new Uri(plan["baseUrl"]!.GetValue<string>().TrimEnd('/') + "/");
var apiKey = Environment.GetEnvironmentVariable("SDKREG_API_KEY")!;
var bearer = Environment.GetEnvironmentVariable("SDKREG_BEARER")!;
var dbPassword = Environment.GetEnvironmentVariable("SDKREG_DB_PASSWORD")!;

var api = new Dictionary<string, Dictionary<string, string>>
{
    ["sdk-auth"] = new()
    {
        ["api-key-query"] = "IHonuaFeatureServerClient.QueryAsync (HonuaSdkOptions.ApiKey)",
        ["bearer-admin-list"] = "IHonuaAdminClient.ListServicesAsync (HonuaSdkOptions.BearerToken)",
        ["bearer-query"] = "IHonuaFeatureServerClient.QueryAsync (HonuaSdkOptions.BearerToken)",
        ["anonymous-refused"] = "IHonuaFeatureServerClient.QueryAsync (no credential)",
    },
    ["sdk-admin-lifecycle"] = new()
    {
        ["create-datasource"] = "IHonuaAdminClient.CreateConnectionAsync",
        ["test-datasource"] = "IHonuaAdminClient.TestConnectionAsync",
        ["publish"] = "IHonuaAdminClient.PublishLayerAsync",
        ["list"] = "IHonuaAdminClient.ListLayersAsync",
        ["served"] = "IHonuaFeatureServerClient.QueryAsync",
        ["unpublish"] = "IHonuaAdminClient.SetLayerEnabledAsync(false)",
        ["unpublished-refused"] = "IHonuaFeatureServerClient.QueryAsync",
    },
    ["sdk-geoservices"] = new()
    {
        ["query"] = "IHonuaFeatureServerClient.QueryAsync",
        ["ids"] = "IHonuaFeatureServerClient.QueryIdsAsync",
        ["count"] = "IHonuaFeatureServerClient.QueryCountAsync",
        ["resolve-edit-ids"] = "IHonuaFeatureServerClient.QueryAsync",
        ["apply-edits-add"] = "IHonuaFeatureServerEditClient.ApplyEditsAsync(adds)",
        ["apply-edits-update"] = "IHonuaFeatureServerEditClient.ApplyEditsAsync(updates)",
        ["apply-edits-delete"] = "IHonuaFeatureServerEditClient.ApplyEditsAsync(deletes)",
        ["edits-state"] = "IHonuaFeatureServerClient.QueryAsync",
        ["add-attachment"] = "IHonuaFeatureAttachmentClient.AddAttachmentAsync",
        ["query-attachments"] = "IHonuaFeatureServerClient.QueryAttachmentsAsync",
    },
    ["sdk-ogc-features"] = new()
    {
        ["items-bbox"] = "IHonuaOgcFeaturesClient.GetItemsAsync(OgcItemsParams.Bbox)",
        ["item"] = "IHonuaOgcFeaturesClient.GetItemAsync",
    },
    ["sdk-ogc-tiles"] = new()
    {
        ["vector-tile"] = "(no OGC API Tiles client in Honua.Sdk)",
        ["raster-tile"] = "(no OGC API Tiles client in Honua.Sdk)",
        ["empty-tile"] = "(no OGC API Tiles client in Honua.Sdk)",
    },
    ["sdk-ogc-processes"] = new()
    {
        ["submit"] = "IHonuaProcessesClient.SubmitJobAsync",
        ["poll"] = "IHonuaProcessesClient.GetJobAsync",
        ["result"] = "IHonuaProcessesClient.GetJobResultsAsync",
    },
    ["sdk-stac"] = new() { ["search"] = "IHonuaStacClient.PostSearchAsync" },
};

ServiceProvider Provider(Action<HonuaSdkOptions> auth)
{
    var services = new ServiceCollection();
    services.AddHonua(options =>
    {
        options.BaseAddress = baseUrl;
        options.UseGrpc = false;
        options.UseGeocoding = false;
        options.UseWfs = false;
        options.UseGeoServices = true;
        options.UseStac = true;
        options.EnableRetry = false;
        auth(options);
    });
    return services.BuildServiceProvider();
}

using var keyed = Provider(options => options.ApiKey = apiKey);
using var bearerProvider = Provider(options => options.BearerToken = bearer);
using var anonymous = Provider(_ => { });
var featureServer = keyed.GetRequiredService<IHonuaFeatureServerClient>();
var edits = keyed.GetRequiredService<IHonuaFeatureServerEditClient>();
var admin = keyed.GetRequiredService<IHonuaAdminClient>();

void Emit(string scenario, string step, string kind, object? value)
{
    var payload = new JsonObject
    {
        ["scenario"] = scenario,
        ["step"] = step,
        ["api"] = api[scenario][step],
        [kind] = value is JsonNode node ? node : JsonSerializer.SerializeToNode(value),
    };
    Console.Out.WriteLine(payload.ToJsonString());
    Console.Out.Flush();
}

// Error identity only: type and status. Messages stay in the job log, never in the receipt.
JsonObject ErrorOf(Exception exception)
{
    int? status = exception switch
    {
        HonuaFeatureServerException fs when fs.GeoServicesErrorCode is int code => code,
        HonuaException honua => honua.HttpStatus,
        HttpRequestException http => (int?)http.StatusCode,
        _ => null,
    };
    var message = exception.Message.Length > 300 ? exception.Message[..300] : exception.Message;
    Console.Error.WriteLine($"[dotnet-driver] {exception.GetType().Name}: {message}");
    return new JsonObject { ["type"] = exception.GetType().Name, ["status"] = status };
}

async Task Step(Dictionary<string, object> state, string scenario, string step, Func<Task<JsonObject>> action, params string[] needs)
{
    var missing = needs.Where(need => !state.ContainsKey(need)).ToArray();
    if (missing.Length > 0)
    {
        Emit(scenario, step, "skipped", $"depends on [{string.Join(", ", missing)}], which did not complete");
        return;
    }
    try
    {
        Emit(scenario, step, "observed", await action());
    }
    catch (NotSupportedException unsupported)
    {
        Emit(scenario, step, "unsupported", unsupported.Message);
    }
    catch (Exception exception)
    {
        Emit(scenario, step, "error", ErrorOf(exception));
    }
}

JsonNode? Scalar(JsonElement element) => element.ValueKind switch
{
    JsonValueKind.Number when element.TryGetInt64(out var whole) => JsonValue.Create(whole),
    JsonValueKind.Number => JsonValue.Create(element.GetDouble()),
    JsonValueKind.String => JsonValue.Create(element.GetString()),
    JsonValueKind.True => JsonValue.Create(true),
    JsonValueKind.False => JsonValue.Create(false),
    _ => null,
};

JsonArray PointRows(FeatureServerQueryResponse response, IEnumerable<string> fields, string? oidField = null)
{
    var rows = new JsonArray();
    foreach (var feature in response.Features ?? [])
    {
        var attributes = new JsonObject();
        foreach (var field in fields)
        {
            attributes[field] = feature.Attributes is { } values && values.TryGetValue(field, out var value) ? Scalar(value) : null;
        }
        var row = new JsonObject { ["attributes"] = attributes, ["x"] = null, ["y"] = null };
        if (feature.Geometry is { ValueKind: JsonValueKind.Object } geometry)
        {
            if (geometry.TryGetProperty("x", out var x)) row["x"] = Scalar(x);
            if (geometry.TryGetProperty("y", out var y)) row["y"] = Scalar(y);
        }
        if (oidField is not null)
        {
            row["objectId"] = feature.Attributes is { } a && a.TryGetValue(oidField, out var oid) ? Scalar(oid) : null;
            row["gid"] = attributes["gid"]?.DeepClone();
        }
        rows.Add(row);
    }
    return rows;
}

JsonObject EditResults(IReadOnlyList<FeatureServerEditResult> results)
{
    var rows = new JsonArray();
    foreach (var result in results)
    {
        rows.Add(new JsonObject { ["success"] = result.Success, ["objectId"] = result.ObjectId, ["code"] = result.Error?.Code });
    }
    return new JsonObject { ["results"] = rows };
}

(JsonNode? X, JsonNode? Y) GeoJsonPoint(JsonElement? geometry)
{
    if (geometry is { ValueKind: JsonValueKind.Object } value && value.TryGetProperty("coordinates", out var coordinates)
        && coordinates.ValueKind == JsonValueKind.Array && coordinates.GetArrayLength() >= 2)
    {
        return (Scalar(coordinates[0]), Scalar(coordinates[1]));
    }
    return (null, null);
}

JsonNode? IdOf(JsonElement? id) => id is { } value ? Scalar(value) : null;

var sites = plan["sites"]!.AsObject();
var sitesService = sites["service"]!.GetValue<string>();
var sitesLayer = sites["layerId"]!.GetValue<int>();
var sitesFields = sites["fields"]!.AsArray().Select(field => field!.GetValue<string>()).ToArray();
var everything = new FeatureServerQueryParams { Where = "1=1", OutFields = "*" };

async Task<JsonObject> RowCount(IHonuaFeatureServerClient client, string service, int layer)
{
    var response = await client.QueryAsync(service, layer, everything);
    return new JsonObject { ["count"] = response.Features?.Count };
}

async Task RunAuth(Dictionary<string, object> state)
{
    const string scenario = "sdk-auth";
    await Step(state, scenario, "api-key-query", () => RowCount(featureServer, sitesService, sitesLayer));
    await Step(state, scenario, "bearer-admin-list", async () =>
    {
        var services = await bearerProvider.GetRequiredService<IHonuaAdminClient>().ListServicesAsync();
        return new JsonObject { ["services"] = new JsonArray(services.Select(service => (JsonNode?)JsonValue.Create(service.ServiceName)).ToArray()) };
    });
    await Step(state, scenario, "bearer-query", () => RowCount(bearerProvider.GetRequiredService<IHonuaFeatureServerClient>(), sitesService, sitesLayer));
    await Step(state, scenario, "anonymous-refused", async () =>
    {
        var response = await anonymous.GetRequiredService<IHonuaFeatureServerClient>().QueryAsync(sitesService, sitesLayer, everything);
        return new JsonObject { ["returned"] = response.Features?.Count };
    });
}

async Task RunAdmin(Dictionary<string, object> state)
{
    const string scenario = "sdk-admin-lifecycle";
    var life = plan["lifecycle"]!.AsObject();
    var db = life["database"]!.AsObject();
    var service = life["service"]!.GetValue<string>();
    await Step(state, scenario, "create-datasource", async () =>
    {
        var created = await admin.CreateConnectionAsync(new CreateSecureConnectionRequest
        {
            Name = life["connectionName"]!.GetValue<string>(),
            Host = db["host"]!.GetValue<string>(),
            Port = db["port"]!.GetValue<int>(),
            DatabaseName = db["databaseName"]!.GetValue<string>(),
            Username = db["username"]!.GetValue<string>(),
            Password = dbPassword,
            SslRequired = false,
            SslMode = "Disable",
        });
        state["connection"] = created.ConnectionId.ToString();
        return new JsonObject { ["connectionId"] = created.ConnectionId.ToString() };
    });
    await Step(state, scenario, "test-datasource", async () =>
        new JsonObject { ["success"] = (await admin.TestConnectionAsync((string)state["connection"])).IsHealthy }, "connection");
    await Step(state, scenario, "publish", async () =>
    {
        var published = await admin.PublishLayerAsync((string)state["connection"], new PublishLayerRequest
        {
            Schema = "honua_data",
            Table = life["table"]!.GetValue<string>(),
            LayerName = life["layerName"]!.GetValue<string>(),
            GeometryColumn = "geom",
            GeometryType = life["geometryType"]!.GetValue<string>(),
            Srid = 4326,
            PrimaryKey = "gid",
            ServiceName = service,
            Enabled = true,
        });
        state["layer"] = published.LayerId;
        return new JsonObject
        {
            ["layerId"] = published.LayerId, ["layerName"] = published.LayerName,
            ["serviceName"] = published.ServiceName, ["enabled"] = published.Enabled,
        };
    }, "connection");
    await Step(state, scenario, "list", async () =>
    {
        var layers = await admin.ListLayersAsync((string)state["connection"], service);
        return new JsonObject
        {
            ["layers"] = new JsonArray(layers.Select(layer => (JsonNode?)new JsonObject
            {
                ["layerId"] = layer.LayerId, ["enabled"] = layer.Enabled, ["layerName"] = layer.LayerName,
            }).ToArray()),
        };
    }, "layer");
    await Step(state, scenario, "served", () => RowCount(featureServer, service, (int)state["layer"]), "layer");
    await Step(state, scenario, "unpublish", async () =>
    {
        var summary = await admin.SetLayerEnabledAsync((string)state["connection"], (int)state["layer"], false, service);
        state["unpublished"] = true;
        return new JsonObject { ["layerId"] = summary.LayerId, ["enabled"] = summary.Enabled };
    }, "layer");
    await Step(state, scenario, "unpublished-refused", async () =>
    {
        var response = await featureServer.QueryAsync(service, (int)state["layer"], everything);
        return new JsonObject { ["returned"] = response.Features?.Count };
    }, "unpublished");
}

async Task RunGeoServices(Dictionary<string, object> state)
{
    const string scenario = "sdk-geoservices";
    var edit = plan["edits"]!.AsObject();
    var editService = edit["service"]!.GetValue<string>();
    var editLayer = edit["layerId"]!.GetValue<int>();
    var editFields = edit["fields"]!.AsArray().Select(field => field!.GetValue<string>()).ToArray();
    var where = sites["where"]!.GetValue<string>();
    var oids = new Dictionary<long, long>();
    var oidField = "objectid";

    await Step(state, scenario, "query", async () =>
        new JsonObject { ["features"] = PointRows(await featureServer.QueryAsync(sitesService, sitesLayer, new FeatureServerQueryParams { Where = where, OutFields = "*" }), sitesFields) });
    await Step(state, scenario, "ids", async () =>
    {
        var ids = await featureServer.QueryIdsAsync(sitesService, sitesLayer, new FeatureServerQueryParams { Where = where });
        return new JsonObject { ["ids"] = new JsonArray(ids.Select(id => (JsonNode?)JsonValue.Create(id)).ToArray()) };
    });
    await Step(state, scenario, "count", async () =>
        new JsonObject { ["count"] = await featureServer.QueryCountAsync(sitesService, sitesLayer, new FeatureServerQueryParams { Where = where }) });
    await Step(state, scenario, "resolve-edit-ids", async () =>
    {
        var response = await featureServer.QueryAsync(editService, editLayer, everything);
        oidField = response.ObjectIdFieldName ?? oidField;
        var rows = PointRows(response, editFields, oidField);
        var resolved = new JsonArray();
        foreach (var row in rows)
        {
            var gid = row!["gid"]!.GetValue<long>();
            var objectId = row["objectId"]!.GetValue<long>();
            oids[gid] = objectId;
            resolved.Add(new JsonObject { ["gid"] = gid, ["objectId"] = objectId });
        }
        state["oids"] = oids;
        return new JsonObject { ["features"] = resolved };
    });
    FeatureServerFeature Feature(Dictionary<string, object?> attributes, double? x = null, double? y = null) => new()
    {
        Attributes = attributes.ToDictionary(pair => pair.Key, pair => JsonSerializer.SerializeToElement(pair.Value)),
        Geometry = x is null ? null : JsonSerializer.SerializeToElement(new { x, y, spatialReference = new { wkid = 4326 } }),
    };
    await Step(state, scenario, "apply-edits-add", async () =>
    {
        var add = edit["add"]!.AsObject();
        var attributes = editFields.ToDictionary(field => field, field => (object?)JsonSerializer.Deserialize<JsonElement>(add[field]!.ToJsonString()));
        var response = await edits.ApplyEditsAsync(editService, editLayer, new FeatureServerEditRequest
        {
            Adds = [Feature(attributes, add["x"]!.GetValue<double>(), add["y"]!.GetValue<double>())],
        });
        return EditResults(response.AddResults);
    }, "oids");
    await Step(state, scenario, "apply-edits-update", async () =>
    {
        var update = edit["update"]!.AsObject();
        var attributes = new Dictionary<string, object?> { [oidField] = oids[update["gid"]!.GetValue<long>()] };
        foreach (var (key, value) in update["attributes"]!.AsObject())
        {
            attributes[key] = JsonSerializer.Deserialize<JsonElement>(value!.ToJsonString());
        }
        var response = await edits.ApplyEditsAsync(editService, editLayer, new FeatureServerEditRequest { Updates = [Feature(attributes)] });
        return EditResults(response.UpdateResults);
    }, "oids");
    await Step(state, scenario, "apply-edits-delete", async () =>
    {
        var response = await edits.ApplyEditsAsync(editService, editLayer, new FeatureServerEditRequest
        {
            Deletes = [oids[edit["delete"]!["gid"]!.GetValue<long>()]],
        });
        return EditResults(response.DeleteResults);
    }, "oids");
    await Step(state, scenario, "edits-state", async () =>
        new JsonObject { ["features"] = PointRows(await featureServer.QueryAsync(editService, editLayer, everything), editFields) }, "oids");
    var attachment = edit["attachment"]!.AsObject();
    var parent = attachment["gid"]!.GetValue<long>();
    await Step(state, scenario, "add-attachment", async () =>
    {
        await using var content = new MemoryStream(Encoding.UTF8.GetBytes(attachment["content"]!.GetValue<string>()));
        var result = await ((IHonuaFeatureAttachmentClient)featureServer).AddAttachmentAsync(new FeatureAttachmentAddRequest
        {
            Source = new FeatureSource { ServiceId = editService, LayerId = editLayer },
            ObjectId = oids[parent],
            Name = attachment["name"]!.GetValue<string>(),
            ContentType = attachment["contentType"]!.GetValue<string>(),
            Content = content,
        });
        state["attached"] = true;
        return new JsonObject { ["results"] = new JsonArray(new JsonObject { ["success"] = result.Succeeded, ["objectId"] = result.AttachmentId }) };
    }, "oids");
    await Step(state, scenario, "query-attachments", async () =>
    {
        var response = await featureServer.QueryAttachmentsAsync(editService, editLayer, [oids[parent]]);
        var infos = response.AttachmentGroups.Where(group => group.ParentObjectId == oids[parent]).SelectMany(group => group.AttachmentInfos);
        return new JsonObject
        {
            ["attachments"] = new JsonArray(infos.Select(info => (JsonNode?)new JsonObject
            {
                ["name"] = info.Name, ["contentType"] = info.ContentType, ["size"] = info.Size,
            }).ToArray()),
        };
    }, "attached");
}

async Task RunOgcFeatures(Dictionary<string, object> state)
{
    const string scenario = "sdk-ogc-features";
    var ogc = keyed.GetRequiredService<IHonuaOgcFeaturesClient>();
    var collection = sites["collectionId"]!.GetValue<string>();
    var bbox = sites["bbox"]!.AsArray().Select(value => value!.GetValue<double>()).ToArray();
    await Step(state, scenario, "items-bbox", async () =>
    {
        var page = await ogc.GetItemsAsync(collection, new OgcItemsParams { Bbox = bbox, Limit = 100 });
        var rows = new JsonArray();
        foreach (var feature in page.Features ?? [])
        {
            var (x, y) = GeoJsonPoint(feature.Geometry);
            rows.Add(new JsonObject { ["id"] = IdOf(feature.Id), ["x"] = x, ["y"] = y });
        }
        return new JsonObject { ["features"] = rows };
    });
    await Step(state, scenario, "item", async () =>
    {
        var feature = await ogc.GetItemAsync(collection, sites["itemId"]!.GetValue<string>());
        var (x, y) = GeoJsonPoint(feature.Geometry);
        var properties = new JsonObject();
        foreach (var (key, value) in feature.Properties ?? [])
        {
            properties[key] = Scalar(value);
        }
        return new JsonObject { ["id"] = IdOf(feature.Id), ["properties"] = properties, ["x"] = x, ["y"] = y };
    });
}

Task RunTiles(Dictionary<string, object> state)
{
    // Honua.Sdk has no OGC API Tiles client; every tile step is a finding, not a skip.
    foreach (var step in new[] { "vector-tile", "raster-tile", "empty-tile" })
    {
        Emit("sdk-ogc-tiles", step, "unsupported", "Honua.Sdk has no OGC API Tiles client (no tile fetch API in any Honua.Sdk.* package)");
    }
    return Task.CompletedTask;
}

async Task RunProcesses(Dictionary<string, object> state)
{
    const string scenario = "sdk-ogc-processes";
    var spec = plan["processes"]!.AsObject();
    var processes = keyed.GetRequiredService<IHonuaProcessesClient>();
    await Step(state, scenario, "submit", async () =>
    {
        var point = spec["point"]!.AsArray();
        var inputs = new Dictionary<string, JsonElement>
        {
            ["wkb"] = JsonSerializer.SerializeToElement(new { type = "Point", coordinates = new[] { point[0]!.GetValue<double>(), point[1]!.GetValue<double>() } }),
            ["srid"] = JsonSerializer.SerializeToElement(spec["srid"]!.GetValue<int>()),
            ["distance"] = JsonSerializer.SerializeToElement(spec["distance"]!.GetValue<double>()),
        };
        var job = await processes.SubmitJobAsync(spec["processId"]!.GetValue<string>(), inputs);
        state["job"] = job.JobId;
        return new JsonObject { ["jobId"] = job.JobId, ["status"] = job.Status };
    });
    await Step(state, scenario, "poll", async () =>
    {
        var deadline = DateTimeOffset.UtcNow.AddSeconds(spec["pollTimeoutSeconds"]!.GetValue<int>());
        var job = await processes.GetJobAsync((string)state["job"]);
        while (job.Status is not ("successful" or "failed" or "dismissed") && DateTimeOffset.UtcNow < deadline)
        {
            await Task.Delay(500);
            job = await processes.GetJobAsync((string)state["job"]);
        }
        if (job.Status == "successful")
        {
            state["succeeded"] = true;
        }
        return new JsonObject { ["status"] = job.Status };
    }, "job");
    await Step(state, scenario, "result", async () =>
    {
        var results = await processes.GetJobResultsAsync((string)state["job"]);
        JsonNode? geometry = null;
        if (results.Outputs.Count > 0)
        {
            var output = results.Outputs.Values.First();
            geometry = JsonNode.Parse(output.GetRawText());
            if (geometry is JsonObject wrapper && wrapper.ContainsKey("value"))
            {
                geometry = wrapper["value"]?.DeepClone();
            }
        }
        return new JsonObject { ["geometry"] = geometry };
    }, "succeeded");
}

async Task RunStac(Dictionary<string, object> state)
{
    var stac = keyed.GetRequiredService<IHonuaStacClient>();
    await Step(state, "sdk-stac", "search", async () =>
    {
        var bbox = sites["bbox"]!.AsArray().Select(value => value!.GetValue<double>()).ToArray();
        var response = await stac.PostSearchAsync(new StacSearchRequest
        {
            Collections = [sites["collectionId"]!.GetValue<string>()],
            Bbox = bbox,
            Limit = 100,
        });
        var rows = new JsonArray();
        foreach (var item in response.Features ?? [])
        {
            var (x, y) = GeoJsonPoint(item.Geometry);
            rows.Add(new JsonObject { ["id"] = item.Id, ["x"] = x, ["y"] = y });
        }
        return new JsonObject { ["features"] = rows };
    });
}

var runners = new Dictionary<string, Func<Dictionary<string, object>, Task>>
{
    ["sdk-auth"] = RunAuth,
    ["sdk-admin-lifecycle"] = RunAdmin,
    ["sdk-geoservices"] = RunGeoServices,
    ["sdk-ogc-features"] = RunOgcFeatures,
    ["sdk-ogc-tiles"] = RunTiles,
    ["sdk-ogc-processes"] = RunProcesses,
    ["sdk-stac"] = RunStac,
};

foreach (var scenario in plan["scenarios"]!.AsArray())
{
    var id = scenario!["id"]!.GetValue<string>();
    try
    {
        await runners[id](new Dictionary<string, object>());
    }
    catch (Exception exception)
    {
        // A crashed scenario leaves its remaining steps unobserved, which the runner judges as failed.
        Console.Error.WriteLine($"[dotnet-driver] scenario {id} crashed: {exception}");
    }
}
