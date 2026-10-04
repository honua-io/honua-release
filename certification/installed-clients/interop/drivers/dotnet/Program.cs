// .NET SDK runner for the interop scenarios.
//
// Runs against the manifest-pinned Honua.Sdk restored from nuget.org only and calls its client
// interfaces, never raw HTTP. Reads one JSON request per line on stdin and answers one JSON reply
// per line on stdout. Client instances live for the whole run, so the identity scenario observes a
// revocation on the very client instance that used the key. Replies carry an observation, or an
// error's type and status only; messages go to stderr.
using System.Text.Json;
using System.Text.Json.Nodes;
using Honua.Sdk;
using Honua.Sdk.Abstractions;
using Honua.Sdk.Admin;
using Honua.Sdk.Admin.Models;
using Honua.Sdk.GeoServices.FeatureServer;
using Honua.Sdk.GeoServices.FeatureServer.Exceptions;
using Honua.Sdk.GeoServices.FeatureServer.Models;
using Honua.Sdk.Studio.Packages;
using Microsoft.Extensions.DependencyInjection;

var plan = JsonNode.Parse(File.ReadAllText(Environment.GetEnvironmentVariable("SDKREG_PLAN")!))!.AsObject();
var baseUrl = new Uri(plan["baseUrl"]!.GetValue<string>().TrimEnd('/') + "/");

ServiceProvider Provider(string key) =>
    new ServiceCollection().AddHonua(options =>
    {
        options.BaseAddress = baseUrl;
        options.ApiKey = key;
        options.UseGrpc = false;
        options.UseGeocoding = false;
        options.UseWfs = false;
        options.UseGeoServices = true;
        options.UseStudio = true;
        options.EnableRetry = false;
    }).BuildServiceProvider();

using var root = Provider(Environment.GetEnvironmentVariable("SDKREG_API_KEY")!);
ServiceProvider? identity = null;
var everything = new FeatureServerQueryParams { Where = "1=1", OutFields = "*" };

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
    Console.Error.WriteLine($"[dotnet-runner] {exception.GetType().Name}: {message}");
    return new JsonObject { ["type"] = exception.GetType().Name, ["status"] = status };
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

string Text(JsonObject args, string name) => args[name]!.GetValue<string>();
int Int(JsonObject args, string name) => args[name]!.GetValue<int>();

// IHonuaAdminClient.PublishLayerAsync
async Task<JsonObject> PublishLayer(JsonObject args)
{
    var published = await root.GetRequiredService<IHonuaAdminClient>().PublishLayerAsync(Text(args, "connectionId"), new PublishLayerRequest
    {
        Schema = Text(args, "schema"),
        Table = Text(args, "table"),
        LayerName = Text(args, "layerName"),
        GeometryColumn = "geom",
        GeometryType = Text(args, "geometryType"),
        Srid = 4326,
        PrimaryKey = "gid",
        ServiceName = Text(args, "service"),
        Enabled = true,
    });
    return new JsonObject
    {
        ["layerId"] = published.LayerId,
        ["layerName"] = published.LayerName,
        ["serviceName"] = published.ServiceName,
        ["enabled"] = published.Enabled,
    };
}

// IHonuaFeatureServerClient.QueryAsync: every row with its attributes and ordinates.
async Task<JsonObject> QueryFeatures(JsonObject args)
{
    var response = await root.GetRequiredService<IHonuaFeatureServerClient>().QueryAsync(Text(args, "service"), Int(args, "layerId"), everything);
    var fields = args["fields"]!.AsArray().Select(field => field!.GetValue<string>()).ToArray();
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
        rows.Add(row);
    }
    return new JsonObject { ["features"] = rows };
}

// IHonuaStudioPackageClient.GetContentItemPointersAsync
async Task<JsonObject> StudioPointers(JsonObject args)
{
    var pointers = await root.GetRequiredService<IHonuaStudioPackageClient>().GetContentItemPointersAsync(Guid.Parse(Text(args, "itemId")));
    return new JsonObject
    {
        ["found"] = pointers is not null,
        ["currentVersionId"] = pointers?.CurrentVersionId?.ToString(),
        ["publishedVersionId"] = pointers?.PublishedVersionId?.ToString(),
    };
}

// IHonuaStudioPackageClient.GetVersionAsync
async Task<JsonObject> StudioVersion(JsonObject args)
{
    var version = await root.GetRequiredService<IHonuaStudioPackageClient>().GetVersionAsync(Guid.Parse(Text(args, "itemId")), Guid.Parse(Text(args, "versionId")));
    return new JsonObject
    {
        ["versionId"] = version.VersionId.ToString(),
        ["contentHash"] = version.ContentHash,
        ["family"] = version.Envelope?.Family.ToString(),
        ["body"] = version.Envelope?.Body is { } body ? JsonNode.Parse(body.GetRawText()) : null,
    };
}

Task<JsonObject> StudioPublishedUrl(JsonObject args) =>
    // Honua.Sdk 1.10.1 reads Studio drafts, versions and pointers, but has no reader for the
    // final publication route (/api/v1/studio/published/{route}) in any Honua.Sdk.* package.
    throw new NotSupportedException("Honua.Sdk has no reader for a published Studio route (no published-content API in any Honua.Sdk.* package)");

async Task<int> IdentityQuery(JsonObject args) =>
    (await identity!.GetRequiredService<IHonuaFeatureServerClient>().QueryAsync(Text(args, "service"), Int(args, "layerId"), everything)).Features?.Count ?? 0;

// IHonuaFeatureServerClient.QueryAsync (HonuaSdkOptions.ApiKey) with the key the admin CLI minted;
// the provider is kept for the revocation probe.
async Task<JsonObject> IdentityUse(JsonObject args)
{
    identity = Provider((await File.ReadAllTextAsync(Text(args, "secretFile"))).Trim());
    return new JsonObject { ["count"] = await IdentityQuery(args) };
}

// Same client instance after revocation: count successes until the first refusal, then confirm it holds.
async Task<JsonObject> IdentityRevoked(JsonObject args)
{
    var bound = args["observationSeconds"]!.GetValue<double>();
    var started = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
    var revokedAt = args["revokedAt"]?.GetValue<double>() ?? started;
    var successes = 0;
    while (true)
    {
        try
        {
            await IdentityQuery(args);
        }
        catch (Exception exception)
        {
            var refused = ErrorOf(exception);
            var after = Math.Round(DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - revokedAt, 3);
            var confirmations = new JsonArray();
            for (var index = 0; index < Int(args, "confirmations"); index++)
            {
                try
                {
                    await IdentityQuery(args);
                    confirmations.Add(false);
                }
                catch (Exception)
                {
                    confirmations.Add(true);
                }
            }
            return new JsonObject
            {
                ["refused"] = true,
                ["status"] = refused["status"]?.DeepClone(),
                ["succeededAfterRevocation"] = successes,
                ["refusedAfterSeconds"] = after,
                ["confirmations"] = confirmations,
                ["observationSeconds"] = bound,
            };
        }
        successes++;
        if (DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0 - started > bound)
        {
            return new JsonObject { ["refused"] = false, ["succeededAfterRevocation"] = successes, ["observationSeconds"] = bound };
        }
        await Task.Delay(250);
    }
}

var ops = new Dictionary<string, Func<JsonObject, Task<JsonObject>>>
{
    ["publish-layer"] = PublishLayer,
    ["query-features"] = QueryFeatures,
    ["studio-pointers"] = StudioPointers,
    ["studio-version"] = StudioVersion,
    ["studio-published-url"] = StudioPublishedUrl,
    ["identity-use"] = IdentityUse,
    ["identity-revoked"] = IdentityRevoked,
};

string? line;
while ((line = await Console.In.ReadLineAsync()) is not null)
{
    if (string.IsNullOrWhiteSpace(line))
    {
        continue;
    }
    var request = JsonNode.Parse(line)!.AsObject();
    var reply = new JsonObject { ["id"] = request["id"]?.DeepClone() };
    try
    {
        reply["observed"] = await ops[request["op"]!.GetValue<string>()](request["args"]?.AsObject() ?? new JsonObject());
    }
    catch (NotSupportedException unsupported)
    {
        reply["unsupported"] = unsupported.Message;
    }
    catch (Exception exception)
    {
        reply["error"] = ErrorOf(exception);
    }
    Console.Out.WriteLine(reply.ToJsonString());
    Console.Out.Flush();
}
identity?.Dispose();
