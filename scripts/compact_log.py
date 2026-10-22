#!/usr/bin/env python3
"""Produce an optional overview from a saved log; never execute its command."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

PROGRAM = "/opt/homebrew/bin/rtk"
MAX_BYTES = 10 * 1024 * 1024
MIN_BYTES = 4096
TIMEOUT = 5
STATS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")


def counts(text):
    for line in reversed(text.splitlines()):
        pairs = STATS.findall(line)
        if pairs:
            return {label.rstrip("s") if label in ("errors", "warnings") else label: int(n) for n, label in pairs}
    return {}


def compact(raw, source_exit_code, kind):
    meta = {"source_exit_code": source_exit_code, "kind": kind,
            "filter_exit_code": None, "filter_stderr": "", "command": None,
            "raw_sha256": hashlib.sha256(raw).hexdigest(), "raw_bytes": len(raw)}

    def finish(data, reason):
        meta.update(reason=reason, compacted=reason == "compacted", output_bytes=len(data),
                    output_sha256=hashlib.sha256(data).hexdigest())
        return data, meta

    if source_exit_code != 0:
        return finish(raw, "source_failed_use_raw")
    if len(raw) < MIN_BYTES:
        return finish(raw, "small_use_raw")
    if len(raw) > MAX_BYTES:
        return finish(raw, "too_large_use_raw")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return finish(raw, "not_utf8_use_raw")
    if kind == "pytest":
        expected = counts(text)
        if not expected or any(expected.get(k, 0) for k in ("failed", "error", "warning")):
            return finish(raw, "unrecognised_or_problem_use_raw")
        if any(word in text for word in ("Traceback", "ImportError", "ERROR", "FAILED", "WARNING")):
            return finish(raw, "problem_use_raw")
    elif kind == "git-log":
        if not text.rstrip().endswith("---END---"):
            return finish(raw, "git_requires_markers_use_raw")
        blocks = [b.strip() for b in text.split("---END---") if b.strip()]
        expected = []
        for block in blocks:
            match = re.match(r"([0-9a-f]{7,40}) ", block)
            if not match:
                return finish(raw, "git_invalid_format_use_raw")
            expected.append(match.group(1))
        if not expected:
            return finish(raw, "git_empty_use_raw")
    else:
        return finish(raw, "unsupported_use_raw")
    meta["command"] = [PROGRAM, "pipe", "--filter", kind]
    env = dict(os.environ, RTK_TELEMETRY_DISABLED="1", RTK_SUPPRESS_HOOK_WARNING="1", RTK_RECALL="0")
    try:
        proc = subprocess.run(meta["command"], input=raw, capture_output=True, timeout=TIMEOUT, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        meta["filter_stderr"] = str(exc)
        return finish(raw, "tool_unavailable_use_raw")
    meta.update(filter_exit_code=proc.returncode, filter_stderr=proc.stderr.decode("utf-8", errors="replace"))
    if proc.returncode != 0:
        return finish(raw, "filter_failed_use_raw")
    try:
        summary = proc.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return finish(raw, "filter_not_utf8_use_raw")
    if kind == "pytest" and counts(summary) != expected:
        return finish(raw, "counts_changed_use_raw")
    if kind == "git-log":
        actual = re.findall(r"^([0-9a-f]{7,40}) ", summary, re.M)
        if actual != expected:
            return finish(raw, "commits_changed_use_raw")
    if not proc.stdout or len(proc.stdout) >= len(raw):
        return finish(raw, "no_reduction_use_raw")
    return finish(proc.stdout, "compacted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--source-exit-code", required=True, type=int)
    parser.add_argument("--kind", required=True, choices=["pytest", "git-log"])
    parser.add_argument("--purpose", required=True, choices=["overview"])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    source, out = args.input.resolve(), args.out.resolve()
    skill_root = Path(__file__).resolve().parents[1]
    if out == skill_root or skill_root in out.parents:
        parser.error("Results must be outside the skill source")
    if out.exists():
        parser.error("Output directory already exists; preserve it and choose a new path")
    if not source.is_file() or source.stat().st_size > MAX_BYTES:
        parser.error("Input must be a saved file of at most 10 MiB; inspect the original directly")
    raw = source.read_bytes()
    data, meta = compact(raw, args.source_exit_code, args.kind)
    out.mkdir(parents=True, exist_ok=False)
    (out / "summary.log").write_bytes(data)
    meta.update(input=str(source), purpose=args.purpose,
                source_exit_code_basis="caller_supplied; retain the original process receipt",
                semantics="overview only; no readiness or functional PASS")
    with (out / "result.json").open("x") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps({"result": str(out / "result.json"), "reason": meta["reason"],
                      "source_exit_code": args.source_exit_code}, ensure_ascii=False))
    return args.source_exit_code if 0 <= args.source_exit_code <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(main())
