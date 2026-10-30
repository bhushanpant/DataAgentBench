"""Rollback guard: stop an arm whose accuracy falls past a margin below Scrybe alone."""
import json
from collections import defaultdict
from pathlib import Path

BASELINE_RUN = "dab-untuned-submit-v1"


def baseline(root: Path) -> dict[str, float]:
    """Task -> Scrybe-alone mean reward over its run_* verdicts under `root`."""
    rewards: dict[str, list[float]] = defaultdict(list)
    for f in sorted(Path(root).glob(f"{BASELINE_RUN}/*/*/run_*/.verdict.json")):
        task = f"{f.parents[2].name}/{f.parents[1].name}"
        rewards[task].append(float(json.loads(f.read_text())["reward"]))
    return {t: sum(v) / len(v) for t, v in rewards.items()}


def _macro(per_task: dict[str, float]) -> float:
    by_ds: dict[str, list[float]] = defaultdict(list)
    for t, r in per_task.items():
        by_ds[t.split("/", 1)[0]].append(r)
    return 100.0 * sum(sum(v) / len(v) for v in by_ds.values()) / len(by_ds)


def check(results: list[dict], base: dict[str, float], *, min_tasks: int = 12,
          min_datasets: int = 4, margin: float = 2.0) -> dict:
    """Compare arm to baseline on completed tasks present in `base` (points, 0-100)."""
    arm = {r["task"]: float(r["reward"]) for r in results if r["task"] in base}
    n = len(arm)
    datasets = len({t.split("/", 1)[0] for t in arm})
    if n == 0 or n < min_tasks or datasets < min_datasets:
        return {"status": "waiting", "arm_macro": None, "base_macro": None,
                "gap": None, "n": n, "datasets": datasets}
    arm_macro = _macro(arm)
    base_macro = _macro({t: base[t] for t in arm})
    gap = arm_macro - base_macro
    return {"status": "rollback" if gap < -margin else "ok", "arm_macro": arm_macro,
            "base_macro": base_macro, "gap": gap, "n": n, "datasets": datasets}


def enforce(arm_dir: Path, verdict: dict) -> None:
    """On rollback, write arm_dir/ROLLBACK.json so the runner stops scheduling new tasks."""
    if verdict.get("status") == "rollback":
        (Path(arm_dir) / "ROLLBACK.json").write_text(json.dumps(verdict, indent=2))
