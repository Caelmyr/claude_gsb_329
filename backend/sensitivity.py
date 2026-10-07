"""Parameter sensitivity analysis: automated scans, curves, heatmaps, ranking.

A *sweep* takes one scene and one or more numeric parameters and runs the
simulation once (or ``replicates`` times) for every scanned value, distilling
each run into a handful of aggregate metrics.  The result answers two
questions at a glance:

* **Which parameters matter?** — a 1-parameter scan becomes a sensitivity
  curve (parameter value vs. metric); a 2-parameter scan becomes a heatmap;
  a 3+ parameter request does not explode combinatorially but runs a
  one-at-a-time (OAT) design around the baseline and returns a sensitivity
  ranking (tornado chart).
* **What actually ran?** — every point records its own status, the number of
  steps it really took (runs may stop early once a metric settles, so
  different values need different run lengths), its wall-clock duration and
  the error if it failed.  One bad value never aborts the scan; it is marked
  ``failed`` and the rest proceed.

Sweeps persist to ``data/sweeps/<id>.json`` after every completed point, so
the frontend can poll progress (``completed``/``total``) while a long scan is
running.  Sweep runs persist only their step-0 snapshot (no per-step heavy
snapshots) to keep disk use modest when scanning many values.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from typing import Any, Dict, List, Optional

from . import catalog, models, storage, util
from .run_manager import manager

# Safety rails for scan size (points × replicates).  The UI warns earlier,
# but the backend hard-caps so a mis-shaped request cannot pin the server.
MAX_PARAMS = 8
MAX_RUNS = 500


# --------------------------------------------------------------------------- #
# Value grids
# --------------------------------------------------------------------------- #
def _snap(v: float, spec: Dict[str, Any]) -> Any:
    """Cast a grid point to the param type, trimming float noise (0.200...4)."""
    if spec["type"] == "int":
        return int(round(v))
    return float(f"{v:.10g}")


def _cast(spec: Dict[str, Any], value: float) -> Any:
    """Coerce a scanned float to the parameter's declared type."""
    return int(round(value)) if spec["type"] == "int" else float(value)


def _unique(seq: List[Any]) -> List[Any]:
    out: List[Any] = []
    for v in seq:
        if not out or v != out[-1]:
            out.append(v)
    return out


def _eq_value(a: Any, b: Any, spec: Dict[str, Any]) -> bool:
    """Equality for scanned values with float-grid tolerance.

    Linear grids computed as ``lo + (hi - lo) * i / n`` rarely land exactly on
    the scene's baseline (e.g. 0.30000000000000004), so float params compare
    with a small relative tolerance; int params compare exactly.
    """
    if a is None or b is None:
        return False
    if spec["type"] == "int":
        return int(a) == int(b)
    return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)


def _as_float(v: Any, what: str) -> float:
    """Parse a numeric scan input, rejecting NaN / infinity / garbage."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{what} 不是有效数字: {v!r}")
    if not math.isfinite(f):
        raise ValueError(f"{what} 必须是有限数值: {v!r}")
    return f


def scan_values(param: Dict[str, Any], spec: Dict[str, Any]) -> List[Any]:
    """Resolve one parameter spec into its ordered, de-duplicated values.

    Modes:
      * ``values`` — an explicit list supplied by the caller;
      * ``linear`` — ``count`` evenly spaced points in ``[start, end]``;
      * ``log``    — ``count`` geometrically spaced points (positive span,
                     useful for rates spanning orders of magnitude).
    """
    mode = param.get("mode", "linear")
    key = param.get("key")
    if mode == "values":
        raw = list(param.get("values") or [])
        if not raw:
            raise ValueError(f"参数 {key} 未提供取值列表")
        vals = [_cast(spec, _as_float(v, f"参数 {key} 的取值")) for v in raw]
        return _unique(sorted(vals))

    start = _as_float(param.get("start"), f"参数 {key} 的区间起点")
    end = _as_float(param.get("end"), f"参数 {key} 的区间终点")
    try:
        count = int(param.get("count", 5))
    except (TypeError, ValueError):
        raise ValueError(f"参数 {key} 的取值数不是整数")
    if count < 2:
        raise ValueError("每个参数至少需要 2 个取值")
    if start == end:
        raise ValueError(f"参数 {param.get('key')} 的区间起点与终点相同")

    if mode == "log":
        if start <= 0 or end <= 0:
            raise ValueError("对数扫描的区间端点必须都大于 0")
        lo, hi = sorted((start, end))
        vals = [lo * (hi / lo) ** (i / (count - 1)) for i in range(count)]
    elif mode == "linear":
        lo, hi = sorted((start, end))
        vals = [lo + (hi - lo) * i / (count - 1) for i in range(count)]
    else:
        raise ValueError(f"未知取值方式: {mode}")
    return _unique(sorted(_snap(v, spec) for v in vals))


def _param_spec(scene: Dict[str, Any], key: str) -> Dict[str, Any]:
    for spec in catalog.model_params(scene["domain"], scene["model"]):
        if spec["key"] == key:
            if spec["type"] not in ("int", "float"):
                raise ValueError(f"参数 {spec['label']} 不是数值参数，无法扫描")
            return spec
    raise ValueError(f"参数不存在: {key}")


def build_plan(scene: Dict[str, Any],
               param_specs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate request params and produce the scan plan.

    Returns ``{"scan_type", "params", "points", "baseline", "total"}`` where
    each point is ``{"param_values": {key: value}, "baseline": bool}``.
    """
    if not param_specs:
        raise ValueError("至少选择一个待扫描参数")
    if len(param_specs) > MAX_PARAMS:
        raise ValueError(f"一次最多扫描 {MAX_PARAMS} 个参数")

    keys = [p["key"] for p in param_specs]
    if len(set(keys)) != len(keys):
        raise ValueError("待扫描参数重复")

    resolved = models.resolve_config(models.Scene.from_dict(scene))
    planned: List[Dict[str, Any]] = []
    for p in param_specs:
        spec = _param_spec(scene, p["key"])
        vals = scan_values(p, spec)
        lo, hi = spec.get("min"), spec.get("max")
        for v in vals:
            if lo is not None and float(v) < lo:
                raise ValueError(f"{spec['label']} 取值 {v:g} 小于下限 {lo:g}")
            if hi is not None and float(v) > hi:
                raise ValueError(f"{spec['label']} 取值 {v:g} 大于上限 {hi:g}")
        baseline = _snap(float(resolved[p["key"]]), spec)
        planned.append({"key": p["key"], "label": spec["label"],
                        "type": spec["type"],
                        "values": vals, "baseline": baseline})

    def make_point(values: Dict[str, Any], is_baseline: bool = False,
                  varied: Optional[str] = None):
        return {"param_values": values, "baseline": is_baseline,
                "varied": varied}

    if len(planned) == 1:
        p = planned[0]
        points = [make_point({p["key"]: v},
                             _eq_value(v, p["baseline"], p))
                  for v in p["values"]]
        scan_type = "line"
    elif len(planned) == 2:
        x, y = planned
        points = []
        for xv in x["values"]:
            for yv in y["values"]:
                is_base = (_eq_value(xv, x["baseline"], x)
                           and _eq_value(yv, y["baseline"], y))
                points.append(make_point({x["key"]: xv, y["key"]: yv}, is_base))
        scan_type = "heatmap"
    else:
        # One-at-a-time around the baseline: one baseline point, then vary a
        # single parameter per point (keeps N params linear instead of N^k).
        base_values = {p["key"]: p["baseline"] for p in planned}
        points = [make_point(dict(base_values), True)]
        for p in planned:
            extra = [v for v in p["values"]
                     if not _eq_value(v, p["baseline"], p)]
            for v in extra:
                points.append(make_point({**base_values, p["key"]: v},
                                         varied=p["key"]))
        scan_type = "oat"

    return {"scan_type": scan_type, "params": planned,
            "baseline": {p["key"]: p["baseline"] for p in planned},
            "points": points, "total": len(points)}


# --------------------------------------------------------------------------- #
# Metric aggregation
# --------------------------------------------------------------------------- #
_AGGS = ("final", "mean", "max", "min")


def aggregate_series(series: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Aggregate a run's per-step series (excluding step 0) per metric.

    For every numeric stat key: ``final`` / ``mean`` / ``max`` / ``min`` plus
    the ``peak_step`` at which the max occurred.
    """
    rows = [r for r in series if int(r.get("step", 0)) > 0]
    out: Dict[str, Dict[str, Any]] = {}
    if not rows:
        return out
    for key in rows[-1]:
        if key == "step":
            continue
        vals = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        if not vals:
            continue
        peak_row = max(
            (r for r in rows if isinstance(r.get(key), (int, float))),
            key=lambda r: r[key])
        out[key] = {
            "final": vals[-1],
            "mean": statistics.fmean(vals),
            "max": max(vals),
            "min": min(vals),
            "peak_step": int(peak_row["step"]),
        }
    return out


def _combine(replicate_aggs: List[Dict[str, Dict[str, Any]]]
             ) -> Dict[str, Dict[str, Any]]:
    """Average per-mode aggregates across replicates; attach sample std."""
    keys = replicate_aggs[0].keys()
    combined: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        combined[key] = {}
        for mode in (*_AGGS, "peak_step"):
            vals = [a[key][mode] for a in replicate_aggs if key in a]
            if mode == "peak_step":
                combined[key][mode] = int(round(statistics.fmean(vals)))
                continue
            combined[key][mode] = statistics.fmean(vals)
            combined[key][f"{mode}_std"] = (
                statistics.stdev(vals) if len(vals) > 1 else 0.0)
    return combined


# --------------------------------------------------------------------------- #
# Sweep lifecycle
# --------------------------------------------------------------------------- #
class SweepRunner:
    """Owns the background threads that execute sweeps in this process."""

    def __init__(self) -> None:
        self._cancel: set = set()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    def create_sweep(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validate and persist a new sweep in ``pending`` state."""
        scene = storage.load_scene(data.get("scene_id", ""))
        if scene is None:
            raise KeyError(f"scene not found: {data.get('scene_id')}")
        steps = int(data.get("steps", 200))
        if steps < 1:
            raise ValueError("运行步数必须 ≥ 1")
        replicates = max(1, int(data.get("replicates", 1)))

        plan = build_plan(scene, list(data.get("params") or []))
        if plan["total"] * replicates > MAX_RUNS:
            raise ValueError(
                f"扫描规模过大：{plan['total']} 组 × {replicates} 次重复 "
                f"> {MAX_RUNS}，请减少取值或重复次数")

        metric_keys = {m["key"] for m in
                       catalog.CATALOG[scene["domain"]]["metrics"]}
        metric = str(data.get("metric") or next(iter(metric_keys)))
        if metric not in metric_keys:
            raise ValueError(f"未知指标: {metric}")
        agg = str(data.get("aggregation", "final"))
        if agg not in _AGGS:
            raise ValueError(f"未知聚合方式: {agg}")

        stop_rule = data.get("stop_rule") or None
        if stop_rule:
            if stop_rule.get("metric") not in metric_keys:
                raise ValueError("提前结束规则引用了未知指标")
            stop_rule = {
                "metric": str(stop_rule["metric"]),
                "op": "above" if stop_rule.get("op") == "above" else "below",
                "threshold": float(stop_rule.get("threshold", 0.0)),
                "patience": max(1, int(stop_rule.get("patience", 1))),
            }

        now = util.now_iso()
        sweep_id = util.new_id("sweep")
        points = [{
            "idx": i,
            "param_values": p["param_values"],
            "baseline": p["baseline"],
            "varied": p.get("varied"),
            "status": "pending",
            "run_ids": [],
            "runs": [],
            "error": "",
            "steps": None,
            "duration_s": None,
            "early_stopped": False,
        } for i, p in enumerate(plan["points"])]

        sweep = {
            "id": sweep_id,
            "name": str(data.get("name") or "参数敏感性分析"),
            "scene_id": scene["id"],
            "scene_name": scene["name"],
            "domain": scene["domain"],
            "model": scene["model"],
            "steps": steps,
            "stop_rule": stop_rule,
            "replicates": replicates,
            "seed": int(data.get("seed", 0)),
            "metric": metric,
            "aggregation": agg,
            "params": plan["params"],
            "scan_type": plan["scan_type"],
            "baseline": plan["baseline"],
            "total": plan["total"],
            "completed": 0,
            "failed": 0,
            "cancelled": False,
            "status": "pending",
            "error": "",
            "points": points,
            "ranking": [],
            "created_at": now,
            "updated_at": now,
            "finished_at": "",
        }
        storage.save_sweep(sweep)
        return sweep

    def start(self, sweep_id: str) -> None:
        with self._lock:
            self._cancel.discard(sweep_id)
            thread = threading.Thread(
                target=self._run, args=(sweep_id,), daemon=True)
            thread.start()

    def cancel(self, sweep_id: str) -> None:
        """Request cancellation; the current point finishes, rest stay pending."""
        with self._lock:
            self._cancel.add(sweep_id)

    def is_cancelled(self, sweep_id: str) -> bool:
        with self._lock:
            return sweep_id in self._cancel

    # ------------------------------------------------------------------ #
    def run_point(self, scene: Dict[str, Any], point: Dict[str, Any],
                  sweep: Dict[str, Any]) -> Dict[str, Any]:
        """Execute every replicate for one scan point and aggregate results.

        Raises are deliberately caught here: a value that makes an engine
        raise must not kill the whole scan.  Returns a result dict; the point
        is ``failed`` only when none of its replicates completed.
        """
        steps = int(sweep["steps"])
        cfg = {**scene.get("config", {}), **point["param_values"]}
        label = _point_label(sweep, point)
        rep_aggs: List[Dict[str, Dict[str, Any]]] = []
        run_records: List[Dict[str, Any]] = []
        errors: List[str] = []

        for r in range(int(sweep["replicates"])):
            rep_seed = int(sweep["seed"]) + r
            run_scene = models.Scene.from_dict(
                {**scene, "config": cfg,
                 "name": f"{sweep['name']} · {label}"
                         + (f"（重复 {r + 1}）" if sweep["replicates"] > 1 else "")})
            t0 = time.monotonic()
            try:
                meta = manager.create_run(
                    run_scene, name=run_scene.name, seed=rep_seed,
                    # Only the step-0 snapshot is needed for sweep analysis;
                    # skipping the rest keeps a big scan cheap on disk.
                    snapshot_interval=steps + 1,
                    tags={"kind": "sweep", "sweep_id": sweep["id"],
                          "point_idx": point["idx"], "replicate": r})
                result = manager.run_batch(
                    meta["id"], steps, keep_engine=False,
                    stop_rule=sweep.get("stop_rule"))
                duration = time.monotonic() - t0
                series = storage.load_series(meta["id"])
                aggs = aggregate_series(series)
                if not aggs:
                    raise RuntimeError("运行未产生任何统计数据")
                rep_aggs.append(aggs)
                run_records.append({
                    "run_id": meta["id"], "seed": rep_seed,
                    "steps": int(result["ran_steps"]),
                    "duration_s": round(duration, 2),
                    "early_stopped": bool(result["early_stopped"]),
                    "stop_reason": result.get("stop_reason", ""),
                })
            except Exception as exc:  # noqa: BLE001 — record and continue
                errors.append(f"重复 {r + 1}: {exc}")

        if not rep_aggs:
            return {"status": "failed", "error": "；".join(errors) or "运行失败",
                    "runs": run_records, "aggregates": {},
                    "steps": None, "duration_s": None, "early_stopped": False}

        aggregates = rep_aggs[0] if len(rep_aggs) == 1 else _combine(rep_aggs)
        total_steps = sum(r["steps"] for r in run_records) // len(run_records)
        total_time = round(sum(r["duration_s"] for r in run_records), 2)
        early = any(r["early_stopped"] for r in run_records)
        return {
            "status": "ok" if not errors else "degraded",
            "error": "；".join(errors),
            "runs": run_records,
            "run_ids": [r["run_id"] for r in run_records],
            "aggregates": aggregates,
            "steps": total_steps,
            "duration_s": total_time,
            "early_stopped": early,
        }

    # ------------------------------------------------------------------ #
    def _run(self, sweep_id: str) -> None:
        sweep = storage.load_sweep(sweep_id)
        if sweep is None:
            return
        scene = storage.load_scene(sweep["scene_id"])
        if scene is None:
            sweep["status"] = "error"
            sweep["error"] = "场景已被删除"
            sweep["updated_at"] = util.now_iso()
            storage.save_sweep(sweep)
            return

        sweep["status"] = "running"
        sweep["updated_at"] = util.now_iso()
        storage.save_sweep(sweep)

        try:
            for point in sweep["points"]:
                if self.is_cancelled(sweep_id):
                    sweep["cancelled"] = True
                    break
                if point["status"] in ("ok", "degraded", "failed"):
                    continue
                point["status"] = "running"
                point["started_at"] = util.now_iso()
                storage.save_sweep(sweep)

                res = self.run_point(scene, point, sweep)
                point.update(res)
                point["finished_at"] = util.now_iso()
                if res["status"] in ("ok", "degraded"):
                    sweep["completed"] += 1
                else:
                    sweep["failed"] += 1
                sweep["updated_at"] = util.now_iso()
                storage.save_sweep(sweep)
        except Exception as exc:  # noqa: BLE001
            sweep["status"] = "error"
            sweep["error"] = str(exc)
            sweep["finished_at"] = sweep["updated_at"] = util.now_iso()
            storage.save_sweep(sweep)
            return

        self._finalize(sweep)

    def _finalize(self, sweep: Dict[str, Any]) -> None:
        """Derive ranking (OAT) and choose finished / partial / stopped."""
        if sweep["scan_type"] == "oat":
            sweep["ranking"] = _rank_oat(sweep)

        now = util.now_iso()
        sweep["finished_at"] = now
        sweep["updated_at"] = now
        with self._lock:
            self._cancel.discard(sweep["id"])
        pending = sum(1 for p in sweep["points"]
                      if p["status"] in ("pending", "running"))
        if sweep["cancelled"] or pending:
            sweep["status"] = "stopped" if sweep["completed"] == 0 else "partial"
        elif sweep["failed"]:
            sweep["status"] = "partial"
        else:
            sweep["status"] = "finished"
        storage.save_sweep(sweep)


# --------------------------------------------------------------------------- #
# OAT ranking
# --------------------------------------------------------------------------- #
def _point_metric(point: Dict[str, Any], sweep: Dict[str, Any]) -> Optional[float]:
    agg = point.get("aggregates", {}).get(sweep["metric"])
    if not agg:
        return None
    return float(agg[sweep["aggregation"]])


def _rank_oat(sweep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Per-parameter range of the selected metric + relative sensitivity.

    ``relative_pct`` normalises the raw range by the mean absolute metric
    level across the scan, so the tornado chart can compare parameters with
    different units on one axis.
    """
    all_vals = [v for p in sweep["points"]
                if (v := _point_metric(p, sweep)) is not None]
    scale = statistics.fmean(abs(v) for v in all_vals) if all_vals else 0.0

    ranking: List[Dict[str, Any]] = []
    for spec in sweep["params"]:
        key = spec["key"]
        # Only points that varied THIS parameter belong on its curve: the
        # shared baseline point plus points with ``varied == key``.
        own = [p for p in sweep["points"]
               if p.get("baseline") or p.get("varied") == key]
        rows = []
        for p in own:
            v = _point_metric(p, sweep)
            if v is not None:
                rows.append((p["param_values"][key], v))
        rows.sort(key=lambda r: r[0])
        values = [v for _, v in rows]
        raw_range = (max(values) - min(values)) if values else 0.0
        direction = 0.0
        if len(values) >= 2:
            direction = values[-1] - values[0]
        ranking.append({
            "key": key,
            "label": spec["label"],
            "points": [{"x": x, "y": y} for x, y in rows],
            "range": raw_range,
            "direction": direction,
            "relative_pct": (abs(raw_range) / scale * 100.0) if scale else 0.0,
        })
    ranking.sort(key=lambda r: r["relative_pct"], reverse=True)
    return ranking


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _point_label(sweep: Dict[str, Any], point: Dict[str, Any]) -> str:
    parts = []
    for spec in sweep["params"]:
        v = point["param_values"][spec["key"]]
        shown = f"{v:g}" if isinstance(v, (int, float)) else str(v)
        parts.append(f"{spec['label']}={shown}")
    return ", ".join(parts)


def summarize(sweep: Dict[str, Any]) -> Dict[str, Any]:
    """Compact progress/result summary used by list views."""
    return {
        "id": sweep["id"],
        "name": sweep["name"],
        "scene_name": sweep["scene_name"],
        "domain": sweep["domain"],
        "model": sweep["model"],
        "scan_type": sweep["scan_type"],
        "status": sweep["status"],
        "total": sweep["total"],
        "completed": sweep["completed"],
        "failed": sweep["failed"],
        "pending": sweep["total"] - sweep["completed"] - sweep["failed"],
        "metric": sweep["metric"],
        "aggregation": sweep["aggregation"],
        "params": [{"key": p["key"], "label": p["label"]}
                   for p in sweep["params"]],
        "created_at": sweep["created_at"],
        "finished_at": sweep.get("finished_at", ""),
    }


# Global singleton used by the Flask app.
sweep_runner = SweepRunner()
