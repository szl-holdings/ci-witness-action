"""Exercise two real pinned-library invocations across an artifact round trip."""
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest
import yaml

from szl_ci_witness.github_artifacts import artifact_prefix
from szl_ci_witness.witness import load_chain, main as witness_main, verify_witness_chain, witness_summary


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
