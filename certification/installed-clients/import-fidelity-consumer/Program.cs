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
var readback = new ConfiguredReadback();
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
        if (operation is "configure-readback" or "repoint-readback" or "rollback-readback")
        {
            var configuration = readback.Change(operation, request);
            Console.WriteLine(JsonSerializer.Serialize(new { ok = true, result = configuration }, json));
            await Console.Out.FlushAsync();
            continue;
        }
        var configuredRead = operation == "configured-read";
        var observedRequests = new List<string>();
        if (configuredRead)
        {
            request = readback.Resolve(request);
            operation = request.GetProperty("operation").GetString()!;
        }
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
            if (configuredRead)
                handler = new ReadbackRequestObserver(handler, observedRequests);
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
        if (configuredRead)
            result = new { configuration = readback.Describe(), observedRequests, value = result };
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


// Application configuration belongs to this clean consumer process. The SDK captures
// constructor options, so each fresh read opens the supported client from the stored
// slot. Reads cannot supply a URL/layer/auth override or recreate the slot on rollback.
internal sealed class ConfiguredReadback
{
    private readonly string _id = Guid.NewGuid().ToString("N");
    private ReadbackTarget? _original;
    private ReadbackTarget? _current;
    private int _revision;

    public object Describe()
    {
        var current = _current ?? throw new InvalidOperationException("configuration is not initialized");
        return new
        {
            configurationId = _id, revision = _revision,
            baseUrl = current.BaseUrl, layerId = current.LayerId,
            candidate = current.Candidate, sourceAuth = current.SourceAuth
        };
    }

    public object Change(string operation, JsonElement request)
    {
        if (operation == "configure-readback")
        {
            if (_current is not null || request.GetProperty("revision").GetInt32() != 0)
                throw new ArgumentException("configuration already initialized");
            _current = ReadbackTarget.Parse(request);
            _original = _current;
        }
        else
        {
            CheckVersion(request);
            if (operation == "rollback-readback")
            {
                if (_revision != 2 || _original == _current)
                    throw new ArgumentException("configuration has no switched version to restore");
                if (request.EnumerateObject().Any(item => item.Name is not ("operation" or "configurationId" or "revision")))
                    throw new ArgumentException("rollback cannot override the original configuration");
                _current = _original;
            }
            else
            {
                if (_revision != 1)
                    throw new ArgumentException("only one repoint is supported by this frozen journey");
                var next = ReadbackTarget.Parse(request);
                if (next == _current)
                    throw new ArgumentException("repoint must change the configured endpoint");
                _current = next;
            }
        }
        _revision++;
        return Describe();
    }

    private void CheckVersion(JsonElement request)
    {
        if (_current is null || request.GetProperty("configurationId").GetString() != _id ||
            request.GetProperty("revision").GetInt32() != _revision)
            throw new ArgumentException("stale or different application configuration");
    }

    public JsonElement Resolve(JsonElement request)
    {
        CheckVersion(request);
        if (request.EnumerateObject().Any(item => item.Name is not ("operation" or "configurationId" or "revision" or "readOperation" or "query")))
            throw new ArgumentException("configured read cannot override its endpoint");
        var operation = request.GetProperty("readOperation").GetString();
        if (operation is not ("layer" or "count" or "ids" or "query"))
            throw new ArgumentException("unsupported configured read");
        var current = _current ?? throw new InvalidOperationException("configuration is not initialized");
        var resolved = new Dictionary<string, object?>
        {
            ["operation"] = operation, ["baseUrl"] = current.BaseUrl,
            ["layerId"] = current.LayerId, ["candidate"] = current.Candidate,
            ["sourceAuth"] = current.SourceAuth
        };
        if (request.TryGetProperty("query", out var query))
            resolved["query"] = query.Clone();
        return JsonSerializer.SerializeToElement(resolved);
    }
}

internal sealed record ReadbackTarget(string BaseUrl, int LayerId, bool Candidate, bool SourceAuth)
{
    public static ReadbackTarget Parse(JsonElement request)
    {
        var address = new Uri(request.GetProperty("baseUrl").GetString()!);
        var layer = request.GetProperty("layerId").GetInt32();
        var candidate = request.GetProperty("candidate").GetBoolean();
        var sourceAuth = request.GetProperty("sourceAuth").GetBoolean();
        if (layer < 0 || address.UserInfo.Length != 0 || address.Query.Length != 0 || address.Fragment.Length != 0 ||
            (address.Scheme != "https" && !(address.Scheme == "http" && address.IsLoopback)))
            throw new ArgumentException("invalid configured service");
        // This producer owns a disposable local candidate, not an arbitrary credential destination.
        if (candidate && (!address.IsLoopback || sourceAuth))
            throw new ArgumentException("candidate configuration must name the owned local stack");
        if (sourceAuth && (address.Scheme != "https" ||
            address.AbsoluteUri.TrimEnd('/') != Environment.GetEnvironmentVariable("IMPORT_FIDELITY_AUTH_SOURCE_URL")?.TrimEnd('/')))
            throw new ArgumentException("configured source credential destination mismatch");
        ArcGisServiceRoot.Parse(address);
        return new ReadbackTarget(address.AbsoluteUri.TrimEnd('/'), layer, candidate, sourceAuth);
    }
}

internal sealed class ReadbackRequestObserver(HttpMessageHandler inner, List<string> requests) : DelegatingHandler(inner)
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        if (request.RequestUri is null || requests.Count >= 32)
            throw new InvalidOperationException("configured read request observation is incomplete");
        // Never retain query parameters, headers or bodies. Responses remain SDK-owned.
        requests.Add(request.RequestUri.GetLeftPart(UriPartial.Path));
        return base.SendAsync(request, cancellationToken);
    }
}
