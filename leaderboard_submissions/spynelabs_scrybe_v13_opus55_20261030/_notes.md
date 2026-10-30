# Notes

- **Scoring.** The full final answer is scored; there is no answer-extraction step. The exporter's local scores use each query's `validate.py` at benchmark commit `61fb21e0766d`.
- **Models and providers.** Planner and verification calls report `claude-opus-5-5` with provider `firstParty` in their raw responses. The request log lines in `opus.jsonl` carry a `provider: openrouter` / `anthropic/claude-opus-5.5` label that is a stale default in the logger, not the route used; the raw response `modelUsage` is the authoritative record. Workers report provider `anthropic`, model `claude-opus-5-5`.
- **Counts.** 330 worker sessions, 3892 messages, 2963 tool calls, 1350 planner/verification calls, 1533 tool-proxy records.
- **Manifest.** `manifest.json` is the log of the whole development workspace. Its `schedule` and `config_changes` cover many other runs; this submission is run ids 41-45, which are outer runs 0-4 in the answer JSON (`agent-config.json` has the mapping). The `trials_per_query` field in it predates this run.
- **Infrastructure retries.** 4 in total, listed in the README; none depends on correctness.
- **Traces.** `spynelabs_scrybe_v13_opus55_20261030_traces.tar.gz` (79 MB) is hosted as a release asset: https://github.com/bhushanpant/DataAgentBench/releases/download/scrybe-v13-traces/spynelabs_scrybe_v13_opus55_20261030_traces.tar.gz.
