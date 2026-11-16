import fnmatch
import json
from pathlib import Path
import subprocess
import sys


def check(root):
    policy = json.loads((root / ".github/hygiene.json").read_text())
    patterns = policy["denied_public_paths"]
    if not isinstance(patterns, list) or not patterns or any(
        not isinstance(pattern, str) or not pattern.strip() for pattern in patterns
    ):
        raise ValueError("denied_public_paths must be a nonempty list of path patterns")
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8").split("\0")
    return sorted(path for path in tracked if path and any(
        fnmatch.fnmatchcase(path, pattern) for pattern in patterns
    ))


def main(root):
    try:
        findings = check(root)
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as error:
        print(f"Public content policy could not be evaluated: {type(error).__name__}")
        return 1
    for path in findings:
        print(f"Archived internal content must stay outside the public repository: {path}")
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main(Path(__file__).resolve().parents[1]))
