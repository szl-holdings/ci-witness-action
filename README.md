# SZL CI Witness — GitHub Action

CI as witness. Each branch run of your workflow appends one hash-chained receipt to
`ci-witness.jsonl`: repository, commit, run id, measured JUnit counts, conclusion,
Python version, timestamp, chained to the previous run. Anyone can recompute the
chain offline. Regressions and fixes are detected from the linkage, not from a
dashboard colour. A red run is a witness event too.

This action wraps [`szl-ci-witness`](https://github.com/szl-holdings/szl-ci-witness)
(Python 3.11+, standard library only) and installs it at an exact, reviewed commit.

## Usage

```yaml
name: tests
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read
  actions: read          # restore the previous chain artifact

concurrency:
  group: ci-witness-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: false

jobs:
  pytest:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.12"
      - run: pip install -e . pytest

      - name: Test
        id: test
        run: python -m pytest -q --junitxml=test-results.xml

      - name: Witness this run
        if: always()
        id: witness
        uses: szl-holdings/ci-witness-action@<40-char-sha>   # pin, then add '# v0.1.0'
        with:
          conclusion: ${{ steps.test.outcome }}
          junit-path: test-results.xml
          setup-python: "false"

      - name: Show chain state
        if: always()
        run: |
          echo "state=${{ steps.witness.outputs.state }} runs=${{ steps.witness.outputs.runs }}"
          echo "regressions=${{ steps.witness.outputs.regressions }} fixes=${{ steps.witness.outputs.fixes }}"
          echo "terminal=${{ steps.witness.outputs.terminal-hash }}"
```

Keep `if: always()` on the witness step so failures are recorded as well as successes.
Pass the real outcome of the test step. Never substitute a guessed count or a
dashboard colour for the JUnit report; `conclusion: success` with a missing or
failing JUnit report fails the witness step on purpose.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `conclusion` | required | `${{ steps.<test>.outcome }}` of your test step |
| `junit-path` | `test-results.xml` | JUnit XML to read measured counts from |
| `chain-path` | `ci-witness.jsonl` | receipt chain file |
| `stream` | `<workflow>:<ref>:python-<ver>` | one chain per stream |
| `python-version` | `3.12` | Python used to run the witness |
| `setup-python` | `true` | set `false` if Python is already set up |
| `witness-ref` | pinned commit | exact `szl-ci-witness` commit to install (40-char SHA only) |
| `token` | `github.token` | needs `actions: read` to restore the previous artifact |
| `retention-days` | `90` | artifact retention |
| `upload` | `true` | persist chain + report on branch runs |

## Outputs

`state` (`MEASURED`, `EPHEMERAL` on pull requests, or `INVALID`), `runs`, `green`, `red`,
`regressions` (JSON list of run ids), `fixes` (JSON list), `terminal-hash`, `artifact-name`.

## Verify offline

```bash
pip install "szl-ci-witness @ git+https://github.com/szl-holdings/szl-ci-witness@470673f48604046f428c4a83e2082fef245b5651"
python -m szl_ci_witness.witness verify  --chain ci-witness.jsonl   # linkage recompute
python -m szl_ci_witness.witness summary --chain ci-witness.jsonl   # runs, green/red, regressions, fixes
```

## Trust boundary (read before relying on it)

- GitHub artifact storage is retention-bounded, not a permanent independent witness.
  When the latest matching artifact is expired or the API fails, restoration fails
  closed and nothing is appended. Archive the terminal hash and the full chain outside
  the repository for independent continuity.
- An empty artifact inventory is labelled `GENESIS_OBSERVED_NO_ARTIFACTS`; it cannot
  prove that no artifacts were deleted. A fully rewritten and rehashed chain cannot be
  detected from that chain alone.
- Receipts are explicitly `UNSIGNED_HONEST`. GitHub origin checks do not turn them into
  cryptographic signatures or proof of production runtime health.
- Pull-request runs record an `EPHEMERAL` chain that is not persisted; only branch runs
  publish trusted history.
- The chain covers runs that reached the witness step. Cancelled or setup-failed runs
  leave no receipt; chain length is not proof that every revision was witnessed.

## Doctrine

Verify predecessors before appending; retain the preceding bytes. Failures are
recorded, never hidden. Timestamps are reported, never backfilled.

## License

Apache-2.0. Copyright 2026 SZL Holdings.
