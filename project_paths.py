"""Release-only paths and IO guards; mathematical modules retain their original names.

Environment options are set by run.py BEFORE importing a problem module. Merely
importing this module creates no directories, reads no workbooks and runs no solver.
"""
from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "results" / "runs"
REFERENCE = ROOT / "results" / "reference"
_RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
_INPUTS = set()
_GENERATED_CACHES = {}
_GUARD_INSTALLED = False
CACHE_NAME = "problem3_communication_geometry_cache.pkl.gz"
CACHE_SHA256 = "c454d192770b5bc93ca2bd94427d807d2614d8e02feb7c3e3fc321e24354a8b2"


class MissingInputError(FileNotFoundError):
    """A declared input is unavailable; no implicit original-project fallback."""


def _under(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _relative(value):
    p = Path(value)
    if p.is_absolute() or p.drive or ".." in p.parts:
        raise ValueError("Expected a relative path without '..': %s" % value)
    return p


def bootstrap():
    """Retain legacy import identities (including pickle class module names)."""
    sys.dont_write_bytecode = True
    folders = [ROOT]
    for group in ("src", "experiments", "scripts"):
        folders.extend(ROOT / group / ("problem%d" % i) for i in range(1, 5))
    folders.append(ROOT / "scripts" / "utilities")
    for folder in reversed(folders):
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    install_readonly_guard()


def raw_root():
    return Path(os.environ.get("UAV_RAW_DIR", str(ROOT / "data" / "raw"))).expanduser().resolve()


def _choose(paths):
    return next((p for p in paths if p.is_file()), paths[0])


def raw_file(filename):
    name = _relative(filename)
    base = raw_root()
    return _choose([base / name, base / "无人机应急物资运输基础数据" / name])


def dem_file():
    name = "镇龙乡及周边30米DEM.tif"
    base = raw_root()
    return _choose([base / "dem" / name, base / name,
                    base / "镇龙乡地理空间数据" / "镇龙乡及周边地理数据" / "数字高程模型数据（DEM）" / name])


def input_file(problem, filename):
    """Explicit intermediate override, processed data, frozen result, experiment archive."""
    problem, filename = _relative(problem), _relative(filename)
    paths = []
    if os.environ.get("UAV_INPUT_DIR"):
        paths.append(Path(os.environ["UAV_INPUT_DIR"]).resolve() / problem / filename)
    paths.extend([ROOT / "data" / "processed" / problem / filename,
                  REFERENCE / problem / filename,
                  ROOT / "experiments" / problem / "results" / filename])
    return _choose(paths)


def require_file(path):
    p = Path(path).resolve()
    if not p.is_file():
        raise MissingInputError(
            "Missing input: %s\nSupply original data with --raw-dir (UAV_RAW_DIR), "
            "intermediates with --input-dir (UAV_INPUT_DIR), or an explicit --template. "
            "No original competition directory is searched automatically." % p)
    record_input(p)
    return p


def record_input(path):
    _INPUTS.add(str(Path(path).resolve()))
    return Path(path)


def recorded_inputs():
    return sorted(_INPUTS)


def run_root():
    path = Path(os.environ.get("UAV_RUN_DIR", str(RUNS / _RUN_ID))).expanduser().resolve()
    if not _under(path, RUNS) or path == RUNS.resolve():
        raise PermissionError("Run directory must be a child of results/runs: %s" % path)
    return path


def output_dir(problem):
    return run_root() / _relative(problem)


def check_output_path(path):
    p = Path(path).resolve()
    if not _under(p, run_root()) or p == run_root():
        raise PermissionError("Output must stay in this independent run directory: %s" % p)
    return p


def ensure_output_path(path):
    p = check_output_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


assert_writable_output = ensure_output_path


def output_file(problem, filename):
    return check_output_path(output_dir(problem) / _relative(filename))


def submission_file():
    """Explicit mapping to the verified composite workbook, never rename a missing original."""
    return input_file("submission", "结果提交.xlsx")


def template_file():
    return Path(os.environ.get("UAV_TEMPLATE", str(ROOT / "data" / "raw" / "结果提交模板.xlsx"))).expanduser().resolve()


def review_file(relative):
    return ROOT / "review_hold" / _relative(relative)


def prepare_writable_input(problem, filename):
    target = ensure_output_path(output_file(problem, filename))
    if not target.exists():
        source = require_file(input_file(problem, filename))
        shutil.copyfile(source, target)
        target.chmod(target.stat().st_mode | stat.S_IWRITE)
    return target


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def cache_input_file():
    return Path(os.environ.get("UAV_CACHE_FILE", str(review_file("cache/problem3/" + CACHE_NAME)))).expanduser().resolve()


def cache_manifest_file():
    return review_file("cache/problem3/problem3_communication_geometry_cache_manifest.xlsx")


def verify_frozen_cache_inputs():
    """Additional IO provenance gate; does not change the algorithm's cache signature."""
    with (ROOT / "docs" / "dependencies.csv").open(encoding="utf-8-sig", newline="") as f:
        deps = {row["dependency_id"]: row for row in csv.DictReader(f)}
    checked = {}
    for key in ("RAW_NODES", "RAW_BOXES", "RAW_UAV", "RAW_RELAY", "RAW_LINK", "DEM",
                "P1_SEGMENTS", "P2_Z1", "P3_ELITE", "P3_TRANSPORT_CHECK", "P3_COVERAGE"):
        row = deps[key]
        if key == "DEM":
            p = dem_file()
        elif key.startswith("RAW_"):
            p = raw_file(Path(row["source_path"]).name)
        else:
            rel = Path(row["candidate_path"]).relative_to("results/reference")
            p = input_file(rel.parts[0], Path(*rel.parts[1:]))
        p = require_file(p)
        digest = sha256(p)
        if digest != row["sha256"]:
            raise ValueError("Frozen cache input mismatch (%s): %s. Rebuild a separate cache; do not reuse the archive." % (key, p))
        checked[key] = digest
    return checked


def assert_trusted_cache(path):
    """Validate bytes before unpickling. A hash of unknown provenance is not an endorsement."""
    path = require_file(path)
    digest = sha256(path)
    if _GENERATED_CACHES.get(str(path)) == digest:
        return path
    if digest == CACHE_SHA256:
        verify_frozen_cache_inputs()
        return path
    explicit = os.environ.get("UAV_CACHE_SHA256", "").lower()
    if explicit and digest == explicit and len(explicit) == 64:
        return path
    raise ValueError("Untrusted pickle cache: %s. Use the frozen verified cache, or explicitly supply "
                     "--cache-sha256 only for a cache whose producer you trust." % path)


def register_generated_cache(path):
    path = check_output_path(path)
    _GENERATED_CACHES[str(path)] = sha256(path)


def prepare_cache(target=None):
    target = check_output_path(target or output_file("problem3", CACHE_NAME))
    if target.is_file():
        assert_trusted_cache(target)
        return target
    source = cache_input_file()
    if source.is_file():
        assert_trusted_cache(source)
        ensure_output_path(target)
        shutil.copyfile(source, target)
    elif os.environ.get("UAV_CACHE_FILE"):
        require_file(source)
    return target


def install_readonly_guard():
    """Protect frozen/input trees even when a caller bypasses path helper functions."""
    global _GUARD_INSTALLED
    if _GUARD_INSTALLED:
        return
    _GUARD_INSTALLED = True

    def protected(value):
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        p = Path(os.fsdecode(value)).resolve()
        roots = [REFERENCE, ROOT / "data" / "raw", ROOT / "data" / "processed", ROOT / "review_hold", raw_root()]
        roots.extend(ROOT / "experiments" / ("problem%d" % i) / "results" for i in range(1, 5))
        if os.environ.get("UAV_INPUT_DIR"):
            roots.append(Path(os.environ["UAV_INPUT_DIR"]).resolve())
        if os.environ.get("UAV_TEMPLATE"):
            if p == Path(os.environ["UAV_TEMPLATE"]).resolve():
                return True
        return any(_under(p, root) for root in roots)

    def audit(event, args):
        write_paths = []
        if event == "open":
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing:
                write_paths = [args[0]]
            elif isinstance(args[0], (str, bytes, os.PathLike)) and protected(args[0]):
                _INPUTS.add(str(Path(os.fsdecode(args[0])).resolve()))
        elif event in ("os.remove", "os.rmdir", "os.chmod", "os.utime", "os.mkdir", "os.truncate"):
            write_paths = [args[0]]
        elif event in ("os.rename", "os.link", "os.symlink"):
            write_paths = list(args[:2])
        for p in write_paths:
            if protected(p):
                raise PermissionError("Read-only input/reference path: %s" % p)

    sys.addaudithook(audit)
