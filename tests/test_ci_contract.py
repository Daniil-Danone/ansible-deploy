import base64
import json
import os
import re
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
    assert "env" not in deploy
    secret_path = next(
        step
        for step in deploy["steps"]
        if step["name"] == "Set temporary secret store path"
    )
    assert "$RUNNER_TEMP/ansible-deploy-secrets" in secret_path["run"]
    assert ">> \"$GITHUB_ENV\"" in secret_path["run"]
    assert deploy["steps"][-1]["if"] == "always()"
    materialize = next(
        step
        for step in deploy["steps"]
        if step["name"] == "Materialize temporary external secret store"
    )
    assert materialize["env"]["DEPLOY_SECRET_STORE_JSON"] == (
        "${{ secrets.ANSIBLE_DEPLOY_SECRET_STORE_JSON }}"  # noqa: S105 - expression
    )
    cli_checkout = next(
        step
        for step in deploy["steps"]
        if step.get("name") == "Check out deployment CLI at immutable SHA"
    )
    assert cli_checkout["with"]["token"] == (
        "${{ secrets.CLI_REPOSITORY_TOKEN }}"  # noqa: S105 - expression
    )
    assert cli_checkout["with"]["persist-credentials"] == "false"
    validation = next(
        step for step in deploy["steps"] if step["name"] == "Validate immutable inputs"
    )
    assert "inputs.deployment_sha" in validation["env"]["DEPLOYMENT_SHA"]
    assert validation["env"]["CLI_REPOSITORY_TOKEN"] == (
        "${{ secrets.CLI_REPOSITORY_TOKEN }}"  # noqa: S105 - expression
    )
    apply = next(
        step
        for step in deploy["steps"]
        if step.get("name") == "Apply complete verified image map to Compose"
    )
    assert apply["env"]["IMAGE_MAP"] == "${{ inputs.image_map }}"
    assert ".deploy/ci_image_contract.py apply" in apply["run"]


def test_release_workflow_publishes_only_version_matching_tags() -> None:
    workflow = _workflow(ROOT / ".github/workflows/release.yml")
    release = workflow["jobs"]["release"]
    steps = [step.get("run", "") for step in release["steps"]]

    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert release["permissions"] == {"contents": "write"}
    verify = next(step for step in release["steps"] if step.get("name", "").startswith("Verify"))
    assert '"$TAG" != "v$version"' in verify["run"]
    assert "deploy_cli.__version__" in verify["run"]
    order = [
        next(i for i, run in enumerate(steps) if marker in run)
        for marker in ('"v$version"', "python -m pytest", "uv build", "gh release create")
    ]
    assert order == sorted(order)
    assert "dist/*.whl dist/*.tar.gz" in steps[order[-1]]


def test_application_caller_keeps_pr_quality_only_and_gates_deployments() -> None:
    workflow = _workflow(ROOT / ".github/examples/application-deploy.yml")
    jobs = workflow["jobs"]

    assert "pull_request" in workflow["on"]
    assert jobs["image_plan"]["if"] == "github.event_name != 'pull_request'"
    assert "pull_request" not in jobs["stage"]["if"]
    assert jobs["image_plan"]["needs"] == "quality"
    assert jobs["build_images"]["needs"] == "image_plan"
    assert jobs["collect_images"]["needs"] == ["image_plan", "build_images"]
    assert jobs["stage"]["needs"] == ["quality", "collect_images"]
    assert jobs["production"]["needs"] == ["quality", "collect_images", "stage"]
    assert jobs["stage"]["with"]["environment"] == "stage"
    assert jobs["production"]["with"]["environment"] == "production"
    assert jobs["stage"]["with"]["deployment_sha"] == (
        "${{ needs.collect_images.outputs.deployment_sha }}"
    )
    expected_map = "${{ needs.collect_images.outputs.image_map }}"
    assert jobs["stage"]["with"]["image_map"] == expected_map
    assert jobs["production"]["with"]["image_map"] == expected_map


def test_application_build_uses_complete_matrix_and_collects_verified_map() -> None:
    workflow = _workflow(ROOT / ".github/examples/application-deploy.yml")
    plan = workflow["jobs"]["image_plan"]
    build = workflow["jobs"]["build_images"]
    collect = workflow["jobs"]["collect_images"]
    build_step = next(
        step
        for step in build["steps"]
        if step.get("name") == "Build, push, and verify service image"
    )
    collect_step = next(step for step in collect["steps"] if step.get("id") == "collect")

    assert build["environment"] == "build"
    assert build["permissions"] == {"contents": "read", "packages": "write"}
    assert build["strategy"]["fail-fast"] == "true"
    assert build["strategy"]["matrix"] == "${{ fromJSON(needs.image_plan.outputs.matrix) }}"
    assert plan["outputs"]["repositories"] == "${{ steps.plan.outputs.repositories }}"
    assert build_step["env"]["SERVICE"] == "${{ matrix.service }}"
    assert '"$REPOSITORY:$DEPLOYMENT_SHA"' in build_step["run"]
    assert "docker buildx build" in build_step["run"]
    assert "docker buildx imagetools inspect" in build_step["run"]
    assert ".deploy/ci_image_contract.py result" in build_step["run"]
    assert ".deploy/ci_image_contract.py collect" in collect_step["run"]
    assert collect["outputs"]["image_map"] == "${{ steps.collect.outputs.image_map }}"


def test_delivery_workflows_do_not_trace_or_artifact_secrets() -> None:
    text = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            ".github/workflows/reusable-deploy.yml",
            ".github/examples/application-deploy.yml",
        )
    )

    assert "set -x" not in text
    assert "echo " not in text
    assert "REGISTRY_TOKEN" not in text.replace("${{ secrets.REGISTRY_TOKEN }}", "")
    workflow = _workflow(ROOT / ".github/examples/application-deploy.yml")
    upload = next(
        step
        for step in workflow["jobs"]["build_images"]["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    )
    assert upload["with"]["path"] == ".deploy-state/ci-images/${{ matrix.service }}.json"
    assert "secret" not in upload["with"]["path"].lower()


def test_workflows_pin_every_third_party_action_to_full_sha() -> None:
    for relative in (
        ".github/workflows/checks.yml",
        ".github/workflows/release.yml",
        ".github/workflows/reusable-deploy.yml",
        ".github/examples/application-deploy.yml",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        third_party_lines = [
            line
            for line in text.splitlines()
            if re.search(r"uses:\s+(?:actions|docker)/[^@\s]+@", line)
        ]
        assert third_party_lines
        for line in third_party_lines:
            assert re.search(r"@[0-9a-f]{40}\s+#\s+v\d", line), line
        assert re.search(r"(?:actions|docker)/[^@\s]+@v\d", text) is None
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
