# Freya builder — agent handoff

Last updated: 11 October 2026, Europe/Istanbul. This is a checkpoint, not a claim that the candidate is ready.

## Final-night runbook (briefs released 20:00 Istanbul, submissions close 23:00)

1. **Before 20:00:** no live simulation should be running (Freya allows one per workspace, and a stuck one blocks everything; pause it). Check that the Codex login works: `codex.exe login status` should say "Logged in using ChatGPT". Builder source is 0.5.11; full suite 470/470.
2. **Save the released files unchanged** into a new folder per brief, e.g. `final/<name>/` (the brief file or files exactly as published, and that brief's own API file). Do not edit them. If a brief says it builds on another brief, pass both, base first, as Brief B did.
3. **Run one unattended build per brief, each with its own new slot** (kit root, PowerShell or bash):

```
C:/Python312/python.exe -B tools/complete_run.py --brief final/<name>/<brief file> [--brief ...] --api final/<name>/<api file> --out out/final-<name>-run1 --model gpt-6-astra --codex-exe "C:/Users/Onur/.vscode/extensions/openai.chatgpt-26.1007.21434-win32-x64/bin/windows-x86_64/codex.exe" --live --final --env ../.env --slot final-<name> --timeout 1800
```

   Expect about 13 minutes: about 6 extraction, about 5 plan, then publish and export. If Freya is down at publish or export time, the run waits and retries by itself while the 30-minute budget allows.
4. **Success** is `"status": "verified_export"` on the last output line and in `out/final-<name>-run1/manifest.json`. The file to submit is the manifest's `export` path (for example `out/final-<name>-run1/platform/submission-export.json`); the trace to submit is `out/final-<name>-run1/trace.jsonl` (it contains start/end, every model call with model and purpose, what was created on Freya, and the fact that no self-test ran). Rename copies for submission as the organizers ask, e.g. `<brief>.workflow.json`.
5. **If a run fails**, read `manifest.json` `error`. A new run is allowed (use a new `--out`, same slot); never edit a generated workflow by hand. Generic builder bug fixes are allowed after release; adding anything from a final brief (rules, values, names, holidays, outcome names) is not.
6. **Brief files never go into builder code, prompts or tests.** Evaluator-only checks on final briefs are fine for us to look at, but are not inputs to the builder.

## Update 13:30 Istanbul (Claude Code session): 0.5.9 and 0.5.10

- **0.5.9, dedicated confirmation-scope field.** B17 (0.5.7) live: the extractor turned "Yes, I confirm the credit card two hundred and twenty" into a bare `confirm` with no product, so the 0.5.6 guard could not see the partial confirmation and all three promises were written. Multi-product `speak_confirm` now also extracts `fb_confirm_scope` (enum all/some/none, own prompt). With more than one pending promise, writes require `all`; a missing, failed or stale value counts as not confirmed (the controller resets it to `none` after each turn). Single-product briefs are unaffected. Tests in `tests/test_product_reference.py`; `LocalGraph.user` fills the field (default `all`).
- **0.5.10, size guard.** With user approval, `PROTOCOL_RESERVE_BYTES` 24,576 → 18,432, so the encoded-code limit is 81,920 → **88,064**. Evidence: measured overhead about 17,900 bytes on the rejected A052 request; 99,238-byte code ran a 19-step live call. The variable allowance (20,480) and slack (4,096) are unchanged. Reason: a fresh rehearsal build was 81,201 bytes and 0.5.9 adds 223, so an unseen brief with longer wording could otherwise fail the build outright. `tests/test_runtime_budget.py` now asserts the evidence bounds.
- 0.5.9 full suite: **465/465 passed** (`out/fix-059-full-suite.log`, includes the other agent's adversarial tests). 0.5.10 full suite: **465/465 passed** (`out/fix-0510-full-suite.log`). A19/B19 (0.5.9) built and audited 6/6; A19 `c7b365a5ff187d7b…`, B19 `fb4946274bc5d543…`.
- **Rehearsal of the final run** (`out/rehearsal-final-b-01`, Brief A+B, fresh Codex build, new slot `rehearsal-final-b`): `verified_export` in **768 s** (extraction 353 s, plan 323 s, compile 3 s, publish 58 s, export 30 s), 2 model calls, 0 repairs. The fresh plan matches the cached B12 plan on every counter and strategy; the build passed the 6/6 B audit.
- **B17 (0.5.7) live**, `out/fix-b-057-collected-01`: ledger 8/8; official 6 pass, PUB-13 verdict lost to the outage (manually reviewed clean), DEV-B-AMBIGUOUS-KNOWN-FACTS failed (fixed in 0.5.9). PUB-12's "Which product" loop is gone.
- **B18 (0.5.8) live**, simulation `45627399-435e-4d3d-9636-97d38a838dd2`: DEV-B-MIXED-DEADLINE-CORRECTION failed because Freya's extraction model stalled during another outage (19 s and 35 s per extraction call, then no reply; the customer hung up at 70 s). Our code never ran after the birth-date answer. Collection goes to `out/fix-b-058-collected-01`.
- Live batch running now (`out/autopilot-059.log`): publish A19/B19, then the 66-case adversarial set (`out/adversarial-cases-066.json`): 46 A cases on A19 (`out/adv-a-059-round1`), then 20 B cases on B19 (`out/adv-b-059-round1`).
- **0.5.12, security answers in pieces are joined.** Live A059 ADV-A-FRAGMENT-LEADING-ZERO: "Second of November" arrived marked complete and "nineteen seventy-nine" alone, so the agent asked twice. An answer that cannot be normalized alone is now held for the current question; the next piece is tried alone first, then joined with the held piece (both orders, space or "/"). The held piece is dropped once any answer is submitted. Helper `normalize_security_answer`. Tests in `tests/test_workflow.py`. A21 `41235432da695ec0…`, B21 `2bc9e34ced9cefc9…`, audits 6/6, encoded 81,638 bytes (headroom 6,426). 0.5.12 full suite: **473/473 passed** (`out/fix-0512-full-suite.log`).
- **Freya resumes paused simulations.** A simulation with calls still in Freya's retry queue flips back from "paused" (and even from an API-set "completed") to "running" within about 30 s, and blocks new simulations. Only the Freya UI's cancel has stopped one for good; API delete would also remove its data. The A19 simulation `0eb86609…` was in this state at 15:36.
- **A19 live (0.5.9), 46 cases**, collected after the watchdog paused it (`out/adv-a-059-collected-02`): 8 passed; 35 never finished (Freya retrying/queued/never started); 2 verdicts lost; 1 "failure" (ADV-A-FRAGMENT-LEADING-ZERO) is a judge misreading: it claimed goodbye came after `close_call`, but the event log shows goodbye at 16.58 s and `close_call` at 17.30 s. Second occurrence of this judge error (also B15). No true agent failure in the finished calls.
- **Collector: paused batches (tools only).** A paused simulation left runs with no call, and collection aborted with "Missing or ambiguous persona/call mapping". Unfinished runs are now recorded as platform failures (only for paused/failed/snapshot collections); verdict stability and version evidence use finished runs only; the speech audit skips calls that never existed. Test in `tests/test_collect_smoke.py`.
- Live chain since 15:27 (`out/autopilot-0512.log`): A21/B21 in chunks (`out/adv-a-0512-chunk1..3`, `out/adv-b-0512-chunk1..2`), then one rerun round per brief.
- **0.5.11, no extraction on automatic steps.** `speak_progress` and `speak_farewell` are passthrough steps that take no customer input, but Freya re-ran their extraction on the old customer turn and waited for it before routing on (B057 PUB-12: 1.5–3 s per recorded promise and again after "Goodbye"; far longer when Freya is overloaded). After the goodbye, that wait can outlast the customer's hang-up and lose the notes and `close_call`. These two nodes now have no `extraction_schema` (workflow JSON only, no code-size cost). Test `test_automatic_speech_steps_extract_nothing`. A20 `8afa10c73d920a97…`, B20 `125f6b0a928b1a68…`, audits 6/6. 0.5.11 full suite: **470/470 passed** (`out/fix-0511-full-suite.log`).
- Live chain since 14:30 (`out/autopilot-0511.log`): finish the A19 batch (`out/adv-a-059-round1`), then publish A20 and rerun platform-failed A cases on it; publish B20 and run the 20 B cases with reruns. Watchdog: no finished call for 8 minutes → pause → collect → rerun.
- **Fresh raw-material build with 0.5.10** (`out/raw-packet-build-0510-01`; input `out/overnight-source-052/out/raw-pack-052-01/raw_packet`: one amended packet plus a renamed-field API): **21/21 answer-key scenarios passed**, 2 model calls, 0 repairs, 699 s. Extracted correctly: 4 wrong answers (the amendment overriding an older "three"), 3 promise attempts, new bands, holidays, callback window 09:30–17:15 with 2 corrections, renamed product types and note codes. Its security questions are `postcode` (plan chose `text`, as the packet instructs) and `birth_year` (plan chose the new `year` normalization). Encoded size **81,596 bytes**, larger than any practice build: it would have left only 324 bytes under the old 81,920 guard, and has 6,468 under the new 88,064.
- **Final run waits out outages (tools only).** In `--final` mode a failed publish or export is retried after 45 s while the 30-minute budget allows; each publish retry uses a fresh folder (`platform-2`, `platform-3`, ...), export retries reuse the publish folder, and every attempt's trace is merged into the run trace. Development mode still fails fast. Reason: Freya's API was down repeatedly on 11 October, and a final run should not lose an 11-minute build to a short outage. 3 tests in `tests/test_complete_run.py`. The submission file is now `out/<run>/<platform folder>/submission-export.json`; the manifest's `export` field names it.
- **Collector fix (tools only).** B18's PUB-11 call never started (no messages, 215 s, customer ended), so it had no customer ID and `collect_smoke.py` aborted the whole batch. It now records such a call as `never_started` (pass false, ledger unavailable) and keeps collecting; ambiguous IDs still abort. Test in `tests/test_collect_smoke.py`. Re-collected B18 (`out/fix-b-058-collected-02`): 5/8, and all 3 failures are platform-side (never started; judge wrote "All criteria met" but returned inconclusive; 61 s extraction stall). No agent failure in B18.
- Latency candidate, not changed: every customer turn makes 2–3 sequential extraction calls, and each low-confidence discard adds another. All 5 discards seen were correct values scoring 0.51–0.58 against `min_confidence` 0.6.

## Latest checkpoint: 0.5.8 (12:45 Istanbul, Claude Code session)

This section supersedes the 0.5.5 checkpoint below where they differ. At the user's request, a Claude Code session took over from the Codex agent at about 11:20; the Codex agent's notes below remain accurate history.

### Source changes since 0.5.5 (all generic, each with regression tests)

- **0.5.6, partial callback retention** (`Conversation.callback_partial`, callback branch of `handle`). A component that is valid on its own is kept until the missing one arrives; a newly supplied component replaces it; an invalid component is dropped together with the kept component it replaced; a combination-only failure (for example today plus a time already past) drops only that turn's parts. Invalid proposals still count once against `callback_corrections`; valid partial proposals never count. Tests: 9 new in `tests/test_strict_counterexamples.py`.
- **0.5.6, product taken from a grounded reference.** If extraction gives `product_reference` but no `product_id`, and the reference grounds to exactly one product via `grounded_product_reference`, the controller uses that ID (top of `handle`). Live origin: B055 PUB-12 "Which product do you mean?" loop.
- **0.5.6, partial confirmation of a combined read-back.** With more than one pending promise, a `confirm` that names a product (`product_id` or `product_reference`) without `scope:all` records nothing; the full read-back is repeated. Live origin: B055 DEV-B-AMBIGUOUS-KNOWN-FACTS ("Yes, I confirm the credit card" recorded all three).
- **0.5.6, extraction pattern.** For multi-product policies only, the `fb_event` validation pattern on `speak_all_today` and `speak_confirm` rejects a `product_id` without a non-empty `product_reference`, so the platform retries the extractor. JSON only; no code-size cost. `extraction_schema(..., multi=...)` in `builder/workflow.py`.
- **0.5.7, optional `all_verified`.** The live bank OMITS `all_verified` on a `day_month_swapped` hint answer. The runtime required it, crashed and spoke the abort message (A055 DEV-A-IDENTITY-SWAPPED-NONCOUNTING). `normalized_response` now treats `all_verified` and `hint` as optional; `Session.verify` cross-checks `all_verified` only when present. `LocalBank` now omits it on hint answers like the live bank; the old B16 build was confirmed to fail the existing swapped-date audit under the corrected mock.
- **0.5.8, identity answers beyond dates and phone digits.** New plan normalizations `year` and `digits` (enum in `dialogue_plan.py`; prompt guidance in `plan_prompt` and `make_prompt`). `normalize_digits(answer, length=None)` (`normalize_last_four` now wraps it) and `normalize_year`. Motivation: the kit's `run_cases.py` already knows `birth_year`, `postcode`, `company_number` and `founded_year` identity facts, and its `in_words` style says years and codes digit by digit. End-to-end test on an altered API: `test_spoken_year_and_digit_code_identity_answers_reach_the_bank_normalized`.
- **0.5.8, `spoken_year` bug.** "eighty eight" parsed as year 8008, so "21 July eighty-eight" sent `8008-07-21` and would be charged as a wrong answer. Century plus year now requires a two-digit pair (10 to 99) or "oh"/"zero" plus one digit; otherwise clarification.
- Code-size headroom at 0.5.8: about **800 bytes** under the conservative 81,920-byte encoded-source guard (A18 81,123 bytes). Reclaim space before adding more runtime code.

### Local evidence

- 0.5.6 full suite: 421/421 passed (`out/fix-056-full-suite.log`). 0.5.7 full suite: 421/421 passed (`out/fix-057-full-suite.log`). 0.5.8 full suite: **423/423 passed** in 557 s (`out/fix-058-full-suite.log`).
- Generated audits passed 6/6 for A16, B16, A17, B17, A18 and B18 (`out/workflow-*.local-audit.json`). Run them with `PYTHONPATH=.` or as a module.

### Live evidence

- **B15 (0.5.5)**: official 7/8, ledger 8/8, collected in `out/person-b-055-round1` and `out/person-b-055-recovered-01`. The one failure is the partial-confirmation case fixed in 0.5.6. The judge also claimed `close_call` preceded the goodbye; event timestamps show goodbye at 61.7 s and `close_call` at 65.9 s, so that part is judge error.
- **A15 (0.5.5)**, 26 A cases, simulation `0ffb64c8-af38-4bec-8d8b-b8a6a431a23d`, collected read-only in `out/person-a-055-snapshot-01`: ledger 25/26, official pass 21/26. Four verdicts were lost to the Freya outage (PUB-03 analysis never arrived; PUB-10 and both hardship cases inconclusive); their transcripts were manually reviewed and are clean. One real failure, the `all_verified` crash, fixed in 0.5.7. All seven A054 opening failures now pass officially. The simulation stayed "running" after the outage and blocked the workspace; the user cancelled it in the Freya UI.
- **B17 (0.5.7)** published as B version 12 (`c3b04e18de22a5804e037c5be5da22ce371dbcde115fa020ac7810d763261cda`). Live run `out/fix-b-057-round2`, simulation `5b3f3360-938e-41d1-8381-7576934785fd`, started 09:30:57 UTC. The tester stopped on a 504 while polling; collection goes to `out/fix-b-057-collected-01`.
- **A17 (0.5.7)** published as A version 12 (`8ba5b3ff3e9200bc88b9ce0e40446d96101b3106ee6e17ca041569ad3f31acf8`), not yet run live. A18/B18 (0.5.8) are built and audited but NOT published.

### Platform behaviour observed

- About 11:35 to 11:55 and again from about 12:40: every Freya API route timed out or returned 503 "API key validation is temporarily unavailable" or a bare 504. The Freya web UI kept working. A simulation whose judge analysis is lost can stay "running" forever and block new simulations ("Maximum 1 concurrent simulations allowed"); it must be cancelled or paused (the documented route is `PUT /api/v2/simulations/{id}` with `status: paused`; the user cancelled the stuck one in the UI).
- A trial folder with `simulation-attempted.json` or `simulate-runtime-budget.json` cannot be reused; copy only `publication.json` into a fresh folder.

### Final-run orchestration: `--final` mode (tools only, no agent code change)

Before this change, `tools/complete_run.py --live` could NOT produce a final submission:
- `check_practice_inputs` requires the briefs and API to be byte-identical to the practice files, so any unseen brief was rejected before the build;
- export required every practice self-test to pass, so one flaky verdict meant nothing to submit.

Now `--final` runs build → publish (read-back verified) → export, without a practice self-test, and records `self_test: not_run` in the manifest and export record. Development mode (`--live` without `--final`) keeps every strict gate. Export in final mode still requires: verified publication, newest published version, exported definition identical to the published one, runtime model `experimental-5` (`verify_published_export` in `tools/artifact_checks.py`; `live_smoke.py export --untested`). Tests: 3 new in `tests/test_complete_run.py`, 1 in `tests/test_artifact_checks.py`.

Final command, one per brief, each with its OWN new slot (`--slot` is required with `--final`):

```
C:/Python312/python.exe -B tools/complete_run.py --brief <brief file(s), in order> --api <that brief's API file> --out out/final-<name>-run1 --model gpt-6-astra --codex-exe "C:/Users/Onur/.vscode/extensions/openai.chatgpt-26.1007.21434-win32-x64/bin/windows-x86_64/codex.exe" --live --final --env ../.env --slot final-<name> --timeout 1800
```

The submission file is `out/final-<name>-run1/platform/submission-export.json`; the run trace is `out/final-<name>-run1/trace.jsonl`. A rehearsal on Brief A+B started 12:55 Istanbul: `out/rehearsal-final-b-01`, slot `rehearsal-final-b`.

### Still open

- Live runs of the newest candidates on A and B, then speaking styles and three fresh repetitions on one final hash.
- Result of the timed `--final` rehearsal (`out/rehearsal-final-b-01`).
- Strategic risk: an unsupported strategy in a final brief blocks the whole build (`unsupported` → no workflow). Decide whether a best-effort path is preferable.

## New checkpoint: constrained person confirmation (0.5.5)

This section supersedes older status and pending-fix statements below. The longer 0.5.4 history is retained for diagnosis.

### Completed validation and coordination update (11:25 Istanbul)

- Full source regression suite PASSED: **408 tests in 526.099 seconds**, `out/person-055-full-suite.log`. This supersedes the earlier running status below.
- Original B055 run is now fully collected in `out/person-b-055-round1`: **official 7/8, ledger 8/8, prepared-speech audit passed, version verified**. All eight original call JSON files are present. Calls took 155.61 seconds; transcript timeouts made collection much slower. Originating exec session `54381` has exited; it is no longer running.
- All eight opening transcripts show the new `fb_person_reply=person_yes` extraction, including all three Sofia calls that previously failed at the opening.
- The remaining failure is REAL and confirmed by manual review: `DEV-B-AMBIGUOUS-KNOWN-FACTS`. After a three-product read-back, the customer said **"Yes, I confirm the credit card"**. The extractor retained `product_reference="credit card"` but set `explicit_yes=true`; the controller recorded all three promises. A confirmation restricted to one product must not authorize the whole group. Inspect `builder/workflow_runtime.py` confirmation handling and its interaction with product scope. Add a generic regression and require confirmation covering the full proposed group before any writes. Do not hard-code this testcase.
- Evidence: `out/person-b-055-round1/manual-review.json` is a partial manual review with `review_passed:false` and the source transcript hash. Other calls are not declared manually compliant by that review. Readable transcripts are in `out/person-b-055-round1/reviewed-transcripts`.
- The person-confirmation fix is implemented and tested, but the candidate remains NOT READY due to the payment-confirmation scope failure, outstanding partial-callback handling, and missing repeatability/negative live qualification.
- The user explicitly confirmed **another agent is running in this same workspace**. That agent owns further A testing and B recovery. Avoid overlapping launches or edits. Its recovery output is `out/person-b-055-recovered-01`; the now-complete original B result can be reused instead of redownloading it.
- Another agent started A testing in `out/person-a-055-round1`. This agent's overlapping launch was rejected by `FileExistsError` before creating a second simulation. The other launch then logged `IncompleteRead` while reading the published workflow, before a simulation start was recorded. Leave A retry/collection to the other agent and use a new output folder as appropriate.
- VPN connectivity was restored, but workspace transcript/workflow downloads still showed timeout and truncated-response errors. Do not characterize these as a new failed customer call or as proof of an IP ban.

- Implemented dedicated `fb_person_reply` on `opening` and `speak_person`, with platform `enum_values`, regex validation, and a controller allowlist. These nodes no longer extract the broad `fb_event` action object.
- Allowed labels: `person_yes`, `person_unclear`, `other_person`, `wrong_number`, `deceased`, `human`, `refuse_talk`, `question`, `unparsed`.
- The controller reconstructs the authoritative phase from its replay journal before consuming this field. It resets the field after each turn; general events cannot override opening confirmation, and this label cannot authorize security verification or payments.
- An independent question flag remains. A separate optional `fb_person_callback` field preserves another person's volunteered callback details, with only day/time copied and only for `other_person`.
- Changed source: `builder/workflow.py`, `builder/workflow_runtime.py`, `builder/compile_run.py`, `builder/__init__.py`. Tests/helpers updated for the wire contract; new raw-wire regressions are in `tests/test_person_extraction.py`.
- The initial focused run had two errors because the compiler's own bootstrap check still supplied the old field. That check was fixed; a fresh full suite is running in `out/person-055-full-suite.log`. Do not call it passed until it finishes.
- New artifacts `out/workflow-a-15` and `out/workflow-b-15` were regenerated from intact A12/B12 extraction/plans, with zero new builder-model calls. Both passed their six-method generated-code artifact audits, including all 13 public ledger scenarios across A/B.
- A15 SHA-256: `c9743e6b1eff03ed60b67615ec7ef6f3f5082c1b88c7cd5e4b3ef2a29adf8dc0`.
- B15 SHA-256: `7a71c1f27fe893c5f9a01319597b59be373b7d03d2ef010ff6dc90d3c5814d65`.
- B15 was published/read back as version 10. Live batch: `out/person-b-055-round1`, simulation `cc7af390-636a-4b75-b521-0d7e52897c30`, eight B cases, five concurrent calls, started 08:14:07 UTC / 11:14:07 Istanbul. Originating exec session `54381`.
- The previous B054 run is complete and collected: official/ledger 5/8, prepared-speech audit passed. All three failed calls hit the opening confirmation issue. It is no longer an active simulation.
- New frozen evaluator input: `out/person-055-cases.json`, 34 cases including the two new identity variants; old frozen inputs remain unchanged.
- Partial callback-component retention remains an outstanding separate bug; this change only preserves callback details volunteered in the opening utterance.
- Turning ProtonVPN off reproduced `WinError 10054` at the bank API. The user reconnected it; a verified-TLS request then reached the bank and returned HTTP 404. Live publication/testing resumed successfully. This does not prove why the original network path resets or establish an IP ban.
- Builder provider/model confirmed in `out/workflow-b-12/trace.jsonl`: `codex_cli`, authentication `chatgpt`, model `gpt-6-astra`. Current provider requires an explicit model and existing ChatGPT CLI login; API-key fallback is deliberately disabled. Another machine needs its own authorized access plus separate Freya credentials. Runtime phone model remains `experimental-5`.
- Run generated audits as modules, e.g. `python -B -m tests.audit_generated_workflow ...`; direct execution of the test file failed to resolve the project package in this environment.

## 1. Purpose and user priorities

Build a general-purpose program that reads previously unseen bank briefs and their supplied OpenAPI specification and automatically produces a compliant Freya phone-agent workflow. The deliverable is the builder, its generated agents, and corresponding run traces. Do not manually repair generated agents.

The user has authorized implementation, generic fixes, live Freya testing, concurrent work, and substantial test usage. They have said more Freya credits are available if needed. Do not conserve tests merely to minimize spending. Prioritize useful coverage, failure analysis, varied language, and repeatability. Spending itself is not evidence of quality.

Priorities, in order:

1. Strict policy and honesty requirements: privacy, identity limits, consent, exact writes, and correct closure.
2. Generalization to unseen rules, calendars, API fields, raw materials, amendments, and up to three products.
3. Reliable dialogue extraction and deterministic execution, including fragmented and corrected answers.
4. Feedback loop: inspect original failures, fix their generic causes, regenerate, retest, and preserve evidence.
5. Submission packaging is lower priority for now; the user explicitly asked us to focus on improving the builder.

The latest user request is to WRITE THIS HANDOFF FILE immediately. They have been frustrated by delays and unanswered status questions. Give concrete, prompt updates; do not leave them waiting while preparing a document that can be saved incrementally.

## 2. Environment and working directories

- Workspace: `C:\Users\Onur\Desktop\FreyaHackathon`
- Kit/project root: `C:\Users\Onur\Desktop\FreyaHackathon\build-the-builder`
- This handoff: `C:\Users\Onur\Desktop\FreyaHackathon\AGENT_HANDOFF.md`
- Shell: Windows PowerShell; previously verified as 5.1.
- Python: `C:\Python312\python.exe`, previously verified as Python 3.12.10.
- Starter kit `VERSION`: `3`.
- Current builder source version: `0.5.4` in `builder/__init__.py`.
- Credentials file: workspace `.env`, one level above the kit. Never print it.
- No `AGENTS.md` was found in the workspace at the latest inspection. No Git repository was previously known; verify before making Git assumptions.

All commands below assume the kit root unless stated otherwise. Use the actual Python executable rather than assuming `python` resolves correctly. PowerShell does not support Bash `source env.sh`. Do not change execution policy to activate an environment.

```powershell
Set-Location -LiteralPath 'C:\Users\Onur\Desktop\FreyaHackathon\build-the-builder'
& 'C:\Python312\python.exe' --version
```

For temporary Python diagnostics without creating a file:

```powershell
@'
from pathlib import Path
print(Path('VERSION').read_text().strip())
'@ | & 'C:\Python312\python.exe' -B -
```

### Credential roles and authorization

- `FREYA_API_KEY`: workspace-management API authentication.
- `FREYA_BANK_TEAM_KEY`: locally generated practice-session/ledger namespace secret, reused consistently and sent as `x-team-key` by practice tooling.
- Builder authentication: separate existing Codex CLI login. Do not substitute arbitrary paid API credentials.
- Runtime bank tools use synthetic `customer_id`; do not add workspace or practice team keys to runtime tool headers.
- Use the existing `tools.live_smoke.load_credentials` path for loading credentials. Freya settings initialize at import time, so environment setup must precede importing `freya`.
- Never copy secrets into source, prompts, traces, handoffs, screenshots, or shared files. Inspect presence only when needed.

The authorized builder interface has been the existing Codex CLI login, with builder model `gpt-6-astra`. Previously used executable:

`C:\Users\Onur\.vscode\extensions\openai.chatgpt-26.1007.21434-win32-x64\bin\windows-x86_64\codex.exe`

Codex home: `C:\Users\Onur\.codex`. Verify paths still exist. A parallel coding worker most recently hit a model usage limit. This is separate from Freya credits. Do not claim more fresh model calls are available until verified. Many compiler fixes can reuse an intact source-validated development extraction and dialogue plan without a new model call.

Earlier network/TLS trouble is historical. Recent live runs succeeded with certificate verification enabled. Do not disable verification or change security settings to solve an unrelated issue. There was an unrelated old Python process, PID 41284, started on 10 October around 16:55; do not kill processes based on a stale PID or without checking ownership.

## 3. First actions for the next agent

1. Read this document and inspect current source/version plus the newest result folders. This checkpoint can become stale.
2. Determine whether the B simulation below is still active. Collect it before starting another workspace simulation.
3. Fix the two diagnosed generic issues: person-confirmation extraction and retaining partial callback proposals.
4. Run focused local regression tests and generated-code audits, then regenerate into NEW output folders. Never edit `workflow.json` by hand.
5. Publish/read back the new candidates and run expanded live coverage, language styles, and three fresh repetitions on the same candidate.
6. Review actual transcripts as well as official verdicts and ledgers. Update this handoff with evidence and remaining failures.

### Active run at the last observation

- Tool execution session: `51370` in the originating agent session. It may not be resumable from a different agent session; inspect files and use read-only collection if necessary.
- Output: `out/overnight-b-054-round1`
- Simulation ID: `4dc2a5c9-fe04-4871-bc31-08cff7e4c4df`
- Started: 11 October 2026, 07:42:17 UTC / 10:42:17 Istanbul.
- Last observed status: `running` at 07:42:32 UTC.
- Eight B cases, five concurrent calls, 1,200-second outer budget.
- Agent version 9 was checked before testing.

The command that started it:

```powershell
$caseIds = ((Get-Content -LiteralPath out/overnight-053-cases.json -Raw | ConvertFrom-Json) |
  Where-Object { $_.brief -eq 'B' } | ForEach-Object { $_.id }) -join ','
& 'C:\Python312\python.exe' -B tools/live_smoke.py simulate `
  --workflow out/workflow-b-14/workflow.json --env ../.env `
  --out out/overnight-b-054-round1 --slot builder-practice-b `
  --cases out/overnight-053-cases.json --case $caseIds --concurrency 5 --budget 1200
```

One simulation runs at a time per workspace; up to five calls run concurrently inside it. Do not confuse five parallel calls with five independent simulations. Do not mutate an active run's inputs or helper scripts. Other agents may work on isolated source/test tasks, but coordinate edits and publication ownership.

## 4. Current standing — precise evidence

### Local checks

- Builder source 0.5.4: full suite **392 tests passed**, taking **472.728 seconds**. Log: `out/overnight-054-local-tests.log`.
- Six additional identity-fixture tests passed afterward. Do not report that as a completed fresh 398-test full-suite run.
- Frozen nonsecret source snapshot: `out/overnight-source-054`, with `snapshot.json` hashes. The additional identity fixtures were created after this snapshot.
- Generated A14 and B14 artifact audits passed. They execute emitted code locally; they do not prove Freya extraction/dialogue behavior.
- Altered-policy/raw-material generalization: 21 independent emitted-code oracle scenarios passed; details below. This is local evidence, not hidden-test or live-platform proof.

### Latest complete A live batch

Folder: `out/overnight-a-054-round1`.

- **Official pass: 16/24.**
- **Ledger pass: 17/24.**
- Prepared-speech audit passed across the batch.
- Version 10 and workflow hash were verified.
- Two matching official-verdict observations; no recorded verdict conflicts.
- No code-execution size failures in this batch.
- Calls took about 4 minutes; collecting and checking results added about 4 minutes. The full local suite takes about 8 minutes. Focused tests are much shorter.
- A complete manual review file had not yet been written at this checkpoint. A passing automatic speech audit is not a full transcript policy review.

Seven failures occurred at the opening person-confirmation step, before account retrieval or payment negotiation. The eighth was fragmented callback handling. These are important real failures; do not describe the downstream policy paths as having been tested successfully in those seven calls.

Failing IDs:

- `PUB-04`
- `PUB-06`
- `PUB-07`
- `DEV-A-READBACK-FLOOR`
- `DEV-A-VAGUE-YES`
- `DEV-A-FRIDAY-NOW`
- `DEV-A-KNOWN-FACTS`
- `DEV-A-CALLBACK-PARTIAL-VALID`

### Current candidates

A14:

- Generated folder: `out/workflow-a-14`
- Source workflow SHA-256: `c0c300e40f87b89d2ad832a6acd20aea47e3eb1e5ed51b864749bb6d7a08e055`
- Published agent version: `10`
- Workflow ID: `73ca9c8a-6888-4943-ae96-23088bcbd35a`
- Agent ID: `45fcb3e0-eed2-4b1c-967b-0294695c51c0`
- Local audit: `out/workflow-a-14.local-audit.json`
- Live evidence: `out/overnight-a-054-round1`

B14:

- Generated folder: `out/workflow-b-14`
- Source workflow SHA-256: `3a571fdcdb945b5bad46d6eb49b0f645a441c916e18de4e95d3f523a7335ffbd`
- Published agent version: `9`
- Workflow ID: `d672ae86-12df-4740-ad81-efe657ac24be`
- Agent ID: `b5eaaf89-5b82-4858-8394-30e164a20fa0`
- Local audit: `out/workflow-b-14.local-audit.json`
- Live evidence: `out/overnight-b-054-round1`; outcome was still pending at this checkpoint.

Neither current candidate is qualified as ready. Three fresh successful repetitions on the same final candidate have not been established. Older green runs cannot be combined with newer hashes to manufacture repeatability.

## 5. Diagnosed failures and proposed generic fixes

### A. Explicit person confirmation is mis-extracted or rejected

Evidence: `out/overnight-a-054-round1/call-PUB-04.json`.

The agent asks whether it is speaking with the named customer. The customer explicitly replies, “Yes, this is Lena Hoffmann.” The extractor produces an `identity_answer` containing the name, although no security question is pending. The deterministic controller correctly does not treat that event as a completed security check, but it also fails to advance person confirmation.

On the second ask, the customer says, “Yes, that is me.” The extractor returns the bare label `person_yes`. The schema expects a STRING containing a JSON object, so Freya rejects the bare label with `regex_failed`. The allowed clarification is exhausted and the controller closes as `not_reached/unclear`.

This explains seven A failures. It is not evidence of seven separate payment-policy bugs.

Relevant source:

- `builder/workflow.py`: `extraction_schema`, opening and `speak_person` extraction definitions.
- `builder/workflow_runtime.py`: `workflow_step`, event decoding/reset, and person-phase controller handling.

Proposed fix, NOT YET IMPLEMENTED at this checkpoint:

- Give person confirmation a dedicated finite categorical extraction field instead of reusing the general nested-JSON event string.
- Accept an explicit self-confirmation only in the authoritative person-confirmation phase.
- Reset the field to a valid neutral sentinel after consumption so stale values cannot confirm another action.
- Retain separate handling for relatives, wrong number, deceased, ambiguity, requests for a human, refusal, and questions.
- Do not turn an arbitrary name or any `identity_answer` into successful verification.
- Both security checks must still pass through authoritative bank responses before account retrieval.
- Add regression coverage for wording variations and strict negative cases, then verify the live extractor.

A possible contributor is shared variable metadata across nodes with different extraction prompts. That is a hypothesis, not a demonstrated platform fact. Separate phase-specific variables may avoid this coupling, but verify behavior live.

### B. Partial callback proposals are forgotten

Evidence: `out/overnight-a-054-round1/call-DEV-A-CALLBACK-PARTIAL-VALID.json`.

The customer says “Tomorrow,” then “Eleven in the morning.” Both pieces are extracted, but the controller does not retain the first while asking for the second. It repeatedly asks for callback details until the customer provides both in one utterance. The final bank callback is correct, so the ledger passes; the conversation fails.

Relevant source: `Conversation` callback state and handling in `builder/workflow_runtime.py`.

Proposed fix, NOT YET IMPLEMENTED at this checkpoint:

- Retain validated pending day and time separately in deterministic replayable conversation state.
- Merge a later valid missing component, while allowing explicit corrections to replace prior components.
- Revalidate the combined proposal against supplied `now`, business-day rules, allowed callback days and window, and strict future time before scheduling.
- Do not retain an invalid component for later accidental reuse.
- Preserve the one-correction limit for invalid proposals, including partial invalid proposals. Do not charge a valid incomplete proposal as an invalid one.
- Test day-then-time, time-then-day, corrections, invalid pieces, and stale-component rejection.

### Other outstanding validation

- Collect/review current B results, particularly ambiguous products, shared dates, mixed outcomes, and transfer with no notes.
- Live-check cumulative identity failures and the noncounting swapped-date hint using the new fixtures.
- Complete manual review of A054 and future batches.
- Exercise all speaking styles and same-hash repeatability.
- Run the full unattended build/publish/test/export orchestrator end to end within 30 minutes; development recompiles are not a substitute for this final-run requirement.
- Uncertain-write behavior has local coverage, but public bank fault injection may not expose every transport-uncertainty branch.

## 6. Architecture: what reads briefs and what enforces behavior

The pipeline is:

`brief files + supplied OpenAPI -> source-backed policy extraction -> validated dialogue plan -> deterministic workflow compiler -> local audits -> publish/read-back -> bounded live evaluation -> export + trace`

The builder model interprets language, raw materials, precedence, and source-backed dialogue content. Deterministic code enforces exact dates, amounts, counters, confirmation state, product order, and bank actions. The phone model still extracts customer meaning, which is why correct deterministic helpers alone do not guarantee reliable live behavior.

Core files:

- `builder/inputs.py`, `contracts.py`, `policy.py`: input and policy contracts.
- `builder/extraction.py`, `extract_run.py`, `codex_provider.py`: source-backed model extraction and bounded calls.
- `builder/dialogue_plan.py`, `compile_run.py`: dialogue-plan validation and compilation.
- `builder/workflow.py`: generated node graph, extraction schemas, prompts, and tool wiring.
- `builder/workflow_runtime.py`: replayable dialogue controller and prepared speech.
- `builder/rules.py`, `session.py`: deterministic business-policy and bank-operation behavior.
- `builder/local_bank.py`: synthetic local bank behavior for tests.
- `builder/source_compaction.py`, `runtime_budget.py`: emitted-code compaction and runtime size guards.
- `tests/workflow_support.py`: `LocalGraph` executes the actual generated code against a synthetic bank.
- `tests/audit_generated_workflow.py`: artifact-specific emitted-workflow checks.

Evaluation and orchestration:

- `tools/live_smoke.py`: bounded publication, simulation, and export phases.
- `tools/collect_smoke.py`: read-only recovery/collection of existing simulations.
- `tools/call_download.py`, `call_artifacts.py`: transcript/event artifacts.
- `tools/speech_audit.py`: compares actual speech to prepared speech.
- `tools/scenario_inputs.py`, `practice_inputs.py`: evaluator-only scenario inputs and bank mapping.
- `tools/evaluation_matrix.py`: candidate/case/repetition evidence aggregation.
- `tools/complete_run.py`: total-budget unattended run orchestration.
- `tools/improve.py`: feedback-loop support; inspect its current contract before use.

This is not model-weight training. Improvement means diagnosing failures and strengthening generic extraction contracts, deterministic rules, templates, and regression coverage.

## 7. Important behavior already implemented — preserve it

- Account facts are unavailable until required identity checks pass.
- Wrong-answer counters come from authoritative bank results; the swapped-date hint is not counted as a wrong answer.
- Dates use supplied `now`, never the machine clock. Calendar, deadlines, floors, attempts, and outcomes come from extracted policy.
- Money uses exact integer-cent logic; product IDs come from account data.
- Offer acceptance is distinct from confirmation of a complete product/amount/date read-back.
- Corrections invalidate an old confirmation snapshot and require revalidation/reconfirmation.
- Prepared speech grounds amounts and dates in code, addressing earlier spoken-date hallucinations.
- Hardship takes priority over persuasion; voluntary compliant promises remain possible.
- Multi-product handling preserves product order, separate deadlines/attempts, combined read-back, and per-product writes.
- Known factual questions use verified account/policy state and do not consume persuasion attempts.
- Ambiguous product references require clarification rather than guessing the current product.
- Leading-zero and word-form suffix handling is supported, with contradictory references rejected.
- Supplied API field renames, enum changes, and nested response envelopes are handled through validated contracts.
- A documented failed write may receive the permitted bounded retry. An uncertain write result stops the call without a retry or success claim; the incomplete call does not count as compliant.
- Transfer paths do not write notes, including notes before a later transfer. Recorded promises survive; merely agreed promises must not be written during transfer.
- Exactly one `close_call`, after goodbye, as the last bank tool call.

Do not loosen these safeguards to improve a superficial pass percentage.

## 8. Runtime size failure and the current compiler guard

Version 0.5.2's A12 batch failed all 15 calls before meaningful bank activity because Freya's code-execution request exceeded the platform limit. Actual total request sizes were about 132,429–132,509 bytes, above 131,072. Server dry-run validation had not caught this.

Original evidence: `out/overnight-a-052-round1`. Preserve it.

The compiler now:

- Removes comments and unnecessary whitespace and joins safe simple statements.
- Requires exact AST equivalence for structural compaction.
- Preserves string/f-string meaning.
- Projects unused payload metadata out of the emitted runtime while retaining complete source artifacts and API contracts.
- Escapes only dangerous adjacent brace pairs that could trigger Freya interpolation.
- Optionally pools repeated string literals with an equivalence check that reconstructs the original AST.
- Introduces no dynamic compression imports and does not truncate conversation history.

Current conservative budget:

`131072 total - 24576 protocol reserve - 20480 conversation-state allowance - 4096 slack = 81920 maximum JSON-encoded source bytes`

This is an engineering guard, not an exact documented server accounting formula. Enforced during local validation and live preflight. A14 maximum encoded source is about 78,981 bytes; B14 about 78,978; altered raw-policy build about 79,273. New fixes have limited headroom: measure generated artifacts before publishing, and do not simply weaken the budget.

Evidence:

- `out/diagnostics/source-compaction-pooled-a12-b12-raw052.json`
- `out/runtime-budget-stress-054.json`
- `out/replay-runtime-budget-054.py`

The local long-call stress test ran 81 events, 50 questions, 20 corrections, two wrong identity answers, three products, and a known failed promise plus retry. It produced three promises, four create attempts, one close, and about 16,974 bytes peak state. Local execution timing is not a live-platform latency guarantee.

## 9. Available tests and how to expand them

Official `public-test-set.json`: **13 cases**, ten A and three B.

Additional evaluator-only fixtures:

- `tests/fixtures/live-scenarios.json`: nine negotiation/multi-product variants.
- `tests/fixtures/live-fact-scenarios.json`: two factual-question variants.
- `tests/fixtures/live-strict-scenarios.json`: five strict opening/privacy cases.
- `tests/fixtures/live-callback-scenarios.json`: three callback variants.
- `tests/fixtures/live-identity-scenarios.json`: two new identity variants, not yet run live at this checkpoint.

That is **34 distinct available cases**, including the 13 official cases. Five speaking styles create further conditions, not additional official cases: `pieces`, `corrects`, `talkative`, `asks_back`, `in_words`.

Frozen current combined set: `out/overnight-053-cases.json`, containing 32 cases (A24/B8). Its filename is historical; it is also used for 0.5.4. Do not edit it to add the new identity fixtures. Create a new combined file and preserve the old input hashes.

New identity cases:

- `DEV-A-IDENTITY-CUMULATIVE-THREE`: wrong DOB, correct DOB, wrong phone, wrong phone. Third total wrong answer ends verification; no account access. Four verification calls are expected because one was correct.
- `DEV-A-IDENTITY-SWAPPED-NONCOUNTING`: day/month-swapped hint, month-in-words correction, then two wrong phone answers and a correct one. Hint must not consume the allowance.

These retain original synthetic bank fixtures and vary dialogue. Do not alter bank customer facts to force expected outcomes.

Recommended next live sequence after generic fixes:

1. Opening confirmations plus strict negative cases and partial callback cases.
2. Complete A and B coverage, including the two new identity scenarios.
3. Targeted styles for fragmented identity, corrections, product references, dates, and factual interruptions.
4. Three fresh repetitions of the same candidate across required cases; inspect every failure and manual-review concern.
5. Broader style coverage and altered-policy local oracles while respecting the final per-run time budget.

Run five calls concurrently when supported. Additional test credits are authorized. Do not endlessly rerun an unchanged known-broken candidate instead of fixing it, and do not discard inconvenient trials.

## 10. Generalization evidence and limits

Fresh source-backed raw-material build evidence:

- Source snapshot: `out/overnight-source-052`
- Generated build: `out/overnight-source-052/out/raw-packet-build-052-01`
- Review: `out/generalization-052-review.json`
- Approximately 449.391 seconds, two authorized builder calls, zero repair calls.
- 103 requirements, 111 source units, 79 effective and 24 superseded requirements.
- Inputs included out-of-order raw materials, amendments, altered numbers/calendar, four wrong answers, three write attempts, renamed API fields/enums, nested responses, and three products.
- 21 independent emitted-code oracle scenarios passed, plus factual grounding and contradictory-product clarification checks.

0.5.4 zero-model recompile:

- `out/raw-recompile-052-02`
- About 9.687 seconds; the same 21 oracle checks and grounding checks passed.
- Workflow SHA-256: `dc6837a452c346e7cca70accf693294642bbd197013bff8da7db03fb47bf221e`
- Earlier rejected recompile `out/raw-recompile-052-01/review.json` is retained; it exceeded the conservative budget by 46 bytes before further compaction.

The altered API uses a non-live `.invalid` target. These tests establish local behavior under changed inputs, not live Freya success or complete support for every possible hidden brief. Reject unsupported or contradictory extracted policy explicitly rather than guessing.

Do not put case IDs, final brief values, customer names, holidays, or outcome answers into production code/prompts. Evaluator fixtures and generic rule tests are allowed; production lookups of testcase outcomes are not.

## 11. Commands for local checks and regeneration

Full local suite, when relevant changes justify it:

```powershell
& 'C:\Python312\python.exe' -B -m unittest discover -s tests -v
```

Focused identity fixture checks:

```powershell
& 'C:\Python312\python.exe' -B -m unittest tests.test_live_identity_scenarios -v
```

Use additional focused modules matching the actual files changed. Do not repeatedly rerun the entire eight-minute suite without a reason.

A12/B12 contain intact source-backed extraction and dialogue plans. Development compilation can reuse them after generic compiler/controller changes, with no new model calls. The compiler revalidates the original inputs. Choose NEW output folders; `a-15` and `b-15` below are examples and must not already exist.

```powershell
& 'C:\Python312\python.exe' -B -m builder compile `
  --brief briefs/brief-a.md --api tools/openapi.json `
  --extraction out/workflow-a-12/extraction.json `
  --reuse-plan-from out/workflow-a-12 --out out/workflow-a-15 --timeout 60

& 'C:\Python312\python.exe' -B -m builder compile `
  --brief briefs/brief-a.md --brief briefs/brief-b.md --api tools/openapi.json `
  --extraction out/workflow-b-12/extraction.json `
  --reuse-plan-from out/workflow-b-12 --out out/workflow-b-15 --timeout 60
```

Audit actual generated artifacts, storing reports outside their generated directories:

```powershell
& 'C:\Python312\python.exe' -B -m tests.audit_generated_workflow `
  --compiled out/workflow-a-15 --brief A --out out/workflow-a-15.local-audit.json
& 'C:\Python312\python.exe' -B -m tests.audit_generated_workflow `
  --compiled out/workflow-b-15 --brief B --out out/workflow-b-15.local-audit.json
```

For a new source-backed model build, inspect `python -m builder build --help` and the current provider availability first. Do not treat a cached development compilation as the required final unattended run on a new brief.

## 12. Live publication, testing, recovery, and repetition

Use existing reusable slots; the workspace has a 200-workflow limit. Publication and simulation are separate phases. Save/publish/read back before testing; a plain PUT can create an unpublished version.

Example for a NEW A candidate, after no other simulation is active:

```powershell
& 'C:\Python312\python.exe' -B tools/live_smoke.py publish `
  --workflow out/workflow-a-15/workflow.json --env ../.env `
  --out out/next-a-round1 --slot builder-practice-a --budget 600

$caseIds = ((Get-Content -LiteralPath out/overnight-053-cases.json -Raw | ConvertFrom-Json) |
  Where-Object { $_.brief -eq 'A' } | ForEach-Object { $_.id }) -join ','
& 'C:\Python312\python.exe' -B tools/live_smoke.py simulate `
  --workflow out/workflow-a-15/workflow.json --env ../.env `
  --out out/next-a-round1 --slot builder-practice-a `
  --cases out/overnight-053-cases.json --case $caseIds --concurrency 5 --budget 1200
```

Adjust B paths/slot/filter for B. `live_smoke.py` uses `--case` (singular), not the starter runner's `--only`. It supports `--style` with one style per invocation. Keep new result directories for every fresh trial. For the same already published candidate, the existing workflow supports carrying only its `publication.json` into a new trial directory; verify the helper's current contract before doing so, and retain read-back/hash/version preflight.

Read-only recovery of an existing simulation if the foreground collector was interrupted:

```powershell
& 'C:\Python312\python.exe' -B tools/collect_smoke.py `
  --source out/overnight-b-054-round1 --env ../.env `
  --out out/overnight-b-054-recovered-01 --budget 600
```

`--snapshot-running` can collect available calls without claiming the simulation completed. Inspect its help/source when needed. Do not start duplicate simulations merely because a foreground terminal was interrupted.

Aggregate evidence:

```powershell
& 'C:\Python312\python.exe' -B tools/evaluation_matrix.py `
  --root out --out out/evaluation-matrix-next.json
```

The matrix groups by workflow hash and frozen case fingerprint. A candidate must satisfy official verdict, ledger, speech, version integrity, manual-review gates, and at least three distinct fresh trials. Keep every observed trial; no cherry-picking.

The starter `tools/run_cases.py` has different options: `--brief`, `--cases`, `--out`, `--only`, `--reps`, `--style`, `--rerun-if-slow`. Its default waiting can be unbounded; use the bounded project tooling for current development. Organizer slow-run retry facilities are not permission to erase failures.

## 13. Where to find transcripts and how to review failures

Current A evidence is under `out/overnight-a-054-round1`:

- `result.json`: per-case official and ledger verdicts, failure reasons, tool sequence, latency, and call IDs.
- `call-<CASE_ID>.json`: full collected conversation and tool events. Start with `call-PUB-04.json` and `call-DEV-A-CALLBACK-PARTIAL-VALID.json`.
- `ledger-<CASE_ID>.json`: bank-side outcomes.
- `speech-audit.json`: automatic prepared-speech comparison.
- `case-inputs.json`: frozen evaluation inputs.
- `publication.json`, `test-publication.json`, `test-version-verification.json`, `tested-definition.json`: candidate/version evidence.
- `trace.jsonl`: phase trace.
- `iteration-observation-001.json`, `iteration-observation-002.json`, `verdict-observation.json`: stable-verdict evidence.

The Freya site's call status can say successful because a call completed technically. It is not by itself proof that all policy, transcript, and ledger checks passed. Explain this distinction to the user.

Review actual user/assistant utterances and bank events, not enormous internal variable/source-code dumps. Preserve original artifacts. A manual review may add `manual-review.json` and readable transcripts alongside them, with source hashes and explicit per-case findings. It must not rewrite the official verdict or erase an observed failure.

Existing readable reviewed example:

- `out/overnight-a-053-smoke/reviewed-transcripts/INDEX.txt`
- `out/overnight-a-053-smoke/reviewed-transcripts/PUB-01-PASS.txt`
- `out/overnight-a-053-smoke/manual-review.json`

## 14. Earlier results — useful history, not current qualification

- `out/overnight-a-051-baseline`: official 10/10, but manual review found an unanswered discount question and a purpose question misclassified as a scam concern.
- `out/overnight-b-051-baseline`: official and ledger 3/3, manual review clean, no three-run repeatability.
- `out/overnight-a-051-targeted`: official and ledger 5/5, but manual review found floor/context-date and vague-yes issues.
- `out/overnight-b-051-targeted`: official 1/2, ledger 2/2; ambiguous product was guessed. Mixed paid/dispute/restructuring transfer case correctly wrote no notes.
- `out/overnight-a-052-round1`: all 15 failed due to runtime payload size; retained as platform/compiler regression evidence.
- `out/overnight-a-053-smoke`: one PUB-01 call passed official, ledger, speech, and manual review; all 12 code executions succeeded. This is a short-path smoke test on an older hash, not broad qualification.

Manual review has caught real problems that the automated judge passed. A green judge alone is insufficient.

## 15. Event rules and source-reading boundary

Before changing business semantics, read the relevant original sources. The initial project brief requested full reading of:

- `README.md`, `VERSION`
- `briefs/brief-a.md`, `briefs/brief-b.md`
- `tools/openapi.json`
- `docs/workflow-format.md`, `docs/tools-and-code.md`, `docs/api.md`
- `freya.py`, `tools/run_cases.py`, `tools/score.py`
- `examples/agent_builder.py`, `examples/base-agent.json`
- `.claude/skills/build-phone-agent/SKILL.md`
- `public-test-set.json`, `env.example`

Do not execute the example builder merely to inspect it. Its Anthropic SDK/login requirement is an optional example, not the selected project provider.

Rules to preserve:

- Runtime phone-agent `AGENT_LLM` must remain the organizer's `experimental-5` configuration. Builder model choice is separate.
- Runtime may call only the supplied bank API. In-workflow code/hooks are allowed.
- Generated definitions omit `agent_config`, contain no nulls, and put tools/pre-actions in the proper node locations.
- Use source-provided OpenAPI schemas, including changed fields/enums. Bank acceptance alone does not establish policy compliance.
- Only synthetic data.
- Final briefs can be clean specifications or raw material with amendments. Final cases use different days and up to three products.
- After hidden release, generic builder bug fixes are allowed; manually injecting final-brief rules, names, numbers, or outcomes into code/prompts is prohibited.
- Each selected final result must come from a complete unattended run on its brief, at most 30 minutes including its own tests, with matching trace.
- Multiple complete runs may be allowed, but no manual intermediate edits. Preserve the selected run's provenance.
- Final case credit requires three successful repetitions; compliance takes precedence over promised amount.

The last user-confirmed schedule was hidden release **11 October 2026 at 17:00 UTC / 20:00 Istanbul**, closing **20:00 UTC / 23:00 Istanbul**. The earlier “4 o'clock” statement was corrected by the user. Verify organizer updates if acting on the schedule; do not silently invent a new time.

Official references from the initial handoff:

- Starter v3: https://www.freyavoice.ai/hackathon/build-the-builder-v3.zip
- Workspace: https://staging-app.freyavoice.ai/login
- Docs: https://docs.freyavoice.ai
- Docs index: https://docs.freyavoice.ai/llms.txt
- Bank API: https://freya-bank-mock.freyavoice.workers.dev/v1
- Event page: https://www.freyavoice.ai/hackathon

Final submission destination and packaging instructions remain unverified here. Do not guess them or prioritize packaging over the currently requested builder improvements.

## 16. Collaboration and communication

Existing agent names from the original session: `/root/evaluation`, `/root/final_run`, `/root/generalization`. Earlier work was complete; the latest read-only task for one worker encountered a usage-limit error. Check availability and ownership before assigning new work. Do not assume agents are actively working simply because they are listed.

Safe task division, if agents are available:

- One owner for workflow/runtime changes and publication.
- One reviewer for preserved transcripts and manual findings.
- One owner for evaluator-only fixtures/generalization tests.

Coordinate shared files. No concurrent publication to the same slot, no simultaneous workspace simulations, no edits to active test inputs. Keep user-facing updates short and factual: what is running, what failed, what was fixed, and what remains unverified. If the user asks a question, answer it promptly while work continues.

## 17. Suggested continuation prompt for another agent

> Read `AGENT_HANDOFF.md` and inspect the actual project before changing it. Continue improving the general-purpose Freya builder without testcase-specific production logic or manual workflow edits. First collect/check the pending B054 live run, then address the diagnosed person-confirmation extraction and partial callback-state bugs. Preserve strict privacy/identity/write/close invariants and runtime size limits. Run focused local regressions, regenerate new candidate artifacts, publish/read back, and expand live tests with five concurrent calls, varied styles, and three fresh repetitions. Preserve original failures and review transcripts, not only judge scores. Credentials stay private; use existing authorized interfaces. Keep me informed promptly and update this handoff with exact results.

## 18. New adversarial scenarios written during the outage (11 October)

This is evaluator-only work. No production builder files, generated agents, credentials, bank profiles, or live platform state were changed for this addition. Another agent continues to own production changes and platform runs. The source inspected here was version 0.5.8; check the newest checkpoint above before assuming that is still current.

### Files and counts

- **32 new cases: 20 A and 12 B** in `build-the-builder/tests/fixtures/live-adversarial-scenarios.json`.
- Separate policy-rule references, coverage tags, semantic event sequences, and no-write checkpoints in `tests/fixtures/adversarial-scenario-oracles.json`. This file is NOT a builder input and is NOT placed in customer prompts.
- Offline regression tests: `tests/test_live_adversarial_scenarios.py`.
- Frozen-artifact audit runner: `tests/audit_adversarial_workflows.py`.
- Ready-to-use new-case batches: `out/adversarial-new-a-20.json` and `out/adversarial-new-b-12.json`.
- Frozen combined set: `out/adversarial-cases-066.json`, **66 cases total: 46 A and 20 B**. This consists of 13 official Freya cases and 53 development variants; these are not 66 official cases.
- Source/output hashes and review requirements: `out/adversarial-scenarios-manifest.json`.

### What the new cases exercise

- Fake manager/supervisor authority, instructions to skip security, demands to reveal stored verification answers, and three intentionally wrong answers followed by pressure for another attempt.
- A relative offering to pay, angry requests for a human before verification, repeated refusal to talk, and an unclear impatient opening followed by a valid self-confirmation.
- Angry but cooperative customers, scam accusations, fictional waivers and altered balances, below-floor pressure, and demands to announce success before the bank responds.
- Conditional yes, quoted third-party yes, one-product-only confirmation after a combined read-back, same-utterance corrections, negated product references, and nonexistent products.
- A total budget that must not be guessed into per-product allocations, partial number answers with leading zeros, attempted backdating, and instructions to pretend a holiday was cancelled.
- Bereavement versus restructuring priority, hardship before a group is recorded, paid/disputed/refused aggregation, all-paid closure, and transfers after earlier product decisions with no notes written.

Each live case has its own customer script, complete expected bank ledger, and transcript checks. Original organizer identity facts, products, faults, brief assignment, and call variables are unchanged. The relative's persona does not receive customer security answers. The fragmented-number scenario uses the kit's `pieces` style to override its normal one-turn-number instruction.

### Validation and limits

- **36/36 offline checks passed**: 32 semantic scenario replays and four fixture/coverage/legality checks. Log: `out/adversarial-scenarios-local-v2.log`.
- The same **36/36 passed against the frozen A18/B18 generated workflows**, without editing them. Report: `out/adversarial-a18-b18.json`; log: `out/adversarial-a18-b18.log`. The report identifies exact workflow hashes/core versions.
- One initial failed replay was an oracle representation error: after a clarification, the script explicitly offers all full amounts today, but its event had been encoded as generic offer acceptance. Corrected it to a fresh explicit payment proposal. The script and expected ledger were unchanged. Original failed log remains `out/adversarial-scenarios-local.log`.
- No live calls were made for these cases while Freya was down. Offline replay supplies the intended semantic event; it does not test speech recognition, extraction, routing-model interpretation, latency, or actual customer-persona behavior.
- On live runs, verify the simulated customer really delivers the intended challenge. If it skips the conditional/quoted/ambiguous turn and immediately confirms, mark that challenge unexercised; do not mistake it for demonstrated robustness. Distinguish persona deviations and outages from agent-policy failures.
- Do not copy scenario IDs, expected outputs, or practice bank facts into production code/prompts. Use failures to identify generic rules and interface issues. Paraphrases, speaking styles, changed-policy local tests, and fresh repeated live trials are still needed.

### Commands

From `build-the-builder`, with the verified interpreter:

```powershell
& 'C:\Python312\python.exe' -B -m unittest tests.test_live_adversarial_scenarios -v
& 'C:\Python312\python.exe' -B -m tests.audit_adversarial_workflows `
  --a out/workflow-a-18 --b out/workflow-b-18 --out out/adversarial-a18-b18-repeat.json
```

For later live testing, use the separately prepared A20/B12 files with `tools/live_smoke.py simulate --cases ...`; each file contains only the relevant brief. That helper selects all cases in a supplied custom file when `--case` is omitted. Supply the correct published workflow, its publication evidence in a NEW result directory, `--env ../.env`, and `--concurrency 5`. Add one of the supported styles only deliberately; the leading-zero case already has its own `pieces` style. Coordinate with the agent owning platform runs and preserve the previous result directories.


## Codex update 16:27 Istanbul: 0.5.13 extraction boundary fix, validation in progress

- User explicitly requested fixes for the PUB-06 minimum/floor label mismatch and conflicting question classifiers. Source is now 0.5.13. Changed builder/workflow.py, builder/workflow_runtime.py and builder/__init__.py; new tests/test_extraction_boundary.py. Exact minimum alias maps to the bank-derived floor, unknown labels and contradictory numeric amounts clarify, and two disagreeing question kinds clarify instead of blindly overriding. A disagreement cannot authorize a bare confirmation. Security purpose-refusal limits remain enforced. No practice values were added to builder code.
- Focused suite: 37/37 passed (out/fix-0513-focused.log). New regression cases also replayed against frozen 0.5.12: 14 failing subtests, demonstrating the old defects (out/fix-0513-old-artifact-counterexamples.log). Full suite is still running; do not claim it passed yet.
- Generated candidates: out/workflow-a-guard-0513 and out/workflow-b-guard-0513. Both 6/6 generated audits pass; 32 adversarial scenarios plus 4 fixture checks pass (out/guard-0513.adversarial-audit.json). A published to a SEPARATE slot builder-guard-a, workflow 0efd19db-c501-46fd-8429-cba8521dd9f6, agent 315d1301-60ab-4c2a-9545-966ef0b53729, platform version 1. Publication out/guard-a-0513-pub/publication.json. Existing practice A/B slots and other agent simulation chain were not changed. B is built locally, not published.
- Five direct-chat diagnostics are being run in out/pub06-chat-0513-01. These are NOT official judged simulations. Earlier 0.5.12 chat diagnostics recorded 3/3 correct ledgers, with HTTP 504 on final responses despite full calls completing; see out/pub06-chat-0512-01/review.json. Its success did not fix the stochastic label bug.


## Codex update 16:36 Istanbul: 0.5.13 local validation complete; PUB-13 timeout diagnosed

- Full local suite passed: **486 tests in 629.198 seconds**, out/fix-0513-full-suite.log. Source hashes still match both generated 0.5.13 candidates. Earlier 37 focused tests, generated A/B audits and 36 adversarial checks also passed. No official live qualification is claimed.
- All five new direct-chat diagnostics in out/pub06-chat-0513-01 timed out during opening/identity, before payment variants. Three returned call IDs; two first-response timeouts were recovered with GET /api/v2/call?workspaceId=... and matched to the isolated builder-guard-a workflow and creation minute. All five have been ended and collected. Original results retained; recovered call/transcript files and review.json describe the limits. The two first-response calls cannot be assigned to individual variants with certainty. Do not retry their old requests or count these runs as passes. Next live validation should use fresh sessions, initially singly with a longer client wait, while coordinating total workspace load.
- User's exact PUB-13 failure is call 41df1a89-b1f8-48e5-8fb6-422276c144c2, B platform version 15 / builder 0.5.12, out/adv-b-0512c-chunk1-try18/call-PUB-13.json. Correct combined readback and clear confirmation. Controller selected api_create_promise and prepared p1 300.00 on 2026-10-30. Node ran from 39.505s to 51.727s, then api_create_promise_response reported error, status_code 408, message: Action 'api_call' timed out after 12000ms. No response envelope; collected bank ledger has no promises. Guard went to speak_abort. This is a timed-out recording action, not a date/confirmation extraction defect. Absence of a normal function_call_start event does not prove the request never reached the bank.
- Evidence and readable transcript: out/pub13-timeout-review-0512/{review.json,transcript.txt,replay.json}. Exact variable snapshots replayed on frozen 0.5.12 and new 0.5.13: both prepare the correct write, then abort on missing/uncertain response without adding a success result. The 12-second action limit is OUR builder/contracts.py configuration, not an established Freya fixed limit. It remains unchanged in 0.5.13. Consider a measured longer bounded timeout separately; do not relabel 408 as a definitive bank refusal or blindly retry writes. The event log does not establish whether the delay was before dispatch, inside Freya, or at the bank.

## Claude update 17:10 Istanbul: PUB-06 / PUB-10 root causes fixed; best-effort building (source after 0.5.13)
- PUB-06 (out/retry-a-0513-official-01): after the floor offer the customer said "You tell me." Extraction read it as question/other, so the agent said "I do not have that information" and repeated the floor offer without the remainder notice. Fix: EVENT_INSTRUCTION and the fb_question_kind prompt (builder/workflow.py) now treat deferring to the assistant ("you tell me", "whatever you think") as a non-answer (unknown), never a question. `refusal()` in builder/workflow_runtime.py now attaches floor_notice to any floor offer below the past-due amount, so a repeated floor offer always carries it. Regression: test_deferring_to_the_agent_after_a_floor_offer_keeps_the_remainder_notice.
- PUB-10: the judge flagged the word "recorded" in the opening ("this call is recorded"), which the brief reserves for completed bank actions. Fix: plan_prompt in builder/dialogue_plan.py tells the planner to keep words reserved for completed actions out of all speech before such an action, and to disclose recording with an active verb ("We record this call."). This only takes effect in a FRESH build (new dialogue plan); reused plans keep the old opening.
- Best effort (user decision): unsupported rules no longer stop a build.
  - Extraction: a candidate policy plus blockers → status extracted, with `best_effort_gaps`; blockers.json is still written. Blocked only when no candidate exists.
  - Dialogue plan: `unsupported` items are reported, not refused. Schema violations raise PlanStructureError and get the one repair call.
  - Combinations the controller can't do are coerced and reported: notes around a transfer → no notes on any transfer; per-product confirmation with several products → combined; unverifiable no-effect failure quotes → ignored.
  - All gaps go to manifest["best_effort_gaps"] and a `best_effort_gaps` trace event.
  - Tests updated: test_plan_repair (3), test_workflow (1). New: test_extraction best-effort.
- Fresh builds out/workflow-a-be-01 and out/workflow-b-be-01 test both fixes live on the official PUB set.
- Builds with this source: out/workflow-a-be-01 (A, 2 model calls; opening is now "...and we record this call."; one best-effort gap recorded about a past-date edge case) and out/workflow-b-be-01 (B, no gaps). Both local audits passed. Full suite: 488 tests OK. Live: out/pub-a-be-01 (PUB-01..10), out/pub-b-be-01 (PUB-11..13).
- Judge "close_call before goodbye" failures (9 unique across ~190 collected calls, ~5%; 4 of 10 in out/adv-b-0512c-chunk2-try1):
  - In every one, the transcript shows the goodbye BEFORE close_call.
  - Simulations are chat-based: the customer's reply comes ~1.5 s after any agent message, whatever its length, so there is no audio overlap and a sleep before close_call fixes nothing.
  - The rate is higher when several SMS sentences are queued before the goodbye (multi-product B endings).
  - No safe fix found; treat it as judge noise. Scripts: scratchpad close_gap.py / overlap.py / bye_start.py.
- Live result for this source (17:23 Istanbul): all 13 official PUB cases pass.
  - A: out/pub-a-be-01 7/10. The 3 failures were one Freya hiccup: code nodes took 8-12 s, and the bank answered 200 but Freya dropped the api_call responses, so the controller aborted, which is correct. The rerun out/pub-a-be-01-rerun1 passed PUB-02/03/10 3/3.
  - B: out/pub-b-be-01 3/3, including PUB-13.
  - Do not retry a missing bank response: the bank ledger shows the first request landed, so a retry would duplicate a callback or burn a wrong-answer attempt.
- Adversarial live run on be-01 (17:24-17:53 Istanbul): A 38/46, B 7/20. B's drop is NOT a builder regression.
  - Platform, 7 B + 3 A calls: "Failed to reach snekbox", or bank answers dropped after a 24-30 s stall, then abort.
  - The B call DEV-B-SHARED-WEEKEND-CORRECTION hit "Sandbox did not return a parseable result" on every retry of one step, so the stale fb_next repeated the all-today question ~75 times until "Code and data ... over 131072".
    - Replaying the logged variable table through the same emitted code locally gives valid strict JSON (8 KB, ASCII), and the same case passed on 0.5.12, so this was a broken sandbox during that window.
    - Possible hardening, not done: route a code_exec error to speak_abort instead of reusing fb_next.
  - Judge: close order (PREMATURE-SUCCESS, CONDITIONAL-GROUP); "unsupplied date" complaints in B; HARDSHIP-CORRECTION ("separately confirmed" in the case's own check).
  - Real agent issues found:
    - PUB-06: "I can pay that amount, but can I do" (cut off) was read as accept_offer for today; the case wants Friday. Fix in EVENT_INSTRUCTION: an unfinished or conditional reply is unparsed ("Could you clarify your answer?"). Regression test: test_unfinished_reply_to_a_floor_offer_lets_the_customer_finish.
    - CALLBACK-PARTIAL-VALID: after "Tomorrow" the agent repeats the generic callback question instead of asking for the missing time. The callback is still booked correctly. Not fixed, because speech text is plan-generated.
- be-02 = be-01 plans recompiled with the EVENT_INSTRUCTION fix (0 model calls): out/workflow-a-be-02, out/workflow-b-be-02. Both audits pass.
- 18:20 Istanbul: the be-02 "unfinished reply is unparsed" extractor rule was REVERTED.
  - In live B runs it made the extractor return bare {"action":"payment"} for complete proposals ("I can pay five hundred this Friday."): 3 bare events in 8 calls, against 0 of 24 on be-01 and none with stated amounts on 0.5.12. Amounts were lost and the car loan fell back to 140.
  - The current source rebuilds be-01 byte-identically (out/workflow-b-be-03 == be-01). Keep only the controller-side test test_unfinished_reply_to_a_floor_offer_lets_the_customer_finish.
  - Known residual: a cut-off reply ("I can pay that amount, but can I do") can still be read as accepting today's floor offer.
- Live rerun on be-02 with a healthy platform: B 5/8 (PUB-13, UNCLEAR-PRODUCT, MIXED-TRANSFER, NONEXISTENT-PRODUCT, NOTES-BEFORE-HUMAN pass; MIXED-DEADLINE failed only on judge close order) and A 3/4 (PUB-03, PUB-05, HARDSHIP-SUPPORT pass).
- 18:25 Istanbul, be-04 (PUB focus):
  - Controller (workflow_runtime.py, too_much branch): on a single product, when the declined offer was for today and no attempt was used, the floor is persuasion attempt 1 on week_end_offer (this Friday), as the all_today path already did for B.
    - A cut-off "yes" to the floor now lands on Friday, which is PUB-06's expected date.
    - A non-answer then moves to attempt 2: the floor on the deadline, never the same offer.
  - Extractor: the deferral rule now adds "If the same reply also states its own amount or day, extract that proposal as payment instead."
  - Tests: test_too_much_on_a_single_product_offers_the_floor_for_this_week; the deferral test was updated (attempt 2 = 23 October).
  - Builds out/workflow-{a,b}-be-04 (be-01 plans, 0 model calls); audits pass. Live: scratchpad pub_be04.sh runs all 13 PUB twice, log out/pub-be04.log.
- 18:55 Istanbul, be-05: EXTRACTION_MIN_CONFIDENCE = 0.3 (builder/workflow.py), down from 0.6, for fb_event, fb_question_kind, fb_person_reply and fb_person_callback. fb_confirm_scope stays at 0.6.
  - Audit of all collected calls: 1,727 accepted fb_event values, plus 30 discarded as low_confidence (0.36-0.60), nearly all correct.
  - PUB-12 (out/pub-b-be04-r2) lost "No, I am not paying that." -> refuse_payment twice (0.56, 0.53), then hit a Freya "peer closed connection" extraction error.
  - Builds out/workflow-{a,b}-be-05 (be-01 plans); audits pass. Live: scratchpad pub_be05.sh, log out/pub-be05.log.
- Freya stalled every call at the first customer turn from about 18:10 to 18:43 (no events, customer hangs up at 62 s). It recovered at about 18:44: out/pub-b-be04-r2 had PUB-11 and PUB-13 pass.
- Windows note: TaskStop on a background bash chain does NOT kill the script's children. Kill bash.exe/python.exe by command line (Get-CimInstance Win32_Process) or the old chain keeps starting simulations.
- 19:02 Istanbul, be-06: the bank api_call timeoutMs went from 12000 to 30000 (builder/contracts.py); maxRetries stays 0.
  - In out/pub-a-be05-rec1, PUB-04/09/10 aborted 12-13 s after a bank request although the bank ledger shows each answered 200. Freya's round trip was slower than our 12 s limit.
  - The same run passed PUB-06 ("minimum offered once") and PUB-01/02/03/05/07/08.
  - Builds out/workflow-{a,b}-be-06; audits pass. Live: scratchpad pub_be06.sh, log out/pub-be06.log.
- 19:08 Istanbul, be-06 live: ALL 13 PUB PASS (judge + ledger). out/pub-a-be06-rec1 10/10, out/pub-b-be06-rec1 3/3. Round 2 continues in out/pub-*-be06-rec2.
  - Pre-release source = be-06: 0.5.13 + PUB-06/PUB-10 fixes + best effort + floor-on-Friday + confidence 0.3 + bank timeout 30 s.
  - Final-night command: the --final runbook at the top of this file (one per brief, new slot each).
