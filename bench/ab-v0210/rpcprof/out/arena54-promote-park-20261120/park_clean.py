import json, urllib.request

E = "/mnt/srcds/Strata/bench/ab-v0210/rpcprof/out/arena54-promote-park-20261120"
userA = open(E + "/park-user1.txt").read()
body = json.dumps({"model": "ud-iq3_xxs", "messages": [{"role": "user", "content": userA}],
                   "max_tokens": 4000, "temperature": 0.2}).encode()
req = urllib.request.Request("http://127.0.0.1:8009/v1/chat/completions",
                             data=body, headers={"Content-Type": "application/json"},
                             method="POST")
with urllib.request.urlopen(req, timeout=200) as r:
    resp = json.loads(r.read())
assistantA = resp["choices"][0]["message"]["content"]
u = resp.get("usage", {})
print("clean-turn usage: prompt=%s completion=%s total=%s cached=%s finish=%s" % (
    u.get("prompt_tokens"), u.get("completion_tokens"), u.get("total_tokens"),
    u.get("prompt_tokens_details", {}).get("cached_tokens"),
    resp["choices"][0].get("finish_reason")))
json.dump({"userA": userA, "assistantA": assistantA, "usage": u}, open(E + "/park-conv.json", "w"))
print("saved park-conv.json; conversation total =", u.get("total_tokens"))
