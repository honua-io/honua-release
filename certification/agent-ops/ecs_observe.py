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
