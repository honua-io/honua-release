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
        if isinstance(self.session, DatabaseJournal):
            count = "1" if self.session.contains(self.script) else "0"
        else:
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


class DatabaseJournal:
    """Read the actual journal over a controller-owned PostgreSQL connection."""

    def __init__(self, connection):
        self.connection = connection

    def contains(self, script: str) -> bool:
        if not isinstance(script, str) or not re.fullmatch(r'[A-Za-z0-9._-]+\.sql', script):
            raise ValueError('invalid migration script')
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM public.schema_versions WHERE scriptname = %s', (script,))
            row = cursor.fetchone()
        if not isinstance(row, (tuple, list)) or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
            raise ObservationMissing('migration journal')
        return row[0] > 0

    def scripts(self) -> list[str]:
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT scriptname FROM public.schema_versions ORDER BY scriptname')
            rows = cursor.fetchall()
        if not isinstance(rows, list) or any(not isinstance(row, (tuple, list)) or len(row) != 1 or
                                              not isinstance(row[0], str) for row in rows):
            raise ObservationMissing('migration journal')
        return [row[0] for row in rows]


def _database_parameters(connection_string: str) -> dict:
    """Parse the Npgsql secret emitted by the cell; require TLS to the cell DB."""
    if not isinstance(connection_string, str) or not connection_string:
        raise ValueError('database connection missing')
    fields = {}
    for part in connection_string.split(';'):
        if not part.strip():
            continue
        key, separator, value = part.partition('=')
        key = key.strip().lower()
        if not separator or key in fields:
            raise ValueError('invalid database connection')
        fields[key] = value.strip()
    required = ('host', 'database', 'username', 'password')
    if any(not fields.get(name) for name in required):
        raise ValueError('invalid database connection')
    port = fields.get('port', '5432')
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError('invalid database port')
    return {'host': fields['host'], 'port': int(port), 'dbname': fields['database'],
            'user': fields['username'], 'password': fields['password'], 'sslmode': 'require',
            'connect_timeout': 30, 'autocommit': True,
            'options': '-c default_transaction_read_only=on'}


def open_controller_state(*, cluster: str, service: str, database_dsn: str):
    """Open clients using the cell controller's verified outputs and private DB secret.

    The caller must close the returned journal connection. Secrets are never logged.
    """
    parameters = _database_parameters(database_dsn)
    import boto3
    import psycopg
    observer = EcsObserver(boto3.client('ecs', region_name=_REGION), cluster, service)
    connection = psycopg.connect(**parameters)
    return observer, DatabaseJournal(connection)


def controller_snapshot(ecs: EcsObserver, journal: DatabaseJournal) -> dict:
    """Independently read the deployed service and committed migration journal."""
    return {'ecs': ecs.task_digests(), 'journal': journal.scripts()}


def open_cell_controller_state(target, *, open_state=open_controller_state):
    """Bind reads to the already-provisioned target's private Terraform state.

    The target must be the controller's provisioned object, never model input or
    a public handoff. Its own methods read outputs and the DB secret in memory.
    """
    workdir = getattr(target, '_workdir', None)
    if workdir is None or getattr(target, 'name', None) != 'aws-ecs':
        raise ValueError('verified ECS target required')
    def output(name):
        result = target._tf(workdir, 'output', '-raw', name)
        value = getattr(result, 'stdout', None)
        if not isinstance(value, str) or not value.strip():
            raise ObservationMissing('cell output')
        return value.strip()
    cluster = output('ecs_cluster_name')
    service = output('ecs_service_name')
    connection = target._connection_string()
    return open_state(cluster=cluster, service=service, database_dsn=connection)
