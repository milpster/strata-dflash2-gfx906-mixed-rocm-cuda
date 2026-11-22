    def _note_disk_line(self, line: str) -> None:
        """The disk tier's `DCACHE <phase> <done> <total> [<secs>]` progress line: disk bytes, so it is recorded
        for the card and never touches the prompt statistics.  A malformed one is ignored: the tier's own line is
        not an answer to anything, and a silent transfer is the watchdog's business, not a reason to kill a step."""
        f = line.split()
        if len(f) not in (4, 5) or f[1] not in DCACHE_PHASES:
            return
        try:
            done, total = int(f[2]), int(f[3])
            secs = float(f[4]) if len(f) == 5 else 0.0
        except ValueError:
            return
        if done > total or not math.isfinite(secs) or secs < 0.0:
            return
        self.disk_io = {"phase": f[1], "done": done, "total": total, "ms": round(secs * 1000.0, 1)}
