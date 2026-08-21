# Context Folding: Evaluation & Token Economics Plan

**Status:** Draft v1 · **Author:** Yian · **Date:** 2026-08-06 · **Companion to:** Context Folding: Design Document (§11, §14–§16)

---

## 1. Purpose and hypotheses

The design document claims two things; this document specifies how to prove or falsify them.

**H1 (token economics):** folding reduces the total cost of long-horizon agent sessions — measured in cache-adjusted tokens and dollars — relative to both no management and compaction, after accounting for the cache misses that projection rebuilds cause and the overhead folding adds (markers, notices, fold/unfold tool traffic).

**H2 (performance):** folding improves, or at minimum does not degrade, task success on long-horizon tasks at equal or lower token budgets, via higher relevance density in context.

These are separable and must be reported separately. H1 can hold while H2 fails (cheaper but dumber), and that outcome has a defined disposition (§9): ship Tier 1 as hygiene, hold Tier 2. The single most important discipline in this plan is refusing to let a strong H1 result launder a weak H2 result.

Two secondary questions ride along: **Q3** — how much of the win is heuristics vs. agent judgment (the Tier 1 → Tier 2 delta, which is the headline research result); **Q4** — which pre-registered triggers fire (unfold-then-refold cycles → peek reintroduction; marker mass growth → consolidation; poor workspace awareness → context_map reintroduction).

## 2. Experimental arms

Six arms. Every arm runs the identical harness, model, tools, and prompts except for the manipulation, so differences attribute to context policy alone.

| arm | context policy | answers |
|---|---|---|
| A0 | none — full transcript always | the do-nothing baseline |
| A1 | provider/framework compaction only (threshold-triggered summarization) | the incumbent |
| A2 | Tier 1 folding only (heuristics, auto-notes; no agent tools) | floor of the folding win; zero-judgment attribution |
| A3 | Tier 1 + Tier 2 (agent `fold`/`unfold`, §6.1 prompting) | the full system |
| A4 | A3 with folding *disabled but tools present* (calls acknowledged, nothing marked) | isolates the cost/distraction of the tool surface itself |
| A5 | A3 with random folds (matched count and size to A3, random span selection among legal targets, template notes) | isolates *judgment* from *removal*: if A5 ≈ A3, verdicts add nothing and removal alone explains the result |

A4 and A5 are the ablations that make the paper defensible. A4 catches the possibility that prompting an agent about workspace hygiene changes its behavior independently of any context change; A5 is the placebo arm — folding advocates must beat random folding, not just no folding.

Arm-crossing rule: A2–A5 share Tier 1 configuration (same ruleset hash, §14.1) so the Tier 2 delta is clean.

## 3. Task suites

Three suites, in ascending realism and descending control.

**S1 — Benchmark tasks.** SWE-bench Verified subset (or equivalent repo-level fix tasks) filtered to instances whose A0 sessions exceed 150K prompt tokens — folding cannot show an effect on tasks that never grow a context. Fixed model, temperature, and tool set; N seeds per instance (§7). Provides: controlled success measurement, paired statistics.

**S2 — Synthetic long-horizon probes.** Constructed tasks that specifically stress the failure modes folding targets and causes:
- *needle-after-fold*: plant a fact in a large tool result, induce its folding (it legitimately looks finished), then require the fact 30+ turns later. Measures recovery behavior: does the agent unfold, re-fetch, or hallucinate?
- *poison persistence*: inject a false claim via a fetched document; later provide contradicting ground truth. Measures whether `poisoned` folding actually stops the falsehood from re-anchoring vs. arms where it lingers.
- *dead-end heavy*: tasks engineered with 3–4 large exploratory dead ends before the productive path. The best-case profile for folding; establishes the effect-size ceiling.
- *reference chains*: tasks whose later steps refer to earlier results positionally ("the second config above"). Stresses dangling references (§10.1) — the arm where folding should look *worst*; measures the floor.

**S3 — Replayed production sessions.** Real recorded sessions (own dogfooding corpus first) re-projected under each arm's policy via §14 counterfactual replay. Provides: realistic workload mix (the exploration-heavy vs. edit-heavy split that determines whether result folds or input-payload folds dominate, §16), and honest token economics on real traffic. Limitation, stated plainly: replay changes what the model *would have seen*, but the recorded actions are fixed — S3 measures token economics exactly and performance not at all. S3 numbers appear only in H1 analyses.

## 4. Metrics

### 4.1 Primary (pre-registered, reported for every arm)

- **Task success rate** — suite-defined (tests pass for S1/S2). H2's metric.
- **Cache-adjusted token cost** — the honest H1 metric, defined in §5. Reported as effective tokens and as dollars.
- **Wall-clock and turn count** — folding adds tool round-trips; a system that saves tokens by spending turns has a real cost the token metric hides.

### 4.2 Secondary (diagnostic; from ledger + decision log)

Per-reason fold rate and unfold rate against the §11 ceilings (`finished` < 5% unfold; `irrelevant` watched hardest); fold latency (last-genuine-use → fold, the §6.1 rhythm check); unfold-then-refold cycle count (peek trigger); re-fetch loop rate from the §16 matcher (the marker-failure signal); note-rejection rate by gate and decider; notice conversion (`acted_on/emitted`); dangling-reference failures (S2 reference-chain suite plus manual audit, §8); marker mass at session end.

### 4.3 Guardrails (any breach stops a ship decision regardless of primary results)

Parity-linter failures ≠ 0; any fold of a protected span; any `sensitive` content in telemetry export; hash-chain mismatch on replay. These are correctness properties dressed as metrics — they gate, they don't trade off.

## 5. Token economics model

### 5.1 Why raw prompt tokens are the wrong metric

With prefix caching, a 200K-token request costs a fraction of nominal when the prefix is cached — and a checkpoint rebuild deliberately breaks the cache. Comparing arms on raw prompt tokens flatters folding (it shrinks the number that matters least) and hides its real cost (the re-prefill it triggers). All H1 accounting therefore uses **effective tokens**:

```
effective_tokens(request) =
    tokens_cache_read  × r        # cached prefix, discounted
  + tokens_cache_write × w        # newly cached content, premium
  + tokens_uncached    × 1.0      # plain input
  + tokens_output      × o        # output multiplier

session_cost = Σ over requests, converted to $ at current prices
```

`r`, `w`, `o` are provider price ratios, recorded in the config snapshot (illustrative current shape: r ≈ 0.1, w ≈ 1.25, o ≈ 5 relative to input; re-read from the price sheet at run time, never hardcoded — treat these numbers as stale until checked).

### 5.2 The fold break-even inequality

A checkpoint rebuild that folds spans totaling `F` tokens at prefix position `p` (with `T` tokens after `p`) pays roughly one re-prefill of the tail and thereafter saves `F` per request:

```
cost of rebuild   ≈ (T − F) × (w − r)          # tail re-cached at write price
saving per request ≈ F × r                      # folded tokens no longer cache-read
                    + F × attention_dividend    # unpriced; measured via H2, not here

break-even after n requests where n ≈ (T − F)(w − r) / (F × r)
```

Worked example at the illustrative ratios: the §13 worked session folds F = 100.6K at a checkpoint with T ≈ 160K after the fold point. Rebuild cost ≈ 59.4K × 1.15 ≈ 68K effective tokens; per-request saving ≈ 100.6K × 0.1 ≈ 10K. Break-even ≈ 7 requests — comfortably cleared by any session that continues 10+ turns, and this is the *pessimistic* frame (it prices the attention benefit at zero and ignores that unfolded arms keep *growing*). The inequality also yields the tuning rule the design asserts but doesn't derive: the 15% checkpoint threshold is not arbitrary — thresholds trade break-even horizon against staleness of savings, and this formula is how the eval tunes it per workload rather than by feel.

### 5.3 Overheads charged to folding

Honest accounting charges the folding arms for: marker floor cost (~30–60 tok × marker count, §9.5); harness notices and ingestion-label bytes; `fold`/`unfold` tool-call round trips (input *and* output tokens — notes are output the model generates); unfold reinstatements (temporarily re-adding what was saved); and every rebuild's cache write. All of these are computable from the decision log; none may be netted out silently.

### 5.4 Reporting

Per arm × suite: effective tokens (median, p90), $ per session, break-even turns realized vs. predicted, savings split by span origin (tool result vs. input payload — the workload-mix signal that decides which demos to lead with). All computed by §14 replay — Σ(counterfactual unfolded projection − actual) per request — never by live counters.

## 6. Measurement infrastructure

Everything derives from the two append-only records. **Ledger + decision log → metrics** is a batch job (`evalctl compute --session <id>`), reproducible from scratch; the projection-hash chain verifies that what was measured is what ran. Counterfactual costs come from re-projection under the comparison arm's config (§14.3). One rule keeps the numbers publishable: **no metric may exist that cannot be recomputed by replay from the session artifacts** — if a number needs a live counter, instrument the log instead until it doesn't.

Environment pinning per run: model snapshot id, harness/config versions (§14.1), tool versions, repo commit for S1 instances, and the provider price sheet used for §5 conversion. Any of these changing mid-campaign starts a new campaign epoch; cross-epoch comparisons are labeled as such.

## 7. Statistical design

Long-horizon agent tasks are high-variance; the design leans on pairing to survive it.

- **Paired at the instance level:** every S1/S2 instance runs under every arm with the same seed set; the analysis unit is the within-instance difference (A3 − A1 success, A3 − A1 effective tokens), not arm-level means. Pairing removes instance difficulty, the dominant variance source.
- **Seeds:** ≥ 5 per instance × arm; success analyzed per-seed (mixed-effects logistic with instance random effect), tokens analyzed on per-instance medians.
- **Sample size:** powered for the decision, not the publication — to detect a 5pp success difference at α=0.05, ~200 paired instances; if early instances show near-zero H2 effect with tight intervals, stop for H2 and continue H1 (sequential design with alpha spending, pre-registered).
- **Multiple comparisons:** the arm grid is 6 × 3 suites × ~10 metrics; primary hypotheses (H1: A3 vs A1 cost; H2: A3 vs A1 success; Q3: A3 vs A2) are pre-registered and tested at full α — everything else is exploratory and labeled so.
- **One-sided where honest:** H1 is directional (folding must be *cheaper*); H2 is two-sided (degradation is the risk being tested, not just absence of improvement).

## 8. Failure analysis protocol

Numbers say whether; audits say why. Two standing procedures:

**Misfold audit.** Sample 30 folds per arm per campaign, stratified by reason class, and grade against the reconstructed context (§14.3): was the line of work actually closed? Does the note answer what the evidence answered? Blind graders (fold shown without arm label). Produces a per-class precision estimate that the automated unfold-rate proxy is calibrated against — if `finished` unfold rate is 2% but audit precision is 80%, the agent is failing silently (not recovering what it should), which unfold rate alone cannot distinguish from good judgment.

**Failure trace review.** Every failed S1/S2 task in a folding arm gets a mechanical first pass: did the failure turn's context reference a folded span (dangling)? Did a re-fetch-loop event precede it? Was there an unfold of the implicated span *after* the failure point (too late)? Tag each failure `fold-implicated / fold-neutral / fold-prevented` (the last: failures in A0/A1 on instances the folding arm passed, traced to distraction or poisoned residue). The fold-implicated rate is the number that belongs next to the headline success delta in any writeup.

## 9. Acceptance criteria and decision table

Pre-registered before the first campaign run; changing them after seeing data requires a logged amendment.

| outcome | disposition |
|---|---|
| H1 ✓ and H2 ✓ (A3 ≥ A1 success, lower cost) | ship A3 as default; publish |
| H1 ✓, H2 flat (CI within ±2pp) | ship A3; claim economics, not intelligence |
| H1 ✓, H2 ✗ (A3 < A1 success, significant) | ship A2 (Tier 1 hygiene) only; Tier 2 back to §6.1 prompting iteration |
| A5 ≈ A3 on both | verdicts add nothing — simplify: keep removal, drop note-quality machinery, revisit taxonomy |
| A2 ≈ A3 (no Tier 2 delta) | agent judgment not paying for its complexity; ship A2, keep tools behind a flag |
| guardrail breach | no ship; fix, rerun campaign epoch |

Trigger thresholds (Q4), also pre-registered: unfold-then-refold cycles > 15% of folds → design peek v2; marker mass > 5% of context at p90 session length → design consolidation; `irrelevant` unfold rate > 25% → tighten Tier 2 prompt or demote `irrelevant` to Tier 3-proposed only.

## 10. Live evaluation (post-ship)

The offline campaign decides shipping; production decides staying shipped. Dogfood cohort with per-session arm assignment (A1/A2/A3, sticky per user-week), telemetry under the §16 privacy rule — counts, rates, and hashes only. Watch the same secondary metrics plus two only production surfaces: user unfold requests and marker click-to-expand rate (§17.4 viewing events — high viewing of a reason class means its notes aren't answering what users wonder), and resume adoption vs. token footprint at resume (§17.1, the visible economic win). Regression tripwires mirror the offline ceilings; a tripped ceiling auto-files the affected sessions (ledger + log, local) for a misfold audit batch.

## 11. Reporting artifact

One campaign → one report, generated from the metric store: arm × suite table for primaries with CIs; the §5.4 economics breakdown with the break-even realized-vs-predicted plot; secondary dashboard against ceilings; audit precision per class; failure-tag distribution; and a decisions section that reads the §9 table row by row against the results. The report generator is part of the eval harness — if a claim can't be generated from the artifacts, it doesn't go in the writeup.

---

*Deliberate scope exclusions: cross-model generalization (single pinned model per campaign; model sensitivity is its own campaign), human preference studies on fold-marker UX (post-ship), and adversarial/injection red-teaming of the fold surface (§10.2 defenses are tested as correctness guardrails here, stress-tested separately).*
