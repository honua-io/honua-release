# Container command runtime

This is the common transport image for Fargate and the dedicated Docker lane.
It is not yet a complete scenario image: the pinned Honua CLI/SDK tools, selected
public knowledge and case input delivery remain backend integration work.

Build only from the explicit allowlist, never copy the release checkout into the
image. From the repository root:

```powershell
python tools/bundle_agent_e2e_container.py --output C:/controller/worker-context
docker build --platform linux/amd64 -t honua-agent-worker:review C:/controller/worker-context
```

The base is pinned to a Linux amd64 manifest and every Python dependency is
version/hash locked. The exported context contains six runtime/build files and no
pack, oracle, prompt corpus or controller logic. Publish and freeze the resulting
OCI digest before running a campaign; a local tag is not a campaign pin.

`HONUA_AGENT_TRANSPORT` binds the exact per-attempt command/reply FIFO URLs, attempt
identity and absolute expiry. Task IAM permits receiving/deleting that attempt's
commands and sending its replies only. A command carries its action identity,
deadline and digest-bound result. The controller must durably record dispatch
before invoking the transport. A timeout or ambiguous send does not permit replay.

The worker uses a synchronous SQLite journal: completed duplicate deliveries
return the stored response; a started action left by a crash becomes interrupted.
Never replace a task using the same queues after loss of this local journal. The
Fargate lifecycle uses a retained one-time launch claim and denies redrive. Docker
must implement the same claim before launching its container.

Containers run non-root with a read-only application filesystem, dropped Linux
capabilities, bounded resources and no host socket. Only workspace and local
journal volumes are writable. Commands receive a clean environment; task-role
network access is still possible, which is why that role has no controller,
oracle, model, provisioning or approval permissions. Command output and worker
replies are untrusted. Independent authenticated probes decide goal completion.

The fixed smoke script tests the actual Linux container's UID, immutable app,
clean command environment, timeout, output bound and background-process cleanup.
It supplies no model and is not live campaign evidence. Live queue/IAM/network
checks and independently enforced cleanup remain mandatory before qualification.
