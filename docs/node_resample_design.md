# Node-level best-of-N resampling from a frozen prefix (design, 2026-09-09)

Status: design only. Nothing here is implemented.

## Goal

Resample the *one* node the evidence blames, N times, with everything before
it frozen, pick the most robust of the N outputs, and continue the rest of
the milestone from that output. Cost is N × one node, not N × the subgraph.
This is the modular advantage a multi-agent graph has over a single long
session: a single agent that fails at step 7 of 9 must be rerun for 9 steps;
a graph reruns step 7.

## What already exists (no new runtime needed for the mechanism)

- Every agent node emits a `RepositoryChangeArtifact` with `patch`,
  `base_revision`, `changed_files`, `source_node` (verified on the tablib
  batch). The workspace state *before* node k is therefore reconstructible:
  fork the milestone base, apply the patches of every repository-editing
  node that ran before k, in graph order.
- Slots are typed at the node level (`upstream_change:
  RepositoryChangeArtifact`, `suite_custody: RepositoryHarnessResultArtifact`).
  `resolve_input_ids` reads `initial_artifacts` **before** edges, so a
  subgraph starting at k can be fed the recorded upstream artifacts exactly
  as the continuation is fed the incumbent's change today.
- The fast loop already forks per candidate (`fork_candidate_workspace`),
  applies patches (`apply_patch`), grades with the frozen suite, records a
  ledger row, and selects by Pareto with the gate as a hard constraint.
- The persistence diagnosis already separates persistent from flaky
  failures across samples; that is the evidence layer the blame reads.

So the resample is a new *candidate kind*, not a new runtime: "prefix
replay + node rerun + selection", reusing the continuation's plumbing.

## The mechanism

Given a milestone with graph G, its incumbent run R (first pass or best
sample), and a blamed node k:

1. **Prefix workspace.** Fork the base; apply, in order, the `patch` of
   every editing node before k as recorded in R. Verify the fork's tree hash
   equals the recorded `base_revision` of k's own change artifact in R
   (this is the check that the prefix is really R's state before k; if it
   fails, refuse -- never resample on the wrong base).
2. **Prefix artifacts.** Fill `initial_artifacts` for every input slot of k
   and of every node after k from R's `node_outputs`, except the slots
   that k's own output feeds. Nodes before k are removed from the candidate
   graph (they are not rerun; their outputs are the injected artifacts).
3. **N samples at k.** Run node k N times from the prefix, each in its own
   forked workspace, each a fresh session (no shared thread), same prompt
   and inputs. N=3 by default (matches `fast_loop_candidates`).
4. **Grade each sample where it stands.** Run the gate (compile, imports,
   contracts, tests, frozen spec suite) on each prefix+sample workspace.
   Record per-test outcomes, not just the score.
5. **Robust selection** (see below) -> one patch P*.
6. **Downstream replay.** Apply P* to the prefix; run the nodes after k with
   `initial_artifacts` pre-filled (k's output slot now points at P*'s
   artifact); gate the final state; enter it into the milestone's Pareto
   selection against the incumbent like any other candidate.

If k is the last editing node, step 6 is just the gate.

## Robust selection

"Best" is not "max score". With N per-test outcome vectors o_1..o_N over
the frozen suite (plus the gate verdicts):

1. Discard samples that fail the gate (hard constraint, as today).
2. Compute the **consensus vector** c: for each test, the majority outcome
   across surviving samples.
3. Score each sample by (a) spec score, (b) Hamming distance to c, (c) size
   of its failing set. Order: highest spec score; among those within the
   quality epsilon (0.02, one test), the one **closest to consensus**; then
   the smallest failing set.
4. Report `agreement = 1 - mean Hamming distance to c`. Low agreement (say
   < 0.8) is itself evidence: the node's task is under-specified by its
   inputs, and the resample should be recorded as inconclusive rather than
   shipping a lucky outlier. This is what makes the output *robust* rather
   than merely best-of-N: a sample that passes a test the others all fail
   is suspicious, not preferred.

The choice of "closest to consensus among the top scorers" is the one to
validate offline (below); the alternative "max score only" is what the
anchor resample already does at subgraph level.

## The diagnosis: which node to blame

This is the part that decides whether the resample is precise. Three
signal layers, combined; the first two are computable offline on every
batch we already have.

### Layer A -- score trajectory by prefix replay (cheap, deterministic)

For the incumbent run R with editing nodes n_1..n_m: build the prefix
workspace after each n_i and run the frozen spec suite (+ gate) on it.
This yields a trajectory s_0 (base), s_1, ..., s_m and, per test, the
node at which it first passed and (if it later fails) the node at which it
regressed. Cost: m pytest runs, seconds each. No model calls.

- A test that never passes: blame by Layer B.
- A test that passed at n_i and fails at n_j (j > i): blame n_j
  (regression), strongest signal there is.
- The node with the largest drop in s, or the node after which the
  persistent failures first appear, is the default blame.

### Layer B -- failure-to-node by ownership (which patch touched the code)

For each persistent failure, take the traceback's deepest frame inside the
repository (already parsed by the failure-key normaliser) and map the file
to the last editing node whose `changed_files` contains it. Ties and
untouched files (the code was never written -- a missing feature) go to
the node whose *role* owns creation: builder/implementer for missing
modules, contract_author for contract-stage failures, test_author when the
suite itself fails to collect. This is the rule floor (`rule_based_roles`)
extended from roles to nodes.

### Layer C -- persistence (which failures are worth a resample)

Only failures persistent across the samples the milestone already has
(incumbent + anchor probes) are blamed; flaky ones are exactly what the
existing anchor resample harvests. A blamed node must own at least one
persistent failure, otherwise the resample is declined and the ledger says
why (`node_resample: no persistent failure attributable to a single node`).

### Combining, and the decision rule

Per node: `blame = w_reg * regressions_introduced + w_own * persistent_failures_owned`,
with regressions weighted higher (they are proven causal). Pick the
**earliest** node among those within one failure of the maximum -- an
earlier fix propagates; a later node may only be patching around an earlier
defect. Special cases, in priority order:

1. Suite collects zero cases on the task interpreter -> blame `test_author`,
   but do not resample: re-author (custody should refuse the suite; see the
   tenacity/pyjwt findings).
2. Contracts stage fails -> blame the node whose patch created the missing
   symbol's module (Layer B), falling back to the first builder.
3. All persistent failures owned by one node -> resample it.
4. Ownership spread over two editing nodes -> resample the earlier one
   with N samples, then let the downstream replay handle the later one;
   if agreement is low, record inconclusive.

The LLM diagnosis is *not* in the loop for node choice. It may still name
the role and shape for the rerun, as today; blame is rule-based on
measured trajectories, because the whole point is precision, and the
ledger shows the LLM-chosen row at 1/7 against the rule-driven
continuation at 27/38.

## Where it plugs in

- A playbook row `pb_q_node_resample` in the quality table (and a failure
  variant), `continue_from_prefix: <node_id>` in metadata, arming via the
  same path as continuations (`_arm_continuations`), with the prefix
  built by a new `_stage_prefix(record, cand_ws)` beside
  `_replay_incumbent`.
- The candidate graph is G with nodes before k dropped (`DropAgentEdit`
  exists in the edit algebra) and `initial_artifacts` set; N is carried in
  metadata and the N sub-runs are executed by the candidate's own loop
  (they are not N candidates in the Pareto set -- one candidate, N
  internal samples, one output).
- Ledger columns: `blamed_node`, `blame_reason`, `n`, `agreement`,
  `chosen_sample`, plus the existing `pfix/ptot`, score, d_best.

## Cost

One node × N model runs + N gate runs + one downstream replay. For a
three-writer milestone with the builder blamed, N=3: three builder
sessions, three gates, one repairer session -- roughly the cost of one
anchor resample plus one node, against the anchor's three full subgraphs.

## Offline validation before any live run (no Codex spend)

On the batches already on disk (tablib, tinydb, python-pathspec, emoji,
imapclient, python-hl7, bplustree):

1. Compute Layer A trajectories from the recorded per-node patches and the
   frozen suites. Question: how often does the persistent set first appear
   at the *last* editing node (resample = continuation) versus an earlier
   one (resample beats continuation)? tinydb's `LRUCache` defect is the
   known case of an early node.
2. Compute Layer B ownership for the held-out failures that only AdaMAS
   fails (the per-test diffs from EXP-20260908-01 and EXP-20260909-01) and
   check that the blamed node owns the files those failures land in.
3. Report a blame table per milestone. If blame lands on a single node with
   >= 1 persistent failure in most searched milestones, the design is worth
   running live; if blame is diffuse, the mechanism needs Layer A at finer
   granularity (per-file) before it is worth the model calls.

Live A/B afterwards, same budget: `pb_q_node_resample` (N=3 at the blamed
node) versus the anchor resample (three subgraphs), measured on the ledger
(`pfix/ptot`, regressions, d_best) and the held-out.

## What this is not

Not a topology change: the graph is the same, the run starts later in it.
Not the adversary or the contract negotiation: those change *what is
measured* and *what may be revised*; this changes *where the budget is
spent*. They compose: the adversary produces the persistent failures that
Layer C needs; contract revision is what a resample of `contract_author`
would be.
