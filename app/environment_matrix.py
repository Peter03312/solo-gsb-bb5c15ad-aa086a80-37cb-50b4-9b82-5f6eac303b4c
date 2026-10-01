"""Multi-environment resolution matrix.

Given the same input as ``/resolve`` plus 2-8 named environments, resolve
the root requirements and their transitive dependencies independently for
each environment's PEP 508 marker overrides.  Each environment keeps its
own ``ok`` / ``conflict`` / ``limit`` outcome and candidate-evaluation
count; one environment hitting the search limit (or being unsatisfiable)
never affects the others, because every solve is an independent call into
the same deterministic resolver.

For the successful environments only, a per-package version matrix is
summarized: a package absent from one successful environment but present
in another is reported with a ``null`` version, so absence stays
distinguishable from a concrete version.  Packages whose version (or
existence) differs across the successful environments are additionally
listed under ``differences``.  With fewer than two successful
environments there is nothing to compare against, so ``differences`` is
empty.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol

from packaging.utils import canonicalize_name
from packaging.version import Version

from .resolver import (
    PackageIndex,
    ResolutionInputError,
    RootEntry,
    resolve,
)
from .serialize import conflict_summary


class _EnvironmentLike(Protocol):
    name: str
    marker_env: Optional[dict[str, str]]


def validate_environments(environments: list[_EnvironmentLike]) -> None:
    """Validate the ``environments`` list (Pydantic already enforces shape).

    Names must be non-empty and unique; duplicates are reported verbatim so
    callers can tell which input entry clashed.  Failures raise
    :class:`ResolutionInputError` like every other semantic validation,
    mapping to the same HTTP 400 body as ``/resolve``.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for env in environments:
        name = env.name
        if not name.strip():
            raise ResolutionInputError("environments: environment name must not be empty")
        if name in seen:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        quoted = ", ".join(repr(name) for name in duplicates)
        raise ResolutionInputError(
            f"environments: names must be unique; duplicate name(s): {quoted}"
        )


def _resolve_environment(
    index: PackageIndex,
    entries: list[RootEntry],
    locks: dict[str, Version],
    *,
    allow_prereleases: bool,
    marker_env: Optional[dict[str, str]],
    max_attempts: int,
) -> dict[str, Any]:
    """Solve for one environment and render the ``/resolve``-shaped body."""
    result = resolve(
        index,
        [entry.dep for entry in entries],
        locks,
        allow_prereleases=allow_prereleases,
        marker_env=marker_env,
        max_attempts=max_attempts,
    )

    body: dict[str, Any] = {
        "status": result.status,
        "attempts": result.attempts,
        "allow_prereleases": allow_prereleases,
    }
    if result.status == "ok":
        body["resolved"] = result.chosen
        body["dependency_graph"] = result.edges
        body["locks"] = result.lock_notes
    else:
        # conflict and limit share the structured conflict report; limit
        # additionally carries the same error shape as POST /resolve.
        body["locks"] = result.lock_notes
        body["conflict"] = conflict_summary(result)
        if result.status == "limit":
            body["error"] = {
                "code": "search_limit_exceeded",
                "message": (
                    f"resolution exceeded {max_attempts} candidate evaluations"
                ),
            }
    return body


def _build_matrix(successful: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize resolved versions across successful environments.

    A package missing from an environment contributes ``None`` (absent),
    distinct from any concrete version string.  Packages are sorted by
    canonical name; the environments appear in their original input order.
    """
    names = [env["name"] for env in successful]

    # Union of every package resolved in any successful environment; absent
    # slots stay None rather than being dropped.
    package_names = set()
    for env in successful:
        package_names.update(env["result"]["resolved"])

    differences: list[str] = []
    package_rows: list[dict[str, Any]] = []
    comparable = len(successful) >= 2
    for package in sorted(package_names, key=canonicalize_name):
        versions = [env["result"]["resolved"].get(package) for env in successful]
        differs = comparable and len(set(versions)) > 1
        if differs:
            differences.append(package)
        package_rows.append(
            {
                "package": package,
                "differs": differs,
                "versions": [
                    {"environment": name, "version": version}
                    for name, version in zip(names, versions)
                ],
            }
        )

    return {
        "environments": names,
        "successful_count": len(successful),
        "packages": package_rows,
        "differences": differences,
    }


def run_environment_matrix(
    index: PackageIndex,
    entries: list[RootEntry],
    locks: dict[str, Version],
    environments: list[_EnvironmentLike],
    *,
    allow_prereleases: bool,
    max_attempts: int,
) -> dict[str, Any]:
    """Resolve every environment independently and render the response body."""
    validate_environments(environments)

    results: list[dict[str, Any]] = []
    successful: list[dict[str, Any]] = []
    counts = {"ok": 0, "conflict": 0, "limit": 0}
    for env in environments:
        body = _resolve_environment(
            index,
            entries,
            locks,
            allow_prereleases=allow_prereleases,
            marker_env=env.marker_env,
            max_attempts=max_attempts,
        )
        counts[body["status"]] += 1
        entry = {"name": env.name, "marker_env": env.marker_env or {}, **body}
        results.append(entry)
        if body["status"] == "ok":
            successful.append({"name": env.name, "result": body})

    return {
        "status": "ok",
        "summary": {
            "environments": len(environments),
            "ok": counts["ok"],
            "conflict": counts["conflict"],
            "limit": counts["limit"],
        },
        "allow_prereleases": allow_prereleases,
        "environments": results,
        "matrix": _build_matrix(successful),
    }
