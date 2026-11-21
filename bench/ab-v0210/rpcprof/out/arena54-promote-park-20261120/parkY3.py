import json, urllib.request

E = "/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/arena54-promote-park-20261120"
c = json.load(open(E + "/park-convY2.json"))
userY2, assistantY2 = c["userY2"], c["assistantY2"]
userZ2 = ("Now focus specifically on the concurrency protocol: explain latch coupling and crabbing in "
          "detail, including how a search acquires and releases latches top-down, how insertions handle "
          "splits without deadlocking, and what happens during a concurrent page split when another "
          "transaction is traversing the same path. Include recovery implications.")
messages = [
    {"role": "user", "content": userY2},
    {"role": "assistant", "content": assistantY2},
    {"role": "user", "content": userZ2},
]
body = json.dumps({"model": "ud-iq3_xxs", "messages": messages, "max_tokens": 1200, "temperature": 0.2}).encode()
req = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions",
                             data=body, headers={"Content-Type": "application/json"},
                             method="POST")
with urllib.request.urlopen(req, timeout=120) as r:
    resp = json.loads(r.read())
assistantZ2 = resp["choices"][0]["message"]["content"]
u = resp.get("usage", {})
print("cleanY3 usage: prompt=%s completion=%s total=%s cached=%s finish=%s" % (
    u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
    u.get("prompt_tokens_details", {}).get("cached_tokens"),
    resp["choices"][0].get("finish_reason")))
json.dump({"userY2": userY2, "assistantY2": assistantY2, "userZ2": userZ2,
           "assistantZ2": assistantZ2, "usage": u}, open(E + "/park-convY3.json", "w"))
print("saved park-convY3.json; conversation total =", u.get("total_tokens"))
