// GeoServices error-surfacing probe — .NET SDK (Honua.Sdk).
//
// Forces the server to return HTTP 200 with a GeoServices {"error": {...}} body and asserts the SDK
// THROWS rather than returning a success object. Companion regression test to sdk-js#309 / sdk-python#122.
//
// Exit-code contract (shared by every language probe):
//   0 = PASS  (SDK threw, as it must)
//   1 = FAIL  (SDK returned success on a 200+{error} — the bug)
//   2 = SKIP  (SDK not installed / not referenced)

using System;

var server = (Environment.GetEnvironmentVariable("HONUA_SERVER_URL") ?? "").TrimEnd('/');

// TODO(#7): once Honua.Sdk is published & version-pinned in the manifest, add the PackageReference in
// Probe.csproj and replace this guarded reflection probe with a direct typed call:
//
//   var client = new Honua.Sdk.Client(new Uri(server));
//   try { var r = await client.GeoServices.QueryAsync(layer: 0, where: "1=1)) DROP", outFields: "*"); }
//   catch (Exception e) { Console.WriteLine($"PASS: .NET SDK threw: {e.GetType().Name}"); return 0; }
//   Console.WriteLine($"FAIL: .NET SDK returned success: {r}"); return 1;
//
// Until the package reference exists this probe reports SKIP so the harness does not fabricate a pass.

var sdkType = Type.GetType("Honua.Sdk.Client, Honua.Sdk");
if (sdkType is null)
{
    Console.WriteLine("SKIP: Honua.Sdk not referenced (PackageReference pending real published artifact)");
    return 2;
}

// GeoServices convention: errors arrive in-band as {"error": {...}} with an HTTP 200 status line.
// TODO(#7): pin the exact endpoint + params that deterministically yield a 200+{error}, and the typed
// call above. This line is unreachable until the reference is added.
Console.WriteLine("SKIP: typed .NET probe not yet wired (see TODO above)");
return 2;
