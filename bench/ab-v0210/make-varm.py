#!/usr/bin/env python3
"""Derive a V-ladder (RAM-relief) arm config from armP0.json (the adopted prod config).

Usage: make-varm.py <ARM> <extra engine args...>

P0's exe/env are kept; the extra args are merged into P0's args (an existing
"--flag value" has its value REPLACED, a boolean flag is appended only if absent),
and the log placeholder becomes logs/<ARM>-rROUND-engine.log so run-arm.sh's sed
rewrites it. Writes arm<ARM>.json next to this script.
"""
import json
import sys

ARM = sys.argv[1]
EXTRA = sys.argv[2:]
B = __file__.rsplit("/", 1)[0]


def merge(args, extra):
    out = list(args)
    i = 0
    while i < len(extra):
        tok = extra[i]
        takes_value = i + 1 < len(extra) and not extra[i + 1].startswith("--")
        if tok.startswith("--") and takes_value:
            val = extra[i + 1]
            i += 2
            if tok in out:
                out[out.index(tok) + 1] = val  # replace the existing value
            else:
                out += [tok, val]
        else:
            i += 1
            if tok not in out:
                out.append(tok)  # boolean flag
    return out


cfg = json.load(open(f"{B}/armP0.json"))
cfg["args"] = merge(cfg.get("args", []), EXTRA)
cfg["log"] = f"logs/{ARM}-rROUND-engine.log"
json.dump(cfg, open(f"{B}/arm{ARM}.json", "w"), indent=1)
print(f"arm{ARM}.json: merged {len(EXTRA)} extra arg(s): {' '.join(EXTRA)}")
