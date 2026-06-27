"""Minimal SemVer + range support — stdlib only (no third-party dep).

Just enough to validate the compatibility matrix: parse `MAJOR.MINOR.PATCH[-prerelease]`,
order versions per the SemVer precedence rules (a pre-release is lower than its release), and
test a version against a space-separated conjunction of comparators (`>=X <Y` etc.).

Deliberately simpler than npm semver in one respect: a pre-release version is compared by plain
precedence everywhere — there is no "a pre-release only satisfies a comparator whose operand
shares its M.M.P" carve-out. Our pins (e.g. `0.0.14-alpha.0`) are written to satisfy their own
ranges (`>=0.0.14-alpha.0 <0.1.0`) under plain precedence, so this is both correct for our data
and easier to reason about. Build metadata (`+...`) is ignored, per spec.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# MAJOR.MINOR.PATCH with an optional -prerelease and optional +build (build is discarded).
_SEMVER_RE = re.compile(
    r"^\s*v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?\s*$"
)

_COMPARATOR_RE = re.compile(r"^\s*(>=|<=|>|<|=)?\s*(.+?)\s*$")


class InvalidVersion(ValueError):
    """A string that does not parse as a SemVer version."""


class InvalidRange(ValueError):
    """A range string that does not parse as a conjunction of comparators."""


@dataclass(frozen=True, order=False)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[object, ...] = ()  # () means a final release (higher than any pre-release)

    # --- ordering ------------------------------------------------------------------------------
    def _key(self) -> tuple:
        # A release sorts above every pre-release of the same M.M.P. Encode that as a leading flag:
        # 1 for release, 0 for pre-release (so release > pre-release when the triples are equal).
        return (self.major, self.minor, self.patch, 1 if not self.prerelease else 0)

    def __lt__(self, other: "Version") -> bool:
        if not isinstance(other, Version):  # pragma: no cover - defensive
            return NotImplemented
        if self._key() != other._key():
            return self._key() < other._key()
        # Same triple, both pre-releases: compare identifier lists per SemVer §11.
        return _cmp_prerelease(self.prerelease, other.prerelease) < 0

    def __le__(self, other: "Version") -> bool:
        return self == other or self < other

    def __gt__(self, other: "Version") -> bool:
        return not self <= other

    def __ge__(self, other: "Version") -> bool:
        return not self < other

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            base += "-" + ".".join(str(p) for p in self.prerelease)
        return base


def _cmp_prerelease(a: tuple[object, ...], b: tuple[object, ...]) -> int:
    # Numeric identifiers compare numerically and rank below alphanumeric ones; a shorter list of
    # otherwise-equal identifiers ranks lower (SemVer §11.4).
    for x, y in zip(a, b):
        xn, yn = isinstance(x, int), isinstance(y, int)
        if xn and yn:
            if x != y:
                return -1 if x < y else 1
        elif xn != yn:
            return -1 if xn else 1  # numeric < alphanumeric
        else:
            if x != y:
                return -1 if x < y else 1
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return 0


def parse(version: str) -> Version:
    m = _SEMVER_RE.match(version or "")
    if not m:
        raise InvalidVersion(f"not a semver version: {version!r}")
    pre: tuple[object, ...] = ()
    if m.group("prerelease"):
        ids: list[object] = []
        for ident in m.group("prerelease").split("."):
            ids.append(int(ident) if ident.isdigit() else ident)
        pre = tuple(ids)
    return Version(int(m.group("major")), int(m.group("minor")), int(m.group("patch")), pre)


def is_semver(value: str) -> bool:
    try:
        parse(value)
        return True
    except InvalidVersion:
        return False


@dataclass(frozen=True)
class Comparator:
    op: str       # one of >= <= > < =
    version: Version

    def satisfied_by(self, v: Version) -> bool:
        return {
            ">=": v >= self.version,
            "<=": v <= self.version,
            ">": v > self.version,
            "<": v < self.version,
            "=": v == self.version,
        }[self.op]


@dataclass(frozen=True)
class Range:
    comparators: tuple[Comparator, ...]
    raw: str

    def satisfied_by(self, v: Version) -> bool:
        return all(c.satisfied_by(v) for c in self.comparators)

    @property
    def floor(self) -> Version | None:
        """Lower bound (from >= or >), or None for an open-below range."""
        lows = [c.version for c in self.comparators if c.op in (">=", ">", "=")]
        return max(lows) if lows else None

    @property
    def ceiling(self) -> Version | None:
        """Upper bound (from < or <=), or None for an open-above range."""
        highs = [c.version for c in self.comparators if c.op in ("<", "<=", "=")]
        return min(highs) if highs else None


def parse_range(spec: str) -> Range:
    if spec is None or not str(spec).strip():
        raise InvalidRange("empty range")
    comparators: list[Comparator] = []
    for token in str(spec).split():
        m = _COMPARATOR_RE.match(token)
        if not m:
            raise InvalidRange(f"bad comparator {token!r} in range {spec!r}")
        op = m.group(1) or "="
        try:
            ver = parse(m.group(2))
        except InvalidVersion as e:
            raise InvalidRange(f"bad version in comparator {token!r}: {e}") from e
        comparators.append(Comparator(op, ver))
    if not comparators:
        raise InvalidRange(f"no comparators parsed from {spec!r}")
    return Range(tuple(comparators), str(spec))


def satisfies(version: str, spec: str) -> bool:
    """Convenience: does `version` satisfy range `spec`?"""
    return parse_range(spec).satisfied_by(parse(version))
