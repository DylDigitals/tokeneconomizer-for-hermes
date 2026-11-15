"""Summarize matched arms (sandbox/cmp-{off,on}-<rep>) from Hermes usage reports plus the
enabled arm's optimizer audit. Effective tokens use upstream's fallback weights
(`_vendor/tokeneconomizer/pricing.py`: input 1, cache read 0.10, cache write 1.25, output 5)."""
import glob, json, os, re

W = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sandbox")
LABELS = ["ctl", "t1", "t2", "t3", "t4", "t5", "t6", "loop", "t7"]
WT = dict(input=1.0, cache_read=0.10, cache_write=1.25, output=5.0)


def eff(d):
    return sum(WT[k] * d.get(k, 0) for k in WT)


def correct(lb, out):
    if lb == "ctl":
        return out.strip().startswith("4")
    if lb == "loop":
        return bool(re.search(r"fail|not succeed|never (exit|succeed)|did not", out, re.I))
    return out.strip().splitlines()[0].strip().rstrip(".") == "2" if out.strip() else False


arms = {}
for R in sorted(glob.glob(f"{W}/cmp-o*-*")):
    name = os.path.basename(R)
    a = dict(input=0, cache_read=0, cache_write=0, output=0, calls=0, elapsed=0.0, aux_eff=0.0,
             correct=0, runs=0, per_run=[])
    for lb in LABELS:
        u = json.load(open(f"{R}/runs/{lb}.usage.json"))
        m = json.load(open(f"{R}/runs/{lb}.meta.json"))
        out = open(f"{R}/runs/{lb}.out").read()
        d = dict(input=u["input_tokens"], cache_read=u["cache_read_tokens"],
                 cache_write=u["cache_write_tokens"], output=u["output_tokens"])
        for k, v in d.items():
            a[k] += v
        aux = u.get("auxiliary") or {}
        a["aux_eff"] += eff(dict(input=aux.get("input_tokens", 0), output=aux.get("output_tokens", 0),
                                 cache_read=aux.get("cache_read_tokens", 0),
                                 cache_write=aux.get("cache_write_tokens", 0)))
        a["calls"] += u["api_calls"]
        a["elapsed"] += float(m["elapsed_s"])
        ok = correct(lb, out)
        a["correct"] += ok
        a["runs"] += 1
        a["per_run"].append((lb, d["input"] + d["cache_read"] + d["cache_write"], u["api_calls"],
                             round(eff(d)), ok))
    opt = f"{R}/home/.tokeneconomizer/optimizer.jsonl"
    o = [json.loads(l) for l in open(opt)] if os.path.exists(opt) else []
    a["optimizer_calls"] = len(o)
    a["optimizer_eff"] = sum(float(x.get("effective_tokens") or 0) for x in o)
    a["optimizer_s"] = round(sum(float(x.get("elapsed_s") or 0) for x in o), 1)
    a["main_eff"] = round(eff(a))
    a["total_eff"] = round(a["main_eff"] + a["optimizer_eff"])
    a["prompt_tokens"] = a["input"] + a["cache_read"] + a["cache_write"]
    arms[name] = a

for name, a in arms.items():
    print(f"{name}: correct {a['correct']}/{a['runs']}  calls {a['calls']}  prompt {a['prompt_tokens']:,}"
          f" (uncached {a['input']:,}, cache read {a['cache_read']:,})  output {a['output']:,}"
          f"  main eff {a['main_eff']:,}  optimizer {a['optimizer_calls']} calls / {round(a['optimizer_eff']):,} eff"
          f" / {a['optimizer_s']} s  total eff {a['total_eff']:,}  elapsed {round(a['elapsed'])} s")
    print("   " + "  ".join(f"{lb}:{p:,}/{c}c{'' if ok else '✗'}" for lb, p, c, e, ok in a["per_run"]))
for kind in ("off", "on"):
    xs = [a for n, a in arms.items() if n.startswith(f"cmp-{kind}-")]
    if xs:
        mean = lambda k: sum(x[k] for x in xs) / len(xs)
        print(f"mean {kind} ({len(xs)}): prompt {mean('prompt_tokens'):,.0f}  main eff {mean('main_eff'):,.0f}"
              f"  total eff {mean('total_eff'):,.0f}  calls {mean('calls'):.1f}  elapsed {mean('elapsed'):.0f} s"
              f"  correct {mean('correct'):.1f}/9")
