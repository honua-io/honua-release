using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Honua.Sdk.Admin;
using Honua.Sdk.GeoServices.FeatureServer;
using Honua.Sdk.GeoServices.FeatureServer.Exceptions;
using Honua.Sdk.GeoServices.FeatureServer.Models;

// Newline-delimited stdin keeps credentials out of process arguments and evidence.
// This consumer has no HTTP request implementation: every service operation uses the public SDK.
var json = new JsonSerializerOptions(JsonSerializerDefaults.Web);
json.Converters.Add(new JsonStringEnumConverter());
var adminMethods = new HashSet<string>(StringComparer.Ordinal)
{
    "DiscoverGeoservicesServiceAsync", "ScanMigrationSourceAsync", "StartGeoservicesImportAsync",
    "GetGeoservicesImportJobStatusAsync", "CancelGeoservicesImportJobAsync", "StartMigrationBatchAsync",
    "GetMigrationBatchAsync", "GetMigrationRunAsync", "GetMigrationRunReconciliationScorecardAsync"
};
while (await Console.In.ReadLineAsync() is { } line)
{
    try
    {
        using var document = JsonDocument.Parse(line);
        var request = document.RootElement;
        var operation = request.GetProperty("operation").GetString()!;
        object? result;
        using var timeout = new CancellationTokenSource(TimeSpan.FromMinutes(5));
        if (operation == "identity")
        {
            var assemblies = new[] { typeof(HonuaAdminClient).Assembly, typeof(HonuaFeatureServerClient).Assembly };
            result = assemblies.Select(assembly => new
            {
                name = assembly.GetName().Name,
                informationalVersion = assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion,
                sha256 = Convert.ToHexStringLower(SHA256.HashData(File.ReadAllBytes(assembly.Location)))
            }).ToArray();
        }
        else
        {
            var address = new Uri(request.GetProperty("baseUrl").GetString()!);
            if (address.Scheme != "https" && !(address.Scheme == "http" && address.IsLoopback))
                throw new ArgumentException("HTTPS or loopback required");
            if (address.UserInfo.Length != 0 || address.Query.Length != 0 || address.Fragment.Length != 0)
                throw new ArgumentException("invalid service URL");
            using var http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false })
            {
                BaseAddress = address,
                Timeout = TimeSpan.FromMinutes(4)
            };
            if (request.TryGetProperty("candidate", out var candidate) && candidate.GetBoolean())
                http.DefaultRequestHeaders.Add("X-API-Key", Environment.GetEnvironmentVariable("IMPORT_FIDELITY_API_KEY"));
            if (adminMethods.Contains(operation))
            {
                var client = new HonuaAdminClient(http);
                var method = typeof(HonuaAdminClient).GetMethods().Single(m => m.Name == operation);
                var supplied = request.GetProperty("arguments").EnumerateArray().ToArray();
                var index = 0;
                var arguments = method.GetParameters().Select(parameter =>
                    parameter.ParameterType == typeof(CancellationToken) ? (object)timeout.Token :
                    index < supplied.Length ? JsonSerializer.Deserialize(supplied[index++], parameter.ParameterType, json) :
                    parameter.HasDefaultValue ? parameter.DefaultValue : throw new ArgumentException("missing argument")
                ).ToArray();
                if (index != supplied.Length)
                    throw new ArgumentException("unexpected argument");
                var task = (Task)method.Invoke(client, arguments)!;
                await task;
                result = task.GetType().GetProperty("Result")!.GetValue(task);
            }
            else
            {
                var root = ArcGisServiceRoot.Parse(address);
                IHonuaFeatureServerClient client = new HonuaFeatureServerClient(http, root.ToClientOptions());
                var layer = request.TryGetProperty("layerId", out var layerElement) ? layerElement.GetInt32() : 0;
                var query = request.TryGetProperty("query", out var queryElement)
                    ? JsonSerializer.Deserialize<FeatureServerQueryParams>(queryElement, json)! : new();
                switch (operation)
                {
                    case "service":
                        using (var raw = await ((HonuaFeatureServerClient)client).GetServiceMetadataAsync(root.ServiceId, timeout.Token))
                            result = raw.RootElement.Clone();
                        break;
                    case "layer":
                        using (var raw = await ((HonuaFeatureServerClient)client).GetLayerMetadataAsync(root.ServiceId, layer, timeout.Token))
                            result = raw.RootElement.Clone();
                        break;
                    case "count": result = await client.QueryCountAsync(root.ServiceId, layer, query, timeout.Token); break;
                    case "ids": result = await client.QueryIdsAsync(root.ServiceId, layer, query, timeout.Token); break;
                    case "extent": result = await client.QueryExtentAsync(root.ServiceId, layer, query, timeout.Token); break;
                    case "query":
                        using (var response = await client.QueryRawAsync(root.ServiceId, layer, query, timeout.Token))
                        using (var raw = JsonDocument.Parse(await response.Content.ReadAsStringAsync(timeout.Token)))
                            result = raw.RootElement.Clone();
                        break;
                    default: throw new ArgumentException("operation outside certification SDK surface");
                }
            }
        }
        Console.WriteLine(JsonSerializer.Serialize(new { ok = true, result }, json));
    }
    catch (Exception error) when (error is not OutOfMemoryException)
    {
        var actual = error is TargetInvocationException invocation ? invocation.InnerException! : error;
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = false, errorType = actual.GetType().Name,
            code = actual is HonuaFeatureServerException fs ? fs.GeoServicesErrorCode : null
        }, json));
    }
    await Console.Out.FlushAsync();
}
