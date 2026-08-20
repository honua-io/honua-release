// Require the frozen JavaScript SDK to surface a real GeoServices error envelope.
const SERVER = (process.env.HONUA_SERVER_URL || "").replace(/\/$/, "");

let sdk;
try {
  sdk = await import("@honua/sdk-js");
} catch (error) {
  console.log(`SKIP: @honua/sdk-js not importable: ${error}`);
  process.exit(2);
}

try {
  const client = new sdk.HonuaClient({ baseUrl: SERVER });
  const result = await client.queryFeatures({
    serviceId: "test",
    layerId: 0,
    where: "__release_missing_field__=1",
    method: "GET",
  });
  console.log(`FAIL: JavaScript SDK returned success on 200+{error}: ${JSON.stringify(result)}`);
  process.exit(1);
} catch (error) {
  if (error instanceof sdk.HonuaHttpError) {
    console.log(`PASS: JavaScript SDK raised HonuaHttpError on 200+{error}: ${error}`);
    process.exit(0);
  }
  console.log(`FAIL: JavaScript SDK raised the wrong error type: ${error}`);
  process.exit(1);
}
