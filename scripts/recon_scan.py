#!/usr/bin/env python3
"""
recon_scan.py -- Phase 0/1 recon helper for the rust-polygon-arbitrage-auditor skill.

This script does NOT find bugs and its output is NOT a verdict. It exists to make
Phase 0 (map the terrain) and Phase 1 (build the pyramid) faster and less error
prone than doing them by eye, by mechanically producing two things:

  1. A rough call graph: every `fn`/`async fn` in the project, and (heuristically,
     via regex -- this is not a real Rust parser, and it does not resolve modules,
     so two functions with the same name in different files will be conflated)
     which other project-local functions it calls. This sorts functions into
     "leaf" (calls no other local function -- audit these first, in Phase 2) and
     "composite" (calls other local functions -- audit these in Phase 3, after
     their dependencies are cleared).

  2. A signal report: every line that matches a pattern worth a second look --
     unwrap()/expect()/panic!, unsafe blocks, f32/f64 usage, swallowed Results,
     large/round numeric literals (candidate decimal-scaling constants), and risky
     numeric casts. Each hit is a LEAD, not a finding -- most will turn out to be
     fine. Treat this like a metal detector: it tells you where to dig. The
     digging -- actually reasoning about whether it's a real bug -- is still on you.

Usage:
    python3 recon_scan.py /path/to/rust/project [--out recon_report.json]

No third-party dependencies -- stdlib only, so it runs anywhere Python 3 does.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

SKIP_DIRS = {"target", ".git", "node_modules", ".cargo"}

FN_RE = re.compile(
    r'^\s*(?P<pub>pub(?:\([^)]*\))?\s+)?(?P<async>async\s+)?fn\s+'
    r'(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>]*>)?\s*\('
)

MONEY_WORDS = re.compile(
    r'price|amount|balance|profit|value|reserve|liquidity|decimals?|'
    r'slippage|gas|cost|revenue|fee|payout|proceeds',
    re.IGNORECASE,
)

SIGNAL_PATTERNS = {
    "unwrap_expect": re.compile(r'\.unwrap\(\)|\.expect\('),
    "panics": re.compile(r'\bpanic!\(|\bunreachable!\(|\btodo!\(|\bunimplemented!\('),
    "unsafe_blocks": re.compile(r'\bunsafe\b'),
    "float_types": re.compile(r'\bf32\b|\bf64\b'),
    "error_swallowing": re.compile(r'\.ok\(\)\s*;|\.unwrap_or_default\(\)|^\s*let\s+_\s*='),
    "risky_casts": re.compile(
        r'\bas\s+f32\b|\bas\s+f64\b|\bas\s+u128\b|\bas\s+u64\b|\bas\s+i64\b|\bas\s+u32\b'
    ),
    "scale_constants": re.compile(
        r'10u128\.pow\(|10_u128\.pow\(|10i128\.pow\(|10u64\.pow\(|1e6\b|1e8\b|1e9\b|1e18\b|\b\d{7,}\b'
    ),
}


def strip_line_comment(line):
    """Best-effort: drop a trailing `// ...` comment so it doesn't trigger pattern
    matches. Not string-literal-aware, so a `//` inside a string will truncate the
    line early -- acceptable for a lead-generating tool, not for a compiler."""
    idx = line.find('//')
    return line if idx == -1 else line[:idx]


def find_matching_brace(lines, start_line_idx, start_col):
    """
    Walk forward from (start_line_idx, start_col) -- which must point at an
    opening '{' -- tracking a simple depth counter, and return the (line_idx, col)
    of the matching '}'. Skips over the contents of double-quoted strings and
    char literals so braces inside log messages/format strings don't confuse the
    count. Heuristic, not a full lexer (doesn't special-case raw strings r"...",
    byte strings, etc.) -- good enough for finding the end of a typical function.
    """
    depth = 0
    in_string = False
    in_char = False
    i, j = start_line_idx, start_col
    n = len(lines)
    while i < n:
        line = lines[i]
        while j < len(line):
            c = line[j]
            if in_string:
                if c == '\\':
                    j += 2
                    continue
                if c == '"':
                    in_string = False
            elif in_char:
                if c == '\\':
                    j += 2
                    continue
                if c == "'":
                    in_char = False
            else:
                if c == '"':
                    in_string = True
                elif c == "'":
                    in_char = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        return i, j
            j += 1
        i += 1
        j = 0
    return None  # unbalanced -- give up gracefully


def extract_functions(rel_path, lines):
    """Return a list of dicts describing every `fn` with a body in this file."""
    functions = []
    for idx, raw_line in enumerate(lines):
        m = FN_RE.match(raw_line)
        if not m:
            continue

        # Find the opening brace, which may be a few lines below the signature
        # (multi-line generics / where-clauses / return types). If a bare ';'
        # shows up first, this is a signature-only declaration (trait method,
        # extern fn) with no body -- skip it rather than misattributing the
        # next function's opening brace to this one.
        brace_line, brace_col = None, None
        for look_ahead in range(idx, min(idx + 15, len(lines))):
            candidate = lines[look_ahead]
            brace_pos = candidate.find('{')
            semi_pos = candidate.find(';')
            if semi_pos != -1 and (brace_pos == -1 or semi_pos < brace_pos):
                break
            if brace_pos != -1:
                brace_line, brace_col = look_ahead, brace_pos
                break

        if brace_line is None:
            continue

        end = find_matching_brace(lines, brace_line, brace_col)
        if end is None:
            continue

        functions.append({
            "name": m.group("name"),
            "file": rel_path,
            "line": idx + 1,
            "is_pub": bool(m.group("pub")),
            "is_async": bool(m.group("async")),
            "body_start": brace_line,
            "body_end": end[0],
        })
    return functions


def scan_signals(rel_path, lines):
    hits = {key: [] for key in SIGNAL_PATTERNS}
    for idx, raw_line in enumerate(lines):
        line = strip_line_comment(raw_line)
        for key, pattern in SIGNAL_PATTERNS.items():
            if pattern.search(line):
                hits[key].append({
                    "file": rel_path,
                    "line": idx + 1,
                    "snippet": raw_line.strip()[:160],
                    "near_money_word": bool(MONEY_WORDS.search(raw_line)),
                })
    return hits


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("project_root", help="Path to the Rust project (or workspace) to scan")
    parser.add_argument("--out", default="recon_report.json", help="Write full JSON report to this path")
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    if not root.exists():
        print(f"error: {root} does not exist", file=sys.stderr)
        sys.exit(1)

    rs_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        for fname in filenames:
            if fname.endswith(".rs"):
                rs_files.append(Path(dirpath) / fname)

    all_functions = []
    all_signals = {key: [] for key in SIGNAL_PATTERNS}
    lines_by_abs_path = {}

    for f in rs_files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"warning: could not read {f}: {e}", file=sys.stderr)
            continue
        lines = text.splitlines()
        lines_by_abs_path[f] = lines
        rel = str(f.relative_to(root))
        all_functions.extend(extract_functions(rel, lines))
        file_signals = scan_signals(rel, lines)
        for key in all_signals:
            all_signals[key].extend(file_signals[key])

    # Build the naive call graph: for each function, search inside its own body
    # span for other known function names used as calls.
    fn_names = {fn["name"] for fn in all_functions}
    rel_to_abs = {str(f.relative_to(root)): f for f in rs_files}

    for fn in all_functions:
        lines = lines_by_abs_path[rel_to_abs[fn["file"]]]
        body_text = "\n".join(lines[fn["body_start"]: fn["body_end"] + 1])
        calls = {
            name for name in fn_names
            if name != fn["name"] and re.search(rf'\b{re.escape(name)}\s*\(', body_text)
        }
        fn["calls_local"] = sorted(calls)
        fn["classification"] = "composite" if calls else "leaf"
        del fn["body_start"]
        del fn["body_end"]

    leaves = [fn for fn in all_functions if fn["classification"] == "leaf"]
    composites = [fn for fn in all_functions if fn["classification"] == "composite"]

    report = {
        "project_root": str(root),
        "files_scanned": len(rs_files),
        "functions_found": len(all_functions),
        "functions": all_functions,
        "signals": all_signals,
    }

    Path(args.out).write_text(json.dumps(report, indent=2))

    print(f"Scanned {len(rs_files)} .rs files, found {len(all_functions)} functions.")
    print(f"  Leaf functions      (Phase 2 -- audit first):        {len(leaves)}")
    print(f"  Composite functions (Phase 3 -- audit after deps):   {len(composites)}")
    print()
    print("Signal counts (leads, not findings -- go verify each one):")
    for key, hits in all_signals.items():
        money_adjacent = sum(1 for h in hits if h["near_money_word"])
        extra = f", {money_adjacent} near a money-related name" if money_adjacent else ""
        print(f"  {key}: {len(hits)} hit(s){extra}")
    print(f"\nFull detail (file:line for every function and every hit) written to {args.out}")


if __name__ == "__main__":
    main()
