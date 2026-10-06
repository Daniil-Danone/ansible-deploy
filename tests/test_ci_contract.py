import base64
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def _workflow(path: Path) -> dict:
    loaded = yaml.load(
        path.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,  # noqa: S506 - strings and collections only
    )
    assert isinstance(loaded, dict)
    return loaded


def _version_bump_step() -> dict:
    job = _workflow(ROOT / ".github/workflows/checks.yml")["jobs"]["version-bump"]
    return next(step for step in job["steps"] if "run" in step)


def test_version_bump_compares_against_immutable_pr_base_commit() -> None:
    job = _workflow(ROOT / ".github/workflows/checks.yml")["jobs"]["version-bump"]
    checkout, step = job["steps"]

    assert job["if"] == "github.event_name == 'pull_request'"
    assert checkout["with"]["fetch-depth"] == "0"
    assert step["env"] == {"BASE_SHA": "${{ github.event.pull_request.base.sha }}"}
    assert 'git cat-file -e "$BASE_SHA^{commit}"' in step["run"]
    assert 'git show "$BASE_SHA:src/deploy_cli/__init__.py"' in step["run"]
    assert "BASE_REF" not in step["run"]
    assert "origin/" not in step["run"]


def _bash() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_bash = Path(git).parents[1] / "usr/bin/bash.exe"
            if git_bash.is_file():
                return str(git_bash)
    elif bash := shutil.which("bash"):
        return bash
    pytest.skip("Bash is required to execute the workflow version gate")


def _run_version_gate(root: Path, base_sha: str) -> subprocess.CompletedProcess[str]:
    bash = _bash()
    environment = os.environ.copy()
    environment["BASE_SHA"] = base_sha
    if os.name == "nt":
        # Use Git Bash coreutils (not Windows sort.exe, which has no -V option).
        environment["PATH"] = str(Path(bash).parent) + os.pathsep + environment["PATH"]
    return subprocess.run(  # noqa: S603 - actual repository workflow, fixed shell invocation
        [bash, "-c", _version_bump_step()["run"]],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_version_bump_keeps_event_base_when_remote_branch_advances(tmp_path: Path) -> None:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required to simulate an advancing base branch")

    def run_git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed Git operations on a temporary repository
            [git, *args], cwd=tmp_path, capture_output=True, text=True, check=True,
        ).stdout.strip()

    run_git("init")
    run_git("config", "user.email", "ci-contract@example.invalid")
    run_git("config", "user.name", "CI Contract Test")
    run_git("config", "commit.gpgsign", "false")
    version_file = tmp_path / "src/deploy_cli/__init__.py"
    version_file.parent.mkdir(parents=True)
    version_file.write_text('__version__ = "0.5.0"\n', encoding="utf-8")
    run_git("add", ".")
    run_git("commit", "-m", "base version")
    base_sha = run_git("rev-parse", "HEAD")
    version_file.write_text('__version__ = "0.5.1"\n', encoding="utf-8")
    run_git("commit", "-am", "head version")
    head_sha = run_git("rev-parse", "HEAD")
    run_git("update-ref", "refs/remotes/origin/develop", head_sha)

    assert run_git("show", "origin/develop:src/deploy_cli/__init__.py") == (
        '__version__ = "0.5.1"'
    )
    result = _run_version_gate(tmp_path, base_sha)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "base=0.5.0 head=0.5.1" in result.stdout
    assert _run_version_gate(tmp_path, head_sha).returncode != 0
    version_file.write_text('__version__ = "0.4.9"\n', encoding="utf-8")
    assert _run_version_gate(tmp_path, base_sha).returncode != 0


@pytest.mark.parametrize("base_sha", ["", "develop", "a" * 39, "A" * 40, "a" * 40 + "\nextra"])
def test_version_bump_rejects_invalid_base_sha(tmp_path: Path, base_sha: str) -> None:
    result = _run_version_gate(tmp_path, base_sha)
    assert result.returncode != 0
    assert "Invalid PR base commit SHA" in result.stdout


def test_version_bump_rejects_unavailable_base_commit(tmp_path: Path) -> None:
    result = _run_version_gate(tmp_path, "a" * 40)
    assert result.returncode != 0
    assert "PR base commit " + "a" * 40 + " is unavailable" in result.stdout


def test_reusable_deploy_has_protected_serial_environment_contract() -> None:
    workflow = _workflow(ROOT / ".github/workflows/reusable-deploy.yml")
    deploy = workflow["jobs"]["deploy"]

    assert "workflow_call" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read", "packages": "read"}
    assert deploy["environment"] == "${{ inputs.environment }}"
    assert deploy["needs"] == "monitoring"
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


def test_reusable_deploy_declares_optional_environment_secret_contract() -> None:
    workflow = _workflow(ROOT / ".github/workflows/reusable-deploy.yml")
    secrets = workflow["on"]["workflow_call"]["secrets"]

    assert set(secrets) == {"CLI_REPOSITORY_TOKEN", "ANSIBLE_DEPLOY_SECRET_STORE_JSON"}
    for declaration in secrets.values():
        assert declaration["required"] == "false"
        assert "Environment" in declaration["description"]


@pytest.mark.parametrize("relative", [
    ".github/examples/application-deploy.yml",
    "src/deploy_cli/templates/project/.github/workflows/deploy.yml",
])
def test_callers_pass_only_declared_deployment_secrets(relative: str) -> None:
    workflow = _workflow(ROOT / relative)
    expected = {
        name: "${{ secrets." + name + " }}"
        for name in ("CLI_REPOSITORY_TOKEN", "ANSIBLE_DEPLOY_SECRET_STORE_JSON")
    }

    for name in ("stage", "production"):
        job = workflow["jobs"][name]
        assert job["secrets"] == expected
        assert "environment" not in job
        assert job["with"]["environment"] == name
    assert "secrets: inherit" not in (ROOT / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("job_name", ["monitoring", "deploy"])
@pytest.mark.parametrize("token_present,store_present", [
    (False, False), (False, True), (True, False), (True, True),
])
def test_called_jobs_validate_both_secrets_before_checkout_without_leaking_values(
    job_name: str, token_present: bool, store_present: bool,
) -> None:
    job = _workflow(ROOT / ".github/workflows/reusable-deploy.yml")["jobs"][job_name]
    steps = job["steps"]
    validation = next(step for step in steps if step.get("name") == "Validate immutable inputs")
    for name in ("CLI_REPOSITORY_TOKEN", "ANSIBLE_DEPLOY_SECRET_STORE_JSON"):
        assert validation["env"][name] == "${{ secrets." + name + " }}"
    checkout_indices = [i for i, step in enumerate(steps) if "uses" in step]
    assert steps.index(validation) < min(checkout_indices)
    code = validation["run"].removeprefix("python - <<'PY'\n").removesuffix("PY\n")
    environment = os.environ.copy()
    environment.update({
        "DEPLOYMENT_SHA": "a" * 40,
        "TOOL_SHA": "b" * 40,
        "HEALTH_URL": "https://stage.example.invalid/health",
        "IMAGE_MAP": json.dumps({"web": "ghcr.io/owner/web@sha256:" + "c" * 64}),
        "CLI_REPOSITORY_TOKEN": "synthetic-cli-token" if token_present else "",
        "ANSIBLE_DEPLOY_SECRET_STORE_JSON": "synthetic-secret-store" if store_present else "",
    })
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository validation script
        [sys.executable, "-c", code], env=environment, capture_output=True, text=True,
        check=False,
    )
    expected_errors = []
    if not token_present:
        expected_errors.append("CLI_REPOSITORY_TOKEN is required for the private CLI checkout")
    if not store_present:
        expected_errors.append(
            "ANSIBLE_DEPLOY_SECRET_STORE_JSON is required for the external secret store"
        )
    assert (result.returncode == 0) == (not expected_errors)
    assert result.stdout == ""
    assert result.stderr == "\n".join(expected_errors) + ("\n" if expected_errors else "")
    assert "synthetic-cli-token" not in result.stdout + result.stderr
    assert "synthetic-secret-store" not in result.stdout + result.stderr


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


def test_monitoring_reconciles_before_application_in_its_own_protected_environment() -> None:
    workflow = _workflow(ROOT / ".github/workflows/reusable-deploy.yml")
    monitoring = workflow["jobs"]["monitoring"]
    steps = monitoring["steps"]

    assert monitoring["environment"] == "monitoring"
    assert monitoring["concurrency"]["group"] == "monitoring-${{ github.repository }}"
    assert monitoring["concurrency"]["cancel-in-progress"] == "false"
    assert workflow["jobs"]["deploy"]["needs"] == "monitoring"
    reconcile = next(step for step in steps if "monitoring deploy" in step.get("run", ""))
    assert reconcile["run"] == "ansible-deploy --project-dir . monitoring deploy"
    assert steps[-1]["if"] == "always()"
    checkout = next(step for step in steps if step.get("name") == (
        "Check out deployment CLI at immutable SHA"
    ))
    assert checkout["with"]["ref"] == "${{ inputs.tool_sha }}"
    materialize = next(step for step in steps if step.get("name") == (
        "Materialize temporary external secret store"
    ))
    assert materialize["env"]["DEPLOY_SECRET_STORE_JSON"] == (
        "${{ secrets.ANSIBLE_DEPLOY_SECRET_STORE_JSON }}"  # noqa: S105 - expression
    )


def test_application_caller_keeps_pr_quality_only_and_gates_deployments() -> None:
    workflow = _workflow(ROOT / ".github/examples/application-deploy.yml")
    jobs = workflow["jobs"]

    assert "pull_request" in workflow["on"]
    assert "github.event_name == 'push'" in jobs["image_plan"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["image_plan"]["if"]
    assert "pull_request" not in jobs["stage"]["if"]
    assert jobs["image_plan"]["needs"] == "quality"
    assert jobs["build_images"]["needs"] == "image_plan"
    assert jobs["collect_images"]["needs"] == ["image_plan", "build_images"]
    assert jobs["stage"]["needs"] == ["quality", "collect_images"]
    assert jobs["production"]["needs"] == ["quality", "collect_images"]
    assert "github.ref == 'refs/heads/main'" in jobs["production"]["if"]
    assert "workflow_dispatch" not in jobs["stage"]["if"]
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
    # Extra env files are just more store-relative entries in the same JSON map.
    payload = {
        "environments/stage/app.env": base64.b64encode(b"APP_ENV=stage\n").decode(),
        "environments/stage/bot.env": base64.b64encode(b"DB_PASSWORD=bot\n").decode(),
    }
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

    expected = {"app.env": b"APP_ENV=stage\n", "bot.env": b"DB_PASSWORD=bot\n"}
    for name, content in expected.items():
        secret = root / "environments/stage" / name
        assert secret.read_bytes() == content
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
