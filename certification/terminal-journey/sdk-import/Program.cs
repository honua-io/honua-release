using System.Reflection;
using System.Text.Json;
using Honua.Sdk.Admin;

// A stdin protocol prevents connection passwords and request bodies appearing in argv.
// Reflection permits a clean capability refusal on the older published 1.6.0 SDK:
// it must never substitute REST calls or compile a newer checkout to hide a missing method.
var wire = new JsonSerializerOptions { PropertyNameCaseInsensitive = true, PropertyNamingPolicy = JsonNamingPolicy.CamelCase };
var allowed = new HashSet<string>(StringComparer.Ordinal)
{
    "CreateConnectionAsync", "TestConnectionAsync", "StartGeoservicesImportAsync",
    "GetGeoservicesImportJobStatusAsync", "PublishLayerAsync"
};
try
{
    using var input = JsonDocument.Parse(await Console.In.ReadToEndAsync());
    var name = input.RootElement.GetProperty("method").GetString()!;
    if (!allowed.Contains(name))
        throw new ArgumentException("method is outside the journey SDK surface");
    var method = typeof(HonuaAdminClient).GetMethods().SingleOrDefault(m => m.Name == name);
    if (method is null)
    {
        Console.WriteLine(JsonSerializer.Serialize(new { status = "blocked", method = name, reason = "published SDK does not ship this method" }, wire));
        return 2;
    }
    var baseUrl = new Uri(Environment.GetEnvironmentVariable("HONUA_JOURNEY_BASE_URL")!);
    if (baseUrl.Scheme != "https" && !(baseUrl.Scheme == "http" && baseUrl.IsLoopback))
        throw new ArgumentException("credential-bearing requests require HTTPS or loopback");
    if (baseUrl.UserInfo.Length != 0 || baseUrl.Query.Length != 0 || baseUrl.Fragment.Length != 0)
        throw new ArgumentException("invalid candidate URL");
    using var http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = false }) { BaseAddress = baseUrl };
    http.DefaultRequestHeaders.Add("X-API-Key", Environment.GetEnvironmentVariable("HONUA_JOURNEY_SDK_KEY"));
    var client = new HonuaAdminClient(http);
    var supplied = input.RootElement.GetProperty("arguments").EnumerateArray().ToArray();
    var parameters = method.GetParameters();
    var invocationArguments = new object?[parameters.Length];
    var index = 0;
    for (var i = 0; i < parameters.Length; i++)
        invocationArguments[i] = parameters[i].ParameterType == typeof(CancellationToken)
            ? CancellationToken.None
            : JsonSerializer.Deserialize(supplied[index++], parameters[i].ParameterType, wire);
    if (index != supplied.Length)
        throw new ArgumentException("unexpected SDK arguments");
    var task = (Task)method.Invoke(client, invocationArguments)!;
    await task;
    var result = task.GetType().GetProperty("Result")!.GetValue(task);
    Console.WriteLine(JsonSerializer.Serialize(new { status = "pass", method = name, result }, wire));
    return 0;
}
catch (Exception error) when (error is not OutOfMemoryException)
{
    // An SDK exception may contain URLs/DSNs/response bodies. Return its type only.
    var actual = error is TargetInvocationException invocation ? invocation.InnerException! : error;
    Console.WriteLine(JsonSerializer.Serialize(new { status = "fail", errorType = actual.GetType().Name }, wire));
    return 1;
}
