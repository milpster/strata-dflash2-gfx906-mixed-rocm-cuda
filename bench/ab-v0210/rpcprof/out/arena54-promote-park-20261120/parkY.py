import json, urllib.request

E = "/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/arena54-promote-park-20261120"
userY = ("Explain the design and operation of a distributed key-value store with strong consistency. "
         "Cover the data model and partitioning strategy, the consensus algorithm used for replication "
         "(leader election, log replication, commit rules), how linearizable reads are served, failure "
         "detection and membership changes, rebalancing and data movement, client SDK behavior, and "
         "operational concerns such as backups, repairs, and capacity planning. Be thorough and technical.")
open(E + "/park-userY.txt", "w").write(userY)
body = json.dumps({"model": "ud-iq3_xxs", "messages": [{"role": "user", "content": userY}],
                   "max_tokens": 2400, "temperature": 0.2}).encode()
req = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions",
                             data=body, headers={"Content-Type": "application/json"},
                             method="POST")
with urllib.request.urlopen(req, timeout=200) as r:
    resp = json.loads(r.read())
assistantY = resp["choices"][0]["message"]["content"]
u = resp.get("usage", {})
print("cleanY usage: prompt=%s completion=%s total=%s cached=%s finish=%s" % (
    u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
    u.get("prompt_tokens_details", {}).get("cached_tokens"),
    resp["choices"][0].get("finish_reason")))
json.dump({"userY": userY, "assistantY": assistantY, "usage": u}, open(E + "/park-convY.json", "w"))
print("saved park-convY.json; conversation total =", u.get("total_tokens"))
