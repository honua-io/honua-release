"""Bounded GitHub CLI requests without authentication or response logging."""
import json
import re
import subprocess
import sys
import time

TRANSIENT = ('error connecting', 'could not resolve host', 'connection reset by peer',
             'tls', 'timeout', 'timed out', 'temporary failure in name resolution')


def gh(*args, payload=None):
    """Retry the SAME request; never auth, log request bodies, or print raw responses."""
    cmd = ['gh', *args]
    if payload is not None:
        cmd += ['--input', '-']
    waited = 0
    delays = iter([10, 30, 60, 120, 80])
    while True:
        p = subprocess.run(cmd, input=None if payload is None else json.dumps(payload),
                           text=True, capture_output=True)
        if p.returncode == 0:
            return json.loads(p.stdout) if p.stdout.strip() else None
        error = p.stderr.lower()
        forbidden = '403' in error
        if not forbidden and not any(s in error for s in TRANSIENT):
            status = re.search(r'HTTP (\d+)', p.stderr)
            raise ValueError(f'GitHub request failed ({status[0] if status else "non-transient response"}): {args[1] if len(args)>1 else args[0]}')
        if waited >= 300:
            break
        # 403 waits share the network retry budget; never device-auth or re-auth.
        delay = min(60 if forbidden else next(delays), 300 - waited)
        if forbidden:
            print(f'GitHub 403: sleeping {delay} seconds before retrying the same request.', file=sys.stderr)
        else:
            print(f'Transient GitHub failure; retry budget used {waited}/300 seconds.', file=sys.stderr)
        time.sleep(delay)
        waited += delay
    raise ValueError('GitHub request failed after the full 300-second network retry budget')

