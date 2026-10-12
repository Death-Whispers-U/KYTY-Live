"""Finds the loops in a dumped shader and writes a copy whose chosen loops stop after a cap.

A diagnostic for GPU hangs: if a run survives with a loop capped, that loop was spinning.
The cap counts every pass through the loop's exit test in one shader invocation (across all
entries into the loop) and, past the limit, takes the exit. It changes the shader's results,
so a capped override is for finding a hang, never a fix.

Works on spirv-dis text (the .spvasm files in shaders/dump). Only loops whose exit is a single
`OpBranchConditional <cond> <merge> <continue>` (either order) can be capped; the exit test
then runs once per iteration.
"""

import re

_LOOP = re.compile(r"^\s*OpLoopMerge (%\S+) (%\S+)")
_FUNCTION = re.compile(r"^\s*%\S+ = OpFunction ")
_LABEL = re.compile(r"^\s*(%\S+) = OpLabel\s*$")
_BRANCH = re.compile(r"^(\s*)OpBranchConditional (%\S+) (%\S+) (%\S+)(.*)$")
_UINT = re.compile(r"^\s*(%\S+) = OpTypeInt 32 0\s*$")
_BOOL = re.compile(r"^\s*(%\S+) = OpTypeBool\s*$")


def _exit_branch(lines, start, merge, cont):
    """The conditional branch to `merge` that every iteration runs: the one ending the header,
    or the block the header falls into through unconditional OpBranch jumps."""
    labels = {}
    for i, line in enumerate(lines):
        m = _LABEL.match(line)
        if m:
            labels[m.group(1)] = i
    i, seen = start + 1, set()
    while i < len(lines) and i not in seen:
        seen.add(i)
        line = lines[i].strip()
        b = _BRANCH.match(lines[i])
        if b:
            return i if merge in (b.group(3), b.group(4)) else None
        if line.startswith("OpBranch "):
            target = line.split()[1]
            if target == merge or target not in labels:
                return None
            i = labels[target] + 1
            continue
        if line.startswith(("OpSelectionMerge", "OpLoopMerge", "OpSwitch", "OpReturn", "OpKill",
                            "OpUnreachable")):
            return None
        i += 1
    return None


def find_loops(text):
    """Every loop, in source order: index, 1-based line, merge/continue labels, and whether it
    leaves when its condition is true or false."""
    lines = text.splitlines()
    loops = []
    for i, line in enumerate(lines):
        m = _LOOP.match(line)
        if not m:
            continue
        merge, cont = m.groups()
        exit_at = _exit_branch(lines, i, merge, cont)
        loop = {"index": len(loops), "line": i + 1, "merge": merge, "continue": cont,
                "cappable": exit_at is not None, "exit_line": exit_at + 1 if exit_at is not None else None,
                "exit_when": None}
        if exit_at is not None:
            loop["exit_when"] = "true" if _BRANCH.match(lines[exit_at]).group(3) == merge else "false"
        loops.append(loop)
    return loops


def cap_loops(text, indices, limit):
    """Returns the shader text with the given loops capped at `limit` iterations per invocation."""
    if limit < 1:
        raise ValueError("the cap must be at least 1 iteration")
    loops = {l["index"]: l for l in find_loops(text)}
    for n in indices:
        if n not in loops:
            raise ValueError(f"there is no loop {n}; this shader has {len(loops)}")
        if not loops[n]["cappable"]:
            raise ValueError(f"loop {n} has no single exit test and cannot be capped")
    lines = text.splitlines()

    uint = bool_ = None
    for line in lines:
        uint = uint or (_UINT.match(line) and _UINT.match(line).group(1))
        bool_ = bool_ or (_BOOL.match(line) and _BOOL.match(line).group(1))
    first_function = next(i for i, l in enumerate(lines) if _FUNCTION.match(l))

    declarations = []
    if uint is None:
        uint = "%kl_uint"
        declarations.append(f"{uint} = OpTypeInt 32 0")
    if bool_ is None:
        bool_ = "%kl_bool"
        declarations.append(f"{bool_} = OpTypeBool")
    declarations += [f"%kl_ptr_uint = OpTypePointer Function {uint}",
                     f"%kl_uint_0 = OpConstant {uint} 0",
                     f"%kl_uint_1 = OpConstant {uint} 1",
                     f"%kl_limit = OpConstant {uint} {int(limit)}"]

    inserts = {}  # line index -> lines to insert before it
    replace = {}
    for n in sorted(set(indices)):
        loop = loops[n]
        at = loop["line"] - 1
        function = max(i for i in range(at) if _FUNCTION.match(lines[i]))
        entry = next(i for i in range(function + 1, len(lines)) if _LABEL.match(lines[i]))
        inserts.setdefault(entry + 1, []).append(
            f"%kl_count_{n} = OpVariable %kl_ptr_uint Function %kl_uint_0")

        b = loop["exit_line"] - 1
        indent, cond, t, f, rest = _BRANCH.match(lines[b]).groups()
        test = [f"%kl_old_{n} = OpLoad {uint} %kl_count_{n}",
                f"%kl_new_{n} = OpIAdd {uint} %kl_old_{n} %kl_uint_1",
                f"OpStore %kl_count_{n} %kl_new_{n}",
                f"%kl_capped_{n} = OpUGreaterThan {bool_} %kl_new_{n} %kl_limit"]
        if loop["exit_when"] == "true":
            test.append(f"%kl_cond_{n} = OpLogicalOr {bool_} {cond} %kl_capped_{n}")
        else:
            test += [f"%kl_stay_{n} = OpLogicalNot {bool_} %kl_capped_{n}",
                     f"%kl_cond_{n} = OpLogicalAnd {bool_} {cond} %kl_stay_{n}"]
        inserts.setdefault(b, []).extend(test)
        replace[b] = f"{indent}OpBranchConditional %kl_cond_{n} {t} {f}{rest}"

    inserts.setdefault(first_function, []).extend(declarations)
    out = []
    for i, line in enumerate(lines):
        out.extend(_pad(x) for x in inserts.get(i, []))
        out.append(replace.get(i, line))
    return "\n".join(out) + "\n"


def _pad(instruction):
    """Lines up a new instruction the way spirv-dis does (result ids right-aligned)."""
    if " = " in instruction:
        rid, rest = instruction.split(" = ", 1)
        return f"{rid:>12} = {rest}"
    return " " * 15 + instruction
