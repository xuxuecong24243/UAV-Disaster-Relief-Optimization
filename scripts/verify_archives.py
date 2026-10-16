"""Read-only frozen-result reconciliation. No search, geometry rebuild or pickle.

Use --raw-dir for the existing Q4 resource-accounting entry (126 subsets).
New JSON evidence is written only into a new directory under results/runs.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
from time import perf_counter
import uuid

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "results/reference"
RESOURCE = ["R_U_A", "R_U_B", "R_U_C", "R_B_A", "R_B_B", "R_B_C", "R_R", "R_E"]
BALANCE = ["B_Q", "B_M", "B_V", "B_N_sortie", "B_T_transport", "B_T_comm"]


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1048576), b""):
            h.update(block)
    return h.hexdigest()


def close(a, b, tol=1e-7):
    return math.isclose(float(a), float(b), rel_tol=0, abs_tol=tol)


def ids(value):
    return [s.strip() for s in str(value or "").split(";") if s.strip()]


def partitions(n, k):
    """Independent exhaustive label assignments, canonicalized to unlabeled masks."""
    return {tuple(sorted(sum(1 << i for i, label in enumerate(labels) if label == g) for g in range(k)))
            for labels in itertools.product(range(k), repeat=n) if len(set(labels)) == k}


def pareto(rows):
    vectors = [[float(row[c]) for c in RESOURCE + BALANCE] for row in rows]
    return [not any(i != j and all(a <= b + 1e-10 for a, b in zip(left, right))
                    and any(a < b - 1e-10 for a, b in zip(left, right))
                    for j, left in enumerate(vectors)) for i, right in enumerate(vectors)]


class Audit:
    def __init__(self):
        self.checks, self.sources, self.cache, self.metrics = [], {}, {}, {}

    def check(self, name, passed, detail=None):
        self.checks.append(dict(id=name, status="PASS" if passed else "FAIL", detail=detail))

    def table(self, name, sheet):
        from openpyxl import load_workbook
        if (name, sheet) not in self.cache:
            path = REF / name
            self.sources["results/reference/" + name] = digest(path)
            with path.open("rb") as f:
                w = load_workbook(f, read_only=True, data_only=True)
                try:
                    rows = iter(w[sheet].values)
                    head = next(rows)
                    self.cache[name, sheet] = [{k: v for k, v in zip(head, row) if k is not None}
                                              for row in rows if row[0] is not None]
                finally:
                    w.close()
        return self.cache[name, sheet]

    def intervals(self, name, rows, key, start, end):
        groups = defaultdict(list)
        for row in rows:
            groups[row[key]].append((float(row[start]), float(row[end])))
        bad = [key for key, intervals in groups.items()
               if any(not math.isfinite(a + b) or b < a for a, b in intervals)
               or any(a[1] > b[0] + 1e-6 for a, b in zip(sorted(intervals), sorted(intervals)[1:]))]
        self.check(name, not bad, dict(resource_count=len(groups), conflicts=bad, tolerance_s=1e-6))
        return len(groups)

    def q1_q2(self):
        q1 = "problem1/problem1_batch_optimization.xlsx"
        batches, boxes = self.table(q1, "selected_batches"), self.table(q1, "box_assignment")
        summary = self.table(q1, "optimization_summary")[0]
        expected = {r["box_id"]: r for r in boxes}
        assigned = [(box, r["sortie_id"]) for r in batches for box in ids(r["box_ids"])]
        self.check("q1_18_batches_80_boxes_once", len(batches) == 18 and len(boxes) == len(expected) == 80
                   and Counter(b for b, _ in assigned) == Counter(expected.keys()))
        self.check("q1_assignment_mapping", all(expected[b]["sortie_id"] == sortie for b, sortie in assigned)
                   and all(len(ids(r["box_ids"])) == r["box_count"] for r in batches))
        energy = sum(r["energy_kwh"] for r in batches)
        duration = sum(r["operation_time_s"] for r in batches)
        self.check("q1_energy_and_cumulative_time", close(energy, summary["minimum_energy_kwh"], 1e-9)
                   and close(duration, summary["minimum_operation_time_s"]))
        self.check("q1_row_components", all(close(r["energy_kwh"], r["outbound_energy_kwh"] + r["return_energy_kwh"], 1e-9)
                   and close(r["operation_time_s"], sum(r[k] for k in ("flight_time_s", "prep_time_s", "loading_time_s", "handover_time_s"))) for r in batches))
        self.metrics["q1"] = dict(sorties=len(batches), boxes=len(boxes), energy_kwh=energy, cumulative_operation_s=duration)
        q2 = "problem2/problem2_Z1_priority_result.xlsx"
        sorties, delivery = self.table(q2, "Sorties"), self.table(q2, "Box_Delivery")
        summary, battery = self.table(q2, "Summary")[0], self.table(q2, "Battery_Schedule")
        by_sortie = {r["Sortie_ID"]: r for r in sorties}
        self.check("q2_20_sorties_80_boxes_once", len(sorties) == len(by_sortie) == 20
                   and Counter(b for r in sorties for b in ids(r["Box_IDs"])) == Counter(expected.keys())
                   and Counter(r["Box_ID"] for r in delivery) == Counter(expected.keys()))
        self.check("q2_delivery_mapping_and_input_fields", all(r["Box_ID"] in ids(by_sortie[r["Sortie_ID"]]["Box_IDs"])
                   and r["Expected_Time_s"] == expected[r["Box_ID"]]["expected_arrival_time_s"]
                   and r["Emergency_Coefficient"] == expected[r["Box_ID"]]["emergency_priority"] for r in delivery))
        delays = [max(0, r["Delivery_Time_s"] - r["Expected_Time_s"]) for r in delivery]
        self.check("q2_box_delays_recomputed", all(close(d, r["Delay_s"]) for d, r in zip(delays, delivery)))
        hard = []
        for r in delivery:
            b = expected[r["Box_ID"]]
            deadlines = [b["expected_arrival_time_s"]] if b["material_type"] == "医疗物资" else []
            if b["is_first_batch"]:
                deadlines.append(b["first_batch_deadline_s"])
            hard.extend(r["Box_ID"] for deadline in deadlines if r["Delivery_Time_s"] > deadline + 1e-7)
        self.check("q2_hard_deadlines_from_q1_box_metadata", not hard, hard)
        z1 = sum(d * r["Emergency_Coefficient"] for d, r in zip(delays, delivery))
        z2, z3 = max(r["Return_Time_s"] for r in sorties), sum(r["Energy_kWh"] for r in sorties)
        self.check("q2_objective_reaggregation", close(z1, summary["Z1"]) and close(z2, summary["Z2"])
                   and close(z3, summary["Z3"], 1e-9) and len(sorties) == summary["Z4"])
        self.check("q2_battery_schedule_matches_sorties", len(battery) == len(sorties)
                   and all(r["Battery_ID"] == by_sortie[r["Sortie_ID"]]["Battery_ID"]
                           and close(r["Task_Start_s"], by_sortie[r["Sortie_ID"]]["Start_Time_s"])
                           and close(r["Charge_End_s"], by_sortie[r["Sortie_ID"]]["Return_Time_s"] + by_sortie[r["Sortie_ID"]]["Recharge_Time_s"]) for r in battery))
        nu = self.intervals("q2_uav_intervals", sorties, "UAV_ID", "Start_Time_s", "Return_Time_s")
        nb = self.intervals("q2_battery_intervals", battery, "Battery_ID", "Task_Start_s", "Charge_End_s")
        checks = self.table(q2, "Feasibility")
        self.check("q2_archived_16_pass_flags", len(checks) == 16 and all(r["结果"] == "PASS" for r in checks), "Archived flags, not rerunning every feasibility function")
        self.metrics["q2"] = dict(sorties=len(sorties), boxes=len(delivery), weighted_delay_s=z1, makespan_s=z2, energy_kwh=z3, uav=nu, batteries=nb)
        return expected

    def q3(self, expected):
        name = "problem3/problem3_vns_20cycles.xlsx"
        transport, relay = self.table(name, "Transport_Sorties"), self.table(name, "Relay_Sorties")
        summary = {r["Metric"]: r["Value"] for r in self.table(name, "Summary")}
        self.check("q3_clean_20_transport_80_boxes", len(transport) == 20
                   and all(re.fullmatch(r"R\d{3}", r["Sortie_ID"]) for r in transport)
                   and Counter(b for r in transport for b in ids(r["Box_IDs"])) == Counter(expected.keys()))
        et, er = sum(r["Energy_kWh"] for r in transport), sum(r["Total_Energy_kWh"] for r in relay)
        z2 = max([r["Return_Time_s"] for r in transport] + [r["Return_End_s"] for r in relay])
        self.check("q3_joint_objective_reaggregation", close(z2, summary["Final_Z2_s"])
                   and close(et + er, summary["Final_Z3_kWh"], 1e-9) and len(transport) + len(relay) == summary["Final_Z4"])
        self.check("q3_relay_energy_components", all(close(r["Total_Energy_kWh"], sum(r[k] for k in
                   ("Outbound_Energy_kWh", "Setup_Energy_kWh", "Service_Energy_kWh", "Return_Energy_kWh")), 1e-9) for r in relay))
        counts = [self.intervals("q3_transport_uav_intervals", transport, "UAV_ID", "Start_Time_s", "Return_Time_s"),
                  self.intervals("q3_transport_battery_intervals", transport, "Battery_ID", "Start_Time_s", "Charge_End_s"),
                  self.intervals("q3_relay_uav_intervals", relay, "Relay_UAV_ID", "Prepare_Start_s", "Turnaround_End_s"),
                  self.intervals("q3_relay_component_intervals", relay, "Energy_Component_ID", "Prepare_Start_s", "Charge_End_s")]
        self.check("q3_physical_resource_counts", counts == [8, 14, 2, 3], counts)
        checks = self.table(name, "Final_Feasibility_Check")
        self.check("q3_archived_13_pass_flags", len(checks) == 13 and all(r["Result"] == "PASS" for r in checks),
                   "Archive only; radio geometry not rerun. First flag does not prove Q2 route identity.")
        comm = self.table("submission/结果提交.xlsx", "Q3_通信保障")
        relayed = [r for r in comm if r["保障方式"] == "中继通信"]
        by_relay, by_transport = {r["Sortie_ID"]: r for r in relay}, {r["Sortie_ID"]: r for r in transport}
        self.check("q3_submitted_communication_schema", len(comm) == 215 and len(relayed) == 104
                   and {r["运输架次编号"] for r in comm} == set(by_transport)
                   and all(r["结束时刻（s）"] >= r["开始时刻（s）"] and r["保障方式"] in ("G01直连", "中继通信") for r in comm))
        errors = []
        for sortie, row in by_transport.items():
            intervals = sorted([r for r in comm if r["运输架次编号"] == sortie], key=lambda r: r["开始时刻（s）"])
            errors.extend([abs(intervals[0]["开始时刻（s）"] - row["Takeoff_Time_s"]), abs(intervals[-1]["结束时刻（s）"] - row["Return_Time_s"])])
            errors.extend(abs(b["开始时刻（s）"] - a["结束时刻（s）"]) for a, b in zip(intervals, intervals[1:]))
        self.check("q3_submitted_intervals_cover_flight_windows", max(errors) <= 1e-7, dict(max_boundary_difference_s=max(errors), geometry_recomputed=False))
        self.check("q3_relay_intervals_match_service_windows", all(r["中继架次编号"] in by_relay
                   and r["运输架次编号"] in ids(by_relay[r["中继架次编号"]]["Covered_Transport_Sorties"])
                   and r["开始时刻（s）"] >= by_relay[r["中继架次编号"]]["Service_Start_s"] - 1e-7
                   and r["结束时刻（s）"] <= by_relay[r["中继架次编号"]]["Service_End_s"] + 1e-7 for r in relayed))
        submitted = [r for r in self.table("submission/结果提交.xlsx", "Q3_中继架次") if str(r["中继架次编号"]).startswith("RS")]
        mapping = [("开始时刻（s）", "Prepare_Start_s"), ("建链完成时刻（s）", "Setup_End_s"), ("服务结束时刻（s）", "Service_End_s"),
                   ("返回O01时刻（s）", "Return_End_s"), ("架次能耗（kWh）", "Total_Energy_kWh")]
        self.check("q3_submission_relay_rows_match", len(submitted) == len(relay) == 3 and all(r["中继架次编号"] in by_relay
                   and r["中继无人机编号"] == by_relay[r["中继架次编号"]]["Relay_UAV_ID"]
                   and r["能源组件编号"] == by_relay[r["中继架次编号"]]["Energy_Component_ID"]
                   and all(close(r[a], by_relay[r["中继架次编号"]][b]) for a, b in mapping) for r in submitted))
        self.metrics["q3"] = dict(transport_sorties=len(transport), relay_sorties=len(relay), transport_energy_kwh=et,
            relay_energy_kwh=er, joint_energy_kwh=et+er, joint_makespan_s=z2, resources=counts,
            archived_weighted_delay_s=summary["Final_Z1"], archived_uncovered_communication_s=summary["Final_Uncovered_Communication_Duration_s"],
            sample_step_s=1.0, detected_transition_tolerance_s=0.01, geometry_recomputed=False)
        return transport

    def q4(self, transport):
        components = [{"S%03d" % i} for i in range(1, 16)]
        for r in transport:
            zones = set(re.findall(r"S\d{3}", r["Route"]))
            merged = set().union(*(c for c in components if c & zones))
            components = [c for c in components if not c & zones] + [merged]
        components.sort(key=min)
        self.check("q4_seven_components_from_routes", len(components) == 7, [sorted(c) for c in components])
        subsets = self.table("problem4/group_subset_results.xlsx", "subset_results")
        self.check("q4_126_subset_masks_complete", len(subsets) == 126 and {r["mask"] for r in subsets} == set(range(1, 127)))
        self.check("q4_subset_zone_membership", all(set(ids(r["service_zones"])) == set().union(
            *(c for i, c in enumerate(components) if int(r["mask"]) & (1 << i))) for r in subsets))
        results = {}
        for k in (2, 3):
            rows = self.table("problem4/problem4_K%d_all.xlsx" % k, "K%d_all" % k)
            generated = partitions(len(components), k)
            archived = {tuple(sorted(int(r["group%d_mask" % i]) for i in range(1, k+1))) for r in rows}
            self.check("q4_k%d_partition_completeness" % k, len(rows) == len(archived) == len(generated) and archived == generated,
                       dict(independent_count=len(generated), archived_rows=len(rows)))
            flags = pareto(rows)
            nondominated = {r["partition_id"] for r, flag in zip(rows, flags) if flag}
            separate = self.table("problem4/problem4_K%d_pareto.xlsx" % k, "Pareto_K%d" % k)
            self.check("q4_k%d_pareto_independent_recalculation" % k, all(f == bool(r["is_pareto"]) for r, f in zip(rows, flags))
                       and nondominated == {r["partition_id"] for r in separate}, dict(dimensions=RESOURCE+BALANCE, tolerance=1e-10, count=len(nondominated)))
            self.check("q4_k%d_resource_column_sums" % k, all(r[total] == sum(r["group%d_%s" % (i, sub)] for i in range(1, k+1))
                for r in rows for total, sub in zip(RESOURCE, ["N_U_A", "N_U_B", "N_U_C", "N_B_A", "N_B_B", "N_B_C", "N_R", "N_E"])))
            errors = []
            for r in rows:
                for column in BALANCE:
                    values = [r["group%d_%s" % (i, column[2:])] for i in range(1, k+1)]
                    mean = sum(values)/k
                    value = 0 if abs(mean) <= 1e-10 else (max(values)-min(values))/mean
                    errors.append(abs(value-r[column]))
            self.check("q4_k%d_balance_recalculation" % k, max(errors) <= 1e-10, dict(max_error=max(errors)))
            results["k%d" % k] = dict(partitions=len(rows), pareto=len(nondominated))
        self.metrics["q4"] = results

    def resource_entry(self, raw_dir, output):
        os.environ.update(UAV_RAW_DIR=str(raw_dir), UAV_RUN_DIR=str(output), MPLCONFIGDIR=str(output/".matplotlib"), MPLBACKEND="Agg")
        for key in ("UAV_INPUT_DIR", "UAV_TEMPLATE"):
            os.environ.pop(key, None)
        sys.path.insert(0, str(ROOT))
        import project_paths
        project_paths.bootstrap()
        import problem4_group_scheduler as q4
        source = Path(q4.phase123.BOX_FILE)
        self.sources["external_raw/" + source.name] = digest(source)
        self.check("q4_entry_uses_frozen_inputs", Path(q4.PROBLEM3_RESULT) == REF/"problem3/problem3_vns_20cycles.xlsx"
                   and Path(q4.phase123.Q3_SUBMISSION) == REF/"submission/结果提交.xlsx")
        start = perf_counter()
        context = q4.load_context()
        archive = {int(r["mask"]): r for r in self.table("problem4/group_subset_results.xlsx", "subset_results")}
        differences = []
        for mask in range(1, 127):
            current = q4.account_subset(mask, context)
            values = {"N_U_"+kind: current.transport_uav_requirement[kind] for kind in "ABC"}
            values.update({"N_B_"+kind: current.battery_requirement[kind] for kind in "ABC"})
            values.update(N_R=current.relay_uav_requirement["R"], N_E=current.relay_energy_requirement["REC"])
            differences.extend(dict(mask=mask, column=key, calculated=value, archive=archive[mask][key])
                               for key, value in values.items() if value != archive[mask][key])
        self.check("q4_1008_resource_counts", not differences, dict(comparisons=1008, differences=differences, load_and_calculation_seconds=perf_counter()-start))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, help="Authorized local raw directory; enables Q4 resource entry.")
    parser.add_argument("--output-dir", type=Path, help="New directory strictly inside results/runs.")
    args = parser.parse_args(argv)
    runs = (ROOT/"results/runs").resolve()
    output = (args.output_dir or runs/("archive_audit_"+uuid.uuid4().hex[:12])).resolve()
    if not output.is_relative_to(runs) or output == runs or output.exists():
        parser.error("--output-dir must be a new directory strictly inside results/runs")
    output.mkdir(parents=True)
    sys.dont_write_bytecode = True
    started = perf_counter()
    audit = Audit()
    with (ROOT/"docs/provenance.csv").open(encoding="utf-8-sig", newline="") as f:
        frozen = {r["release_path"]: r["sha256"] for r in csv.DictReader(f) if r["release_path"].startswith("results/reference/")}
    before = {name: digest(ROOT/name) for name in frozen if (ROOT/name).is_file()}
    audit.check("all_36_frozen_hashes", len(frozen) == len(before) == 36 and before == frozen)
    completed_resource = False
    try:
        expected = audit.q1_q2()
        transport = audit.q3(expected)
        audit.q4(transport)
        if args.raw_dir is not None:
            audit.resource_entry(args.raw_dir.resolve(), output)
            completed_resource = True
    except Exception as exc:
        message = str(exc).replace(str(ROOT), "<project>")
        if args.raw_dir is not None:
            message = message.replace(str(args.raw_dir.resolve()), "<external_raw>")
        audit.check("execution_error", False, dict(type=type(exc).__name__, message=message))
    after = {name: digest(ROOT/name) for name in before}
    audit.check("reference_bytes_unchanged_after_audit", before == after)
    failures = sum(c["status"] == "FAIL" for c in audit.checks)
    report = dict(status="PASS" if not failures else "FAIL", python=platform.python_version(),
        scope="Archive reconciliation only: no optimization, radio geometry audit or pickle deserialization",
        q4_resource_accounting="executed" if completed_resource else "not_executed_or_incomplete",
        tolerances=dict(energy_kwh=1e-9, time_s=1e-7, resource_interval_s=1e-6, pareto=1e-10),
        checks=audit.checks, check_count=len(audit.checks), failures=failures, metrics=audit.metrics,
        source_sha256=audit.sources, reference_sha256=after, runtime_seconds=perf_counter()-started)
    target = output/"summary.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(dict(status=report["status"], checks=len(audit.checks), failures=failures,
                         report=target.relative_to(ROOT).as_posix(), runtime_seconds=report["runtime_seconds"]), ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
