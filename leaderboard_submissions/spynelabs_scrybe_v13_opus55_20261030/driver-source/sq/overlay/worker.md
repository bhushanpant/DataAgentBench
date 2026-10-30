## Environment lessons
- Write files only under /home/ide/dab-work (the query and labelling tools save their results there); /tmp/opencode and the current directory are not writable, so `cat > x.py` or `mkdir` outside it fails with "Permission denied". Python can also run from stdin (`python3 - <<'EOF' ... EOF`).
- Do not `ls` or `cd` outside your own working area; if a path gives "Permission denied" once, do not retry it, use the tools and paths you were given.
- If a helper command fails to start (sandbox or container error, missing runtime, invalid token), do not call it again; continue with the query tool that already works.
- Use only the tools your assigned role permits; a read-only role must not call write tools.
- When an error appears, change the command; never rerun an identical failing command.

## Method lessons
- When a label or category must be inferred from free text, do one careful labelling pass, review only the borderline items once, then commit; do not keep rebuilding alternative classifiers, and record the sensitivity as a red flag instead.
- A degenerate result (a large tie at the cutoff, near-duplicate entities in a top-N, every group giving the same trivial value) usually means the entity key or a definition is wrong: re-check what counts as one distinct entity and how the measure is defined before committing.
- Copy names and labels exactly as stored in the data; do not retype or normalise them.
- Report computed numbers at full precision unless the question or format specifies rounding.
- Identifiers and join keys are often written differently across sources (case, padding, prefixes, separators): sample real values from both sides, normalise before joining, and check how many rows matched.
- A value the question needs may sit inside a free-text field rather than its own column: search the text fields before concluding it is missing.
- Look at actual rows before deciding how to join or parse; then answer with every value the question asks for.
