import json

import pytest

from scripts import pull_deploy


@pytest.mark.parametrize("branch,repository", [("other", pull_deploy.REPOSITORY), ("main", "other/repo")])
def test_unexpected_build_cannot_deploy(monkeypatch, tmp_path, branch, repository):
    monkeypatch.setattr(pull_deploy, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["pull_deploy.py"])
    run = {
        "head_sha": "a" * 40, "head_branch": branch, "conclusion": "success",
        "head_repository": {"full_name": repository},
    }
    monkeypatch.setattr(pull_deploy, "download", lambda _: json.dumps({"workflow_runs": [run]}).encode())
    with pytest.raises(AssertionError):
        pull_deploy.main()
    assert not list(tmp_path.iterdir())


def test_failed_deployment_is_not_repeated(monkeypatch, tmp_path):
    monkeypatch.setattr(pull_deploy, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["pull_deploy.py"])
    state = tmp_path / ".deployment"
    state.mkdir()
    (state / "attempt").write_text("a" * 40)
    run = {
        "head_sha": "a" * 40, "head_branch": "main", "conclusion": "success",
        "head_repository": {"full_name": pull_deploy.REPOSITORY},
    }
    monkeypatch.setattr(pull_deploy, "download", lambda _: json.dumps({"workflow_runs": [run]}).encode())
    with pytest.raises(RuntimeError, match="Previous attempt failed"):
        pull_deploy.main()
    assert not (state / "revision").exists()
