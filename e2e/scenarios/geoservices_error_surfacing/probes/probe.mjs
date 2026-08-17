// GeoServices error-surfacing probe — JS SDK (@honua/sdk-js).
//
// Forces the server to return HTTP 200 with a GeoServices `{ error: {...} }` body and asserts the SDK
// REJECTS/throws rather than resolving with a success object. Regression test for sdk-js#309.
//
// Exit-code contract (shared by every language probe):
//   0 = PASS  (SDK threw/rejected, as it must)
//   1 = FAIL  (SDK resolved success on a 200+{error} — the bug)
//   2 = SKIP  (SDK not installed)

const SERVER = (process.env.HONUA_SERVER_URL || "").replace(/\/$/, "");

let sdk;
try {
  sdk = await import("@honua/sdk-js");
} catch (e) {
  console.log(`SKIP: @honua/sdk-js not importable: ${e}`);
  process.exit(2);
}

async function force200Error() {
  // GeoServices convention: errors arrive in-band as { error: { code, message, details } } with an
  // HTTP 200 status line — the trap that sdk-js#309 fell into by trusting the status code.
  // TODO(#7): pin the exact endpoint + params that deterministically yield a 200+{error}.
  const client = new sdk.Client({ baseUrl: SERVER }); // TODO(#7): confirm constructor
  return client.geoservices.query({
    layer: 0,
    where: "1=1)) DROP", // malformed predicate -> server returns 200+{error}
    outFields: "*",
  });
}

try {
  const result = await force200Error();
  console.log(`FAIL: js SDK resolved success on 200+{error}: ${JSON.stringify(result)}`);
  process.exit(1);
} catch (e) {
  console.log(`PASS: js SDK threw on 200+{error}: ${e}`);
  process.exit(0);
}
