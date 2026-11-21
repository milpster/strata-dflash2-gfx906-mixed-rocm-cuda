import json, urllib.request, sys

E = "/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/arena54-promote-park-20261120"
user1 = open(E + "/park-user1.txt").read()
d = json.load(open(E + "/park-turn1.json"))
assistant1 = d["choices"][0]["message"]["content"]
user2 = ("Please continue the previous answer, going even deeper into cache coherence protocols, "
         "memory disambiguation, store-to-load forwarding, and the trade-offs of wider issue queues. "
         "Add quantitative examples and keep it technical and exhaustive.")
messages = [
    {"role": "user", "content": user1},
    {"role": "assistant", "content": assistant1},
    {"role": "user", "content": user2},
]
body = json.dumps({"model": "ud-iq3_xxs", "messages": messages, "max_tokens": 3500, "temperature": 0.2}).encode()
req = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions",
                             data=body, headers={"Content-Type": "application/json"},
                             method="POST")
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        resp = json.loads(r.read())
    u = resp.get("usage", {})
    print("pre-park usage: prompt=%s completion=%s total=%s cached=%s" % (
        u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
        u.get("prompt_tokens_details", {}).get("cached_tokens")))
    # save exact conversation pieces
    json.dump({"user1": user1, "assistant1": assistant1, "user2": user2,
               "assistant2": resp["choices"][0]["message"]["content"], "usage": u},
              open(E + "/park-pre.json", "w"))
    print("saved park-pre.json; total conversation tokens now at/above", u.get("total_tokens"))
except Exception as ex:
    print("ERR", ex)
    sys.exit(1)
