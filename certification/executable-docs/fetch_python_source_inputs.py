"""Fetch only the reviewed protected witness. Run only from trusted release code.

The caller scopes its read credential to this process. Neither the credential nor
its environment is retained in the output. Parsing or executing the SDK witness
belongs to a subsequent, credential-free step.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

import python_source_inputs as inputs


def api(path: str, *, pages: bool = False):
    command = ["gh", "api", "--hostname", "github.com", path]
    if pages:
        command += ["--paginate", "--slurp"]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if len(result.stdout) > 16 * 1024 * 1024:
        raise inputs.InputRefusal("producer metadata exceeds its finite bound")
    return inputs.read_json(result.stdout)


def fetch(destination: Path) -> dict:
    selection, _ = inputs.selected_inputs()
    producer, artifact = selection["producer"], selection["artifact"]
    prefix = "repos/" + inputs.REPOSITORY + "/actions/"
    run = api(prefix + "runs/" + str(producer["runId"]))
    metadata = api(prefix + "artifacts/" + str(artifact["id"]))
    pages = api(prefix + "runs/" + str(producer["runId"]) + "/attempts/"
                + str(producer["attempt"]) + "/jobs?per_page=100", pages=True)
    if not isinstance(pages, list) or not pages or any(not isinstance(page.get("jobs"), list) for page in pages):
        raise inputs.InputRefusal("producer attempt job listing is incomplete")
    jobs = [job for page in pages for job in page["jobs"]]
    if any(page.get("total_count") != len(jobs) for page in pages):
        raise inputs.InputRefusal("producer attempt job census differs")
    inputs.verify_producer(selection, run, metadata, jobs)
    destination.mkdir(mode=0o700)  # Never overwrite another retained download.
    originals = {"run.json": run, "artifact.json": metadata, "jobs.json": jobs}
    for name, value in originals.items():
        (destination / name).write_text(json.dumps(value, sort_keys=True) + "\n")
    command = ["gh", "api", "--hostname", "github.com", "--allow-escape-sequences",
               prefix + "artifacts/" + str(artifact["id"]) + "/zip"]
    digest, size = hashlib.sha256(), 0
    with (destination / "original.zip").open("xb") as output:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            assert process.stdout is not None
            for chunk in iter(lambda: process.stdout.read(8 * 1024 * 1024), b""):
                size += len(chunk)
                if size > artifact["size"]:
                    raise inputs.InputRefusal("producer archive exceeds selected byte bound")
                output.write(chunk)
                digest.update(chunk)
            if process.wait() != 0:
                raise inputs.InputRefusal("protected producer archive download failed")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
            if process.stdout is not None:
                process.stdout.close()
    if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise inputs.InputRefusal("protected producer archive byte identity differs")
    receipt = {"schema": "honua.python-source-input-fetch/v1", "repository": inputs.REPOSITORY,
               "runId": producer["runId"], "jobId": producer["jobId"], "attempt": producer["attempt"],
               "implementationSha": producer["sourceSha"], "subjectSha": selection["subject"]["sourceSha"],
               "artifactId": artifact["id"], "archiveBytes": size, "archiveSha256": digest.hexdigest(),
               "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}
    (destination / "fetch.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(fetch(args.output), sort_keys=True))
