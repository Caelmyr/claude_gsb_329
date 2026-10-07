"""Parameter sensitivity analysis: automated sweeps over parameter ranges.

A *sweep* takes one scene and one or more numeric parameters, expands each
parameter into a list of values (explicit or ``from/to/count``), then runs
every combination to completion as a normal run and distils each run's series
into per-metric aggregates (final / peak / mean).  The results are aggregated
into:

* **curves** — parameter value vs. metric (one swept parameter, or one-at-a-time
  "OAT" screening of several parameters, where a shared baseline keeps the cost
  linear in the number of values instead of exponential);
* **heatmap** — a full 2-parameter grid;
* **sensitivity ranking** — a normalised range per parameter so users can see
  at a glance which parameters matter and which are basically irrelevant.

Robustness requirements baked in here:

* combinations differ wildly in cost — every point records its own
  ``steps_run`` and ``duration_ms``, and the sweep file is re-written after
  every point so the UI can poll live progress and compute an ETA from
  *measured* durations;
* a combination may fail mid-run — the failure is captured on the point
  (``status: "error"`` + message) and the scan continues with the rest;
* scans can be long — they run on a background thread, can be stopped at any
  time (partial results are kept), and are capped (:data:`MAX_POINTS`) with a
  validation error that suggests the cheaper OAT mode.

Every combination is executed through :data:`backend.run_manager.manager` as a
real run, so each point links to a full inspectable run (series, snapshots,
report) exactly like a comparison experiment.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from typing import Any, Dict, List, Optional

from . import catalog, models, storage, util
from .run_manager import manager

# Caps that keep a single scan reviewable and bounded in time/disk.
MAX_PARAMS = 4
MAX_VALUES_PER_PARAM = 40
MAX_POINTS = 400
MAX_STEPS = 5000
MAX_REPLICATES = 10

AGGREGATES = ("final", "peak", "mean")

# Background execution registry: sweep_id -> thread, plus an abort set.
_THREADS: Dict[str, threading.Thread] = {}
_ABORT: set = set()
_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# Construction / validation
# --------------------------------------------------------------------------- #
def _fnum(v: Any) -> float:
    """Canonical numeric key for bucketing parameter values."""
    f = float(v)
    return int(f) if f == int(f) else f


def expand_values(spec: Dict[str, Any], param_spec: Dict[str, Any]) -> List[Any]:
    """Expand one parameter spec into a de-duplicated, clamped value list.

    Accepts either an explicit ``values`` list or ``from``/``to``/``count``
    (inclusive linspace).  Values are coerced to the parameter's type and
    clamped to its catalog ``min``/``max``.
    """
    ptype = param_spec.get("type")
    if ptype not in ("int", "float"):
        raise ValueError(f"参数 {param_spec.get('label')} 不是数值参数，无法扫描")

    raw = spec.get("values")
    if raw:
        values = list(raw)
    else:
        lo = float(spec.get("from", param_spec.get("min", 0)))
        hi = float(spec.get("to", param_spec.get("max", 1)))
        count = int(spec.get("count", 5))
        if count < 2:
            raise ValueError(f"参数 {param_spec.get('label')} 至少需要 2 个取值")
        if hi < lo:
            lo, hi = hi, lo
        step = (hi - lo) / (count - 1)
        values = [lo + i * step for i in range(count)]

    lo, hi = param_spec.get("min"), param_spec.get("max")
    out: List[Any] = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"参数 {param_spec.get('label')} 的取值 {v!r} 不是数字")
        if lo is not None:
            f = max(float(lo), f)
        if hi is not None:
            f = min(float(hi), f)
        out.append(int(round(f)) if ptype == "int" else round(f, 6))

    deduped: List[Any] = []
    for v in out:
        if v not in deduped:
            deduped.append(v)
    return deduped


def resolve_mode(requested: str, n_params: int) -> str:
    """Resolve the scan mode: ``curve`` (1 param), ``grid`` (2-param full
    factorial heatmap) or ``oat`` (one-at-a-time screening, any count)."""
    if requested in ("curve", "grid", "oat"):
        if requested == "grid" and n_params != 2:
            raise ValueError("网格扫描（热力图）需要恰好 2 个参数")
        if requested == "curve" and n_params != 1:
            raise ValueError("单参数曲线扫描需要恰好 1 个参数")
        return requested
    if n_params == 1:
        return "curve"
    if n_params == 2:
        return "grid"
    return "oat"


def create_sweep(scene: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate ``spec`` against the scene's catalog and build the sweep dict.

    Raises :class:`ValueError` with a human-readable message on bad input.
    """
    domain, model = scene["domain"], scene["model"]
    param_specs = {p["key"]: p for p in catalog.model_params(domain, model)}

    raw_params = spec.get("params") or []
    if not 1 <= len(raw_params) <= MAX_PARAMS:
        raise ValueError(f"请选择 1–{MAX_PARAMS} 个扫描参数")

    params: List[Dict[str, Any]] = []
    seen = set()
    for rp in raw_params:
        key = str(rp.get("key", ""))
        ps = param_specs.get(key)
        if ps is None:
            raise ValueError(f"模型 {domain}/{model} 下不存在参数: {key}")
        if key in seen:
            raise ValueError(f"参数 {ps['label']} 重复选择")
        seen.add(key)
        values = expand_values(rp, ps)
        if len(values) < 2:
            raise ValueError(f"参数 {ps['label']} 至少需要 2 个不同取值")
        if len(values) > MAX_VALUES_PER_PARAM:
            raise ValueError(
                f"参数 {ps['label']} 取值过多（{len(values)} 个，上限 "
                f"{MAX_VALUES_PER_PARAM}）；请增大取值间隔")
        params.append({"key": key, "label": ps["label"], "values": values})

    mode = resolve_mode(str(spec.get("mode", "auto")), len(params))

    metric_keys = [m["key"] for m in catalog.CATALOG[domain]["metrics"]]
    metrics = [m for m in (spec.get("metrics") or []) if m in metric_keys]
    if not metrics:
        metrics = metric_keys[:1]

    steps = max(1, min(MAX_STEPS, int(spec.get("steps", 200))))
    replicates = max(1, min(MAX_REPLICATES, int(spec.get("replicates", 1))))
    base_config = models.resolve_config(models.Scene.from_dict(scene))

    sweep: Dict[str, Any] = {
        "id": util.new_id("sweep"),
        "name": str(spec.get("name") or "敏感性扫描"),
        "scene_id": scene["id"],
        "scene_name": scene.get("name", ""),
        "domain": domain,
        "model": model,
        "mode": mode,
        "params": params,
        "metrics": metrics,
        "steps": steps,
        "replicates": replicates,
        "seed": int(spec.get("seed", base_config.get("seed", 0)) or 0),
        "base_config": base_config,
        "status": "pending",      # pending | running | finished | stopped | error
        "error": "",
        "total": 0,
        "done": 0,
        "failed": 0,
        "points": [],
        "created_at": util.now_iso(),
        "updated_at": util.now_iso(),
        "started_at": "",
        "finished_at": "",
    }
    total = len(build_plan(sweep))
    if total > MAX_POINTS:
        raise ValueError(
            f"共需运行 {total} 组，超过上限 {MAX_POINTS}；请减少取值个数 / 重复次数，"
            f"或在多参数时改用逐参数（OAT）模式")
    sweep["total"] = total
    return sweep


def build_plan(sweep: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Expand the sweep into the concrete list of combinations to run.

    OAT mode runs one shared baseline (all parameters at the scene's configured
    values) plus each parameter value on its own, so the cost grows linearly;
    values equal to the baseline are skipped because the baseline already
    covers them.  ``curve``/``grid`` modes run the full cartesian product.
    """
    params = sweep["params"]
    reps = range(int(sweep.get("replicates", 1)))
    plan: List[Dict[str, Any]] = []
    if sweep["mode"] == "oat":
        base = sweep.get("base_config", {})
        for r in reps:
            plan.append({"values": {}, "replicate": r})
        for p in params:
            base_v = base.get(p["key"])
            for v in p["values"]:
                if base_v is not None and _fnum(v) == _fnum(base_v):
                    continue  # already covered by the shared baseline
                for r in reps:
                    plan.append({"values": {p["key"]: v}, "replicate": r})
    else:
        keys = [p["key"] for p in params]
        for combo in itertools.product(*(p["values"] for p in params)):
            for r in reps:
                plan.append({"values": dict(zip(keys, combo)), "replicate": r})
    return plan


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #
def summarize_metrics(series: List[Dict[str, Any]],
                      keys: List[str]) -> Dict[str, Dict[str, Any]]:
    """Distil a run's series into per-metric final/peak/mean aggregates."""
    out: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        vals = [(r.get("step", i), r[key]) for i, r in enumerate(series)
                if isinstance(r.get(key), (int, float))]
        if not vals:
            continue
        vs = [v for _, v in vals]
        out[key] = {
            "final": vs[-1],
            "peak": max(vs),
            "mean": sum(vs) / len(vs),
            "peak_step": max(vals, key=lambda p: p[1])[0],
        }
    return out


def _combo_label(sweep: Dict[str, Any], combo: Dict[str, Any]) -> str:
    if combo["values"]:
        label = ", ".join(f"{k}={v}" for k, v in combo["values"].items())
    else:
        label = "基线"
    if int(sweep.get("replicates", 1)) > 1:
        label += f" · 重复{combo['replicate'] + 1}"
    return label


def _run_point(sweep: Dict[str, Any], combo: Dict[str, Any]) -> Dict[str, Any]:
    """Run one combination to completion; never raises.

    The point records its own duration and actual steps run, so combinations
    with very different costs are individually accounted for.  Any failure
    (bad config, engine error, …) is captured on the point and the scan moves
    on to the next combination.
    """
    values = combo["values"]
    cfg = {**sweep.get("base_config", {}), **values}
    seed = int(sweep.get("seed", 0)) + combo["replicate"]
    label = _combo_label(sweep, combo)
    point: Dict[str, Any] = {
        "label": f"{sweep['name']} · {label}",
        "values": values,
        "replicate": combo["replicate"],
        "seed": seed,
        "status": "ok",
        "run_id": "",
        "steps_run": 0,
        "duration_ms": 0,
        "metrics": {},
        "error": "",
    }
    t0 = time.perf_counter()
    try:
        scene = models.Scene.from_dict({
            "id": sweep["scene_id"],
            "name": point["label"],
            "domain": sweep["domain"],
            "model": sweep["model"],
            "config": cfg,
            "interventions": [],
        })
        meta = manager.create_run(
            scene, name=point["label"], seed=seed,
            snapshot_interval=max(1, int(sweep["steps"]) // 20))
        point["run_id"] = meta["id"]
        result = manager.run_batch(meta["id"], int(sweep["steps"]),
                                   keep_engine=False)
        point["steps_run"] = result["step"]
        point["metrics"] = summarize_metrics(
            storage.load_series(meta["id"]), sweep["metrics"])
    except Exception as exc:  # noqa: BLE001 — failure must not kill the scan
        point["status"] = "error"
        point["error"] = str(exc) or exc.__class__.__name__
        if point["run_id"]:
            # Keep the partial run inspectable, but mark it failed and drop
            # its engine so repeated failures cannot leak memory.
            try:
                meta = storage.load_run_meta(point["run_id"])
                if meta:
                    meta["status"] = "error"
                    meta["updated_at"] = util.now_iso()
                    storage.save_run_meta(meta)
                manager.unload(point["run_id"])
            except Exception:  # noqa: BLE001
                pass
    point["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    return point


def execute(sweep_id: str) -> None:
    """Worker: run the whole plan, persisting progress after every point."""
    sweep = storage.load_sweep(sweep_id)
    if sweep is None:
        return
    try:
        plan = build_plan(sweep)
        sweep["total"] = len(plan)
        sweep["status"] = "running"
        sweep["started_at"] = util.now_iso()
        storage.save_sweep(sweep)

        for combo in plan:
            if _is_aborted(sweep_id) or not _exists(sweep_id):
                break
            point = _run_point(sweep, combo)
            sweep["points"].append(point)
            sweep["done"] += 1
            if point["status"] != "ok":
                sweep["failed"] += 1
            sweep["updated_at"] = util.now_iso()
            if not _exists(sweep_id):
                # Deleted mid-scan: drop the just-finished (unpersisted) run
                # as well and stop without resurrecting the sweep file.
                if point.get("run_id"):
                    try:
                        manager.delete_run(point["run_id"])
                    except Exception:  # noqa: BLE001
                        pass
                break
            storage.save_sweep(sweep)

        if not _exists(sweep_id):
            return
        if _is_aborted(sweep_id):
            sweep["status"] = "stopped"
        elif sweep["done"] > 0 and sweep["failed"] == sweep["done"]:
            sweep["status"] = "error"
            first = sweep["points"][0].get("error", "") if sweep["points"] else ""
            sweep["error"] = "所有取值组合均运行失败" + (f"（如：{first}）" if first else "")
        else:
            sweep["status"] = "finished"
        sweep["finished_at"] = util.now_iso()
        sweep["updated_at"] = sweep["finished_at"]
        storage.save_sweep(sweep)
    except Exception as exc:  # noqa: BLE001
        sweep["status"] = "error"
        sweep["error"] = str(exc)
        if _exists(sweep_id):
            storage.save_sweep(sweep)
    finally:
        with _LOCK:
            _THREADS.pop(sweep_id, None)
            _ABORT.discard(sweep_id)


def _is_aborted(sweep_id: str) -> bool:
    with _LOCK:
        return sweep_id in _ABORT


def _exists(sweep_id: str) -> bool:
    """False once the sweep file was deleted (worker must stop and not save)."""
    return os.path.exists(storage.sweep_path(sweep_id))


def _is_alive(sweep_id: str) -> bool:
    with _LOCK:
        thread = _THREADS.get(sweep_id)
        return bool(thread and thread.is_alive())


def start_sweep(sweep_id: str) -> None:
    """Launch the background worker for a freshly created sweep."""
    thread = threading.Thread(target=execute, args=(sweep_id,), daemon=True)
    with _LOCK:
        _THREADS[sweep_id] = thread
    thread.start()


def stop_sweep(sweep_id: str) -> None:
    """Ask a running sweep to stop after its current combination."""
    with _LOCK:
        _ABORT.add(sweep_id)


def refresh_status(sweep: Dict[str, Any]) -> Dict[str, Any]:
    """Mark a sweep ``stopped`` if its worker vanished (e.g. server restart)."""
    if sweep.get("status") in ("pending", "running") and not _is_alive(sweep["id"]):
        sweep["status"] = "stopped"
        sweep["updated_at"] = util.now_iso()
        storage.save_sweep(sweep)
    return sweep


def delete_sweep(sweep_id: str, keep_runs: bool = False) -> bool:
    """Delete a sweep and (by default) all runs it created."""
    stop_sweep(sweep_id)
    sweep = storage.load_sweep(sweep_id)
    if sweep is None:
        return False
    if not keep_runs:
        for point in sweep.get("points", []):
            run_id = point.get("run_id")
            if run_id:
                try:
                    manager.delete_run(run_id)
                except Exception:  # noqa: BLE001
                    pass
    return storage.delete_sweep(sweep_id)


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #
def _curve_for(sweep: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Bucket points by one parameter's value and aggregate each metric."""
    mode = sweep.get("mode", "curve")
    base = sweep.get("base_config", {})
    buckets: Dict[Any, Dict[str, Any]] = {}
    for pt in sweep.get("points", []):
        if mode == "oat":
            if set(pt["values"]) - {key}:
                continue  # varies a different parameter
            value = pt["values"].get(key, base.get(key))
        else:
            if key not in pt["values"]:
                continue
            value = pt["values"][key]
        if value is None:
            continue
        bucket = buckets.setdefault(_fnum(value), {"ok": [], "failed": 0})
        if pt["status"] == "ok":
            bucket["ok"].append(pt)
        else:
            bucket["failed"] += 1

    curve: Dict[str, Any] = {}
    for metric in sweep["metrics"]:
        for agg in AGGREGATES:
            rows = []
            for value in sorted(buckets):
                bucket = buckets[value]
                vals = [pt["metrics"][metric][agg] for pt in bucket["ok"]
                        if agg in pt.get("metrics", {}).get(metric, {})]
                if not vals:
                    continue
                rows.append({
                    "value": value,
                    "mean": sum(vals) / len(vals),
                    "min": min(vals),
                    "max": max(vals),
                    "n": len(vals),
                    "failed": bucket["failed"],
                })
            curve.setdefault(metric, {})[agg] = rows
    return curve


def _sensitivity(curve: Dict[str, Any]) -> Dict[str, Any]:
    """Normalised range of each metric's mean curve (bigger = more sensitive)."""
    out: Dict[str, Any] = {}
    for metric, aggs in curve.items():
        for agg, rows in aggs.items():
            if len(rows) < 2:
                out.setdefault(metric, {})[agg] = None
                continue
            means = [r["mean"] for r in rows]
            rng = max(means) - min(means)
            scale = max(abs(max(means)), abs(min(means)))
            lo_row = min(rows, key=lambda r: r["mean"])
            hi_row = max(rows, key=lambda r: r["mean"])
            out.setdefault(metric, {})[agg] = {
                "range": rng,
                "norm": rng / (scale + 1e-9),
                "min_at": lo_row["value"],
                "max_at": hi_row["value"],
            }
    return out


def _heatmap(sweep: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Full 2-parameter grid: mean aggregate per (x, y) cell + failure counts."""
    if sweep.get("mode") != "grid" or len(sweep.get("params", [])) != 2:
        return None
    kx, ky = sweep["params"][0]["key"], sweep["params"][1]["key"]
    cells_by: Dict[Any, Dict[str, Any]] = {}
    for pt in sweep.get("points", []):
        if kx not in pt["values"] or ky not in pt["values"]:
            continue
        cell = cells_by.setdefault(
            (_fnum(pt["values"][kx]), _fnum(pt["values"][ky])),
            {"ok": [], "failed": 0})
        if pt["status"] == "ok":
            cell["ok"].append(pt)
        else:
            cell["failed"] += 1

    xs = sorted({c[0] for c in cells_by})
    ys = sorted({c[1] for c in cells_by})
    grids: Dict[str, Any] = {}
    for metric in sweep["metrics"]:
        for agg in AGGREGATES:
            cells = []
            for (x, y), cell in sorted(cells_by.items()):
                vals = [pt["metrics"][metric][agg] for pt in cell["ok"]
                        if agg in pt.get("metrics", {}).get(metric, {})]
                cells.append({
                    "x": x, "y": y,
                    "mean": (sum(vals) / len(vals)) if vals else None,
                    "n": len(vals),
                    "failed": cell["failed"],
                })
            grids.setdefault(metric, {})[agg] = cells
    return {"x_key": kx, "y_key": ky,
            "x_label": sweep["params"][0]["label"],
            "y_label": sweep["params"][1]["label"],
            "x_values": xs, "y_values": ys, "grids": grids}


def analyze(sweep: Dict[str, Any]) -> Dict[str, Any]:
    """Compute curves / heatmap / sensitivity ranking from the raw points."""
    points = sweep.get("points", [])
    ok = [p for p in points if p["status"] == "ok"]
    durations = [p.get("duration_ms", 0) for p in points]
    steps_run = [p.get("steps_run", 0) for p in ok]

    curves: Dict[str, Any] = {}
    sensitivity: Dict[str, Any] = {}
    for p in sweep.get("params", []):
        curve = _curve_for(sweep, p["key"])
        curves[p["key"]] = curve
        sensitivity[p["key"]] = _sensitivity(curve)

    return {
        "summary": {
            "total": sweep.get("total", 0),
            "done": sweep.get("done", 0),
            "ok": len(ok),
            "failed": sweep.get("failed", 0),
            "elapsed_ms": sum(durations),
            "avg_ms": (sum(durations) / len(durations)) if durations else 0,
            "steps_min": min(steps_run) if steps_run else 0,
            "steps_max": max(steps_run) if steps_run else 0,
        },
        "curves": curves,
        "sensitivity": sensitivity,
        "heatmap": _heatmap(sweep),
        "failed_points": [
            {"label": p["label"], "values": p["values"], "error": p["error"]}
            for p in points if p["status"] != "ok"
        ],
    }
