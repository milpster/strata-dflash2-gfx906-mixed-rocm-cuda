    @staticmethod
    def disk_state() -> dict:
        """What the disk tier's card shows: the folder and its budget, what it holds, and its counters."""
        return {"enabled": False, "dir": None, "io": None, "budget_mib": None, "generations": 0, "bytes": 0,
                "sidecars": 0, "corrupt": 0, "foreign": 0, "parks": 0, "idle_saves": 0, "restores": 0,
                "evictions": 0, "superseded": 0, "refusals": 0, "draft_pair_hits": 0, "draft_rebuilds": 0,
                "last_event": None, "last_tokens": None, "last_ms": None, "last_at": None,
                "last_serial": None, "last_refusal": None}

    def snapshot(self) -> dict:
        """A copy the card may keep: the nested disk state is not shared with the live one."""
        return {**self.state, "disk": dict(self.state["disk"])}
