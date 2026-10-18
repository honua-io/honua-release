---
type: slice
title: "Run a geoprocessing job"
description: "Run a geoprocessing job — over OGC API - Processes, from JavaScript, Python and .NET."
resource: "https://honua.io/docs/geoprocessing/"
tags: ["shape:map", "task:geoprocessing", "protocol:ogc-api-processes", "capability:process.ogc-api-processes", "capability:process.geoprocessing", "sdk:js", "sdk:python", "sdk:dotnet", "sample:gp-runner"]
generated: "2026-08-27"
---

# Run a geoprocessing job

Run a geoprocessing job — over OGC API - Processes, from JavaScript, Python and .NET.

## See it run

[Run a geoprocessing job](../../demo-geoprocessing.html) — Submit, poll, inspect, and cancel an asynchronous process.

## What it is

**OGC API Processes.** Submit and poll jobs through OGC API - Processes.

**Geoprocessing Task Execution.** Submit and poll geoprocessing tasks through the Esri GeoServices GPServer surface. Print/export-specific tasks are gated by printing.* entitlement keys.

## Set it up

### Console

> Not in the Console yet — [track it here](https://github.com/honua-io/honua-site/issues/219).

### CLI

> Not in the CLI yet — [track it here](https://github.com/honua-io/honua-sdk-js/issues/1424).

### Admin API

> Not in the Admin API yet — [track it here](https://github.com/honua-io/honua-site/issues/235).

## Use it

### JavaScript

```bash
npm install @honua/sdk-js@0.1.16
```

```js
import { HonuaClient } from '@honua/sdk-js';

const client = new HonuaClient({ baseUrl: 'http://localhost:8080', apiKey: '<your-api-key>' });
const inputs = { wkb: { type: 'Point', coordinates: [-156.33, 20.8] }, distance: 0.01, srid: 4326 };

const processes = client.ogcProcesses();
const run = await processes.execute({ processId: 'geometry.buffer', mode: 'async', inputs });
const stopWatching = run.watch((snapshot) => console.log(snapshot.status));
const output = await run.results({ deadlineMs: 30_000, maxAttempts: 20 });
stopWatching();
console.log(output.outputs.outputFeatureLayer.value.geometry.type);
```

### Python

> Partly there in the Python SDK — [track the rest here](https://github.com/honua-io/honua-sdk-python/issues/196).

```bash
python3 -m pip install honua-sdk==0.1.16
```

```python
from honua_sdk import HonuaClient

client = HonuaClient('http://localhost:8080', api_key='<your-api-key>')
inputs = {'wkb': {'type': 'Point', 'coordinates': [-156.33, 20.8]}, 'distance': 0.01, 'srid': 4326}

gp = client.geoprocessing()
job = gp.submit_inputs('geometry.buffer', inputs)
terminal = gp.wait(job, poll_interval=0.5, timeout=30.0)
output = gp.results(terminal.job_id)
print(output['outputFeatureLayer']['value']['geometry']['type'])
```

### .NET

> Partly there in the .NET SDK — [track the rest here](https://github.com/honua-io/honua-sdk-dotnet/issues/293).

```bash
dotnet new console -o geoprocessing && cd geoprocessing
dotnet add package Honua.Sdk --version 1.11.1
```

```csharp
using System.Text.Json;
using Honua.Sdk.Processes;

using var http = new HttpClient { BaseAddress = new Uri("http://localhost:8080") };
http.DefaultRequestHeaders.Add("X-API-Key", "<your-api-key>");
var processes = new HonuaProcessesClient(http);
var cancellationToken = CancellationToken.None;
var inputs = new Dictionary<string, JsonElement>
{
    ["wkb"] = JsonSerializer.SerializeToElement(new { type = "Point", coordinates = new[] { -156.33, 20.8 } }),
    ["distance"] = JsonSerializer.SerializeToElement(0.01),
    ["srid"] = JsonSerializer.SerializeToElement(4326),
};

var job = await processes.SubmitJobAsync("geometry.buffer", inputs, cancellationToken);
while (job.Status is "accepted" or "running")
{
    await Task.Delay(500, cancellationToken);
    job = await processes.GetJobAsync(job.JobId, cancellationToken);
}
var output = await processes.GetJobResultsAsync(job.JobId, cancellationToken);
Console.WriteLine(job.Status);
```

### Mobile

> Not in the mobile SDKs yet — [track it here](https://github.com/honua-io/honua-server/issues/2448).

## Ask it

### MCP

> Not in the MCP server yet — [track it here](https://github.com/honua-io/honua-server/issues/3269).

## Underneath

Protocols: `OGC API - Processes`

Capability keys: [process.ogc-api-processes](../../evidence-process-ogc-api-processes.html), [process.geoprocessing](../../evidence-process-geoprocessing.html)

[How this is checked](../../evidence-process-ogc-api-processes.html)
