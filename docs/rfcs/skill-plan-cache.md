# Skill Plan Cache (SPC)

**Status:** DRAFT — workshopping on a fork, not yet opened against upstream.

**Author:** Adrien Evian (fork: VerdantRhizome/hermes-agent), drafted with agent
assistance.

**Inspired by (not implementing):**
- Zhang, Wornow, Olukotun. *Agentic Plan Caching: Test-Time Memory for Fast
  and Cost-Efficient LLM Agents.* NeurIPS 2025. arXiv:2506.14852. Test-time
  memory that extracts, stores, adapts, and reuses structured plan templates
  across semantically similar agent tasks, matched by keyword extraction and
  adapted by a lightweight model — reported ~46–50% cost/latency reduction.
- *SkillRT: Compiling Skills for Efficient Execution Everywhere*
  (arXiv:2604.03088). AOT/JIT compilation of "skill" text into optimized
  structures with environment binding and concurrency extraction, so a
  runtime parses compiled artifacts instead of re-feeding raw skill text to
  the model every time.

Neither paper's system is proposed here verbatim. Hermes' skills are
markdown procedures loaded by progressive disclosure, not the graph/DSL
representations either paper compiles — this RFC adapts the *idea* (cache a
successful plan, replay it cheaply on a recognized-similar task) to Hermes'
actual architecture, scoped down hard to respect the Footprint Ladder and
prompt-cache-safety rules in `AGENTS.md`.

## Problem

A skill like `cad-fem-integration-testing` or the FreeCAD/Gmsh pipeline in
this user's own workflow gets invoked repeatedly with the same underlying
procedure and only the parameters changing (part geometry, mesh size, load
value). Every invocation currently re-derives the tool-call sequence from
scratch: the LLM re-reads the skill text, re-reasons about which
terminal/execute_code calls to make, and re-generates the glue script
(bash/python) — full input + output token cost and full latency, even though
the *procedure* is identical to five runs ago and only the numbers changed.

This is exactly the class of waste APC targets ("plans," not chat turns) and
SkillRT targets ("skills," not chat turns) — but Hermes has no analog. Skills
are pure text today: no compiled artifact, no plan memoization, no
fingerprint-matched replay path.

## Non-goals (v0)

- **Not** a new core model tool. Every core tool ships on every API call
  (`AGENTS.md`, Footprint Ladder) and plan-cache lookup/replay does not meet
  the bar for "fundamental, broadly useful to nearly every user."
- **Not** automatic silent replay. A cached plan re-executing a bash/rust/
  system-binary sequence without the model in the loop is a security
  surface — replay must go through the same approval gates
  (`security.approval_mode`, terminal command approval) as a freshly
  LLM-generated command. No bypass.
- **Not** general-purpose semantic caching of arbitrary chat turns — that's
  provider-side prompt caching, which Hermes already has
  (`agent/prompt_caching.py`) and is orthogonal to this.
- **Not** mid-conversation cache mutation. Plan capture/promotion happens at
  natural boundaries (`on_session_end`, an explicit `/skills cache-plan`
  command) — never mid-turn, so it cannot invalidate the provider prompt
  cache the way a rebuilt system prompt would.

## Proposal (v0, scoped for real review)

A **plugin** (Footprint Ladder rung 4) plus a **CLI command** (rung 2). No
core tool.

### 1. Capture: `on_session_end` hook, opt-in per skill

The plugin registers `on_session_end`. If the session invoked a skill (skill
name is already tracked for slash-command telemetry) and the turn sequence
ends with tool calls that succeeded (no `status: error` in the relevant
`post_tool_call` events), the plugin proposes — via a normal agent
follow-up, not a silent background write — promoting the tool-call sequence
to a cached plan:

```
This looks like a repeatable procedure. Save it as a cached plan for
`cad-fem-integration-testing`? [y/N]
```

On confirmation, the plugin extracts an ordered template of tool calls with
literal task-specific values (paths, numeric params, names) replaced by
named slots, using the same class of lightweight extraction APC describes
(a cheap auxiliary-model pass, not the main model) — Hermes already has an
auxiliary-model config surface (`auxiliary:` in `config.yaml`) this can
reuse rather than inventing a new one.

### 2. Storage: inside the skill's own directory, using the existing skill file API

The template is written via `ctx` calls into the **skill's own**
`scripts/` or a new sibling `cache/` folder — reusing `skill_manage`'s
existing `write_file` action and directory allowlist (`references/`,
`templates/`, `scripts/`, `assets/`), extended to accept a `cache/`
subfolder for these artifacts. Format: a small JSON manifest (slots, source
skill version hash, capture date) plus, for the bash/rust/system-binary
case specifically, the literal script file the agent already generated and
ran — e.g. `cache/mesh-and-solve.plan.json` referencing
`scripts/mesh-and-solve.sh`. This is not a new storage subsystem; it's the
skill's own directory, which already ships scripts today (hand-written, by
convention) — SPC just automates *capturing* one that the agent already
wrote live instead of requiring the user to notice and save it manually.

### 3. Invalidation

The manifest records a hash of the source `SKILL.md` at capture time. If the
skill file changes, cached plans under it are marked stale (not deleted) and
skipped until manually reviewed — the same "fail closed, never silently
replay a target that could be wrong" posture used for the plugin
config/state bridge's write validation.

### 4. Replay: explicit CLI command, not automatic

```
hermes skills replay <skill-name> [--plan <name>] -- <substituted args>
```

The agent invokes this the same way it invokes any other `hermes <subcommand>`
via `terminal` — guided by a short addition to the skill's own instructions
("if a matching cached plan exists, prefer replaying it over re-deriving the
procedure"), not a new tool. The command:

1. Loads the plan manifest, resolves slots from the given args (or asks the
   model to resolve them — still cheaper than re-deriving the whole plan).
2. Prints the resolved script/tool-call sequence for the user/approval gate
   to see before running — same command-approval path a freshly generated
   script goes through.
3. Executes it, reports success/failure back to the agent turn as normal
   tool output.

Fingerprint matching ("is this new request similar enough to replay this
cached plan") is deliberately **not automatic** in v0. The agent decides
whether to call `hermes skills replay` the same way it decides whether to
call any other command — guided by the skill text noting the cache exists.
Automatic keyword/embedding matching (the actual APC mechanism) is a
follow-up once there's a corpus of real captured plans to tune matching
against, not a v0 commitment.

## Why this respects the Footprint Ladder

| Rung | Used? | Why |
|---|---|---|
| 1. Extend existing code | Partially | Reuses `skill_manage` write paths, existing auxiliary-model config, existing approval gates |
| 2. CLI command + skill | **Yes** | `hermes skills replay` + a short skill-text convention |
| 3. Service-gated tool | No | Not needed — replay is a shell command, not a structured tool call |
| 4. Plugin | **Yes** | Capture logic lives entirely in `on_session_end`/`post_tool_call` hooks, in `~/.hermes/plugins/` |
| 5. MCP server | No | No cross-host reuse case yet |
| 6. New core tool | **No** | Explicitly rejected — the whole point is to avoid adding schema weight for a capability most users won't exercise |

## Open questions to workshop before this goes near upstream

1. **Where does the "propose caching this" prompt live** — is `on_session_end`
   the right hook, or should capture be a deliberate `/skills cache-plan`
   command only (fully opt-in, zero automatic proposing) for v0?
2. **Slot extraction quality** — a cheap auxiliary-model pass may mis-slot
   values that look like literals but are load-bearing (e.g. a unit string).
   Needs a concrete eval before trusting it on anything safety-relevant
   (see `AGENTS.md` punch-list item #1 — this user has already hit a silent
   unit-misinterpretation bug in a downstream tool; a caching layer must not
   add a second one).

   **Proposed answer — don't let a model guess slots, verify them
   symbolically first:**

   The single-shot "ask a model which values look like parameters" approach
   is exactly the shape of failure that produced the FreeCAD unit bug: a
   model can plausibly mis-generalize a load-bearing constant (a unit
   string, a solver flag, a precision digit count) as safely swappable data.
   Don't give the model that judgment call at all where it can be avoided.

   - **Slot *identification* is symbolic, not model-judged.** Require at
     least two successful captures of the same skill before a plan is
     eligible for caching at all (this is a `cache` state transition already
     implicit in the "propose caching" flow in step 1 — a plan needs N≥2
     real runs before promotion, not one). Anti-unify / AST-diff the two
     tool-call sequences: **only positions that actually varied across the
     two real runs are candidate slots.** A value that was constant across
     every observed run stays a literal, full stop — a model is never asked
     to guess "is this safe to generalize" on unseen data, because the slot
     boundary comes from empirical evidence, not inference. This also
     directly catches the class of bug in punch-list item #1: a unit
     string (`"MPa"`) is constant across every run of the same skill and
     therefore is never proposed as a slot; a mesh size or file path that
     legitimately changes run-to-run is.
   - **Slot *labeling/naming* (a much lower-stakes task — assigning a
     human-readable name and JSON schema type to an already-proven slot,
     not deciding whether it's a slot) is a good fit for a cheap
     structured-output model call**, since a wrong label is a cosmetic/UX
     problem, not a silent-wrong-value problem. Candidates worth spiking,
     roughly best-fit-first for *this* sub-task specifically:
     - **DeepSeek-V4 (Flash, free via HF)** — already this user's
       fallback-chain model, proven tool-call reliability, zero new vendor
       or cost to introduce.
     - **MiniMax M2.5** — independently benchmarked (2026) as unusually
       disciplined at bare, unwrapped JSON output (98.6% quality, 100%
       format compliance across a 38-task structured-output benchmark) at
       ~$0.07/run — exactly the profile wanted for a machine-parsed plan
       manifest with no human reading the raw output.
     - **GLM 5.1/5.2** — MIT-licensed, structured-coding benchmarks
       competitive with closed frontier models; fits this user's ongoing
       provider-consolidation goal (z.AI coding plan evaluation already
       underway).
   - **Final template/script emission is a plausible fit for a diffusion
     code model** (Mercury Coder, LLaDA 2), spiked separately from the
     labeling step and purely as a latency optimization, not a
     safety-relevant one. The task at that point — fill in already-verified
     slot positions in an already-fixed-structure script — is closer to
     masked inpainting than open-ended left-to-right generation, which is
     the diffusion-LM architectural strength (bidirectional refinement,
     revise-any-position, reported 2–10x latency win over autoregressive
     models at this profile). This is unproven for the slot-*judgment* step
     specifically (no benchmark evidence yet of diffusion coders reasoning
     about unit-bearing vs. freely-swappable values) — treat it strictly as
     an optional final-stage speed optimization once slots are already
     symbolically verified and labeled, never as a replacement for the
     verification step above.

   Net effect: a three-stage pipeline where the only stage touching a raw
   LLM judgment call is the lowest-stakes one.

   ```
   symbolic diff across ≥2 captured runs   → WHICH positions are slots (no LLM)
        ↓
   cheap structured-output LLM              → label/name slots, write JSON manifest
   (DeepSeek-V4-Flash / MiniMax M2.5 / GLM 5.x)
        ↓
   (optional, latency-motivated) diffusion coder → fast final template/script emission
   (Mercury Coder / LLaDA 2)
   ```

   This still needs a concrete eval once a prototype exists — the above is
   a proposed design, not a validated one.
3. **Does this belong in core `plugins/` at all**, or is it exactly the kind
   of "third-party/niche" capability `CONTRIBUTING.md` says should ship as a
   standalone plugin repo rather than an in-tree PR? Leaning toward:
   prototype as a standalone plugin first, and only propose folding the CLI
   subcommand into core if real usage shows the file-write conventions need
   to be officially blessed (i.e. the `cache/` directory addition to
   `skill_manage`'s allowlist is the only piece that plausibly needs a core
   change; everything else can live in the plugin).
4. **Multi-skill plans** — real workflows chain skills (e.g. FreeCAD skill →
   Blender skill). Out of scope for v0; v0 is single-skill only.

## Concrete dogfood candidate

The user's own FreeCAD → Gmsh → CalculiX FEA pipeline (`cad-fem-integration-
testing`, `cad-agent-frontier` skills) has exactly the repeated-procedure /
varying-parameters shape this targets: mesh + solve steps that are
byte-identical in structure across many runs, differing only in geometry
input and load values. A v0 prototype should be built against that skill
first, not built speculatively.
