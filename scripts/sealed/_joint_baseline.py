"""Section 3.4 baselines (one-off, sealed because it reads the held-out cache): numbers only."""
import json, sys, collections
sys.path.insert(0, "scripts/sealed")
import heldout_matrix as hm
def ev(x):
    try: return eval(x) if isinstance(x,str) else x
    except Exception: return x
TASKS = sys.argv[1].split(",")
cs=[json.loads(l) for l in open('outputs/evolution/ledger/v1/candidates.jsonl')]
ms=[json.loads(l) for l in open('outputs/evolution/ledger/v1/milestones.jsonl')]
A1={(r["task"],r["milestone"]):r for r in json.load(open("outputs/author_eval/A1/records.s0.json"))}
ref_cache={}
def valid(rec):
    return set(hm.reference_failures(rec["task"], __import__("pathlib").Path(rec["suite_dir"])))
for t in TASKS:
    recs=[m for m in ms if m["task_id"][3:]==t]
    seen=set(); one=[]; 
    for m in recs:
        one.append(m.get("first_run_gate")=="committed" and not ev(m.get("first_run_persistent")))
    import heldout_results as hr
    sys.path.insert(0, "scripts")
    import author_eval as ae
    from pathlib import Path as _P
    fr=[]
    for c in cs:
        if c["task_id"][3:]!=t or c["candidate_kind"]!="first_run":
            continue
        ws=c.get("workspace_ref")
        if not ws:
            hits=sorted(_P(c["run_dir"]).glob(f"tasks/rb_*/workspaces/{c['milestone_id']}/repo"))
            ws=str(hits[0]) if hits else ""
        if ws:
            c=dict(c, workspace_ref=ws)
            if hm.cached(t,c["milestone_id"],ws.rstrip("/")) is None:
                hr.results_for(t,c["milestone_id"],ws.rstrip("/"))
            rec=A1.get((t,c["milestone_id"]))
            if rec and ws.rstrip("/") not in rec["suite_results"]:
                rec["suite_results"][ws.rstrip("/")]=ae.run_suite(_P(ws),_P(rec["suite_dir"]),ae.ENV_ROOT/t/"bin"/"python")
            fr.append(c)
    hacc=[]
    for c in fr:
        h=hm.cached(t,c["milestone_id"],c["workspace_ref"].rstrip("/"))
        if h and h.get("attributed"):
            pc=hm.per_case(h); hacc.append(sum(v=="pass" for v in pc.values())/len(pc))
    r0=[c for c in cs if c["task_id"][3:]==t and c["candidate_kind"]=="R0"]
    filled=sum(1 for c in r0 if all(v=="pass" for v in (ev(c.get("per_case_results")) or {}).values()))
    vnet=[]; hnet=[]
    for c in r0:
        mid=c["milestone_id"]; inc=[f for f in fr if f["milestone_id"]==mid and str(f.get("run_dir")).rstrip("/")==str(c.get("run_dir")).rstrip("/")]
        if not inc: continue
        iw=inc[0]["workspace_ref"].rstrip("/"); rw=c["workspace_ref"].rstrip("/")
        h0=hm.per_case(hm.cached(t,mid,iw)); h1=hm.per_case(hm.cached(t,mid,rw))
        if h0 and h1:
            hnet.append(sum(1 for k in h0 if h0[k]=="fail" and h1.get(k)=="pass")-sum(1 for k in h0 if h0[k]=="pass" and h1.get(k)=="fail"))
        rec=A1.get((t,mid))
        if rec and iw in rec["suite_results"] and rw in rec["suite_results"]:
            ok=valid(rec); tier={x["case_id"]:x["tier"] for x in rec["suite_cases"]}
            a=rec["suite_results"][iw]; b=rec["suite_results"][rw]
            keys=[k for k in a if k not in ok and tier.get(k,"hard")=="hard"]
            vnet.append(sum(1 for k in keys if a[k]=="fail" and b.get(k)=="pass")-sum(1 for k in keys if a[k]=="pass" and b.get(k)=="fail"))
    f=lambda xs: f"{sum(xs)/len(xs):+.2f} (n={len(xs)})" if xs else "-"
    print(f"{t:14s} one-pass {sum(one)}/{len(one)}  first-run held-out acc {sum(hacc)/len(hacc) if hacc else float('nan'):.2f} (n={len(hacc)})  "
          f"R0 runs {len(r0)} gate-filled {filled}  R0 net vs first run: verifier {f(vnet)} held-out {f(hnet)}")

json.dump(list(A1.values()), open("outputs/author_eval/A1/records.s0.json","w"), indent=1)
