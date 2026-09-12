"""NL2Repo Medium core set on gpt-5.5: AdaMAS v9 and v0 control vs five direct-LLM baselines, one scoring path."""
import json, glob, re, os, subprocess
SP="/tmp/claude-0/-root/6b36fa73-7f84-4a06-bc56-25c33ee2b4ea/scratchpad"
ev=open(f"{SP}/nl2_chain.out").read()
runs={}
for tag,b in re.findall(r"^RUN (\S+) rc=0 batch=(\S+)", ev, re.M): runs[tag]=b
ov=json.load(open(f"{SP}/nl2_baseline_overrides.json")) if os.path.exists(f"{SP}/nl2_baseline_overrides.json") else {}
NOTE={"aiofiles":"src layout; 9 reference non-passes are env (root os.access, aiohttp)","python-jose":"3.5.0 vs document 458 cases (470 collected)","ftfy":"346 vs document 336","voluptuous":"tests.py renamed; 148/149 on reference"}
def score(rec):
    if rec is None: return "—"
    tot=rec.get("tests_total") or 0; p=rec.get("passed") or 0
    if rec.get("pass_rate") is not None: return f"{rec['pass_rate']:.3f}"
    return f"{p/tot:.2f}*" if tot else "—"
def adamas(tag):
    b=runs.get(tag)
    if not b: return ("—","—","—","—","(no run)")
    try: ho=score(json.load(open(f"{b}/hidden_eval.json"))["results"][0])
    except Exception: ho="—"
    kind=[]; pers=[]; cont=[]; commit=[]
    for f in glob.glob(f"{b}/nl2_*/tasks/*/task_execution.json"):
        d=json.load(open(f)); subs=d.get("subtasks") or {}; fs=d.get("fast_loop_states") or {}
        for ms,v in subs.items():
            st=fs.get(ms) or {}; P=st.get("persistence") or {}; cands=st.get("candidates") or []
            kind.append("none" if not cands else ("quality" if P or any(c.get("candidate_id")=="incumbent_first_pass" for c in cands) else "failure"))
            if P: pers.append(str(len(P.get("persistent",[]))))
            c=[c for c in cands if "continue" in str(c.get("playbook_id",""))]
            if c: sc=c[0].get("behaviour_score"); cont.append(f"{'-' if sc is None else f'{sc:.2f}'}{'✓' if c[0].get('status')=='committed' else '✗'}")
            commit.append("C" if v.get("status")=="committed" else "F")
    return (ho,"/".join(kind) or "—","/".join(pers) or "—","/".join(cont) or "—","".join(commit))
base={}
for m in ("solo","best_of_3","self_refine","writer_reviewer","debate"):
    try:
        for r in json.load(open(f"/root/projects/mas-baselines/outputs_nl2/{m}/hidden_eval.json")).get("results",[]): base.setdefault(r["task_id"],{})[m]=r
    except Exception: pass
for t,d in ov.items():
    for m,r in d.items(): base.setdefault(t,{})[m]=r
TASKS="aiofiles emoji python-dotenv python-pathspec tablib tenacity ftfy python-jose voluptuous".split()
print("| task | AdaMAS v9 | search | persist | continuation | milestones | AdaMAS v0 (control) | solo | best_of_3 | self_refine | writer_rev | debate | note |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for t in TASKS:
    h9,k9,p9,c9,m9=adamas(f"v9_{t}"); h0=adamas(f"v0_{t}")[0]; bl=base.get(f"nl2_{t}",{})
    print(f"| {t} | {h9} | {k9} | {p9} | {c9} | {m9} | {h0} | " + " | ".join(score(bl.get(m)) for m in ("solo","best_of_3","self_refine","writer_reviewer","debate")) + f" | {NOTE.get(t,'')} |")
print("\n* = raw passed/pinned where the strict eval declined; C/F = milestone committed/failed; held-out = upstream suite at the count-matched tag")
