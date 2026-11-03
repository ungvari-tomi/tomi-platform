import json
from types import SimpleNamespace

import pytest

from scripts import ci_select


def test_documentation_only_skips_runtime_jobs():
    selected = ci_select.select(["docs/ci-cd.md", "README.md", "AGENTS.md"])
    assert not any(value for key, value in selected.items() if key != "tests")
    assert selected["tests"] == []


def test_local_helper_selects_related_tests_without_images():
    selected = ci_select.select(["scripts/release_review.py", "retailflow/infrastructure.py"])
    assert selected["checks"] and selected["terraform"]
    assert not selected["integration"] and not selected["dependencies"]
    assert set(selected["tests"]) == {
        "tests/test_release_review.py",
        "tests/test_infrastructure.py",
    }


@pytest.mark.parametrize(
    "path", ["retailflow/api.py", "uv.lock", ".github/workflows/ci.yml", "unknown"]
)
def test_shared_or_unknown_changes_fail_open_to_full_coverage(path):
    selected = ci_select.select([path])
    assert all(value for key, value in selected.items() if key != "tests")
    assert selected["tests"] == ["tests"]


def test_delivery_forces_full_coverage_for_docs():
    assert ci_select.select(["README.md"], full=True)["integration"]
    assert ci_select.select([], full=True)["tests"] == ["tests"]


def test_changed_test_is_selected_and_container_tests_enable_docker():
    selected = ci_select.select(["tests/test_release_review.py"])
    assert selected["tests"] == ["tests/test_release_review.py"]
    assert not selected["integration"]
    assert ci_select.select(["tests/test_orchestration_container.py"])["integration"]


def test_terraform_hash_test_requires_real_terraform_job():
    selected = ci_select.select(["tests/test_image_inputs.py"])
    assert selected["terraform"] and not selected["integration"]


def test_monitor_scripts_select_focused_tests_without_image_builds():
    selected = ci_select.select(["scripts/monitor.py", "scripts/monitor_warehouse.py"])
    assert selected["checks"] and not selected["integration"]
    assert selected["tests"] == ["tests/test_monitor.py"]


def test_local_mail_selects_contracts_and_opt_in_container_test():
    selected = ci_select.select(["scripts/monitor_mail.py"])
    assert selected["tests"] == ["tests/test_monitor_mail.py"]
    assert not selected["integration"]
    assert ci_select.select(["tests/test_monitor_mail.py"])["integration"]


def test_deleted_test_triggers_full_coverage():
    assert ci_select.select(["tests/test_removed_contract.py"])["tests"] == ["tests"]


@pytest.mark.parametrize("before", [None, "0" * 40, "not-a-sha"])
def test_unavailable_diff_requires_full_coverage(before):
    assert ci_select.changed_paths({"before": before}, "a" * 40) is None


def test_pr_diff_includes_deleted_paths_without_shell(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b"old/path\0new/path\0")

    monkeypatch.setattr(ci_select.shutil, "which", lambda name: "git")
    monkeypatch.setattr(ci_select.subprocess, "run", run)
    paths = ci_select.changed_paths({"pull_request": {"base": {"sha": "a" * 40}}}, "b" * 40)
    assert paths == ["old/path", "new/path"]
    assert calls[0][-2:] == ["a" * 40, "b" * 40]
    assert "--no-renames" in calls[0]


@pytest.mark.parametrize("event_name,forced", [("workflow_dispatch", "false"), ("push", "true")])
def test_manual_or_reusable_delivery_outputs_full_selection(
    monkeypatch, tmp_path, event_name, forced
):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": "a" * 40}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("RETAILFLOW_FULL_VALIDATION", forced)
    monkeypatch.setattr(
        ci_select, "changed_paths", lambda *a: pytest.fail("Full run must not diff")
    )
    monkeypatch.setattr("sys.argv", ["ci_select"])
    ci_select.main()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["integration"] == "true"
    assert json.loads(values["tests"]) == ["tests"]


def test_selected_test_runner_rejects_options_and_path_traversal(monkeypatch):
    monkeypatch.setenv("RETAILFLOW_SELECTED_TESTS", '["--ignore=tests"]')
    monkeypatch.setattr("sys.argv", ["ci_select", "--run-tests"])
    with pytest.raises(ValueError, match="Invalid selected"):
        ci_select.main()
