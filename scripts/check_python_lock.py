"""Compare installed distributions with exact requirements in a source/release lock."""

from __future__ import annotations

import importlib.metadata
import re
import sys
from pathlib import Path


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def logical_lines(path: Path) -> list[str]:
    result: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pending = f"{pending} {stripped}".strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        result.append(pending)
        pending = ""
    if pending:
        result.append(pending)
    return result


def exact_requirements(path: Path, seen: set[Path] | None = None) -> dict[str, tuple[str, str]]:
    seen = seen or set()
    path = path.resolve()
    if path in seen:
        return {}
    seen.add(path)
    requirements: dict[str, tuple[str, str]] = {}
    for raw_line in logical_lines(path):
        line = raw_line.split(" #", 1)[0].strip()
        if line.startswith(("-r ", "--requirement ")):
            nested = line.split(maxsplit=1)[1]
            requirements.update(exact_requirements(path.parent / nested, seen))
            continue
        if line.startswith("-"):
            continue
        without_hashes = re.sub(r"\s+--hash=sha256:[a-fA-F0-9]{64}", "", line).strip()
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^]]+\])?==([^;\s]+)(?:\s*;.*)?", without_hashes)
        if not match:
            raise ValueError(f"lock entry is not an exact supported pin: {raw_line}")
        requirements[canonical(match.group(1))] = (match.group(1), match.group(2))
    return requirements


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: check_python_lock.py <requirements-lock.txt>")
    lock = Path(sys.argv[1])
    requirements = exact_requirements(lock)
    if not requirements:
        raise SystemExit(f"no exact requirements found in {lock}")
    installed = {canonical(dist.metadata["Name"]): dist.version for dist in importlib.metadata.distributions() if dist.metadata.get("Name")}
    problems: list[str] = []
    for key, (display_name, expected) in sorted(requirements.items()):
        actual = installed.get(key)
        if actual is None:
            problems.append(f"{display_name} is missing (expected {expected})")
        elif actual != expected:
            problems.append(f"{display_name} is {actual} (expected {expected})")
    if problems:
        raise SystemExit("locked-package mismatch:\n- " + "\n- ".join(problems))
    print(f"{len(requirements)} exact locked distributions match")


if __name__ == "__main__":
    main()
