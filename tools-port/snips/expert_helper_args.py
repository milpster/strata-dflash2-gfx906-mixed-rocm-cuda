def expert_helper_args(cfg: dict, args: list[str]) -> list[str]:
    """(opt-in, docs/MIXED_GPU_HELPER.md): "expert_helper_socket": "<path>" in the config starts the
    engine with --expert-helper-socket, the out-of-process CUDA expert helper's AF_UNIX channel.  The
    helper process itself is started BY HAND before the server (the engine never spawns it; absent, the
    engine warns and the CPU path serves its rows for the whole run).  Optional keys:
    "expert_helper_layers", "expert_helper_slots", "expert_helper_reserve_mib",
    "expert_helper_timeout".  Opt-in "expert_helper_socket2": "<path>" adds the prompt tier's own
    second channel (the helper's --socket2).  Without the keys - the default, every shipped config -
    the engine's arguments are the config's, unchanged.

    Like parallel_args/pool_cores_args this returns ONLY the new flags (the caller composes with
    `args += expert_helper_args(cfg, args)`): returning the accumulated list here would send the engine
    every argument TWICE - a repeated --lora loads the adapter twice, which is a different model, and
    accumulating flags lose last-value-wins."""
    sock = cfg.get("expert_helper_socket")
    if not isinstance(sock, str) or not sock.strip() or "--expert-helper-socket" in args:
        return []
    delta = ["--expert-helper-socket", sock.strip()]
    # Opt-in SECOND channel ("expert_helper_socket2": "<path>", the helper's --socket2): the prompt
    # tier's own socket, so the window tier and the prompt tier stop queueing behind each other's
    # in-flight request.  Absent (the default, every shipped config) the engine keeps one channel.
    sock2 = cfg.get("expert_helper_socket2")
    if (isinstance(sock2, str) and sock2.strip() and sock2.strip() != sock.strip()
            and "--expert-helper-socket2" not in args):
        delta += ["--expert-helper-socket2", sock2.strip()]
    for key, flag in (("expert_helper_layers", "--expert-helper-layers"),
                      ("expert_helper_slots", "--expert-helper-slots"),
                      ("expert_helper_reserve_mib", "--expert-helper-reserve-mib"),
                      ("expert_helper_timeout", "--expert-helper-timeout-ms")):
        v = cfg.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0 and flag not in args:
            delta += [flag, str(v)]
    return delta
