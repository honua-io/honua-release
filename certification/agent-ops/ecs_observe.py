"""Read ECS task digests. This client has no write methods."""
from __future__ import annotations

import re

_REGION = "us-east-1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ObservationMissing(RuntimeError):
    pass


class EcsObserver:
    def __init__(self, client, cluster: str, service: str, region: str = _REGION):
        if region != _REGION or not isinstance(cluster, str) or not cluster or "/" in cluster:
            raise ValueError("cluster")
        if not isinstance(service, str) or not service or "/" in service:
            raise ValueError("service")
        self.client = client
        self.cluster = cluster
        self.service = service

    def task_digests(self) -> dict:
        described = self.client.describe_services(cluster=self.cluster, services=[self.service])
        services = described.get("services") if isinstance(described, dict) else None
        if described.get("failures") or not isinstance(services, list) or len(services) != 1:
            raise ObservationMissing("service")
        service = services[0]
        task_definition = service.get("taskDefinition")
        if (service.get("status") != "ACTIVE" or not isinstance(task_definition, str)
                or not isinstance(service.get("runningCount"), int)
                or service.get("runningCount") < 1
                or service.get("runningCount") != service.get("desiredCount")):
            raise ObservationMissing("service")
        listed = self.client.list_tasks(
            cluster=self.cluster, serviceName=self.service, desiredStatus="RUNNING")
        arns = listed.get("taskArns") if isinstance(listed, dict) else None
        if not isinstance(arns, list) or not arns or any(not isinstance(arn, str) for arn in arns):
            raise ObservationMissing("tasks")
        described_tasks = self.client.describe_tasks(cluster=self.cluster, tasks=arns)
        tasks = described_tasks.get("tasks") if isinstance(described_tasks, dict) else None
        if described_tasks.get("failures") or not isinstance(tasks, list) or len(tasks) != len(arns):
            raise ObservationMissing("tasks")
        definition = self.client.describe_task_definition(taskDefinition=task_definition)
        containers = ((definition.get("taskDefinition") or {}) if isinstance(definition, dict) else {})
        declared = containers.get("containerDefinitions") if isinstance(containers, dict) else None
        if not isinstance(declared, list) or not declared:
            raise ObservationMissing("task definition")
        digests = []
        for task in tasks:
            task_containers = task.get("containers") if isinstance(task, dict) else None
            if not isinstance(task_containers, list) or not task_containers:
                raise ObservationMissing("image digest")
            for container in task_containers:
                digest = container.get("imageDigest") if isinstance(container, dict) else None
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    raise ObservationMissing("image digest")
                digests.append(digest)
        if any(digest != digests[0] for digest in digests):
            raise ObservationMissing("digest mismatch")
        return {
            "task_definition": task_definition,
            "image_digest": digests[0],
            "running": service["runningCount"],
            "desired": service["desiredCount"],
            "images": [item.get("image") for item in declared if isinstance(item, dict)],
        }


class MigrationStateReader:
    """Controller-side evidence from one candidate task log and the migration journal.

    The log stream and pending script must come from the controller's verified task
    and pre-upgrade inventory, never from model output.
    """

    def __init__(self, session, logs, *, log_group: str, log_stream: str,
                 script: str, evidence_id: str, backend_pid: int, start_time_ms: int):
        if (not isinstance(log_group, str) or not log_group or
                not isinstance(log_stream, str) or not log_stream or
                not isinstance(script, str) or not re.fullmatch(r"[A-Za-z0-9._-]+\.sql", script) or
                not isinstance(evidence_id, str) or not re.fullmatch(r"[0-9a-f]{32}", evidence_id) or
                type(backend_pid) is not int or backend_pid <= 0 or
                type(start_time_ms) is not int or start_time_ms < 0):
            raise ValueError("invalid migration observation binding")
        self.session, self.logs = session, logs
        self.log_group, self.log_stream, self.script = log_group, log_stream, script
        self.evidence_id, self.backend_pid, self.start_time_ms = evidence_id, backend_pid, start_time_ms

    def __call__(self) -> dict:
        # The script is validated above and originates in the controller inventory.
        count = self.session.query(
            f"SELECT count(*) FROM public.schema_versions WHERE scriptname='{self.script}'")
        if count not in ("0", "1"):
            raise ObservationMissing("migration journal")
        params = {"logGroupName": self.log_group, "logStreamName": self.log_stream,
                  "startTime": self.start_time_ms, "startFromHead": True, "limit": 10000}
        messages = []
        token = None
        for _ in range(10):
            if token is not None:
                params["nextToken"] = token
            page = self.logs.get_log_events(**params)
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise ObservationMissing("migration logs")
            for event in page["events"]:
                if not isinstance(event, dict) or not isinstance(event.get("message"), str):
                    raise ObservationMissing("migration logs")
                messages.append(event["message"])
            next_token = page.get("nextForwardToken")
            if not isinstance(next_token, str) or not next_token:
                raise ObservationMissing("migration log cursor")
            if token == next_token:
                break
            token = next_token
        else:
            raise ObservationMissing("migration log page incomplete")
        text = "\n".join(messages)
        scripts = list(re.finditer(r"Executing Database Server script '([^']+)'", text))
        failure = bool(scripts and scripts[-1].group(1) == self.script and
                       re.search(r"migration[^\n]{0,200}?fail", text[scripts[-1].end():], re.I))
        return {"evidence_id": self.evidence_id, "backend_pid": self.backend_pid,
                "migration_failed": failure,
                "journal_absent": count == "0"}
