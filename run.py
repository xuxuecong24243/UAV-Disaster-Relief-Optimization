"""Unified launcher. Does not select a solver or run an experiment without a target."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import runpy
import sys
import traceback

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent


def targets():
    return {p.stem: p for group in ("src", "experiments", "scripts")
            for p in sorted((ROOT / group).glob("*/*.py")) if not p.name.startswith("_")}


def main():
    parser = argparse.ArgumentParser(description="Run one archived workflow with isolated outputs. Large solvers are not smoke tests.")
    parser.add_argument("target", nargs="?", help="module filename without .py; use --list")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--raw-dir", type=Path)
    parser.add_argument("--input-dir", type=Path, help="previous run/processed root containing problemN subdirectories")
    parser.add_argument("--run-dir", type=Path, help="NEW directory below results/runs; default timestamp + random ID")
    parser.add_argument("--template", type=Path)
    parser.add_argument("--cache-file", type=Path)
    parser.add_argument("--cache-sha256", help="explicit trust only for a cache from a known producer")
    args, forwarded = parser.parse_known_args()
    choices = targets()
    if args.list:
        for name, path in choices.items():
            print("%-45s %s" % (name, path.relative_to(ROOT).as_posix()))
        return 0
    if args.target not in choices:
        parser.error("Choose a target from --list.")
    for option, variable in (("raw_dir", "UAV_RAW_DIR"), ("input_dir", "UAV_INPUT_DIR"),
                             ("run_dir", "UAV_RUN_DIR"), ("template", "UAV_TEMPLATE"),
                             ("cache_file", "UAV_CACHE_FILE")):
        value = getattr(args, option)
        if value is not None:
            os.environ[variable] = str(value.expanduser().resolve())
    if args.cache_sha256:
        os.environ["UAV_CACHE_SHA256"] = args.cache_sha256
    import project_paths as paths
    run_dir = paths.run_root()
    if run_dir.exists():
        parser.error("Run directory already exists; select a new directory to avoid overwriting a previous experiment.")
    run_dir.mkdir(parents=True)
    os.environ["UAV_RUN_DIR"] = str(run_dir)
    os.environ["MPLCONFIGDIR"] = str(run_dir / ".matplotlib")
    os.environ.setdefault("MPLBACKEND", "Agg")
    (run_dir / ".tmp").mkdir()
    os.environ["TMP"] = os.environ["TEMP"] = str(run_dir / ".tmp")
    paths.bootstrap()
    started = datetime.now(timezone.utc).isoformat()
    manifest = {"started_utc": started, "target": args.target, "source": choices[args.target].relative_to(ROOT).as_posix(),
                "source_sha256": paths.sha256(choices[args.target]), "argv": sys.argv,
                "python": sys.version, "status": "running", "inputs": [], "outputs": []}
    manifest["code_sha256"] = {p.relative_to(ROOT).as_posix(): paths.sha256(p)
                               for p in [*choices.values(), ROOT / "run.py", ROOT / "project_paths.py"]}
    for package in ("numpy", "pandas", "matplotlib", "openpyxl", "rasterio", "pyproj", "gurobipy", "networkx", "ortools"):
        try:
            manifest.setdefault("packages", {})[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            manifest.setdefault("packages", {})[package] = None
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Run directory:", run_dir, flush=True)
    sys.argv = [str(choices[args.target])] + (forwarded[1:] if forwarded[:1] == ["--"] else forwarded)
    exit_code = 0
    try:
        module = importlib.import_module(args.target)
        special = {"problem3_baseline": "run_baseline", "problem3_communication": "run_independent_tests",
                   "problem3_core": "run_step3", "problem3_vns": "run_vns",
                   "problem4_group_scheduler": "run_phase456_validation"}
        entry = getattr(module, special.get(args.target, "main"), None)
        if entry is None:
            runpy.run_path(str(choices[args.target]), run_name="__main__")
        else:
            entry()
        manifest["status"] = "completed"
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)
        manifest["status"] = "completed" if exit_code == 0 else "failed"
        manifest["error"] = str(exc)
    except Exception as exc:
        exit_code = 1
        manifest["status"] = "failed"
        manifest["error"] = "%s: %s" % (type(exc).__name__, exc)
        traceback.print_exc()
    finally:
        for filename in paths.recorded_inputs():
            p = Path(filename)
            if p.is_file():
                manifest["inputs"].append({"path": str(p), "sha256": paths.sha256(p)})
        for p in sorted(run_dir.rglob("*")):
            if p.is_file() and p != manifest_path and not any(x.startswith(".") for x in p.relative_to(run_dir).parts):
                manifest["outputs"].append({"path": p.relative_to(run_dir).as_posix(), "sha256": paths.sha256(p)})
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["exit_code"] = exit_code
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
