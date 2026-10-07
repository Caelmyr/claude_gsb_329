"""Headless command-line batch runner.

Run a scene from the terminal without the web UI — useful for quick parameter
sweeps and for verifying the simulation + storage layer independently:

    python3 cli.py --list
    python3 cli.py --scene scene_epidemic_abm --steps 200 --report
    python3 cli.py --scene scene_traffic_ca --steps 500 --snapshot-interval 5

Parameter sensitivity sweeps (mirrors the web page)::

    # 1-D curve: scan beta over 9 values, look at the peak infected count
    python3 cli.py --sweep scene_epidemic_abm --param beta --range 0 0.6 \
                   --count 9 --steps 150 --metric infected --agg max

    # 2-D heatmap: beta × gamma grid
    python3 cli.py --sweep scene_epidemic_abm \
                   --param beta --range 0 0.6 --count 5 \
                   --param gamma --range 0.02 0.2 --count 5 \
                   --steps 150 --metric infected --agg max
"""

from __future__ import annotations

import argparse
import sys
import time

from backend import models, report, run_manager, sensitivity, storage, util


def list_scenes() -> None:
    for s in storage.list_scenes():
        print(f"{s['id']:24s} {s['domain']:8s}/{s['model']:3s}  {s['name']}")


def run_one(scene_id: str, steps: int, snapshot_interval: int,
            make_report: bool) -> int:
    scene = storage.load_scene(scene_id)
    if scene is None:
        print(f"error: scene not found: {scene_id}", file=sys.stderr)
        return 1
    scene_obj = models.Scene.from_dict(scene)
    meta = run_manager.manager.create_run(
        scene_obj, snapshot_interval=snapshot_interval)
    print(f"run {meta['id']}: {meta['name']} "
          f"({meta['domain']}/{meta['model']}) seed={meta['seed']}")

    result = run_manager.manager.run_batch(meta["id"], steps, keep_engine=True)
    print(f"finished at step {result['step']}")
    for k, v in result["stats"].items():
        print(f"  {k:16s} {v}")

    if make_report:
        rpt = report.generate_report(meta["id"])
        print("\nsummary:")
        for line in rpt["summary"]:
            print(f"  - {line}")
    return 0


def run_sweep(args) -> int:
    scene = storage.load_scene(args.sweep)
    if scene is None:
        print(f"error: scene not found: {args.sweep}", file=sys.stderr)
        return 1
    if not args.param:
        print("error: --sweep needs at least one --param", file=sys.stderr)
        return 1
    if len(args.range) != len(args.param) or len(args.count) != len(args.param):
        print("error: give one --range and one --count per --param",
              file=sys.stderr)
        return 1

    params = []
    for key, rng, count in zip(args.param, args.range, args.count):
        params.append({"key": key, "mode": "linear",
                       "start": float(rng[0]), "end": float(rng[1]),
                       "count": int(count)})

    payload = {"scene_id": args.sweep, "steps": args.steps, "params": params,
               "metric": args.metric, "aggregation": args.agg,
               "replicates": args.replicates, "seed": args.seed}
    try:
        sweep = sensitivity.sweep_runner.create_sweep(payload)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"sweep {sweep['id']}: {sweep['total']} groups "
          f"× {sweep['replicates']} run(s) = "
          f"{sweep['total'] * sweep['replicates']} runs")
    sensitivity.sweep_runner.start(sweep["id"])

    while True:
        cur = storage.load_sweep(sweep["id"])
        print(f"\r  {cur['completed']}/{cur['total']} ok, "
              f"{cur['failed']} failed   ", end="", flush=True)
        if cur["status"] in ("finished", "partial", "stopped", "error"):
            break
        time.sleep(0.5)
    print()

    cur = storage.load_sweep(sweep["id"])
    print(f"status: {cur['status']}")
    if cur.get("error"):
        print(f"error: {cur['error']}", file=sys.stderr)
    for p in cur["points"]:
        if p["status"] == "failed":
            print(f"  FAILED  {p['error']}")
            continue
        val = p["aggregates"][cur["metric"]][cur["aggregation"]]
        label = ", ".join(f"{k}={v:g}" for k, v in p["param_values"].items())
        print(f"  {label:32s} {cur['metric']}.{cur['aggregation']}="
              f"{val:10.4g}  ({p['steps']} steps, {p['duration_s']:.1f}s"
              f"{' early-stop' if p['early_stopped'] else ''})")
    if cur["scan_type"] == "oat":
        print("\nsensitivity ranking (relative %):")
        for r in cur["ranking"]:
            print(f"  {r['label']:20s} {r['relative_pct']:8.1f}%")
    return 0 if cur["failed"] == 0 else 2


def main() -> int:
    p = argparse.ArgumentParser(description="Headless simulation batch runner")
    p.add_argument("--list", action="store_true", help="list available scenes")
    p.add_argument("--scene", help="scene id to run")
    p.add_argument("--steps", type=int, default=200, help="steps per run")
    p.add_argument("--snapshot-interval", type=int, default=1,
                   help="persist a full snapshot every N steps")
    p.add_argument("--report", action="store_true", help="generate a report")

    p.add_argument("--sweep", metavar="SCENE_ID",
                   help="run a parameter sensitivity sweep on a scene")
    p.add_argument("--param", action="append", default=[],
                   help="parameter key to scan (repeatable)")
    p.add_argument("--range", action="append", nargs=2, metavar=("START", "END"),
                   default=[], help="scan interval, one per --param")
    p.add_argument("--count", action="append", default=[],
                   help="number of scan values, one per --param")
    p.add_argument("--metric", default="",
                   help="key metric to summarise (domain default if omitted)")
    p.add_argument("--agg", default="final",
                   choices=["final", "mean", "max", "min"],
                   help="how to summarise the metric per run")
    p.add_argument("--replicates", type=int, default=1,
                   help="replicate runs per value (stochastic averaging)")
    p.add_argument("--seed", type=int, default=0,
                   help="base random seed; replicate r uses seed+r")
    args = p.parse_args()

    storage.ensure_dirs()
    if args.list:
        list_scenes()
        return 0
    if args.sweep:
        return run_sweep(args)
    if not args.scene:
        p.print_help()
        return 1
    return run_one(args.scene, args.steps, args.snapshot_interval, args.report)


if __name__ == "__main__":
    sys.exit(main())
