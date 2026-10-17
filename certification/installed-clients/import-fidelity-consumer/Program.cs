using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Honua.Sdk.Abstractions;
using Honua.Sdk.Abstractions.Features;
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
    "GetMigrationBatchAsync", "GetMigrationRunAsync", "GetMigrationRunReconciliationScorecardAsync",
    "UpdateLayerMetadataAsync"
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
            var transport = new HttpClientHandler { AllowAutoRedirect = false };
            HttpMessageHandler handler = transport;
            if (request.TryGetProperty("sourceAuth", out var sourceAuth) && sourceAuth.GetBoolean())
            {
                if (address.AbsoluteUri.TrimEnd('/') != Environment.GetEnvironmentVariable("IMPORT_FIDELITY_AUTH_SOURCE_URL")?.TrimEnd('/') ||
                    address.Scheme != "https" || (request.TryGetProperty("candidate", out var own) && own.GetBoolean()))
                    throw new ArgumentException("source credential destination mismatch");
                var token = Environment.GetEnvironmentVariable("IMPORT_FIDELITY_SOURCE_TOKEN");
                if (string.IsNullOrEmpty(token))
                    throw new ArgumentException("source token missing");
                handler = new ArcGisSourceCredentialHandler(new ArcGisSourceCredential { Mode = ArcGisCredentialMode.Token, Token = token }, transport);
            }
            using var http = new HttpClient(handler)
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
                    case "attachments":
                        result = await ((HonuaFeatureServerClient)client).ListAttachmentsAsync(new FeatureAttachmentListRequest
                        {
                            Source = new FeatureSource { ServiceId = root.ServiceId, LayerId = layer },
                            ObjectId = request.GetProperty("objectId").GetInt64()
                        }, timeout.Token);
                        break;
                    case "attachment-hash":
                        var content = await ((HonuaFeatureServerClient)client).DownloadAttachmentAsync(new FeatureAttachmentDownloadRequest
                        {
                            Source = new FeatureSource { ServiceId = root.ServiceId, LayerId = layer },
                            ObjectId = request.GetProperty("objectId").GetInt64(),
                            AttachmentId = request.GetProperty("attachmentId").GetInt64()
                        }, timeout.Token);
                        await using (var stream = content.Content)
                        using (var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256))
                        {
                            var buffer = new byte[65536];
                            long size = 0;
                            int length;
                            while ((length = await stream.ReadAsync(buffer, timeout.Token)) != 0)
                            {
                                size += length;
                                if (size > 16777216)
                                    throw new InvalidOperationException("attachment exceeds qualification bound");
                                hash.AppendData(buffer, 0, length);
                            }
                            result = new { size, sha256 = Convert.ToHexStringLower(hash.GetHashAndReset()) };
                        }
                        break;
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
        var problem = (actual as HonuaException)?.ProblemDetails;
        var problemType = problem?.Type;
        // These local diagnostic labels classify fixed validation messages, never echo them.
        var diagnosticReason = problem?.Detail switch
        {
            "Invalid request body" => "invalid-request-body",
            "Request body is required" => "missing-request-body",
            "ServiceUrl is required" => "missing-source-url",
            "TableName is required" => "missing-target-table",
            "Invalid table name. Use only letters, numbers, and underscores." => "invalid-target-table",
            "Invalid target schema. Use only letters, numbers, and underscores." => "invalid-target-schema",
            "Target schema is not a configured operational schema. Import targets are limited to the default operational schema, public, and Database:OperationalSchemas." => "unregistered-operational-schema",
            "Schema is reserved for server metadata or database system catalogs and cannot be used by an import." => "reserved-target-schema",
            "ServiceUrl must be a valid HTTPS URL" => "invalid-source-https-url",
            "ServiceUrl must not include embedded credentials." => "source-embedded-credentials",
            "ServiceUrl must not include credential query parameters; use the credentials object instead." => "source-query-credentials",
            "ServiceUrl resolves to a private, loopback, or unresolvable network address, which is not allowed." => "source-address-rejected",
            "ServiceUrl host is not in the configured Migration:AllowedServiceHostSuffixes allowlist." => "source-host-not-allowed",
            "Distributed import coordination is unavailable. Retry when Redis is healthy." => "import-coordination-unavailable",
            _ => (string?)null
        };
        // Keep bounded identifiers; response bodies, occurrence details and exception messages
        // may contain request data and never enter certification evidence.
        if (problemType is not { Length: > 0 and <= 160 } ||
            problemType.Any(character => !char.IsAsciiLetterOrDigit(character) && !":/._-#".Contains(character)))
            problemType = null;
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            ok = false, errorType = actual.GetType().Name,
            httpStatus = (actual as HonuaException)?.HttpStatus,
            problemType,
            diagnosticReason,
            code = actual is HonuaFeatureServerException fs ? fs.GeoServicesErrorCode : null
        }, json));
    }
    await Console.Out.FlushAsync();
}
