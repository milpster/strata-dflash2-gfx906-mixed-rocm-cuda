#!/usr/bin/env python3
"""Aggregate per-file mapping residency (Size/Rss/Pss) from /proc/PID/smaps.

Usage: file_residency.py PID [PID...] [--match=substr1,substr2]

Read-only evidence tool for the expert-pack lock trial (2026-11-19).
Only file-backed mappings are reported; anonymous arenas are ignored.
"""
import re
import sys

HEADER = re.compile(r'^([0-9a-f]+)-([0-9a-f]+) \S+ \S+ \S+ \S+\s*(.*)$')


def smaps_files(pid):
    out = {}
    try:
        f = open(f'/proc/{pid}/smaps', 'r')
    except (FileNotFoundError, ProcessLookupError):
        return None
    with f:
        path = None
        for line in f:
            m = HEADER.match(line)
            if m:
                path = m.group(3).strip() or None
                continue
            if path and path.startswith('/'):
                for k in ('Size:', 'Rss:', 'Pss:'):
                    if line.startswith(k):
                        d = out.setdefault(path, {})
                        d[k] = d.get(k, 0) + int(line.split()[1])
    return out


def main():
    pids, matches = [], None
    for a in sys.argv[1:]:
        if a.startswith('--match='):
            matches = [s for s in a.split('=', 1)[1].split(',') if s]
        elif a.isdigit():
            pids.append(int(a))
    if not pids:
        sys.exit(__doc__)
    rows = []
    for pid in pids:
        files = smaps_files(pid)
        if files is None:
            print(f'pid {pid}: smaps unreadable (process gone?)')
            continue
        for path, d in files.items():
            if matches and not any(s in path for s in matches):
                continue
            rows.append((d.get('Rss:', 0), d.get('Size:', 0),
                         d.get('Pss:', 0), pid, path))
    rows.sort(reverse=True)
    print(f'{"Rss_MiB":>9} {"Size_MiB":>9} {"Pss_MiB":>9} {"Pid":>8}  Path')
    for rss, size, pss, pid, path in rows:
        if rss == 0 and size == 0:
            continue
        print(f'{rss/1024:9.1f} {size/1024:9.1f} {pss/1024:9.1f} {pid:8d}  {path}')
    tr = sum(r[0] for r in rows)
    ts = sum(r[1] for r in rows)
    print(f'TOTAL matched: Rss {tr/1024:.1f} MiB, mapped Size {ts/1024:.1f} MiB')


if __name__ == '__main__':
    main()
