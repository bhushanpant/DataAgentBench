"""Read-only manifest recovery from an existing, identically pinned package cache.

Never installs/downloads dependencies or changes a project. Only exact registry
pins are supported; missing, partial, ambiguous or mismatched caches fail closed.
The caller parses the private copy and retains its provenance, within its budget.
"""
import hashlib
import os
from pathlib import Path
import re
import shutil
import time

import scrybe_core as core


def _inside(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("package/project paths must stay inside their project")
    return path


def _spec(root):
    paths = [root / n for n in ("packages.yml", "dependencies.yml") if (root / n).is_file()]
    if len(paths) != 1:
        raise ValueError("exactly one package declaration is required")
    if any(p.is_symlink() for p in [*paths, root / "package-lock.yml", root / "dbt_project.yml"]):
        raise ValueError("symlinked package metadata is unsupported")
    spec = core.read_yaml(str(paths[0]))
    lock = core.read_yaml(str(root / "package-lock.yml"))
    if not isinstance(spec, dict) or set(spec) != {"packages"}:
        raise ValueError("only registry package dependencies are supported")
    declared, locked = spec["packages"], lock.get("packages") if isinstance(lock, dict) else None
    if not isinstance(declared, list) or not declared or not isinstance(locked, list) or not locked:
        raise ValueError("package declarations and lock entries must be nonempty")
    versions = {}
    names = []
    for row in locked:
        if not isinstance(row, dict) or set(row) - {"name", "package", "version"}:
            raise ValueError("only registry lock entries are supported")
        name, package, version = (row.get(k) for k in ("name", "package", "version"))
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_]+", name)
                or not isinstance(package, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", package)
                or not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version)
                or name in names or package in versions):
            raise ValueError("invalid or duplicate locked package")
        versions[package] = version
        names.append(name)
    for row in declared:
        if (not isinstance(row, dict) or set(row) != {"package", "version"}
                or row.get("package") not in versions or row.get("version") != versions[row["package"]]):
            raise ValueError("declared packages must match exact locked versions")
    config = core.read_yaml(str(root / "dbt_project.yml")) or {}
    package_path = _inside(root, str(config.get("packages-install-path", "dbt_packages")))
    if package_path == root.resolve():
        raise ValueError("package directory cannot be the project root")
    for key in ("model-paths", "seed-paths", "snapshot-paths", "macro-paths", "test-paths", "analysis-paths"):
        for value in core.project_meta(str(root))[key]:
            _inside(root, value)
    return spec, locked, names, package_path


def private_project(project, candidates, destination, deadline):
    """Return (private project path, cache provenance), or raise; no network/writes to inputs."""
    source = Path(project).resolve()
    spec, locked, names, own_cache = _spec(source)
    if any(own_cache.glob("*/dbt_project.yml")):
        raise ValueError("refuse to replace a partially installed package cache")
    donors = []
    for candidate in candidates:
        if time.time() >= deadline:
            raise TimeoutError("package recovery budget exhausted")
        root = Path(candidate).resolve()
        if root == source:
            continue
        try:
            other_spec, other_lock, other_names, cache = _spec(root)
            if spec != other_spec or locked != other_lock or names != other_names:
                continue
            if all((cache / name / "dbt_project.yml").is_file() for name in names):
                donors.append((root, cache))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    if len(donors) != 1:
        raise ValueError("need exactly one matching installed package cache")
    donor, cache = donors[0]
    destination = Path(destination)
    copied, size = 0, 0
    digest = hashlib.sha256()

    def copy_tree(src, dst, skip_cache=None):
        nonlocal copied, size
        for directory, dirs, files in os.walk(src, followlinks=False):
            if time.time() >= deadline:
                raise TimeoutError("package recovery budget exhausted")
            here = Path(directory)
            dirs[:] = sorted(n for n in dirs if n not in core.SKIP_DIRS and not n.startswith(".")
                             and (skip_cache is None or (here / n).resolve() != skip_cache))
            if any((here / n).is_symlink() for n in dirs):
                raise ValueError("symlinked package/project directories are unsupported")
            for name in sorted(files):
                if (name.startswith(".") and name != ".dbtignore") or name.endswith((".pyc", ".duckdb", ".duckdb.wal", ".msgpack")):
                    continue
                path = here / name
                if path.is_symlink() or not path.is_file():
                    raise ValueError("symlinked or special package/project files are unsupported")
                copied += 1
                size += path.stat().st_size
                if copied > 10000 or size > 64 * 1024 * 1024 or time.time() >= deadline:
                    raise ValueError("package recovery size/time limit")
                relative = path.relative_to(src)
                target = dst / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)

    copy_tree(source, destination, own_cache)
    copied_cache = destination / own_cache.relative_to(source)
    # Copy only packages explicitly represented by the matching lock, not other cached projects.
    for name in names:
        if (cache / name).is_symlink():
            raise ValueError("symlinked package roots are unsupported")
        copy_tree(cache / name, copied_cache / name)
    # Fingerprint exactly the copied package files, including their relative identities.
    for path in sorted(copied_cache.rglob('*')):
        if time.time() >= deadline:
            raise TimeoutError("package recovery budget exhausted during fingerprinting")
        if path.is_file():
            digest.update(str(path.relative_to(copied_cache)).encode() + b"\0" + path.read_bytes() + b"\0")
    return str(destination), {"source_project": str(donor), "packages": locked,
                              "package_tree_sha256": digest.hexdigest(), "copied_files": copied,
                              "copied_bytes": size, "network": "none", "original_projects_modified": False}
