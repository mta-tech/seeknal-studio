# Seeknal Studio agent pilot

This pilot uses the real Seeknal agent and engine. `pilot/template/` is the
repeatable source-only fixture. `tests/pilot_e2e.py` recreates
`pilot/workspace/` from that template for every live run. The completed first
pilot remains openable at `pilot/` with its model-generated transform and real
output. The two CSV inputs are synthetic and contain six orders and three
customers; the source-only template intentionally contains no target transform.

Provider contract:

- Sumopod OpenAI-compatible API: `https://ai.sumopod.com/v1`
- Exact model: `deepseek-v4.1-flash:netra`
- Credential file: `~/sumopod-ai-api-key`, read by the backend only
- No provider/model fallback is accepted

## Open the completed pilot

Double-click `Open Seeknal Pilot.command`, or run:

```sh
python3 open_pilot.py
```

This launcher always opens `pilot/workspace/`; it accepts no project argument.
It verifies the synthetic CSV fixture and removes only `ATLAS_API_URL` and
`ATLAS_PORTAL_URL` from the pilot child process so unrelated operator settings
cannot make this standalone fixture fail closed. The ordinary Studio launcher
and every other project retain their normal governance environment.

Acceptance sequence:

1. Recreate and start Studio on `pilot/workspace/`; verify owned-runtime project identity.
2. Submit the prompt through the browser and observe real tool events.
3. Confirm the agent produced only a draft and did not apply or run it.
4. Review the real diff, validate the exact draft, and explicitly apply it.
5. Explicitly run the pipeline from Studio.
6. Inspect the materialized Parquet output and compare it to
   `pilot/template/EXPECTED_OUTPUT.json`.
7. Verify lineage includes `transform.customer_value_pilot` with both sources.

The executable receipt is written to `evidence/pilot/e2e-real.json`. It records
the provider/model, tool names, file hashes, job IDs, output values, and status,
but never the API key or raw authorization headers.

Verified frozen-source evidence:

- `evidence/pilot/e2e-real.json`: the browser-submitted real model turn, exact
  client/runtime turn correlation, draft-only tool calls, reviewed/applied SHA,
  explicit run, three expected rows, and two lineage edges.
- `evidence/pilot/e2e-metamorphic.json`: one added synthetic paid order changed
  the expected aggregate, then source restoration recovered the baseline.
- `evidence/pilot/e2e-native.json`: actual Webframe loaded the verified owned
  runtime, three-node graph, fresh three-row preview, and successful validation.
- `evidence/pilot/e2e-package.json`: the rebuilt `.app` matched all eight bundled
  source hashes, loaded the completed project, and stopped its backend when the
  native window closed.

`evidence/pilot/fixed-launcher-native.json` further proves the fixed launcher
opened only the verified synthetic workspace, removed exactly the two unrelated
Atlas URL variables, ran all three nodes successfully, and displayed a fresh
three-row preview without recording credential data. Normal Studio project
launches retain their governance environment.

The launcher-hardened source passes 63 discovered tests, 9 frontend contract
checks, and 3 source-only fixture checks: 75 checks total. Independent
architecture review is CLEAR. The pilot is verified; the wider tracked goal
still awaits the root workflow's final state audit and completion transition.
