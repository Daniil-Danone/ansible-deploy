import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]


def _workflow(path: Path) -> dict:
    loaded = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,  # noqa: S506 - strings and collections only
    )
    assert isinstance(loaded, dict)
    return loaded


def test_reusable_deploy_has_protected_serial_environment_contract() -> None:
    workflow = _workflow(ROOT / ".github/workflows/reusable-deploy.yml")
    deploy = workflow["jobs"]["deploy"]

    assert "workflow_call" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read", "packages": "read"}
    assert deploy["environment"] == "${{ inputs.environment }}"
    assert deploy["concurrency"]["cancel-in-progress"] == "false"
    assert deploy["timeout-minutes"] == "45"
    assert "inputs.deployment_sha" in deploy["steps"][0]["env"]["DEPLOYMENT_SHA"]
    assert deploy["steps"][-1]["if"] == "always()"
    materialize = next(
        step
        for step in deploy["steps"]
        if step["name"] == "Materialize temporary external secret store"
    )
    assert materialize["env"]["DEPLOY_SECRET_STORE_JSON"] == (
        "${{ secrets.ANSIBLE_DEPLOY_SECRET_STORE_JSON }}"  # noqa: S105 - expression
    )


def test_application_caller_keeps_pr_quality_only_and_gates_deployments() -> None:
    workflow = _workflow(ROOT / ".github/examples/application-deploy.yml")
    jobs = workflow["jobs"]

    assert "pull_request" in workflow["on"]
    assert "pull_request" not in jobs["stage"]["if"]
    assert jobs["stage"]["needs"] == "quality"
    assert jobs["production"]["needs"] == ["quality", "stage"]
    assert jobs["stage"]["with"]["environment"] == "stage"
    assert jobs["production"]["with"]["environment"] == "production"
    assert jobs["stage"]["with"]["deployment_sha"] == "${{ github.sha }}"


def test_workflows_pin_supported_action_majors_and_define_timeouts() -> None:
    for relative in (
        ".github/workflows/checks.yml",
        ".github/workflows/reusable-deploy.yml",
        ".github/examples/application-deploy.yml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "actions/checkout@v4" not in text
        assert "actions/setup-python@v5" not in text
        workflow = _workflow(ROOT / relative)
        for job in workflow["jobs"].values():
            if "uses" not in job:
                assert "timeout-minutes" in job


def test_secret_materializer_writes_only_named_files_with_restrictive_modes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "external store"
    payload = {"environments/stage/app.env": base64.b64encode(b"APP_ENV=stage\n").decode()}
    environment = os.environ.copy()
    environment["ANSIBLE_DEPLOY_SECRETS_DIR"] = str(root)
    environment["DEPLOY_SECRET_STORE_JSON"] = json.dumps(payload)

    subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(ROOT / "scripts/materialize_ci_secrets.py")],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    secret = root / "environments/stage/app.env"
    assert secret.read_bytes() == b"APP_ENV=stage\n"
    if os.name != "nt":
        assert secret.stat().st_mode & 0o777 == 0o600


def test_secret_materializer_rejects_path_traversal(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["ANSIBLE_DEPLOY_SECRETS_DIR"] = str(tmp_path / "store")
    environment["DEPLOY_SECRET_STORE_JSON"] = json.dumps({"../escape": "eA=="})

    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository script
        [sys.executable, str(ROOT / "scripts/materialize_ci_secrets.py")],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not (tmp_path / "escape").exists()
