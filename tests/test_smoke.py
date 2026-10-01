"""Smoke tests whose only job is to produce a measured JUnit report for the self-test."""
import yaml, pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

def test_action_metadata_is_well_formed():
    d = yaml.safe_load((ROOT / "action.yml").read_text())
    assert d["runs"]["using"] == "composite"
    assert d["inputs"]["conclusion"]["required"] is True

def test_every_action_ref_is_sha_pinned():
    d = yaml.safe_load((ROOT / "action.yml").read_text())
    for step in d["runs"]["steps"]:
        if "uses" in step:
            ref = step["uses"].split("@", 1)[1]
            assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), step["uses"]

def test_witness_ref_default_is_a_commit_sha():
    d = yaml.safe_load((ROOT / "action.yml").read_text())
    ref = d["inputs"]["witness-ref"]["default"]
    assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)
