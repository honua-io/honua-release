#!/usr/bin/env python3
"""Compare lifecycle pins against actual Terraform console bytes; no providers."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from agent_e2e_worker_lifecycle import terraform_definition_digest


def main():
    fixture = Path(__file__).parent/'fixtures/worker-definition-digest/terraform-installed-slot.json'
    cases = [
        ('actual-installed-asl', fixture.read_text(encoding='utf-8')),
        ('escapes', '{"<key>":"<>&\\u2028\\u2029","literal":"\\\\u003c\\\\u2028","unicode":"😀é"}'),
        ('numbers', '{"values":[1.0,-0.0,1e6,1e-7,1.2300,9007199254740993,0.123456789012345678901234567890123456789]}'),
        ('normalization', '{"e\\u0301":"e\\u0301","z":null,"a":[true,false]}'),
        ('precise-number', '{"value":0.'+'1234567890'*12+'12345678}'),
    ]
    with tempfile.TemporaryDirectory(prefix='honua-definition-digest-') as directory:
        version = json.loads(subprocess.run(['terraform', 'version', '-json'], cwd=directory,
            check=True, capture_output=True, text=True, timeout=30).stdout)
        if version['terraform_version'] != '1.13.5':
            raise RuntimeError('definition digest control requires reviewed Terraform 1.13.5')
        for name, original in cases:
            encoded = base64.b64encode(original.encode('utf-8')).decode('ascii')
            expression = 'jsonencode(jsondecode(base64decode("'+encoded+'")))\n'
            response = subprocess.run(['terraform', 'console', '-no-color'], cwd=directory,
                input=expression, check=True, capture_output=True, text=True, timeout=30)
            actual = json.loads(response.stdout).encode('utf-8')
            expected = 'sha256:'+hashlib.sha256(actual).hexdigest()
            if terraform_definition_digest(original) != expected:
                raise RuntimeError('Terraform definition digest differs: '+name)
            print(json.dumps({'control': name, 'terraformVersion': version['terraform_version'],
                'actualTerraformDigest': expected}))


if __name__ == '__main__':
    main()
