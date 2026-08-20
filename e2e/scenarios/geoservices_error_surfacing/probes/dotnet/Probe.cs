using Honua.Sdk.GeoServices.FeatureServer;
using Honua.Sdk.GeoServices.FeatureServer.Exceptions;
using Honua.Sdk.GeoServices.FeatureServer.Models;

var server = (Environment.GetEnvironmentVariable("HONUA_SERVER_URL") ?? "").TrimEnd('/');
using var http = new HttpClient { BaseAddress = new Uri(server) };
var client = new HonuaFeatureServerClient(http);

try
{
    var result = await client.QueryAsync(
        "test",
        0,
        new FeatureServerQueryParams { Where = "__release_missing_field__=1" });
    Console.WriteLine($"FAIL: .NET SDK returned success on 200+{{error}}: {result}");
    return 1;
}
catch (HonuaFeatureServerException exception)
{
    Console.WriteLine($"PASS: .NET SDK raised HonuaFeatureServerException on 200+{{error}}: {exception.Message}");
    return 0;
}
catch (Exception exception)
{
    Console.WriteLine($"FAIL: .NET SDK raised the wrong exception: {exception.GetType().Name}: {exception.Message}");
    return 1;
}
