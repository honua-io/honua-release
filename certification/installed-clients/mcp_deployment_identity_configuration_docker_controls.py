"""Real owned Docker template boundary controls; not a server/candidate journey."""
import json
import os
import re
import secrets
import subprocess
import unittest
from unittest.mock import patch
import mcp_deployment_identity_configuration as config

IMAGE="redis:7.2@sha256:29e8589c3f9ba699b5f7aa4b3c7733c58852a3626439e619aa0ee78de08c6ca0"

class DaemonProjectionControls(unittest.TestCase):
    def execute(self,*args):
        return subprocess.run(list(args),check=True,capture_output=True,text=True,timeout=30).stdout.strip()
    def setUp(self):
        if os.environ.get("GITHUB_ACTIONS")!="true":raise ValueError("Docker projection controls require workflow-owned daemon")
        self.marker="mcp-tenant-config-"+secrets.token_hex(12);self.container=None
    def inspect_owned(self):
        template='{"containerId":{{json .Id}},"imageId":{{json .Image}},"running":{{json .State.Running}},"name":{{json .Name}},"marker":{{json (index .Config.Labels "honua.release.control")}},"image":{{json .Config.Image}}}'
        value=json.loads(self.execute("docker","inspect","--format",template,self.container))
        if value.get("containerId")!=self.container or value.get("name")!="/"+self.marker or value.get("marker")!=self.marker or value.get("image")!=IMAGE:
            raise ValueError("control container ownership differs")
        return {key:value[key] for key in ("containerId","imageId","running")}|{"candidateSourceSha":"a"*40}
    def provision(self,extra=None):
        command=["docker","create","--name",self.marker,"--label","honua.release.control="+self.marker,
            "--network","none","--memory","32m","--cpus","0.25","--pids-limit","32"]
        for key,value in (dict(config.ENV)|{"HONUA_ADMIN_PASSWORD":"explicit-offline-private-sentinel"}|(extra or {})).items():command.extend(["--env",key+"="+value])
        command.extend(["--entrypoint","/bin/sh",IMAGE,"-c","sleep 30"])
        self.container=self.execute(*command)
        if not re.fullmatch(r"[a-f0-9]{64}",self.container):raise ValueError("new owned control ID malformed")
        self.inspect_owned();self.execute("docker","start",self.container)
    def tearDown(self):
        if self.container:
            self.inspect_owned();self.execute("docker","rm","--force",self.container)
    def test_actual_daemon_selects_safe_values_without_returning_unrelated_credentials(self):
        self.provision();expected=config.declaration("mcp-deployment-"+"d"*12)
        result=config.observe_configuration(expected,self.inspect_owned)
        self.assertEqual(result["environment"],config.ENV)
        output=self.execute("docker","inspect","--format",config.option_template(),self.container)
        self.assertNotIn("explicit-offline-private-sentinel",output)
        self.assertEqual(sum(json.loads(line)=={"selectedScope":True} for line in output.splitlines()),len(config.ENV))
    def test_actual_case_insensitive_scope_census_refuses_extra_without_returning_its_value(self):
        self.provision({"multitenancy__UnreviewedPrivateOption":"explicit-offline-namespace-sentinel"})
        output=self.execute("docker","inspect","--format",config.option_template(),self.container)
        self.assertNotIn("explicit-offline-namespace-sentinel",output)
        self.assertNotIn("explicit-offline-private-sentinel",output)
        self.assertEqual(sum(json.loads(line)=={"selectedScope":True} for line in output.splitlines()),len(config.ENV)+1)
        with self.assertRaisesRegex(ValueError,"unreviewed case-insensitive authorization configuration refused"):config.observe_configuration(config.declaration("mcp-deployment-"+"d"*12),self.inspect_owned)

if __name__=="__main__":unittest.main()
