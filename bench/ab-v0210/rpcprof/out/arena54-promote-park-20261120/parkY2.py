import json, urllib.request

E = "/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/arena54-promote-park-20261120"
userY2 = ("Describe the internals of a B-tree index as used in a relational database storage engine. "
          "Cover the node/page layout (keys, pointers, records, page header), the search and range-scan "
          "algorithms, insertion with node splits and split propagation, deletion with merging and "
          "redistribution, how the tree stays balanced, fill factor and page utilization, concurrency "
          "control (latch coupling, crabbing, index B-tree locks), logging and recovery (undo/redo for "
          "page splits), prefix compression, overflow pages for large values, and the trade-offs between "
          "B-trees and log-structured indexes for point lookups versus range scans. Be exhaustive.")
open(E + "/park-userY2.txt", "w").write(userY2)
body = json.dumps({"model": "ud-iq3_xxs", "messages": [{"role": "user", "content": userY2}],
                   "max_tokens": 1000, "temperature": 0.2}).encode()
req = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions",
                             data=body, headers={"Content-Type": "application/json"},
                             method="POST")
with urllib.request.urlopen(req, timeout=120) as r:
    resp = json.loads(r.read())
assistantY2 = resp["choices"][0]["message"]["content"]
u = resp.get("usage", {})
print("cleanY2 usage: prompt=%s completion=%s total=%s cached=%s finish=%s" % (
    u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
    u.get("prompt_tokens_details", {}).get("cached_tokens"),
    resp["choices"][0].get("finish_reason")))
json.dump({"userY2": userY2, "assistantY2": assistantY2, "usage": u}, open(E + "/park-convY2.json", "w"))
print("saved park-convY2.json; conversation total =", u.get("total_tokens"))
