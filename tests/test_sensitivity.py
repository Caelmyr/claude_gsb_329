"""Tests for parameter sensitivity sweeps.

Covers scan-plan construction (linear/log/OAT grids, de-duplication, guard
rails), metric aggregation, early stopping, per-point failure isolation and a
full end-to-end sweep executed through the same runner the API uses.

Run directly::

    python3 tests/test_sensitivity.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import models, sensitivity, storage  # noqa: E402

_SCENE = {
    "id": "scene_test_epi", "name": "测试疫情场景",
    "domain": "epidemic", "model": "abm",
    "config": {"width": 200, "height": 200, "n": 60, "beta": 0.3, "gamma": 0.05,
               "speed": 2.0, "radius": 6.0, "initial_infected": 5,
               "vaccination_rate": 0.0, "movement": "random_walk"},
    "interventions": [],
}


def check(name, fn) -> None:
    try:
        fn()
        print(f"PASS  {name}")
    except AssertionError as exc:
        print(f"FAIL  {name}: {exc}")
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  {name}: {exc!r}")
        raise


# --------------------------------------------------------------------------- #
def test_plan_line() -> None:
    plan = sensitivity.build_plan(_SCENE, [
        {"key": "beta", "mode": "linear", "start": 0.0, "end": 1.0, "count": 5}])
    assert plan["scan_type"] == "line"
    assert plan["total"] == 5
    vals = [p["param_values"]["beta"] for p in plan["points"]]
    assert vals == [0.0, 0.25, 0.5, 0.75, 1.0], vals
    assert plan["baseline"]["beta"] == 0.3
    # A grid that includes the scene baseline marks that point as baseline.
    plan_b = sensitivity.build_plan(_SCENE, [
        {"key": "beta", "mode": "linear", "start": 0.3, "end": 0.9, "count": 3}])
    base = next(p for p in plan_b["points"] if p["baseline"])
    assert base["param_values"]["beta"] == 0.3


def test_plan_log_and_dedup() -> None:
    plan = sensitivity.build_plan(_SCENE, [
        {"key": "beta", "mode": "log", "start": 0.01, "end": 1.0, "count": 3}])
    vals = [p["param_values"]["beta"] for p in plan["points"]]
    assert len(vals) == 3 and vals[0] < vals[1] < vals[2]
    assert abs(vals[0] - 0.01) < 1e-9 and abs(vals[-1] - 1.0) < 1e-9

    # int parameter + a coarse interval collapses duplicates; they are removed
    plan2 = sensitivity.build_plan(_SCENE, [
        {"key": "initial_infected", "mode": "linear",
         "start": 1, "end": 2, "count": 20}])
    ivals = [p["param_values"]["initial_infected"] for p in plan2["points"]]
    assert len(set(ivals)) == len(ivals)
    assert all(isinstance(v, int) for v in ivals)


def test_plan_heatmap() -> None:
    plan = sensitivity.build_plan(_SCENE, [
        {"key": "beta", "mode": "linear", "start": 0.0, "end": 0.6, "count": 3},
        {"key": "gamma", "mode": "linear", "start": 0.0, "end": 0.2, "count": 3}])
    assert plan["scan_type"] == "heatmap"
    assert plan["total"] == 9
    assert len({tuple(sorted(p["param_values"].items()))
                for p in plan["points"]}) == 9


def test_plan_oat() -> None:
    plan = sensitivity.build_plan(_SCENE, [
        {"key": "beta", "mode": "linear", "start": 0.1, "end": 0.5, "count": 3},
        {"key": "gamma", "mode": "linear", "start": 0.02, "end": 0.1, "count": 3},
        {"key": "radius", "mode": "linear", "start": 2.0, "end": 10.0, "count": 3}])
    assert plan["scan_type"] == "oat"
    # One baseline point + every non-baseline value of each parameter.
    n_extra = sum(len(p["values"]) -
                  sum(1 for v in p["values"]
                      if sensitivity._eq_value(v, p["baseline"], p))
                  for p in plan["params"])
    assert plan["total"] == 1 + n_extra, plan["total"]
    base = next(p for p in plan["points"] if p["baseline"])
    assert base["varied"] is None
    by_varied = {}
    for p in plan["points"]:
        if p["varied"]:
            by_varied.setdefault(p["varied"], 0)
            by_varied[p["varied"]] += 1
    # gamma baseline 0.05 is not on the [0.02, 0.06, 0.1] grid -> 3 points
    assert by_varied == {"beta": 2, "gamma": 3, "radius": 2}, by_varied


def test_plan_validation() -> None:
    def expect_error(params, needle=""):
        try:
            sensitivity.build_plan(_SCENE, params)
        except ValueError as exc:
            assert needle in str(exc), (needle, str(exc))
            return
        raise AssertionError("expected ValueError")

    expect_error([], "至少")
    expect_error([{"key": "beta", "mode": "linear",
                   "start": 0.0, "end": 5.0, "count": 5}], "上限")
    expect_error([{"key": "beta", "mode": "linear",
                   "start": 0.5, "end": 0.5, "count": 5}], "相同")
    expect_error([{"key": "beta", "mode": "log",
                   "start": -1.0, "end": 1.0, "count": 5}], "大于 0")
    expect_error([{"key": "movement", "mode": "values", "values": ["x"]}], "数值参数")
    expect_error([{"key": "beta", "mode": "linear",
                   "start": "nan", "end": 1.0, "count": 5}], "有限数值")


def test_aggregate() -> None:
    series = [{"step": 0, "infected": 5},
              {"step": 1, "infected": 10},
              {"step": 2, "infected": 20},
              {"step": 3, "infected": 4}]
    agg = sensitivity.aggregate_series(series)
    assert agg["infected"] == {"final": 4, "mean": 34 / 3, "max": 20,
                               "min": 4, "peak_step": 2}, agg
    # step 0 must not contaminate the aggregation
    assert sensitivity.aggregate_series([{"step": 0, "x": 99}]) == {}


def test_early_stop() -> None:
    from backend.run_manager import manager
    scene = models.Scene.from_dict(
        {**_SCENE, "config": {**_SCENE["config"], "gamma": 1.0, "beta": 0.0}})
    meta = manager.create_run(scene, seed=1, snapshot_interval=9999)
    try:
        res = manager.run_batch(
            meta["id"], 200, keep_engine=False,
            stop_rule={"metric": "infected", "op": "below",
                       "threshold": 1.0, "patience": 5})
        assert res["early_stopped"], res
        assert res["ran_steps"] < 200, res["ran_steps"]
        assert "infected" in res["stop_reason"]
    finally:
        manager.delete_run(meta["id"])


def test_point_failure_isolated() -> None:
    """A value that breaks the engine is recorded on the point, not raised."""
    runner = sensitivity.SweepRunner()
    sweep = {"id": "sweep_test", "name": "t", "steps": 10, "replicates": 1,
             "seed": 0, "stop_rule": None,
             "params": [{"key": "beta", "label": "感染概率 β"}]}
    good = {"idx": 0, "param_values": {"beta": 0.2}, "baseline": False}
    bad = {"idx": 1, "param_values": {"beta": "not-a-number"}, "baseline": False}
    r1 = runner.run_point(_SCENE, good, sweep)
    r2 = runner.run_point(_SCENE, bad, sweep)
    assert r1["status"] == "ok" and r1["aggregates"]["infected"]["final"] is not None
    assert r2["status"] == "failed" and r2["error"]
    from backend.run_manager import manager
    for rid in r1["run_ids"]:
        manager.delete_run(rid)


def test_end_to_end_sweep() -> None:
    """Full runner path: valid groups + one injected bad group + early stop."""
    runner = sensitivity.SweepRunner()
    payload = {
        "scene_id": _SCENE["id"], "name": "端到端 β 扫描", "steps": 120,
        "metric": "infected", "aggregation": "max", "replicates": 1,
        "seed": 3,
        "stop_rule": {"metric": "infected", "op": "below",
                      "threshold": 1.0, "patience": 5},
        "params": [{"key": "beta", "mode": "linear",
                    "start": 0.0, "end": 0.6, "count": 4}],
    }
    sweep = runner.create_sweep(payload)
    assert sweep["total"] == 4

    # Inject a 5th, guaranteed-failing point without touching validation.
    sweep["points"].append({
        "idx": 4, "param_values": {"beta": "boom"}, "baseline": False,
        "varied": None, "status": "pending", "run_ids": [], "runs": [],
        "error": "", "steps": None, "duration_s": None,
        "early_stopped": False})
    sweep["total"] = 5
    storage.save_sweep(sweep)

    runner._run(sweep["id"])
    done = storage.load_sweep(sweep["id"])

    assert done["status"] == "partial", done["status"]
    assert done["completed"] == 4 and done["failed"] == 1
    statuses = [p["status"] for p in done["points"]]
    assert statuses.count("ok") == 4 and statuses.count("failed") == 1
    for p in done["points"][:4]:
        assert p["steps"] is not None and p["steps"] <= 120
        assert p["duration_s"] is not None
        assert "infected" in p["aggregates"]
        assert p["runs"][0]["run_id"].startswith("run_")
    failed = done["points"][4]
    assert failed["error"] and failed["steps"] is None

    # Curve data is ordered by beta and reflects a real response range.
    peaks = [(p["param_values"]["beta"],
              p["aggregates"]["infected"]["max"]) for p in done["points"][:4]]
    peaks.sort()
    assert peaks[-1][1] > peaks[0][1], peaks

    summ = sensitivity.summarize(done)
    assert summ["completed"] == 4 and summ["failed"] == 1 and summ["pending"] == 0
    storage.delete_sweep(sweep["id"])


def test_oat_sweep_ranking() -> None:
    runner = sensitivity.SweepRunner()
    payload = {
        "scene_id": _SCENE["id"], "name": "OAT 排名", "steps": 60,
        "metric": "infected", "aggregation": "max", "replicates": 1,
        "seed": 5,
        "params": [
            {"key": "beta", "mode": "linear", "start": 0.05, "end": 0.6, "count": 3},
            {"key": "gamma", "mode": "linear", "start": 0.02, "end": 0.4, "count": 3},
            {"key": "speed", "mode": "linear", "start": 0.5, "end": 6.0, "count": 3},
        ],
    }
    sweep = runner.create_sweep(payload)
    assert sweep["scan_type"] == "oat"
    assert sweep["total"] >= 7
    runner._run(sweep["id"])
    done = storage.load_sweep(sweep["id"])
    assert done["status"] == "finished", done.get("error")
    assert len(done["ranking"]) == 3
    assert done["completed"] == done["total"]
    rels = [r["relative_pct"] for r in done["ranking"]]
    assert rels == sorted(rels, reverse=True)
    for r in done["ranking"]:
        assert {"key", "label", "points", "range", "relative_pct"} <= set(r)
        # Baseline point + this parameter's own non-baseline values only —
        # points varying other parameters must not leak onto this curve.
        n_own = len([p for p in done["points"] if p.get("varied") == r["key"]])
        assert len(r["points"]) == 1 + n_own, (r["key"], len(r["points"]))
    xs = {r["key"]: {pt["x"] for pt in r["points"]}
          for r in done["ranking"]}
    assert len({tuple(sorted(v)) for v in xs.values()}) == 3
    storage.delete_sweep(done["id"])


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        old = storage.DATA_DIR
        storage.DATA_DIR = td
        try:
            storage.ensure_dirs()
            storage.save_scene(_SCENE)
            tests = [
                ("plan: 1-d linear grid", test_plan_line),
                ("plan: log grid + int dedup", test_plan_log_and_dedup),
                ("plan: 2-d heatmap grid", test_plan_heatmap),
                ("plan: OAT design", test_plan_oat),
                ("plan validation errors", test_plan_validation),
                ("series aggregation", test_aggregate),
                ("batch early stop rule", test_early_stop),
                ("point failure isolation", test_point_failure_isolated),
                ("end-to-end sweep (ok+failed, early stop)", test_end_to_end_sweep),
                ("OAT sweep ranking", test_oat_sweep_ranking),
            ]
            for name, fn in tests:
                check(name, fn)
        finally:
            storage.DATA_DIR = old
    print("\nall sensitivity tests passed")


if __name__ == "__main__":
    main()
