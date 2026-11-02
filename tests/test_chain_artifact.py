"""Exercise two real pinned-library invocations across an artifact round trip."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import zipfile

import pytest
import yaml

from szl_ci_witness.github_artifacts import artifact_prefix
from szl_ci_witness.witness import (
    load_chain,
    main as witness_main,
    verify_witness_chain,
    witness_record,
    witness_summary,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("chain_artifact", ROOT / "scripts" / "chain_artifact.py")
artifact = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact)
REPO = "szl-holdings/ci-witness-action"
STREAM = "selftest:refs/heads/main:python-3.12"


def fixture_api(archive=None, *, archive_name="ci-witness.jsonl"):
    def fetch(path, token):
        assert token == "TEST_ONLY"
        if path.endswith("/zip"):
            blob = io.BytesIO()
            with zipfile.ZipFile(blob, "w") as zipped:
                zipped.writestr(archive_name, archive)
            return blob.getvalue()
        if "/actions/runs/" in path:
            run = int(path.split("/actions/runs/")[1].split("/")[0])
            return json.dumps({
                "id": run, "run_attempt": 1, "name": "selftest",
                "repository": {"full_name": REPO},
                "head_repository": {"full_name": REPO},
                "event": "push", "head_branch": "main", "head_sha": "a" * 40,
                "workflow_id": 42,
            }).encode()
        rows = [] if archive is None else [{
            "id": 7, "name": artifact_prefix(STREAM) + "123-1",
            "expired": False, "workflow_run": {"id": 123},
        }]
        return json.dumps({"artifacts": rows}).encode()
    return fetch


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "GITHUB_REPOSITORY": REPO, "GITHUB_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        "WITNESS_STREAM": STREAM, "GH_TOKEN": "TEST_ONLY",
    }.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def record(chain, report, conclusion="success"):
    assert witness_main([
        "record-junit", "--chain", str(chain), "--junit", str(report),
        "--conclusion", conclusion, "--python", "3.12",
    ]) == 0


@pytest.mark.parametrize("chain_name", ["ci-witness.jsonl", "receipts/custom history.jsonl"])
def test_two_runs_preserve_predecessor_bytes_and_detect_regression(runner, monkeypatch, chain_name):
    chain = Path(chain_name)
    report = runner / "reports" / "integration.xml"
    report.parent.mkdir()
    report.write_text("<testsuite><testcase/></testsuite>", encoding="utf-8")
    genesis = artifact.restore_chain(str(chain), fetch=fixture_api())
    assert genesis["state"] == "GENESIS_OBSERVED_NO_ARTIFACTS"
    record(chain, report)
    first_bytes = chain.read_bytes()
    first_stage = artifact.stage_artifact(str(chain), str(report), str(runner))
    assert {p.name for p in first_stage.iterdir()} == {"ci-witness.jsonl", "test-results.xml"}
    assert (first_stage / "ci-witness.jsonl").read_bytes() == first_bytes
    assert (first_stage / "test-results.xml").read_bytes() == report.read_bytes()

    # A new runner has no local history: only the uploaded canonical member.
    next_runner = runner / "next-runner"
    next_runner.mkdir()
    monkeypatch.chdir(next_runner)
    monkeypatch.setenv("GITHUB_RUN_ID", "124")
    restored = artifact.restore_chain(str(chain), fetch=fixture_api(
        (first_stage / "ci-witness.jsonl").read_bytes()
    ))
    assert restored["state"] == "RESTORED"
    assert restored["records"] == 1
    assert chain.read_bytes() == first_bytes
    if chain_name != "ci-witness.jsonl":
        assert not Path("ci-witness.jsonl").exists()

    report.write_text("<testsuite><testcase><failure/></testcase></testsuite>", encoding="utf-8")
    record(chain, report, "failure")
    records = load_chain(str(chain))
    assert [r["run_id"] for r in records] == ["123", "124"]
    assert records[1]["prev_hash"] == records[0]["chain_hash"]
    assert chain.read_bytes().startswith(first_bytes)
    assert verify_witness_chain(records)[0] is True
    assert witness_summary(records)["regressions"] == ["124"]
    second_stage = artifact.stage_artifact(str(chain), str(report), str(next_runner))
    assert (second_stage / "ci-witness.jsonl").read_bytes() == chain.read_bytes()


def test_existing_destination_is_not_overwritten(runner):
    chain = runner / "custom.jsonl"
    chain.write_bytes(b"preexisting history")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        artifact.restore_chain(str(chain), fetch=fixture_api())
    assert chain.read_bytes() == b"preexisting history"


def test_noncanonical_old_archive_fails_without_resetting_history(runner, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "124")
    chain = runner / "custom.jsonl"
    with pytest.raises(ValueError, match="invalid predecessor archive"):
        artifact.restore_chain(str(chain), fetch=fixture_api(b"{}", archive_name="custom.jsonl"))
    assert not chain.exists()


def test_stage_rejects_tampered_chain(runner):
    chain = runner / "custom.jsonl"
    record(chain, runner / "missing.xml", "failure")
    damaged = load_chain(str(chain))[0]
    damaged["conclusion"] = "success"
    chain.write_text(json.dumps(damaged) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tampered"):
        artifact.stage_artifact(str(chain), str(runner / "missing.xml"), str(runner))
    assert not list(runner.glob("ci-witness-artifact-*"))


@pytest.mark.parametrize(
    ("key", "shadow"),
    [("run_id", "shadowed"), ("passed", 999)],
    ids=("top-level", "nested"),
)
def test_stage_rejects_duplicate_json_object_keys(runner, key, shadow):
    chain = runner / "ambiguous.jsonl"
    missing = runner / "missing.xml"
    record(chain, missing, "failure")
    original = load_chain(str(chain))[0]
    raw = json.dumps(original, sort_keys=True)
    marker = json.dumps(key) + ":"
    assert marker in raw
    ambiguous = raw.replace(
        marker,
        json.dumps(key) + ":" + json.dumps(shadow) + "," + marker,
        1,
    )
    # Python's default decoder silently keeps the last duplicate value.
    assert json.loads(ambiguous) == original
    chain.write_text(ambiguous + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON object keys"):
        artifact.stage_artifact(str(chain), str(missing), str(runner))
    assert not list(runner.glob("ci-witness-artifact-*"))


@pytest.mark.parametrize("constant", ("NaN", "Infinity", "-Infinity"))
def test_stage_rejects_nonfinite_json_constants(runner, constant):
    chain = runner / "nonfinite.jsonl"
    missing = runner / "missing.xml"
    record(chain, missing, "failure")
    original = load_chain(str(chain))[0]
    raw = json.dumps(original, sort_keys=True)
    marker = '"run_id":'
    assert marker in raw
    ambiguous = raw.replace(marker, marker + constant + "," + marker, 1)
    # The default decoder accepts the non-standard token and the later key wins.
    assert json.loads(ambiguous) == original
    chain.write_text(ambiguous + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite JSON number"):
        artifact.stage_artifact(str(chain), str(missing), str(runner))
    assert not list(runner.glob("ci-witness-artifact-*"))


def test_stage_rejects_recomputed_finite_syntax_numeric_overflow(runner):
    chain = runner / "overflow.jsonl"
    missing = runner / "missing.xml"
    overflow = witness_record(
        REPO,
        "a" * 40,
        "123",
        "failure",
        float("inf"),
        0,
        "3.12",
        ts="2026-11-02T00:00:00Z",
    )
    # The upstream canonicalizer emits Infinity and accepts its recomputed hash.
    assert verify_witness_chain([overflow])[0] is True
    raw = json.dumps(overflow, sort_keys=True).replace("Infinity", "1e999")
    assert json.loads(raw)["tests"]["passed"] == float("inf")
    chain.write_text(raw + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-finite JSON number"):
        artifact.stage_artifact(str(chain), str(missing), str(runner))
    assert not list(runner.glob("ci-witness-artifact-*"))


def test_stage_bounds_read_before_rejecting_oversized_chain(runner, monkeypatch):
    chain = runner / "oversized.jsonl"
    limit = 50_000_000
    # A sparse file makes the test input without retaining a second large
    # byte buffer. Measure the real file reader, not a fake decoder/verifier.
    with chain.open("wb") as target:
        target.truncate(limit + 32768)
    original_open = Path.open
    reads = []

    class RecordingReader:
        def __init__(self, reader):
            self.reader = reader

        def __enter__(self):
            self.reader.__enter__()
            return self

        def __exit__(self, *args):
            return self.reader.__exit__(*args)

        def read(self, size=-1):
            data = self.reader.read(size)
            reads.append((size, len(data)))
            return data

    def observe_open(path, *args, **kwargs):
        opened = original_open(path, *args, **kwargs)
        return RecordingReader(opened) if path == chain else opened

    monkeypatch.setattr(Path, "open", observe_open)
    with pytest.raises(ValueError, match="predecessor archive limit"):
        artifact.stage_artifact(str(chain), str(runner / "missing.xml"), str(runner))
    assert reads == [(limit + 1, limit + 1)]
    assert not list(runner.glob("ci-witness-artifact-*"))


def test_stage_accepts_exact_limit_without_changing_verified_bytes(runner):
    chain = runner / "boundary.jsonl"
    missing = runner / "missing.xml"
    record(chain, missing, "failure")
    # Blank trailing JSONL whitespace preserves the valid record while
    # exercising the actual production boundary, not a reduced test limit.
    remaining = 50_000_000 - chain.stat().st_size
    with chain.open("ab") as target:
        while remaining:
            chunk = min(remaining, 1024 * 1024)
            target.write(b" " * chunk)
            remaining -= chunk
    original = chain.read_bytes()
    assert len(original) == 50_000_000
    staged = artifact.stage_artifact(str(chain), str(missing), str(runner))
    assert (staged / "ci-witness.jsonl").read_bytes() == original


def test_missing_junit_remains_an_honestly_recorded_failure(runner):
    chain = runner / "custom.jsonl"
    missing = runner / "missing.xml"
    record(chain, missing, "failure")
    staged = artifact.stage_artifact(str(chain), str(missing), str(runner))
    assert [p.name for p in staged.iterdir()] == ["ci-witness.jsonl"]
    assert load_chain(str(staged / "ci-witness.jsonl"))[0]["tests"] == {"passed": None, "failed": None}


def test_action_routes_the_same_input_to_restore_record_and_stage():
    metadata = yaml.safe_load((ROOT / "action.yml").read_text())
    steps = {step.get("id"): step for step in metadata["runs"]["steps"]}
    for name in ("restore", "witness", "stage"):
        assert steps[name]["env"]["CHAIN"] == "${{ inputs.chain-path }}"
    assert 'chain_artifact.py" restore' in steps["restore"]["run"]
    assert 'chain_artifact.py" stage' in steps["stage"]["run"]
    upload = metadata["runs"]["steps"][-1]
    assert upload["with"]["path"] == "${{ steps.stage.outputs.directory }}"
    pin = metadata["inputs"]["witness-ref"]["default"]
    assert f"szl-ci-witness@{pin}" in (ROOT / "requirements-test.txt").read_text()


def test_pull_request_records_nested_chain_without_restore(runner, monkeypatch):
    """Execute the actual witness step with restoration skipped, as on PRs."""
    chain = runner / "receipts" / "custom history.jsonl"
    report = runner / "test-results.xml"
    report.write_text("<testsuite><testcase/></testsuite>", encoding="utf-8")
    output = runner / "github-output"
    for key, value in {
        "CHAIN": str(chain), "JUNIT": str(report),
        "TEST_CONCLUSION": "success", "RESTORE_OUTCOME": "skipped",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)
    metadata = yaml.safe_load((ROOT / "action.yml").read_text())
    step = next(s for s in metadata["runs"]["steps"] if s.get("id") == "witness")
    result = subprocess.run(["bash", "-c", step["run"]], env=os.environ.copy(),
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert verify_witness_chain(load_chain(str(chain)))[0]
    assert "state=EPHEMERAL\n" in output.read_text()
    assert not list(runner.glob("ci-witness-artifact-*"))

