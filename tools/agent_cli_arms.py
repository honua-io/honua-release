"""Sequential host runner. Local Docker must remain healthy; remote cells are pre-provisioned."""
import argparse
import fcntl
import json
from pathlib import Path
import subprocess
import sys
import uuid

from agent_e2e_cli_runner import target_url
from agent_cli_sessions import MODELS

ROOT = Path(__file__).resolve().parents[1]


def docker_healthy():
    for command in (['docker', 'info', '--format', '{{.ServerVersion}}'],
                    ['docker', 'ps', '--format', '{{.Status}}']):
        result = subprocess.run(command, capture_output=True, timeout=20)
        if result.returncode or b'unhealthy' in result.stdout.lower():
            raise RuntimeError('Docker is unhealthy; stopping the sequential campaign')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--surface', choices=('corpus', 'canary', 'ops'), required=True)
    parser.add_argument('--agent', choices=tuple(MODELS), required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--cases', default='P10:A', help='corpus: comma-separated Pxx:A/Pxx:B; canary: all stages')
    parser.add_argument('--trials', type=int, default=1)
    parser.add_argument('--target', type=target_url, default='local')
    parser.add_argument('--target-config', type=Path)
    parser.add_argument('--deployment-receipt', type=Path)
    parser.add_argument('--pack', type=Path)
    parser.add_argument('--image-ref')
    parser.add_argument('--lock-digest')
    parser.add_argument('--lock-path', type=Path)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.model not in MODELS[args.agent] or args.trials <= 0 or args.timeout <= 0:
        parser.error('invalid model pairing or trial/time budget')
    if args.output.exists(): parser.error('output directory exists; preserve every previous run')
    if args.target != 'local' and (not args.target_config or not args.deployment_receipt):
        parser.error('remote target needs its reviewed target config and deployment receipt')
    if args.surface == 'ops':
        # No subscription invocation or Docker activity until real OPS scenarios exist.
        sys.path.insert(0, str(ROOT / 'certification/agent-ops'))
        import cli_agent
        cli_agent.require_real_scenarios()
        return cli_agent.main(argv)
    cases = args.cases.split(',') if args.surface == 'corpus' else ['journey']
    if len(cases) != len(set(cases)): parser.error('duplicate cases')
    if args.surface == 'corpus':
        import re
        if any(not re.fullmatch(r'P\d\d:[AB]', case) for case in cases): parser.error('only A/B corpus cases are admitted')
    import yaml
    server = yaml.safe_load((ROOT/'platform-manifest.yaml').read_bytes())['components']['honua-server']
    common = ['--agent', args.agent, '--model', args.model, '--target', args.target, '--timeout', str(args.timeout)]
    if args.lock_digest:
        if not args.lock_path: parser.error('--lock-digest requires --lock-path')
        common += ['--lock-digest', args.lock_digest, '--lock-path', str(args.lock_path)]
    else:
        common += ['--image-ref', args.image_ref or server['image'].split('@')[0]+'@'+server['digest']]
    for option in ('target_config','deployment_receipt'):
        if getattr(args, option): common += ['--'+option.replace('_','-'), str(getattr(args,option).resolve())]
    lock_path = Path.home()/'.cache/honua-cli-arms.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: parser.error('another CLI arms campaign owns this machine')
        args.output.mkdir(parents=True)
        rows=[]
        campaign=uuid.uuid4().hex
        for trial in range(1, args.trials+1):
            for case in cases:
                if args.target == 'local': docker_healthy()
                output=args.output/f'trial-{trial:03d}'/case.replace(':','-')/'receipt.json'
                if args.surface == 'corpus':
                    command=[sys.executable,str(ROOT/'tools/agent_e2e_cli_runner.py'),*common,
                             '--scenario',case.split(':')[0],'--variant',case.split(':')[1]]
                    if args.pack: command+=['--pack',str(args.pack)]
                else:
                    command=[sys.executable,str(ROOT/'tools/terminal_model_canary.py'),*common]
                result=subprocess.run([*command,'--output',str(output)], timeout=args.timeout*10+3600)
                if args.target == 'local': docker_healthy()
                if not output.exists(): raise RuntimeError('case failed without a receipt; stop for infrastructure')
                receipt=json.loads(output.read_bytes())
                receipt.update(campaign=campaign, trial=trial)
                output.write_text(json.dumps(receipt,indent=2)+'\n')
                rows.append({'case':case,'trial':trial,'status':receipt['status'],'receipt':str(output)})
                (args.output/'index.json').write_text(json.dumps({'campaign':campaign,'trials':rows},indent=2)+'\n')
                if receipt.get('status') == 'infrastructure':
                    raise RuntimeError('case infrastructure or cleanup failed; stop without retry')
                if result.returncode not in (0,1): raise RuntimeError('runner crashed; campaign stopped')
        return 0 if all(row['status']=='pass' for row in rows) else 1


if __name__ == '__main__': raise SystemExit(main())
