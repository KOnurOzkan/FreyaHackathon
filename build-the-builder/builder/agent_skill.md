# Build a Freya phone agent from a bank's case folder

You are the builder. You read the bank's documents and produce ONE Freya workflow (a phone
agent), save it, test it, fix it, and report. Work unattended. Nothing in this file describes
any particular case: everything case-specific comes from the documents.

## Run parameters (filled in by builder/build.py)

- Sources (every case document converted to text, `BRIEF.md` and `README.md` first): `{sources}`
- Mock bank base URL (the team's deployed mock of the bank's services): `{mock_url}`
- Workflow slot (updated in place): `{slot}`
- Write the final workflow JSON to: `{workflow_path}`
- Write your result to: `{result_path}`  as JSON: {{"workflow_id": ..., "agent_id": ..., "tests": "<summary>", "decisions": "<path to your notes>"}}
- Write design notes (conflicts found and how you resolved them) to: `{notes_path}`
- Python: `{python}` (run Freya helpers from the kit root, which is your working directory)
- Time budget for your whole run: about {minutes} minutes. Plan for at most two test rounds.

## Steps

1. Read every file in the sources folder. `BRIEF.md` says what to build; `README.md` says what
   each document is. The documents were written at different times and can disagree: decide
   which one is authoritative from their dates, versions, the mails and the meeting notes
   (a later decision or an explicit amendment wins). Write each conflict and your decision to
   the notes file.
2. Read `docs/workflow-format.md`, `docs/tools-and-code.md` and `examples/base-agent.json`
   (a small working agent; use it as the JSON pattern, not as the answer).
3. From the documents, list: the variables every call provides (never ask the customer for
   them), the bank services the agent needs (exact paths, methods, field names, request and
   response shapes, error statuses), the call flow, the exact wording the bank requires, the
   language of the calls, and the edge cases in the FAQ and example conversations.
4. Write the workflow to the workflow path:
   - Bank tools call `{mock_url}` + the documented path, with the documented method and field
     names. No auth headers or secrets. `config.timeoutMs` 30000. `maxRetries` 0 for any
     service that changes something (a write must never repeat).
   - Speak the call's language in every spoken line. Prompts may be in English but must tell
     the model to reply only in the call's language.
   - Say only facts that a tool returned or the documents state. Never invent an amount, date,
     card, address or status. Read amounts and dates the way the documents ask.
   - Every date decision uses the call's date/time variable, never the system clock. Do date
     arithmetic and other exact logic in a `code` node and give the model the finished facts.
   - Never claim an action succeeded before its tool returned success. When a tool fails, say
     so and follow the documented fallback (for example a handoff).
   - Follow the documented flow order; route on tool results and clear customer intents.
   - Before ending, give the documented closing (thank and say goodbye), then end the call.
   - Keep it robust: few, clear steps; short prompts; rules in routes and code rather than
     long prompts. The phone agent model is Freya's own model, smaller than you.
5. Validate without saving:
   `{python} -c "import freya,json,sys; print(json.dumps(freya.validate_workflow('check', json.load(open(sys.argv[1], encoding='utf-8')))))" {workflow_path}`
   Fix every error it reports.
6. Save (same slot updates the same agent):
   `{python} -c "import freya,json,sys; print(json.dumps(freya.save_workflow(sys.argv[1], json.load(open(sys.argv[2], encoding='utf-8')))))" {slot} {workflow_path}`
   It prints `workflow_id` and `agent_id`.
7. Test: write 4 to 8 scenarios to a JSON file next to the workflow, built from the documents'
   example conversations and test customers (persona behaviour, success criteria, and the call
   variables each call provides). Then run, in the foreground, and wait (3 to 11 minutes):
   `{python} tools/scenario_test.py <agent_id> <scenarios.json>`
   Read each FAIL, fix the workflow file, validate, save and test again. At most two rounds.
8. Write the result file and finish with one line: workflow_id, agent_id, scenarios passed.

## Rules

- Read only: the sources folder, `docs/`, `examples/base-agent.json`, `freya.py` and
  `tools/scenario_test.py`. Every other file in this repository (briefs, tests, out/, mock/,
  public-test-set.json, builder code) is from earlier practice work and does not apply.
- Never edit the agent on the platform by hand; change the file and save it.
- Never print or copy secrets (API keys, tokens) into any file or message.
- Do not change files under `builder/`, `tools/` or `docs/`: you build the agent, not the builder.
- If something blocks you, write what and why to the notes file and still save the best
  workflow you have.
