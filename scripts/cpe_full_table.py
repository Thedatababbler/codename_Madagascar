"""Combined CPE table on gpt-5.5: AdaMAS (continuation arm) vs five direct-LLM baselines, one scoring path."""
import json, glob, re, os, subprocess
SP="/tmp/claude-0/-root/6b36fa73-7f84-4a06-bc56-25c33ee2b4ea/scratchpad"
ev=open(f"{SP}/full55_chain.out").read()
batches={}
for t,b in re.findall(r"^RUN (\S+) rc=0 batch=(\S+)", ev, re.M): batches[t]=b   # last successful run wins
CEIL={"bplustree":"held-out imports undocumented ENDIAN (19 cases)","parsel":"test_selector imports undocumented LXML_SUPPORTS_HUGE_TREE (162/250 unreachable)",
      "portalocker":"conftest imports undocumented LockerType (63/63 unreachable)","flask":"conftest imports undocumented flask.globals.app_ctx (env pytest<8 pinned)",
      "xmnlp":"reference needs tensorflow + downloaded model weights: unscoreable","trailscraper":"15 modules counted statically (boto not importable in env)",
      "cookiecutter":"2 repository/ modules blocked; pin partly static","zxcvbn":"pin partly static"}
def heldout(path):
    try: d=json.load(open(path)); r=d["results"][0]
    except Exception: return None
    tot=r.get("tests_total") or 0; p=r.get("passed") or 0
    if r.get("pass_rate") is not None: return f"{r['pass_rate']:.3f}"
    return f"{p/tot:.2f}*" if tot else "—"
def adamas_row(task):
    b=batches.get(task)
    if not b: return ("—","—","—","—","(no run)")
    ho=heldout(f"{b}/hidden_eval.json") or "—"
    # every milestone failed the gate -> nothing committed; report the agent
    # workspace (ungated, the baselines' footing) with a dagger when available
    ung=f"{SP}/{task}_ungated.json"
    if ho in ("0.00*","0.000","—") and os.path.exists(ung):
        u=heldout(ung)
        if u and u not in ("—",): ho=u.rstrip("*")+"†"
    kind=[]; pers=[]; cont=[]; commit=[]
    for f in glob.glob(f"{b}/{task}/tasks/*/task_execution.json"):
        d=json.load(open(f))
        subs=d.get("subtasks") or {}
        fs=d.get("fast_loop_states") or {}
        for ms,v in subs.items():
            st=fs.get(ms) or {}; P=st.get("persistence") or {}; cands=st.get("candidates") or []
            if not cands: kind.append("none"); 
            else: kind.append("quality" if P or any(c.get("candidate_id")=="incumbent_first_pass" for c in cands) else "failure")
            if P: pers.append(str(len(P.get("persistent",[]))))
            c=[c for c in cands if "continue" in str(c.get("playbook_id",""))]
            if c:
                sc=c[0].get("behaviour_score"); cont.append(f"{'-' if sc is None else f'{sc:.2f}'}{'✓' if c[0].get('status')=='committed' else '✗'}")
            commit.append("C" if v.get("status")=="committed" else "F")
    return (ho, "/".join(kind) or "—", "/".join(pers) or "—", "/".join(cont) or "—", "".join(commit))
led={}
for t,b in batches.items():
    out=subprocess.run([".venv/bin/python","scripts/summarize_fast_loop_ledger.py","--glob",f"{b}/{t}/tasks/*/task_execution.json"],capture_output=True,text=True).stdout
    led[t]=[m.group(1) for m in re.finditer(r"pb_q_continue_improve \| \w+ \| [0-9.—]+ \| [-0-9.—]+ \| (\d+/\d+)", out)]
base={}
for m in ("solo","best_of_3","self_refine","writer_reviewer","debate"):
    try: d=json.load(open(f"/root/projects/mas-baselines/outputs/{m}/hidden_eval.json"))
    except Exception: continue
    for r in d.get("results",[]):
        tot=r.get("tests_total") or 0; p=r.get("passed") or 0
        base.setdefault(r["task_id"],{})[m]=f"{r['pass_rate']:.3f}" if r.get("pass_rate") is not None else (f"{p/tot:.2f}*" if tot else "—")
TASKS="bplustree imapclient pyjwt simpy cookiecutter csvs-to-sqlite deprecated djangorestframework-simplejwt flask parsel portalocker python-hl7 rsa tinydb trailscraper voluptuous xmnlp zxcvbn".split()
print("| task | AdaMAS | search | persist | continuation (score, ✓committed) | pfix/ptot | milestones | solo | best_of_3 | self_refine | writer_rev | debate | note |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for t in TASKS:
    ho,kind,pers,cont,commit=adamas_row(t); bl=base.get(t,{})
    print(f"| {t} | {ho} | {kind} | {pers} | {cont} | {'/'.join(led.get(t,[])) or '—'} | {commit} | " + " | ".join(bl.get(m,'—') for m in ('solo','best_of_3','self_refine','writer_reviewer','debate')) + f" | {CEIL.get(t,'')} |")
print("\n* = raw passed/pinned where the strict eval declined to score (static pin or blocked module); † = ungated agent workspace (no milestone committed); C/F = milestone committed/failed")
