<!-- Exact six code blocks, in order, from https://github.com/honua-io/honua-site/blob/4327a1ec8e4000556d57122b50c30f428868b4b3/docs/geoprocessing/index.md
Original UTF-8 source SHA256: ddb529d19ed48f1de609befba1c1f08d476a2bca4a577498baf1605eba4edcdb
The original guide is authoritative for prose/citations. Every block hash is checked against the frozen inventory. -->

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
