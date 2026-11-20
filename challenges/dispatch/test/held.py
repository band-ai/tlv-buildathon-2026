"""The dispatch score of a stage: how far the service's held days land between the starting
policy (0) and every order placed delivered on time (100), which no day with too few couriers
allows.

The held days and the starting policy's value on each are in `reference/score-key.jsonl`,
written by `tools/key.py`. A day on which the service breaks a rule, answers late or
misreports what happened scores 0.
"""
from __future__ import annotations

import json
import pathlib

import world

# The organizers' copy, or the one a judges package carries.
KEY = pathlib.Path(__file__).resolve().parents[1] / "reference" / "score-key.jsonl"
if not KEY.is_file():
    KEY = pathlib.Path(__file__).resolve().parents[3] / "harness/keys/dispatch-score-key.jsonl"


def day_score(value: int, baseline: int, ceiling: int) -> float:
    """A day's value as a share of the way from `baseline` to `ceiling`, within 0..100."""
    return round(100 * min(1.0, max(0.0, (value - baseline) / (ceiling - baseline))), 1)


def score(base_url: str, stage: int, key: pathlib.Path = KEY) -> dict:
    """Play every held day of `stage` against the service at `base_url`."""
    days = []
    for line in key.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["stage"] != stage:
            continue
        day = world.make_day(row["seed"], orders=row["orders"], couriers=row["couriers"],
                             stage=stage)
        service = world.Http(base_url)
        entry = {"seed": row["seed"], "baseline": row["planned"],
                 "ceiling": sum(order["price"] for order in day["orders"])}
        played = None
        try:
            played = world.World(day, service, stage)
            played.play()
            world.audit(played)
            entry["value"] = played.result.value
            entry["score"] = day_score(entry["value"], row["planned"], entry["ceiling"])
        except (world.WorldError, world.httpx.HTTPError, KeyError, TypeError, ValueError) as error:
            entry.update(value=None, score=0.0, broke=f"{type(error).__name__}: {error}"[:300])
        entry.update(calls=service.calls, slowest_seconds=round(service.slowest, 3),
                     refused=played.result.refused if played else None)
        service.close()
        days.append(entry)
    return {"stage": stage, "days": days,
            "dispatch_score": round(sum(d["score"] for d in days) / len(days), 1)}
