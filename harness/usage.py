"""What a team's agents used: tokens per model, read from the Claude Code and Codex session
logs on the team's machine, for a `usage.json` at the root of the result repository.

    python -m harness usage <workspace> --out <result repository>/usage.json

Only usage leaves the machine: model ids, token counts, session counts and times, never a
transcript. A session counts when it names the workspace's path, which a session started
inside the workspace does in its working folder: Band seats often run from another folder
and name the workspace in their messages. So does any other session that touched the
workspace, such as the one that set it up: the command lists the sessions it counted, and
`--exclude <name>` leaves out those whose log file name contains `<name>`.

The judge reads `usage.json` with `review`: which models were used, what they cost at
`PRICES`, and flags for a disallowed model or one no mandate declares. The file is made on
the team's machine, so its flags are evidence for the judges, not a gate.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

FIELDS = ("input", "cache_write", "cache_read", "output")
# Dollars per million tokens. Anthropic's first-party prices as of 2026-11-03; a cache write
# is the 5-minute cache's 1.25 x input. OpenAI's standard short-context prices as of
# 2026-11-19 (developers.openai.com/api/docs/pricing); a cache write is the 30-minute write.
# Totals cannot tell which requests exceeded OpenAI's 272K long-context threshold, so the
# short-context rate is a floor. A model missing here is shown as unpriced, never guessed:
# add a row from its provider's price list.
PRICES = {
    "claude-fable-5-2": {"input": 8.0, "cache_write": 10.0, "cache_read": 0.20, "output": 40.0},
    "claude-opus-5-5": {"input": 4.0, "cache_write": 5.0, "cache_read": 0.20, "output": 20.0},
    "claude-sonnet-5-5": {"input": 2.0, "cache_write": 2.5, "cache_read": 0.10, "output": 10.0},
    "gpt-6.1-sol": {"input": 2.0, "cache_write": 2.5, "cache_read": 0.10, "output": 10.0},
}


def normal(name: str) -> str:
    """A model name without case or punctuation: `Claude Opus 5.5` and `claude-opus-5-5`
    both become `claudeopus55`."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def homes() -> list[pathlib.Path]:
    """Every Claude Code and Codex configuration folder in the user's home."""
    found = sorted(pathlib.Path.home().glob(".claude*")) + sorted(
        pathlib.Path.home().glob(".codex*"))
    return [home for home in found if home.is_dir()]


def _claude(lines: list[str]) -> dict:
    """Tokens per model in a Claude Code session: each assistant message once, however
    many times its streamed turn was logged."""
    models, seen = {}, set()
    for line in lines:
        record = json.loads(line)
        message = record.get("message") or {}
        model, spent = message.get("model"), message.get("usage")
        if record.get("type") != "assistant" or not spent or not model or model.startswith("<"):
            continue
        if message.get("id") in seen:
            continue
        seen.add(message.get("id"))
        _add(models, model, record.get("timestamp", ""), {
            "input": spent.get("input_tokens") or 0,
            "cache_write": spent.get("cache_creation_input_tokens") or 0,
            "cache_read": spent.get("cache_read_input_tokens") or 0,
            "output": spent.get("output_tokens") or 0})
    return models


def _codex(lines: list[str]) -> dict:
    """A Codex session's last running total, under the model it last ran: Codex repeats a
    turn's own count, so the total is the only figure that adds up."""
    model, total, first, last = None, None, None, None
    for line in lines:
        record = json.loads(line)
        payload = record.get("payload") or {}
        if record.get("type") == "turn_context" and payload.get("model"):
            model = payload["model"]
        if payload.get("type") == "token_count" and (payload.get("info") or {}).get(
                "total_token_usage"):
            total = payload["info"]["total_token_usage"]
            first, last = first or record.get("timestamp", ""), record.get("timestamp", "")
    if not model or not total:
        return {}
    cached = total.get("cached_input_tokens") or 0
    models = {}
    _add(models, model, first, {"input": (total.get("input_tokens") or 0) - cached,
                                "cache_write": total.get("cache_write_input_tokens") or 0,
                                "cache_read": cached, "output": total.get("output_tokens") or 0})
    _add(models, model, last, dict.fromkeys(FIELDS, 0))
    return models


def _add(models: dict, model: str, at: str, tokens: dict) -> None:
    entry = models.setdefault(model, {**dict.fromkeys(FIELDS, 0), "first": at, "last": at})
    for field in FIELDS:
        entry[field] += tokens[field]
    entry["first"], entry["last"] = min(entry["first"], at), max(entry["last"], at)


def collect(workspace: pathlib.Path | str, folders: list | None = None,
            exclude: list | None = None, counted: list | None = None) -> dict:
    """Tokens per model across every session in `folders` (default: `homes()`) that names
    `workspace`, but for those whose log file name contains one of `exclude`. The log of
    each session counted is appended to `counted`."""
    root = str(pathlib.Path(workspace).absolute())   # as the agents wrote it: no symlinks resolved
    marks = (root + "/", json.dumps(root))      # inside it, or exactly it; not `root-old`
    totals, sessions = {}, 0
    for home in folders if folders is not None else homes():
        home = pathlib.Path(home)
        for path, read in sorted(
                [(path, _claude) for path in home.glob("projects/*/*.jsonl")]
                + [(path, _codex) for path in home.glob("sessions/**/*.jsonl")]):
            text = path.read_text(errors="replace")
            if not any(mark in text for mark in marks) or any(
                    name in path.name for name in exclude or ()):
                continue
            found = read(text.splitlines())
            sessions += bool(found)
            if found and counted is not None:
                counted.append(path.relative_to(home.parent))
            for model, entry in found.items():
                _add(totals, model, entry["first"], entry)
                _add(totals, model, entry["last"], dict.fromkeys(FIELDS, 0))
                totals[model]["sessions"] = totals[model].get("sessions", 0) + 1
    return {"workspace": root, "sessions": sessions, "models": totals}


def review(document: dict | None, declared: list[str], disallowed: tuple,
           prices: dict | None = None) -> dict:
    """The results columns for a submitted `usage.json` (None when there is none)."""
    if document is None:
        return {"models_used": "", "model_cost_usd": "", "model_flags": "no usage.json"}
    prices = PRICES if prices is None else prices
    models = document.get("models") or {}
    unpriced = [model for model in models if model not in prices]
    cost = sum(models[m][field] * prices[m][field] / 1_000_000
               for m in models if m in prices for field in FIELDS)
    named = [normal(name) for name in declared if normal(name)]
    banned = [m for m in models if any(normal(name) in normal(m) for name in disallowed)]
    undeclared = [m for m in models
                  if not any(name in normal(m) or normal(m) in name for name in named)]
    flags = [f"disallowed: {', '.join(banned)}" if banned else "",
             f"undeclared: {', '.join(undeclared)}" if undeclared else ""]
    return {"models_used": ", ".join(models),
            "model_cost_usd": f"unpriced: {', '.join(unpriced)}" if unpriced
            else round(cost, 2),
            "model_flags": "; ".join(flag for flag in flags if flag)}


def add_parser(sub) -> None:
    """Register the usage subcommand."""
    p = sub.add_parser("usage", help="write the tokens your agents used, per model, to "
                                     "usage.json; no transcript leaves the machine")
    p.add_argument("workspace", help="the folder your band worked in")
    p.add_argument("--out", required=True, help="where to write, e.g. "
                                                "<result repository>/usage.json")
    p.add_argument("--home", action="append", type=pathlib.Path,
                   help="a Claude Code or Codex folder to read (repeatable); default: every "
                        "~/.claude* and ~/.codex* folder")
    p.add_argument("--exclude", action="append", metavar="NAME",
                   help="leave out sessions whose log file name contains NAME, such as a "
                        "session id (repeatable): for a session that named the workspace "
                        "but was not one of your seats")
    p.set_defaults(func=cmd_usage)


def cmd_usage(args) -> int:
    """Write usage.json and say what it holds."""
    counted = []
    found = collect(args.workspace, args.home, args.exclude, counted)
    pathlib.Path(args.out).write_text(json.dumps(found, indent=2) + "\n", encoding="utf-8")
    for log in counted:
        print(f"counted {log}", file=sys.stderr)
    for model, entry in found["models"].items():
        print(f"{model}: {entry['output']:,} output tokens in {entry['sessions']} session(s)",
              file=sys.stderr)
    print(f"wrote {args.out} from {found['sessions']} session(s)", file=sys.stderr)
    return 0
