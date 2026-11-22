    def _disk_line(self, line: str, now: float) -> bool:
        """Fold one of the disk tier's log lines into the card's disk state; True when the line was one of them.
        The tier's park line reports what the tier holds AFTER the park, so its counts are the current ones;
        evictions and supersessions are per park and add up."""
        d = self.state["disk"]
        m = self.DISK_ON.search(line)
        if m:
            d.update(enabled=True, dir=m.group(1), io="direct" if m.group(2) == "direct I/O" else "buffered",
                     budget_mib=int(m.group(3)), generations=int(m.group(4)), bytes=int(m.group(5)) << 20,
                     sidecars=int(m.group(6)), corrupt=int(m.group(9)), foreign=int(m.group(10)),
                     last_event="open", last_at=now)
            return True
        m = self.DISK_OFF.search(line)
        if m:
            d.update(enabled=False, generations=0, bytes=0, sidecars=0, last_event="off", last_at=now,
                     last_refusal=self._refusal(m.group(1)))
            return True
        m = self.DISK_PARK.search(line)
        if m:
            d["parks"] += 1
            if m.group(2).strip() == "idle":
                d["idle_saves"] += 1
            d["generations"], d["bytes"], d["sidecars"] = int(m.group(5)), int(m.group(6)) << 20, int(m.group(9))
            d["evictions"] += int(m.group(7))
            d["superseded"] += int(m.group(8))
            serial = self.DISK_SERIAL.search(m.group(3))
            d["last_serial"] = int(serial.group(1)) if serial else None
            d.update(last_event="park", last_tokens=int(m.group(1)), last_ms=float(m.group(4)), last_at=now,
                     last_refusal=None)
            return True
        m = self.DISK_RESTORE.search(line)
        if m:
            d["restores"] += 1
            if "pair:" in m.group(4):
                d["draft_pair_hits"] += 1
            if "draft miss" in m.group(4):
                d["draft_rebuilds"] += 1
            d.update(last_event="restore", last_tokens=int(m.group(1)), last_ms=float(m.group(3)), last_at=now,
                     last_refusal=None)
            return True
        m = self.DISK_SKIP.search(line)
        if m:
            d["refusals"] += 1
            d.update(last_event="skip " + m.group(1), last_at=now, last_refusal=self._refusal(m.group(2)))
            return True
        return False

    @classmethod
    def _refusal(cls, why: str) -> str:
        """The tier's concise reason code ("quota", "free-space", "protected", ...); a caller's own words when
        the line names no code.  Never more than a short phrase."""
        m = cls.DISK_REFUSAL.search(why)
        return (m.group(1) if m else why.strip())[:120]
