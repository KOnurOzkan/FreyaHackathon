# Local Freya workflow builder

The offline commands prepare inputs, check source-backed policy data, perform exact
calculations, and test action sequencing against an in-memory bank. They need no
credentials or network. The separate `extract` command uses an existing Codex
ChatGPT login to interpret briefs and their API; it consumes model usage quota.

The `build` command reads briefs and OpenAPI, extracts policy and conversation
parameters, and generates a guarded Freya workflow JSON in one unattended local
run. At the 11 October 2026 development checkpoint, the frozen **0.5.2** source
passed [**333 local tests**](../out/overnight-052-local-tests.log). Fresh A and B dialogue planning produced
`out/workflow-a-12` and `out/workflow-b-12`, one model call each using prior
validated extraction. Both generated artifacts passed their local audits:
[A audit](../out/workflow-a-12.local-audit.json) and
[B audit](../out/workflow-b-12.local-audit.json). These are development compilations,
not fresh complete extraction/build runs or official simulation results.

The preceding 0.5.1 candidates passed all **13 public cases officially** in one
repetition: [A 10/10](../out/overnight-a-051-baseline/result.json) and
[B 3/3](../out/overnight-b-051-baseline/result.json). Manual review still found an
unanswered ancillary question in A. Targeted trials exposed vague-date handling,
known-fact answers, contextual dates and ambiguous product selection defects,
including violations in cases the judge passed. Original verdicts and findings
remain in the baseline folders and
[A targeted review](../out/overnight-a-051-targeted/manual-review.json) /
[B targeted review](../out/overnight-b-051-targeted/manual-review.json).

The 0.5.2 A live round **failed all 15 calls before any bank tool**: the platform
rejected code/data inputs above its 131,072-byte limit. All results are collected
in `out/overnight-a-052-round1`; B publication was preserved but testing held.
Version 0.5.3 adds AST-checked compiler compaction, removes unused embedded plan
metadata, and enforces a conservative code/state/protocol budget before publishing
or testing. New `out/workflow-a-13` and `out/workflow-b-13` artifacts passed their
local audits; the full local suite and live verification are pending. A raw
altered-API recompile remains blocked by the conservative budget, by 46 bytes.
The next live evaluation input contains 32 public and development scenarios,
including strict opening/privacy/transfer, known facts, and partial callbacks.
No current candidate has established
three-run repeatability, submission readiness or voice/audio compliance. No
submission export has been made.

A fresh 0.5.1 raw-materials build used two model calls and passed 21 emitted-code
scenarios in 511.094 seconds including evaluation. Its
[trace](../out/overnight-source-generalization-03/out/raw-packet-build-03/trace.jsonl)
and [evaluation](../out/overnight-source-generalization-03/out/raw-packet-build-03/evaluation.json)
are retained. A fresh 0.5.2 raw-materials run passed all 21 scenarios in 449.391
seconds with two model calls, plus nine factual-question turns and conflicting
product-reference checks: [review](../out/generalization-052-review.json). These synthetic trials
test generalization boundaries; they do not prove unseen-brief understanding.

Earlier failures remain available, including the wrong spoken dates and premature
recording claims in `out/improvement-a-03/strict-reviewed-transcripts` and
`out/improvement-a-03/regression-reviewed-transcripts`, and the altered-API trial
`out/eval-api-04`. The history below records the original outcomes without
promoting earlier candidates on the strength of later repairs.

## Prepared speech and action boundaries

Prepared speech, introduced in 0.5.0, uses source-backed sentence templates in the dialogue
plan. Every conversation node uses prepared TTS entry text with a verbatim chat
fallback. The runtime fills product labels, returned identifiers, amounts, dates,
and callback times from verified account data and controller state. The dialogue
model no longer receives instructions to compose offers or infer their dates.
Version 0.5.2 answers minimum-payment, balance and deadline questions from verified
facts without changing a pending agreement or consuming a persuasion attempt.
Other questions receive the source-authorized missing-information response.

An accepted offer is distinct from confirmation of a complete read-back. Even if
the extractor labels an adjusted-date acceptance as `confirm`, the offer stage
can only create a draft and request confirmation. A changed amount/date invalidates
the previous confirmation and is checked and read back again. Success text is
selected only after a successful authoritative response; uncertain writes still
stop without a retry or a claim of success. No generated workflow is edited by
hand, and the phone-agent provider/model remains the starter default.

Shared corrections retain every affected product's terms while an adjusted offer
is discussed. Read-back waits until all pending terms are resolved. Collective
product selection requires a quoted reference matching a unique account label or
identifier; ambiguous selections retain the proposed terms for clarification.
Version 0.5.3 resolves exact numeric suffixes spoken as digit words, including
leading zeros and double/triple digits, while rejecting wrong, partial, or
colliding references. These checks have passed locally and await live review.
Invalid partial callback dates or times also consume the source-defined correction
allowance; valid incomplete proposals remain uncommitted until complete.

`tools/speech_audit.py` compares collected assistant text with the exact text
prepared at each conversation-node entry. It permits whitespace and message
splitting only; changed words, figures, dates, extra speech, or missing delivery
block the development evaluator even if the official judge passed. Official
scores remain unchanged. Reports are saved as `speech-audit.json`; the export
helper independently performs this check before export. This is a speech-fidelity
check, not proof that policy extraction or customer-intent interpretation was
correct. Voice/audio delivery still requires separate validation.

The new regression tests cover weekend adjustments, mislabelled acceptance,
changed confirmations, failed writes, combined product deadlines, missing facts,
and transcript discrepancies. The generated A artifact also passed twelve synthetic weekend/date-wording
checks across all months of a different year (`out/rigid-speech-month-variants.json`).

A candidate `out/workflow-a-10` was published and read back as version six. The
five-case strict simulation started, but status collection encountered HTTP
504 followed by repeated HTTP 503 responses stating that API-key validation was
temporarily unavailable. The existing five calls were later recovered, all ended:
all five bank-ledger checks passed and all thirty-five assistant responses matched
their prepared text. Original official outcomes are two passes, two inconclusives,
and one failure. The failure incorrectly treats the mandatory call-recording
opening as a promise-recording claim; its original verdict is preserved. The two
inconclusive explanations describe compliant behavior but remain nonpasses.
The stuck simulation aggregate was paused only after collecting every ended call.
Recovery evidence is in `out/improvement-a-04/recovery-status.json`; readable
transcripts are in `out/improvement-a-04/recovered-reviewed-transcripts`.
This was an earlier recovery checkpoint. Later A and B publication and full public
coverage are recorded in the 0.5.1 results above; three-run qualification remains
outstanding. No submission export was made.

## Run it on this Windows setup

### Improvement loop (development)

`tools/improve.py` evaluates one immutable generated candidate. It runs strict
local regressions, the full local suite and an audit of the emitted artifact;
only then can it publish and start a strict live batch. A failure stops expansion.
If the strict batch passes, it finishes the untested cases of repetition one,
then runs two fresh full repetitions. Every selected case must pass three times;
the first strict batch counts toward that total. Every invocation creates fresh
bank sessions. There is one simulation at a
time, with up to five calls in parallel, and a shared 30-minute evaluation budget.
An interrupted remote test is marked `needs_collection`, never passed or retried
blindly. Collect that saved simulation before starting another.

Example for a newly generated A candidate (choose a new output directory):

```powershell
& 'C:\Python312\python.exe' -B tools/improve.py --compiled out/workflow-a-12 --out out/my-evaluation --brief A --strict PUB-01,PUB-02,PUB-03,PUB-08,PUB-10 --cases PUB-01,PUB-02,PUB-03,PUB-04,PUB-05,PUB-06,PUB-07,PUB-08,PUB-09,PUB-10 --history out/live-smoke-a-03 --live --env ../.env --slot builder-practice-a --concurrency 5 --reps 3 --seconds 1800
```

Omit `--live`, `--env` and `--slot` for local gates only. `--reps 1` is a diagnostic
pass, not evidence of three-run reliability. Test IDs are evaluator selectors;
they never reach the builder's policy or workflow prompts. `--history` is
repeatable and accepts prior folders containing `result.json` and optionally
`manual-review.json`. The reports retain all failures. Known failures or review
findings on the same workflow hash block promotion even after later green runs.
The export helper also rejects an attached blocking manual review.

Each round saves `manifest.json`, `trace.jsonl`, per-stage logs, publication and
full live evidence, and `feedback.json`. Feedback prioritizes strict failures and
transcript findings. The development cycle is: inspect the evidence and source
policy, add a regression with altered inputs, fix generic builder code, regenerate
without editing the workflow, then evaluate again. The evaluator automates the
checks and feedback; it does not fine-tune weights or automatically patch code.

Strict regression coverage includes identity limits from one through five,
complete versus fragmented word-form dates, varied product amounts and supplied
dates, documented write-failure retry limits, and uncertainty after an earlier
known failure. Other local suites cover changed calendars, API fields, amendment
precedence and contradictory sources. These are development regressions, not
claims of unseen-case performance. Keep additional cases separate for evaluation;
never feed their expected answers into the builder or copy final-brief values into
code. After hidden release, only generic fixes are allowed, followed by complete
unattended builds from the supplied inputs.

`automated_gates_passed` is limited to the selected tests; it is not proof of full
transcript compliance. This development loop makes no submission export and is
separate from the final complete-run budget. `tools/complete_run.py` remains the
entry point for a fresh, unattended build/test/export with its own total deadline.

For a generic code fix with **unchanged inputs**, skip both model calls during
development by reusing an intact generated plan. Its original brief/API/extraction
hashes must match, and the plan is validated against the prior runtime payload:

```powershell
& 'C:\Python312\python.exe' -B -m builder compile --brief briefs/brief-a.md --api tools/openapi.json --extraction out/workflow-a-12/extraction.json --reuse-plan-from out/workflow-a-12 --out out/my-recompiled-a --timeout 60
```

This produced a new artifact in under a second in the measured development run.
It does not edit the prior workflow. The trace labels it `development_recompile`
with zero model calls and the reused artifact hashes. A changed brief, changed
API or incompatible plan requires fresh extraction/planning. Final competition
runs use `build`/`complete_run.py` without this development cache.

Readable transcripts can be generated from a collected batch without network:

```powershell
& 'C:\Python312\python.exe' -B tools/transcripts.py --source out/improvement-a-01/regression_1 --out out/my-readable-transcripts
```

The index links each call to Freya. Text files preserve the original spoken
messages and show automated verdicts separately from manual review findings.

### Local commands

Python 3.12.10 was verified at the path below. No installation, environment
activation, or execution-policy change is needed. All dependencies are in Python's
standard library. Run from the starter kit directory:

```powershell
Set-Location 'C:\Users\Onur\Desktop\FreyaHackathon\build-the-builder'
& 'C:\Python312\python.exe' -B -m unittest discover -s tests -v
```

Prepare both practice briefs and their API specification:

```powershell
& 'C:\Python312\python.exe' -B -m builder prepare --brief briefs/brief-a.md --brief briefs/brief-b.md --api tools/openapi.json --out out/my-offline-run
```

Use a **new output directory** for each invocation. Existing outputs are never
overwritten. Paths are relative to the current working directory. `--brief` is
repeatable; supply A and then B for this practice bundle. Input order is recorded
but is not treated as proof of amendment precedence.

The output directory contains:

- `manifest.json`: source hashes, tool names, and explicit completion status.
- `input-bundle.json`: full source text and resolved tool contracts for the
  extraction adapter. Only give this command intended synthetic brief files.
- `tool-templates.json`: seven **unbound, nondeployable** Freya tool templates
  derived from this OpenAPI, with fixed URL/method and no baked customer arguments.
- `trace.jsonl`: metadata-only preparation trace with start/end and zero model or
  network calls. This is not yet a final competition builder trace.

Validate the development policy fixture separately:

```powershell
& 'C:\Python312\python.exe' -B -m builder check-policy --brief briefs/brief-a.md --brief briefs/brief-b.md --policy tests/fixtures/practice-b.policy.json
```

## Automatic extraction (model access required)

The adapter uses the installed native Codex CLI, with an explicit builder model.
It confirms ChatGPT authentication and refuses API-key authentication. It does
not load `.env`, install dependencies, copy credentials, or alter persisted Codex
settings. Shell, web, app, plugin, memory, and multi-agent features are disabled
for the child invocation, with a read-only sandbox and no inherited user config.
The full intended synthetic brief/API bundle is sent to the selected model.

This machine's existing CLI and configured model were checked on 10 October 2026:

```powershell
& 'C:\Python312\python.exe' -B -m builder extract --brief briefs/brief-a.md --brief briefs/brief-b.md --api tools/openapi.json --out out/my-extraction --model gpt-6-astra --codex-exe 'C:\Users\Onur\.vscode\extensions\openai.chatgpt-26.1007.21434-win32-x64\bin\windows-x86_64\codex.exe' --codex-home 'C:\Users\Onur\.codex' --timeout 600
```

Use your own verified executable path after extension upgrades. `--codex-exe`
can be omitted when a native `codex` executable is on PATH; shell wrappers are
not invoked. `--codex-home` defaults to the existing Codex home. `--model` is
always explicit and separate from the phone agent's unchanged `AGENT_LLM`.

The run inventories all nonempty source text into numbered units, asks for
effective and superseded requirements with exact quotations, and checks that
every unit is accounted for. File order alone cannot establish amendment
precedence. The output includes narrative obligations as well as the core's
supported numeric/action parameters. API field bindings and policy enum values
are validated against the supplied specification. Unsupported or contradictory
policies should be reported as blockers; no partial policy is emitted when
blockers are present.

Outputs in each new run directory:

- `input-bundle.json`, `prompt.txt`, `response-schema.json`: exact model inputs.
- `extraction.json`: final unedited model response, including requirements,
  evidence, coverage, precedence, bindings, and any blockers. Each response is
  also kept as `extraction-attempt-N.json`; repair prompts are kept separately.
- `policy.json`: validated effective parameters accepted by `Policy.load`.
- `bindings.json`: semantic request fields and bank-body response paths.
- `blockers.json`: emitted instead of policy/bindings when extraction is blocked.
- `manifest.json`, `trace.jsonl`: source hashes, CLI/model/authentication type,
  model-call purpose, prompt hash, token counts when reported, elapsed time,
  result, and explicit unverified/not-implemented boundaries.

Exit codes: `0` structurally valid extraction, `2` invalid input/provider failure,
`3` model-reported blockers. An invalid model result is retained for diagnosis,
but no accepted policy is emitted. The adapter makes one initial model call and
allows **at most one structural repair call** after validator rejection, within
the same deadline. Set `--max-repairs 0` to disable that extra call. Repair uses
the original inputs, the unedited previous response, and the validator's error;
there are no manually supplied policy answers. Model-reported blockers, provider
failures and timeouts are not automatically retried. There is no fallback
provider. `--timeout` covers input preparation,
authentication, generation and validation (default five minutes; maximum thirty).
Generation reserves five seconds for validation and output. This is an extraction
budget, not yet the eventual complete build/test/export budget.
The first full A+B trial hit the five-minute limit. The example therefore allows
ten minutes; a minimal access probe succeeded in about six seconds. Full-run
timings and failures are retained in each run's manifest and trace.

Requirements and source-unit coverage concern the brief files. The complete API
constraints/descriptions remain in the input bundle; bindings and policy values
are checked against those contracts. This inventory does not duplicate API-only
requirements as uncited brief rules.

Measured development result on 10 October 2026: `out/extraction-ab-04` read A+B
and their API in a complete unattended extraction run using `gpt-6-astra` through
ChatGPT-authenticated Codex CLI. It produced 76 requirement records covering all
87 source units in 320.547 seconds, with one model call and no repair. The core
parameters matched the independent B development fixture. All 78 offline tests
passed; nine additional B ledger/guard checks passed after loading the generated
policy directly. That separate audit is `out/extraction-ab-04.local-checks.json`.
These are development checks, not official Freya results or a complete builder.

**Validation cannot prove semantic correctness.** Valid quotations do not prove
that a rule was interpreted faithfully; source-unit coverage does not prove that
every clause was captured. Narrative requirements are not executable dialogue.
`manifest.json` keeps `semantic_correctness_verified: false`. The fixtures in
`tests/fixtures` are never read by the real extraction path.

## What is implemented

- Explicit UTF-8 brief loading, source hashes, line references and exact-quote
  checks. Duplicate JSON keys and non-finite JSON numbers are rejected.
- A bounded OpenAPI reader: seven POST tools, one literal HTTPS server, internal
  nonrecursive references, typed fields, enums, required properties, common
  numeric/string/array constraints. Unknown schema features are rejected rather
  than silently ignored. No remote reference is fetched.
- Tool templates, explicit request-field binding, and runtime response adapters,
  including renamed fields, nested response paths and changed enum values.
- Validated offline policy data: calendar, overdue bands, floor-field mappings,
  identity sequence, attempt limits, note/transfer/outcome mappings, and evidence.
- Date calculations anchored only to supplied `now`; inclusive callback windows;
  calendar-week weekday semantics; business-day deadlines and adjustments.
- Integer-cent monetary comparisons with exact two-decimal input validation.
- Normalized action guards: ordered verification, authoritative product IDs,
  per-product attempts, combined confirmation, corrections/refusals that revoke
  confirmation, explicit failed-write retry limits, and uncertainty handling.
- Deferred notes, transfer precedence, preservation of recorded promises,
  account-order outcome aggregation, and one final `close_call` attempt.
- A deliberately small synthetic bank and reuse of the kit's pure ledger scorer,
  with an additional exactly-once closing assertion.
- A source-backed dialogue plan: clarification counters, verification questions,
  required wording, negotiation strategy and documented API failure semantics.
- A workflow compiler with separate speaking, controller, bank-action and ending
  nodes. Conversation nodes have no bank tools; prepared arguments are validated
  against the actual supplied API schema before each bank call.
- A replay journal that resumes the tested action guards around asynchronous
  bank calls. Previously completed calls return their saved results locally;
  only the first new guarded call is sent to an API node. Generic retries are off.
- Per-product recording acknowledgments, human-request pauses, deferred notes,
  and a final close step reached after the farewell speech-completion transition.

## Generate a workflow

This uses the same approved ChatGPT CLI authentication as extraction, consuming
model quota. No Freya credential is read and no Freya API request is made.

```powershell
& 'C:\Python312\python.exe' -B -m builder build --brief briefs/brief-a.md --brief briefs/brief-b.md --api tools/openapi.json --out out/my-build --model gpt-6-astra --codex-exe 'C:\Users\Onur\.vscode\extensions\openai.chatgpt-26.1007.21434-win32-x64\bin\windows-x86_64\codex.exe' --codex-home 'C:\Users\Onur\.codex' --timeout 900
```

`build` runs extraction, dialogue planning, compilation, structural checks, and
three emitted-code bootstrap/privacy checks under one deadline. The default is
15 minutes; the maximum is 30. Extraction receives at most 85% of the budget,
including at most one structural repair; compilation receives the remaining
time, with at least 40 seconds reserved for planning and compilation. A timeout,
changed source/input file, or unsupported policy fails the run explicitly. This budget does
not yet include official simulations or exports, which are not connected.

The new output directory contains `extraction/`, `compiled/`, a top-level
`manifest.json`, and a complete `trace.jsonl` combining the child stage traces.
`compiled/workflow.json` is the generated definition. The compiled directory
also contains the unedited dialogue-plan response, exact prompt/schema, accepted
policy/bindings, embedded runtime data, source/code hashes, and local validation
results. The manifest explicitly distinguishes a draft from a published agent
and from `freya.export_workflow` output.

For development, reuse a previous extraction without repeating that model call:

```powershell
& 'C:\Python312\python.exe' -B -m builder compile --brief briefs/brief-a.md --brief briefs/brief-b.md --api tools/openapi.json --extraction out/extraction-ab-04/extraction.json --out out/my-compiled-workflow --model gpt-6-astra --codex-exe 'C:\Users\Onur\.vscode\extensions\openai.chatgpt-26.1007.21434-win32-x64\bin\windows-x86_64\codex.exe' --codex-home 'C:\Users\Onur\.codex' --timeout 360
```

`compile` validates that extraction against the supplied sources and makes one
dialogue-planning call. Its trace explicitly says it reused a prior extraction;
it must not be presented as a fresh complete competition build. Fix the builder
and rerun it; do not edit generated agent JSON or insert brief-specific answers
into compiler code.

Measured development runs on 10 October 2026:

- `out/build-ab-01`: complete unattended local build, 365.203 seconds, two model
  calls, no repair. Its extracted parameters matched the independent B fixture.
- After generic result-mapping and stale-route safeguards were added,
  `out/workflow-b-02` recompiled that run's extraction with a fresh dialogue plan:
  42.734 seconds, one model call, 24 nodes and seven isolated bank action nodes.
  This was the first published development candidate. The earlier build artifact is
  retained for its trace; it predates those compiler fixes.
- At that earlier checkpoint, all 100 local unit/regression tests passed. A separate audit of the unedited
  current artifact passed the three public B ledger scenarios and five additional
  guard scenarios, through its own emitted code and hooks. Results and the exact
  artifact hash are in `out/workflow-b-02.local-audit.json`.
- `out/complete-a-01`: a fresh local Brief A build through the shared-budget
  controller, 253.219 seconds and two model calls. Its own generated code passed
  all ten A public ledger scenarios and five guards; see
  `out/complete-a-01.local-audit.json`. This was not an official simulation.
- `out/workflow-b-03` was the previous development candidate, generated with
  builder 0.3.3 from the earlier accepted A+B extraction and a new model-produced
  dialogue plan. Its three B ledger scenarios and five guards pass locally.
  Live chat exposed two compiler interface problems: the opening's fallback
  dialogue lacked the supplied name, and extraction returned bare labels instead
  of JSON containing an action. Generic prompt/validation fixes address both.
  No generated artifact was manually edited. This development recompile still
  needs a fresh complete build before final submission.

To repeat that development audit, choosing a new output filename:

```powershell
& 'C:\Python312\python.exe' -B -m tests.audit_generated_workflow --compiled out/workflow-b-02 --out out/my-workflow-audit.json
```

The audit uses test fixtures, never a case-ID strategy inside the builder. A final
candidate needs a new complete unattended run with the current builder and the
agreed live validation/test/export stages. Development publication is recorded
separately below; it does not turn an earlier local build into a final submission.

## Live development testing

Verified bank connectivity was restored after the Windows VPN connected. Python
reached the bank with certificate verification enabled and created practice
sessions. The original network reset TLS specifically for the bank hostname;
this does not establish whether the original IP is blocked. No persistent TLS or
security settings were changed. A one-off credential-free insecure diagnostic
had failed too.

Published B slot: `builder-practice-b`, workflow
`d672ae86-12df-4740-ad81-efe657ac24be`, agent
`b5eaaf89-5b82-4858-8394-30e164a20fa0`. Runtime `experimental-5` is unchanged.
Development results on 10 October 2026:

- Version 2, `out/workflow-b-03`: PUB-11 failed with no bank calls. A month-name
  security answer did not normalize; dialogue invented account facts and promise
  success while code stayed unverified. Full recovered evidence is in
  `out/live-smoke-b-02-recovered`, including `result.json` and `diagnosis.json`.
- Version 3, `out/workflow-b-04`: verification, account retrieval and all-product
  read-back worked. One real promise was recorded, but the model did not trigger
  continuation and goodbye repeated until timeout. Evidence:
  `out/live-smoke-b-03-recovered`.
- Version 4, `out/workflow-b-05`: all three real promises were recorded; the
  required acknowledgment followed each, goodbye preceded exactly one final
  close, and the assistant hung up. PUB-11 still failed: the extractor labeled
  "this Friday" as next_weekday, producing the wrong date. Simulation observation
  took 56.07 seconds. Evidence: `out/live-smoke-b-04`.
- Builder 0.3.7 preserves the quoted relative-date phrase and computes literal
  this/next-weekday expressions in code. It also asks the planning model to carry
  required wording into each dialogue stage. This addresses an additional manual
  review finding: earlier transcripts omitted required account-introduction
  wording. These are generic builder changes; no generated agent was hand-edited.
- `out/workflow-b-06` is a retained intermediate development compilation with
  stage instructions but without the subsequent relative-date fix. It was not
  published. The following historical compilations were `out/workflow-b-07` and
  `out/workflow-a-04`; current candidates are listed at the top of this document.
- Version 5, `out/workflow-b-07`: all three B cases ran concurrently and passed
  the automated ledger/transcript evaluation (78.75 seconds observed; approximately
  six-second slowest agent turns). Full evidence is in `out/live-smoke-b-05`.
  Manual transcript review found that PUB-12 says a promise was recorded while
  `recorded_products` was empty, before customer confirmation and the first bank
  promise write. The eventual bank write order is correct, but the premature
  success claim violates policy. `manual-review.json` records the blocker;
  automated 3/3 is not a claim of complete compliance or submission readiness.

A slot: `builder-practice-a`, workflow `73ca9c8a-6888-4943-ae96-23088bcbd35a`,
agent `45fcb3e0-eed2-4b1c-967b-0294695c51c0`. `out/workflow-a-04` was published
as version 2, with read-back in `out/live-smoke-a-03`. Its ten local public ledger
scenarios and five guards passed. The ten-case live batch used concurrency five
and passed 6/10 automatically in 179.57 seconds observed. All calls, ledgers,
verdicts and actual version evidence were collected. Failures:

- PUB-03: third word-form birth date was not normalized; dialogue claimed
  exhaustion despite the controller asking for clarification, then reopened the
  question. Only two verification calls reached the bank and the close reason
  was `declined` rather than `failed`.
- PUB-04: the direct robot question was ignored during date negotiation.
- PUB-08: the required one-business-day payment-processing explanation was missing.
- PUB-10: Freya's HTTP 500 error path skipped the post-hook envelope. The error
  status and explicit no-effect bank message were present in other event fields,
  but our controller received an empty envelope and stopped as if the outcome
  were uncertain. No permitted retry or final bank close occurred.

Manual review also found omitted credit-card endings, an unsupported hardship
recording claim, and an incorrect weekend explanation for a Monday holiday in
automatically passing calls. See `out/live-smoke-a-03/manual-review.json`.
The automated score is retained unchanged and is not a complete compliance claim.

The parent ignored `.env` holds the workspace key and separate practice team
secret. Live helpers load only those roles and redact credentials/customer
secrets. No builder provider key is loaded. Current local-suite evidence is linked
in the development checkpoint above.

`tools/live_smoke.py` supports `publish`, `simulate`, and `export`. Use a fresh
output directory for a new publication/test, and reuse slots to avoid creating
new agents. `--case` accepts comma-separated exact IDs, such as
`PUB-01,PUB-10` or `PUB-11,PUB-12,PUB-13`. Add `--cases PATH` to select development
personas from a validated scenario file; bank identities and account fixtures stay
unchanged. The shipped scenario files are evaluation inputs, never builder inputs.
Each invocation creates at most one
simulation, with up to five simultaneous calls (`--concurrency 5`, the default),
a bounded observation period and no slow-run discard. Smaller batches use their
actual case count. A and B batches run sequentially because the workspace permits
only one simulation at a time. The helper uses the kit personas, ledger scorer and transcript judge.
Actual call-version evidence must match the selected published definition.

Transient management GET connection failures receive at most three read attempts.
POST/PUT requests and runtime bank writes are never automatically replayed. Call
records use compressed, time-bounded downloads: large repetitive logs otherwise
downloaded very slowly on this network. Completed calls are cached within a run.

After interrupted local polling, recover the existing completed simulation:

```powershell
& 'C:\Python312\python.exe' -B tools/collect_smoke.py --source out/original-live-run --env ../.env --out out/new-recovery-directory
```

Recovery only reads remote state, refuses to score a still-running call, matches
cases to their saved persona IDs, and collects full calls, ledgers, judge verdicts
and actual versions. Never create a replacement simulation to hide an interrupted
or failed result. An uncertain creation response requires investigation first.

Two earlier version-2 pre-verification chat probes (`out/chat-identity-b-02` and
`out/chat-identity-b-03`) showed correct names and the first security question.
They were ended and collected, but were not full public-case passes. The reusable
`tools/chat_probe.py` stops before a security answer and always attempts cleanup.

## What the tests establish

The test suite blocks network sockets. It checks boundary conditions, alternate
rule values and field names, failures before and after a possible commit, partial
success, stale confirmation, and output metadata/credential isolation.

It also drives **scripted semantic actions** for each of the 13 public fixtures
and compares the resulting local ledger against the kit's expected records.
These are local ledger regressions, **not 13 official Freya passes**. The customer
scripts are not interpreted, no model speaks, and no transcript judge runs.
Selecting a public case ID happens only in test code, never in builder logic.

The emitted workflow code and API hooks are also executed locally, using the
same 13 public ledger scenarios plus confirmation changes, fragmented security
answers, failed writes, transfer interruptions, renamed API fields and nested
response envelopes. The local graph driver supplies semantic events and marks
speech completion explicitly. It does not simulate Freya's extraction, routing,
dialogue model, speech interruption timing or code sandbox.

`practice-a.policy.json` and `practice-b.policy.json` are manually prepared,
source-referenced **development fixtures**. Practice values live there, not in
the reusable rule engine. They cover the supported numeric/action subset, not
all 47 A rules and B amendments. Source references prove where quoted text came
from; they cannot prove that an interpretation is correct or complete.

## Synthetic briefs and generated-code evaluations

`tests/brief_evals.py` prepares three independent development trials: a calendar
and counter amendment with a superseded email placed last, a renamed/nested API
with different identity questions and wire codes, and two irreconcilable memos.
The expected answers live in a separate test-only file. The real builder receives
only the explicit brief and OpenAPI paths; it cannot read the oracle or public
test answers through its tool-disabled model invocation.

The fixture API uses `https://synthetic-bank.invalid/v1`. These workflows are for
local evaluation only and must not be published to the practice slot.

```powershell
& 'C:\Python312\python.exe' -B -m tests.brief_evals prepare --out out/my-eval-pack
& 'C:\Python312\python.exe' -B -m tests.brief_evals run --pack out/my-eval-pack --case calendar_amendment --out out/my-calendar-eval --model gpt-6-astra --codex-exe 'C:\Users\Onur\.vscode\extensions\openai.chatgpt-26.1007.21434-win32-x64\bin\windows-x86_64\codex.exe' --codex-home 'C:\Users\Onur\.codex' --timeout 1200
& 'C:\Python312\python.exe' -B -m tests.brief_evals check --pack out/my-eval-pack --case calendar_amendment --run out/my-calendar-eval --out out/my-expanded-eval.json
```

`prepare` and `check` use no model or network. `run` uses the approved Codex login
and consumes model quota. Use `api_identity_change` or `conflicting_memos` for the
other trials. Positive-case checks compare core parameters and wording, then run
seven scenarios through the artifact's own emitted code: shared-date handling,
person-confirmation limits, callback corrections, failed verification, successful
retry, exhausted retry with partial success, and transfer without notes. The
checker also verifies the generated workflow hash. The contradiction trial passes
only when the builder explicitly blocks and emits no accepted policy/workflow.

Measured real model trials on 10 October 2026:

- `out/eval-calendar-02`: complete build in 367.359 seconds; changed calendar,
  counters, callback window and mandatory wording matched the independent oracle.
  All seven emitted-code scenarios passed in its expanded audit.
- `out/eval-api-03`: complete build in 374.484 seconds; renamed/nested API fields,
  changed outcome/queue codes and text identity questions passed the oracle and
  all seven emitted-code scenarios.
- `out/eval-conflicts-01`: the contradictory memos were explicitly blocked in
  246.234 seconds, with no accepted policy or workflow.

Earlier failed/blocked trials remain on disk. They led to generic fixes for
extraction capability descriptions, precise coverage feedback, and unused date
normalization when identity questions are textual. The synthetic calendar pack
was revised to resolve an ambiguous inherited date-offer rule; both pack versions
and their original run artifacts are retained. These are limited development
evaluations, not proof of arbitrary unseen-brief understanding.

`tests/test_fault_matrix.py` injects missing, malformed and ambiguous responses and
unknown HTTP failures at all seven bank tools, including replies lost after a
write committed. It also checks inconsistent verification counters and invalid
account records. These remain local semantic tests, not transcript or voice tests.

## One run with a shared budget

`tools/complete_run.py` coordinates the existing builder and the live development
helper under one deadline, capped at 30 minutes. Its default mode builds locally;
`--live` explicitly enables validation/publication, one selected public simulation,
and export. The live phases have only been tested with process doubles so far.

```powershell
& 'C:\Python312\python.exe' -B tools/complete_run.py --brief briefs/brief-a.md --api tools/openapi.json --out out/my-complete-a --model gpt-6-astra --codex-exe 'C:\Users\Onur\.vscode\extensions\openai.chatgpt-26.1007.21434-win32-x64\bin\windows-x86_64\codex.exe' --codex-home 'C:\Users\Onur\.codex' --timeout 1800
```

For an explicitly authorized live run, add `--live --env ../.env --slot SLOT
--case CASE_ID`. Choose the case for the supplied practice brief (for example
PUB-01 for A or PUB-11 for B). This is currently a single-case smoke-test pipeline,
not the final multi-case/repeated-test and automatic workflow-repair loop.

The controller reserves time for export and final checks, refuses to start a test
without enough remaining time, and never automatically repeats a simulation.
Creation intent is recorded before the POST so losing its response does not
permit a duplicate. A run marked `needs_collection` or
`simulation_creation_uncertain` is incomplete: investigate/collect that existing
remote run before starting another. A deadline failure cannot produce an accepted
submission, even if a draft file exists. An over-budget remote run still needs to
be collected separately; it cannot retroactively turn the timed-out build into a
valid 30-minute run. No undocumented cancellation endpoint is assumed.

Before a simulation, the helper checks the publication again and retains the exact
tested definition. Every collected call needs actual `agentVersionNumber` or the
observed call-level `entityVersionNumber` evidence; conflicting reports fail.
the expected version alone is insufficient. Missing or mismatched version evidence
blocks export. Export uses `freya.export_workflow`, compares identifiers and the
complete saved definition, checks the runtime model and requires all selected tests
to pass. This verifies artifact consistency, not the organizer's final packaging.

Workflow schemas and exports are saved intact after checking for known secret
values. Generic log redaction is not applied to schema field names. Transcripts
and diagnostic traces continue to redact keys and synthetic customer secrets.

## Deliberate limits and next connection points

1. **Brief understanding:** the first provider adapter is the existing Codex CLI.
   It produces a source-backed inventory and a validated core policy. Three
   altered-input trials now pass their intended accept/reject checks, but broader
   semantic evaluations on unseen inputs are still needed. Exact quotations
   and structural validation are necessary checks, not proof of completeness.
2. **Natural conversation:** the generated workflow asks Freya's extractor to
   classify user speech into semantic events and its dialogue model to speak
   the prepared text computed by the controller. Those prompts still need repeated
   integration tests with
   fragmented/corrected/word-form answers and interruptions. A local farewell
   marker is not proof that an agent actually said goodbye. All speaking stages
   use prepared text, but interpreting customer intent still depends on the
   extractor and requires independent transcript review.
3. **Runtime integration:** request/response adapters and the workflow assembler
   are implemented against the local kit documentation. The emitted Python uses
   only `json`, `re`, and `datetime` imports, bundling our guards without a package
   installation. Initial controller code has now executed in Freya's chat route.
   Live testing has exercised verification, account retrieval, three promise
   writes, response persistence, automatic continuation and a final close.
   Actual tested version evidence and saved-definition read-back are checked.
   The 0.5.1 baseline covered all 13 public cases officially; broader paths and
   repeated natural-language behavior still need validation. Earlier failures
   and later manual findings remain part of the evidence.
4. **Completion boundary:** local finalization seals the session before writing
   deferred notes and closing. Freya interruption behavior around that boundary
   needs real integration tests. A note failure or unknown transport result
   stops further bank actions and reports an incomplete call; it may therefore
   fail the required final-close invariant and must not count as a passing case.
   Uncertain results are never automatically retried or reported as success.
5. **Supported formats:** UTF-8 Markdown/text briefs, a documented subset of
   OpenAPI 3.0/3.1, exact structured dates, naive supplied local timestamps, and
   two-decimal currency in ordinary decimal notation. PDFs, timezone conversion,
   schema composition, external references, authentication, arbitrary negotiation
   strategies and multi-product per-product confirmation need explicit adapters.
   The plan validator rejects unsupported strategies; semantic faithfulness of
   model-selected parameters is still unproven on unseen policies.
6. **Live builder:** the separate complete-run controller now coordinates build,
   publication, one public smoke test and verified export under a shared budget.
   Its live path still needs a real end-to-end trial. The live helper now supports
   multi-case selection. Fresh-session repetitions are available in the development
   evaluator; automatic workflow repair is not implemented. The `builder build` command
   remains local-only. Bank connectivity is currently working through the VPN.
   Local source quotations and passing ledger tests cannot establish platform
   compliance or a final score.

Offline commands never invoke a provider CLI. No builder command loads `.env`,
imports `freya.py`, or changes `AGENT_LLM`. A root `.gitignore` excludes
credentials, caches and generated local output. Original starter files are kept
as supplied. Generated extraction artifacts contain the supplied brief text;
use synthetic inputs and review artifacts before sharing them.
