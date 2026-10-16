"""Lightweight release checks. No ALNS/VNS search, full enumeration or cache rebuild."""
from __future__ import annotations

import ast
import csv
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
os.environ.setdefault("UAV_RUN_DIR", str(ROOT / "results" / "runs" / ("tests_" + uuid.uuid4().hex[:10])))
os.environ.setdefault("MPLCONFIGDIR", str(Path(os.environ["UAV_RUN_DIR"]) / ".matplotlib"))
os.environ.setdefault("MPLBACKEND", "Agg")
import project_paths as paths
paths.bootstrap()


def setUpModule():
    paths.run_root().mkdir(parents=True, exist_ok=True)


def requires_private_files(*files):
    """Skip archived-data checks when their explicitly declared inputs are absent.

    A public-source checkout does not include frozen workbooks. Skipping is not
    archive reproduction; all original assertions still run when inputs exist.
    """
    missing = [Path(p).relative_to(ROOT).as_posix() for p in files if not Path(p).is_file()]
    return unittest.skipIf(bool(missing), "Private archived inputs not bundled: " + ", ".join(missing))


with (ROOT / "docs/provenance.csv").open(encoding="utf-8-sig", newline="") as _stream:
    FROZEN_FILES = tuple(ROOT / row["release_path"] for row in csv.DictReader(_stream)
                         if row["release_path"].startswith("results/reference/"))

P1_SEGMENTS = ROOT / "results/reference/problem1/segment_preprocessing.xlsx"
P1_BATCHES = ROOT / "results/reference/problem1/problem1_batch_optimization.xlsx"
P2_FORMAL = ROOT / "results/reference/problem2/problem2_Z1_priority_result.xlsx"
P3_FORMAL = ROOT / "results/reference/problem3/problem3_vns_20cycles.xlsx"
Q4_REPORT_INPUTS = tuple(ROOT / "results/reference/problem4" / name for name in (
    "problem4_K2_all.xlsx", "problem4_K3_all.xlsx", "problem4_K2_pareto.xlsx",
    "problem4_K3_pareto.xlsx", "group_subset_results.xlsx", "relay_duplication_analysis.xlsx",
    "problem4_energy_makespan_verification.xlsx", "problem4_final_selection_analysis.xlsx"))


class ReleaseTests(unittest.TestCase):
    @requires_private_files(*FROZEN_FILES)
    def test_all_reference_hashes(self):
        with (ROOT / "docs/provenance.csv").open(encoding="utf-8-sig", newline="") as f:
            frozen = [r for r in csv.DictReader(f) if r["release_path"].startswith("results/reference/")]
        self.assertEqual(len(frozen), 36)
        for row in frozen:
            with self.subTest(file=row["release_path"]):
                self.assertEqual(paths.sha256(ROOT / row["release_path"]), row["sha256"])

    @requires_private_files(P1_SEGMENTS, ROOT / "results/reference/submission/结果提交.xlsx")
    def test_paths_independent_of_cwd(self):
        old = Path.cwd()
        expected = paths.input_file("problem1", "segment_preprocessing.xlsx")
        try:
            os.chdir(ROOT.parent)
            self.assertEqual(paths.input_file("problem1", "segment_preprocessing.xlsx"), expected)
            self.assertTrue(expected.is_file())
            self.assertEqual(paths.submission_file(), ROOT / "results/reference/submission/结果提交.xlsx")
        finally:
            os.chdir(old)

    def test_input_override_is_explicit(self):
        prepared = paths.ensure_output_path(paths.output_file("prepared/problem1", "segment_preprocessing.xlsx"))
        prepared.write_bytes(b"path resolution fixture only")
        with patch.dict(os.environ, {"UAV_INPUT_DIR": str(prepared.parents[1])}):
            self.assertEqual(paths.input_file("problem1", prepared.name), prepared)

    def test_no_output_escape(self):
        for destination in (ROOT / "results/reference/problem1/forbidden.xlsx", ROOT.parent / "forbidden.xlsx", ROOT / "data/raw/forbidden.xlsx"):
            with self.subTest(path=destination), self.assertRaises(PermissionError):
                paths.check_output_path(destination)
        with self.assertRaises(ValueError):
            paths.output_file("problem1", "../../outside.xlsx")
        with patch.dict(os.environ, {"UAV_RUN_DIR": str(ROOT / "results/reference")}):
            with self.assertRaises(PermissionError):
                paths.run_root()

    @requires_private_files(P1_SEGMENTS)
    def test_reference_write_guard(self):
        frozen = paths.input_file("problem1", "segment_preprocessing.xlsx")
        before = paths.sha256(frozen)
        with self.assertRaises(PermissionError):
            frozen.open("wb")
        with self.assertRaises(PermissionError):
            frozen.chmod(0o666)
        self.assertEqual(paths.sha256(frozen), before)

    @requires_private_files(ROOT / "results/reference/problem4/problem4_paper_tables.xlsx")
    def test_append_input_is_copied(self):
        source = paths.input_file("problem4", "problem4_paper_tables.xlsx")
        writable = paths.prepare_writable_input("problem4", source.name)
        self.assertNotEqual(source, writable)
        self.assertEqual(paths.sha256(source), paths.sha256(writable))
        self.assertTrue(writable.is_relative_to(paths.run_root()))

    def test_missing_input_message(self):
        with self.assertRaisesRegex(paths.MissingInputError, "--raw-dir"):
            paths.require_file(ROOT / "data/raw/intentionally_absent_test_input.xlsx")

    def test_unknown_pickle_rejected(self):
        unknown = paths.ensure_output_path(paths.output_file("security", "untrusted.pkl.gz"))
        unknown.write_bytes(b"not a pickle; must reject before deserialization")
        with patch.dict(os.environ, {"UAV_CACHE_SHA256": ""}), self.assertRaisesRegex(ValueError, "Untrusted pickle"):
            paths.assert_trusted_cache(unknown)

    def test_syntax_and_imports_from_foreign_cwd(self):
        sources = [p for group in ("src", "experiments", "scripts") for p in sorted((ROOT / group).glob("*/*.py"))]
        self.assertEqual(len(sources), 41)
        for p in sources:
            ast.parse(p.read_text(encoding="utf-8-sig"), filename=str(p), feature_version=9)
        foreign = paths.run_root() / "foreign_cwd"
        foreign.mkdir(exist_ok=True)
        code = "import sys,importlib;sys.path.insert(0,sys.argv[1]);import project_paths;p=project_paths;p.bootstrap();" \
               "[importlib.import_module(n) for n in sys.argv[2:]];print('IMPORTED',len(sys.argv)-2)"
        names = [p.stem for p in sources]
        if importlib.util.find_spec("ortools") is None:
            names.remove("problem2_cpsat_resource_refinement")
        completed = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT), *names], cwd=foreign,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("IMPORTED %d" % len(names), completed.stdout)
        self.assertEqual(list(foreign.iterdir()), [], "Import unexpectedly wrote files into CWD")

    def test_formal_q2_parameters(self):
        import problem2_final_run_and_visualization as formal
        spec = json.loads((ROOT / "docs/formal_versions.json").read_text(encoding="utf-8-sig"))["problem2_parameters"]
        for name, value in spec.items():
            actual = getattr(formal, name)
            self.assertEqual(list(actual) if isinstance(actual, tuple) else actual, value, name)

    @requires_private_files(P1_BATCHES, P2_FORMAL, P3_FORMAL)
    def test_q1_q2_q3_archived_structure(self):
        import pandas as pd
        q1 = paths.input_file("problem1", "problem1_batch_optimization.xlsx")
        sorties = pd.read_excel(q1, "selected_batches")
        boxes = pd.read_excel(q1, "box_assignment")
        self.assertEqual(len(sorties), 18)
        self.assertEqual(len(boxes), 80)
        self.assertTrue(boxes.box_id.is_unique)
        self.assertEqual(sorties.box_count.sum(), 80)
        q2 = paths.input_file("problem2", "problem2_Z1_priority_result.xlsx")
        summary = pd.read_excel(q2, "Summary").iloc[0]
        self.assertEqual((summary.Max_Iterations, summary.Random_Seed), (1000, 20260927))
        self.assertEqual([summary["Lambda%d" % i] for i in range(1, 5)], [.55, .15, .15, .15])
        self.assertEqual(len(pd.read_excel(q2, "Sorties")), 20)
        q3 = pd.read_excel(paths.input_file("problem3", "problem3_vns_20cycles.xlsx"), "Transport_Sorties")
        self.assertEqual(len(q3), 20)
        self.assertTrue(q3.Sortie_ID.astype(str).str.fullmatch(r"R\d{3}").all())

    @requires_private_files(P1_BATCHES)
    def test_gurobi_q1_model_construction_only(self):
        import pandas as pd
        import gurobipy as gp
        import problem1_batch_optimization as q1
        frozen = paths.input_file("problem1", "problem1_batch_optimization.xlsx")
        boxes = pd.read_excel(frozen, "box_assignment")
        selected = pd.read_excel(frozen, "selected_batches")
        selected["box_ids_tuple"] = selected.box_ids.str.split(";").map(tuple)
        try:
            model, variables, *_ = q1.build_set_partitioning_model(boxes, selected)
        except gp.GurobiError as error:
            self.skipTest("Gurobi environment/license: " + str(error))
        try:
            model.update()
            self.assertEqual((model.NumVars, model.NumConstrs), (18, 80))
            self.assertEqual(model.SolCount, 0)
        finally:
            model.dispose()

    @requires_private_files(*Q4_REPORT_INPUTS, P3_FORMAL)
    def test_q4_final_tables_in_memory(self):
        # The archived validator checks sys.modules for Gurobi. Isolate it from
        # the Q1 construction test; retain all 25 original expected PASS checks.
        code = """
import sys
sys.path.insert(0, sys.argv[1])
import project_paths
project_paths.bootstrap()
import problem4_final_paper_results as q4
hashes = {p: q4.file_hash(p) for p in q4.SOURCE_FILES}
sources = q4.read_sources()
groups, summary, _ = q4.calculate_execution(sources)
tables = q4.build_tables(sources, groups, summary)
validation = q4.validate(sources, groups, summary, tables, hashes)
assert len(validation) == 25
assert validation['结果'].eq('PASS').all(), validation.to_string()
assert all(q4.file_hash(p) == digest for p, digest in hashes.items())
print('25 PASS')
"""
        completed = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT)], cwd=paths.run_root(),
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("25 PASS", completed.stdout)

    @unittest.skipUnless(os.environ.get("UAV_RAW_DIR"), "External raw data intentionally not bundled; set UAV_RAW_DIR")
    def test_raw_data_and_q4_resource_entry(self):
        import pandas as pd
        import rasterio
        import problem4_group_scheduler as q4
        for name in ("调度中心与服务区.xlsx", "物资需求与配送时限.xlsx", "运输无人机数据.xlsx", "中继无人机数据.xlsx", "通信链路参数.xlsx"):
            with pd.ExcelFile(paths.require_file(paths.raw_file(name))) as workbook:
                self.assertGreater(len(workbook.sheet_names), 0)
        with rasterio.open(paths.require_file(paths.dem_file())) as dem:
            self.assertGreater(dem.width * dem.height, 0)
            self.assertIsNotNone(dem.crs)
        context = q4.load_context()
        self.assertEqual(len(context.communication_intervals), 215)
        archived = pd.read_excel(paths.input_file("problem4", "group_subset_results.xlsx"), "subset_results").set_index("mask")
        for mask in (1, 32, 126):
            current = q4.account_subset(mask, context)
            row = archived.loc[mask]
            for kind in "ABC":
                self.assertEqual(current.transport_uav_requirement[kind], row["N_U_" + kind])
                self.assertEqual(current.battery_requirement[kind], row["N_B_" + kind])
            self.assertEqual(current.relay_uav_requirement["R"], row.N_R)
            self.assertEqual(current.relay_energy_requirement["REC"], row.N_E)

    @unittest.skipUnless(os.environ.get("UAV_RAW_DIR"), "Frozen cache validation requires explicit external raw data")
    def test_verified_cache_class_identity(self):
        import problem3_segment_communication_cache as cache
        self.assertEqual(len(paths.verify_frozen_cache_inputs()), 11)
        loaded = cache.load_cache(paths.cache_input_file())
        self.assertIsInstance(loaded, cache.CommunicationGeometryCache)


if __name__ == "__main__":
    unittest.main(verbosity=2)
