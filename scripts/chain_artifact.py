"""Bind the configured chain path to the pinned witness artifact contract."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from szl_ci_witness.github_artifacts import restore
from szl_ci_witness.witness import verify_witness_chain


def restore_chain(destination: str, *, fetch=None) -> dict:
    """Restore the predecessor where record-junit will append this run."""
    Path(destination).parent.mkdir(parents=True, exist_ok=True)
    options = {} if fetch is None else {"fetch": fetch}
    return restore(
        os.environ["GITHUB_REPOSITORY"],
        os.environ["WITNESS_STREAM"],
        os.environ["GITHUB_RUN_ID"],
        os.environ["GITHUB_RUN_ATTEMPT"],
        os.environ["GH_TOKEN"],
        destination=destination,
        **options,
    )


def stage_artifact(chain_path: str, junit_path: str, runner_temp: str) -> Path:
    """Package verified bytes with the canonical member required by restore.

    upload-artifact preserves relative directories for multiple input paths.
    A private staging directory keeps the archive member ci-witness.jsonl at
    its root even when chain-path or junit-path uses another name/directory.
    """
    content = Path(chain_path).read_bytes()
    if len(content) > 50_000_000:
        raise ValueError("witness chain exceeds the predecessor archive limit")
    records = [json.loads(line) for line in content.decode("utf-8").splitlines() if line.strip()]
    ok, detail = verify_witness_chain(records)
    if not ok:
        raise ValueError(detail)
    report = Path(junit_path)
    junit = report.read_bytes() if report.exists() else None
    directory = Path(tempfile.mkdtemp(prefix="ci-witness-artifact-", dir=runner_temp))
    (directory / "ci-witness.jsonl").write_bytes(content)
    # A missing JUnit report can accompany an honestly recorded failure; the
    # witness record itself enforces that it can never claim measured success.
    if junit is not None:
        (directory / "test-results.xml").write_bytes(junit)
    return directory


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("restore", "stage"))
    args = parser.parse_args(argv)
    if args.operation == "restore":
        result = restore_chain(os.environ["CHAIN"])
        print(json.dumps(result, sort_keys=True))
        output = "artifact_name=" + result["artifact_name"]
    else:
        directory = stage_artifact(os.environ["CHAIN"], os.environ["JUNIT"], os.environ["RUNNER_TEMP"])
        output = "directory=" + str(directory)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as target:
        target.write(output + "\n")


if __name__ == "__main__":
    main()
