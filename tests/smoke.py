"""Smoke tests for the simulation engines, storage and run lifecycle.

Run directly::

    python3 tests/smoke.py

Each check is independent and prints PASS / FAIL; the script exits non-zero on
the first failure so it can be wired into CI or a pre-commit hook.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import models, report, storage, sweep  # noqa: E402
from backend.engine import make_engine  # noqa: E402
from backend.run_manager import manager  # noqa: E402

_ENGINES = ["traffic/ca", "traffic/abm", "ecology/ca", "ecology/abm",
            "epidemic/ca", "epidemic/abm"]


def check(name: str, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc}")
        sys.exit(1)


def engines_step() -> None:
    for key in _ENGINES:
        d, m = key.split("/")
        eng = make_engine(d, m, seed=42)
        for _ in range(5):
            eng.step()
        assert eng.step_count == 5, key
        assert len(eng.individuals()) > 0, f"{key} has no individuals"
        stats = eng.stats()
        assert stats, f"{key} produced empty stats"
        snap = eng.snapshot()
        assert snap["step"] == 5
        assert snap["stats"] == stats


def interventions_apply() -> None:
    eng = make_engine("epidemic", "abm", seed=1)
    before = eng.stats()["susceptible"]
    res = eng.apply_intervention({"type": "vaccinate", "params": {"fraction": 1.0}})
    assert res["applied"], res
    assert eng.stats()["susceptible"] == 0
    assert before > 0


def storage_atomic_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as td:
        # Redirect the module's DATA_DIR for this isolated check.
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            storage.save_scene({"id": "x", "name": "t", "updated_at": "z"})
            assert storage.load_scene("x")["name"] == "t"
            storage.save_step("r1", 0, {"step": 0, "v": 1})
            storage.save_step("r1", 7, {"step": 7, "v": 2})
            assert storage.load_step("r1", 7)["v"] == 2
            assert storage.list_steps("r1") == [0, 7]
        finally:
            storage.DATA_DIR = old


def run_lifecycle() -> None:
    scene = models.Scene(domain="epidemic", model="abm",
                         config={"n": 200, "width": 300, "height": 300,
                                 "initial_infected": 5})
    meta = manager.create_run(scene, seed=1, snapshot_interval=2)
    rid = meta["id"]
    try:
        r = manager.step(rid, 6)
        assert r["step"] == 6
        series = manager.get_series(rid)
        assert series[0]["step"] == 0 and series[-1]["step"] == 6
        assert len(manager.get_individuals(rid, 6)) == 200
        # snapshot_interval=2 -> full snapshots persisted at 0,2,4,6
        steps = storage.list_steps(rid)
        assert steps == [0, 2, 4, 6], steps
        rpt = report.generate_report(rid)
        assert rpt["steps"] == 7
    finally:
        manager.delete_run(rid)


def sweep_value_expansion() -> None:
    spec = {"key": "beta", "label": "β", "type": "float",
            "min": 0.0, "max": 1.0, "default": 0.4}
    vals = sweep.expand_values({"from": 0.1, "to": 0.5, "count": 5}, spec)
    assert vals == [0.1, 0.2, 0.3, 0.4, 0.5], vals
    # explicit values are clamped to the catalog range and de-duplicated
    vals = sweep.expand_values({"values": [0.2, 0.2, 5.0, -1.0]}, spec)
    assert vals == [0.2, 1.0, 0.0], vals
    # int parameters are coerced to ints
    ispec = {"key": "lanes", "label": "车道", "type": "int",
             "min": 1, "max": 8, "default": 3}
    vals = sweep.expand_values({"from": 1, "to": 3, "count": 3}, ispec)
    assert vals == [1, 2, 3] and all(isinstance(v, int) for v in vals), vals
    # non-numeric parameters are rejected
    bspec = {"key": "flag", "label": "开关", "type": "bool", "default": True}
    try:
        sweep.expand_values({"count": 3}, bspec)
        raise AssertionError("bool param should be rejected")
    except ValueError:
        pass


def sweep_plan_modes() -> None:
    scene = {"id": "s1", "name": "t", "domain": "epidemic", "model": "ca",
             "config": {"beta": 0.4, "gamma": 0.1}}
    # grid mode: full cartesian product
    sw = sweep.create_sweep(scene, {
        "params": [{"key": "beta", "values": [0.1, 0.2]},
                   {"key": "gamma", "values": [0.1, 0.2, 0.3]}],
        "mode": "grid", "steps": 5})
    assert sw["mode"] == "grid"
    assert sw["total"] == 6, sw["total"]
    # oat mode: shared baseline + one value at a time (linear cost);
    # values equal to the baseline config are covered by the baseline run
    sw = sweep.create_sweep(scene, {
        "params": [{"key": "beta", "values": [0.1, 0.4, 0.9]},
                   {"key": "gamma", "values": [0.2, 0.3]}],
        "mode": "oat", "steps": 5})
    plan = sweep.build_plan(sw)
    assert sw["total"] == 1 + 2 + 2, sw["total"]   # baseline + beta(0.1,0.9) + gamma
    assert plan[0]["values"] == {}
    assert {"beta": 0.4} not in [p["values"] for p in plan]
    # oversized scans are rejected with a readable error
    try:
        sweep.create_sweep(scene, {
            "params": [{"key": "beta", "from": 0, "to": 1, "count": 21},
                       {"key": "gamma", "from": 0, "to": 1, "count": 21}],
            "mode": "grid", "steps": 5})
        raise AssertionError("oversized grid should be rejected")
    except ValueError as exc:
        assert "OAT" in str(exc) or "逐参数" in str(exc)


def sweep_lifecycle() -> None:
    scene = {"id": "scene_sweep_test", "name": "扫描测试", "domain": "epidemic",
             "model": "ca", "config": {"width": 25, "height": 25, "seed": 3},
             "interventions": []}
    storage.save_scene({**scene, "created_at": "", "updated_at": ""})
    sw = sweep.create_sweep(scene, {
        "name": "smoke 扫描",
        "params": [{"key": "beta", "values": [0.1, 0.5, 0.9]}],
        "metrics": ["infected", "recovered"], "steps": 15, "replicates": 2})
    storage.save_sweep(sw)
    try:
        sweep.execute(sw["id"])   # synchronous: same code path as the worker
        sw = storage.load_sweep(sw["id"])
        assert sw["status"] == "finished", sw["status"]
        assert sw["done"] == 6 and sw["failed"] == 0, (sw["done"], sw["failed"])
        # every point ran fully and recorded its own duration / steps
        for p in sw["points"]:
            assert p["status"] == "ok" and p["steps_run"] == 15, p
            assert p["duration_ms"] >= 0 and p["run_id"]
            assert "infected" in p["metrics"] and "final" in p["metrics"]["infected"]
        # replicates of the same value got distinct seeds; values share the
        # same seed set so differences come from the parameter, not the seed
        by_value = {}
        for p in sw["points"]:
            by_value.setdefault(p["values"]["beta"], set()).add(p["seed"])
        assert all(len(s) == 2 for s in by_value.values()), by_value
        assert len({s for ss in by_value.values() for s in ss}) == 2
        a = sweep.analyze(sw)
        curve = a["curves"]["beta"]["infected"]["final"]
        assert [r["value"] for r in curve] == [0.1, 0.5, 0.9], curve
        assert all(r["n"] == 2 for r in curve)   # two replicates aggregated
        assert a["sensitivity"]["beta"]["infected"]["final"]["norm"] >= 0
        assert a["summary"]["ok"] == 6 and a["heatmap"] is None
        # each point produced a real, inspectable run
        meta = storage.load_run_meta(sw["points"][0]["run_id"])
        assert meta and meta["status"] == "finished"
    finally:
        sweep.delete_sweep(sw["id"])
        storage.delete_scene(scene["id"])
    assert storage.load_sweep(sw["id"]) is None
    # deleting the sweep also removed its runs
    assert all(storage.load_run_meta(p["run_id"]) is None for p in sw["points"])


def sweep_failure_isolation() -> None:
    scene = {"id": "scene_sweep_fail", "name": "失败隔离", "domain": "epidemic",
             "model": "ca", "config": {"width": 20, "height": 20},
             "interventions": []}
    storage.save_scene({**scene, "created_at": "", "updated_at": ""})
    sw = sweep.create_sweep(scene, {
        "params": [{"key": "beta", "values": [0.1, 0.5, 0.9]}],
        "metrics": ["infected"], "steps": 5})
    storage.save_sweep(sw)
    original = manager.create_run
    def flaky(scene_obj, **kwargs):
        if scene_obj.config.get("beta") == 0.5:
            raise RuntimeError("模拟中途崩溃")
        return original(scene_obj, **kwargs)
    manager.create_run = flaky
    try:
        sweep.execute(sw["id"])
    finally:
        manager.create_run = original
    try:
        sw = storage.load_sweep(sw["id"])
        # one combination failed mid-scan; the rest still ran to completion
        assert sw["status"] == "finished", sw["status"]
        assert sw["done"] == 3 and sw["failed"] == 1, (sw["done"], sw["failed"])
        bad = [p for p in sw["points"] if p["status"] == "error"]
        assert len(bad) == 1 and "模拟中途崩溃" in bad[0]["error"]
        a = sweep.analyze(sw)
        assert len(a["failed_points"]) == 1
        curve = a["curves"]["beta"]["infected"]["final"]
        assert [r["value"] for r in curve] == [0.1, 0.9]   # failed value excluded
    finally:
        sweep.delete_sweep(sw["id"])
        storage.delete_scene(scene["id"])


def sweep_grid_oat_and_control() -> None:
    scene = {"id": "scene_sweep_grid", "name": "网格", "domain": "epidemic",
             "model": "ca", "config": {"width": 25, "height": 25, "beta": 0.4,
                                       "gamma": 0.1, "initial_infected": 5},
             "interventions": []}
    storage.save_scene({**scene, "created_at": "", "updated_at": ""})
    ids = []
    try:
        # grid mode -> 2D heatmap cells
        sw = sweep.create_sweep(scene, {
            "params": [{"key": "beta", "values": [0.2, 0.8]},
                       {"key": "gamma", "values": [0.1, 0.5]}],
            "metrics": ["infected"], "steps": 10, "mode": "grid"})
        storage.save_sweep(sw)
        ids.append(sw["id"])
        sweep.execute(sw["id"])
        a = sweep.analyze(storage.load_sweep(sw["id"]))
        hm = a["heatmap"]
        assert hm and hm["x_values"] == [0.2, 0.8] and hm["y_values"] == [0.1, 0.5]
        cells = hm["grids"]["infected"]["final"]
        assert len(cells) == 4 and all(c["mean"] is not None for c in cells)

        # oat mode -> per-param curves share the baseline point, linear cost
        sw = sweep.create_sweep(scene, {
            "params": [{"key": "beta", "values": [0.1, 0.4, 0.9]},
                       {"key": "gamma", "values": [0.05, 0.3]}],
            "metrics": ["infected"], "steps": 10, "mode": "oat"})
        storage.save_sweep(sw)
        ids.append(sw["id"])
        sweep.execute(sw["id"])
        sw = storage.load_sweep(sw["id"])
        assert sw["done"] == 1 + 2 + 2, sw["done"]   # baseline + 2 + 2
        a = sweep.analyze(sw)
        # baseline (beta=0.4 / gamma=0.1 from the scene config) joins each curve
        assert [r["value"] for r in a["curves"]["beta"]["infected"]["final"]] == [0.1, 0.4, 0.9]
        assert [r["value"] for r in a["curves"]["gamma"]["infected"]["final"]] == [0.05, 0.1, 0.3]
        assert a["heatmap"] is None
        assert a["sensitivity"]["gamma"]["infected"]["final"]["norm"] >= 0

        # stop before the worker starts -> stopped with partial (zero) results
        sw = sweep.create_sweep(scene, {
            "params": [{"key": "beta", "from": 0.1, "to": 0.9, "count": 5}],
            "steps": 10})
        storage.save_sweep(sw)
        ids.append(sw["id"])
        sweep.stop_sweep(sw["id"])
        sweep.execute(sw["id"])
        sw = storage.load_sweep(sw["id"])
        assert sw["status"] == "stopped" and sw["done"] == 0, sw["status"]

        # stale "running" sweep (e.g. server restarted) reconciles to stopped
        sw["status"] = "running"
        storage.save_sweep(sw)
        assert sweep.refresh_status(storage.load_sweep(sw["id"]))["status"] == "stopped"

        # deleting a sweep mid-scan stops the worker and it stays deleted
        sw = sweep.create_sweep(scene, {
            "params": [{"key": "beta", "values": [0.1, 0.5, 0.9]}], "steps": 5})
        storage.save_sweep(sw)
        ids.append(sw["id"])
        original = sweep._run_point
        finished_run_ids = []
        def deleting(sweep_dict, combo):
            point = original(sweep_dict, combo)
            finished_run_ids.append(point["run_id"])
            sweep.delete_sweep(sw["id"])
            return point
        sweep._run_point = deleting
        try:
            sweep.execute(sw["id"])
        finally:
            sweep._run_point = original
        assert storage.load_sweep(sw["id"]) is None   # not resurrected by the worker
        # the in-flight combination's run was cleaned up too
        assert all(storage.load_run_meta(r) is None for r in finished_run_ids)
    finally:
        for sid in ids:
            sweep.delete_sweep(sid)
        storage.delete_scene(scene["id"])


def main() -> None:
    check("six engines step and snapshot", engines_step)
    check("interventions apply", interventions_apply)
    check("atomic sharded storage", storage_atomic_roundtrip)
    check("run lifecycle + report", run_lifecycle)
    check("sweep value expansion", sweep_value_expansion)
    check("sweep plan modes + caps", sweep_plan_modes)
    check("sweep lifecycle + analysis", sweep_lifecycle)
    check("sweep failure isolation", sweep_failure_isolation)
    check("sweep grid/oat analysis + stop", sweep_grid_oat_and_control)
    print("\nall smoke tests passed")


if __name__ == "__main__":
    main()
