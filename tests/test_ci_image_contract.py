import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
HELPER = ROOT / "src/deploy_cli/templates/project/.deploy/ci_image_contract.py"
SHA = "a" * 40
DIGESTS = {
    "backend": "sha256:" + "b" * 64,
    "frontend": "sha256:" + "c" * 64,
}


def _demo(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    shutil.copytree(ROOT / "examples/demo-app", project)
    return project


def _run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter and checked-in helper
        [sys.executable, str(HELPER), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines())


def _plan(project: Path, tmp_path: Path) -> tuple[dict[str, object], dict[str, str]]:
    output = tmp_path / "plan.output"
    _run(
        "plan",
        "--project-dir",
        str(project),
        "--registry-prefix",
        "ghcr.io/acme",
        "--deployment-sha",
        SHA,
        "--github-output",
        str(output),
    )
    values = _outputs(output)
    return json.loads(values["matrix"]), json.loads(values["repositories"])


def _record_results(results: Path, repositories: dict[str, str]) -> None:
    for service, repository in repositories.items():
        _run(
            "result",
            "--service",
            service,
            "--repository",
            repository,
            "--digest",
            DIGESTS[service],
            "--deployment-sha",
            SHA,
            "--output",
            str(results / f"{service}.json"),
        )


def test_demo_plan_builds_every_declared_image_service(tmp_path: Path) -> None:
    matrix, repositories = _plan(_demo(tmp_path), tmp_path)

    assert matrix == {
        "include": [
            {
                "context": "backend",
                "dockerfile": "backend/Dockerfile",
                "repository": "ghcr.io/acme/demo-backend",
                "service": "backend",
            },
            {
                "context": "frontend",
                "dockerfile": "frontend/Dockerfile",
                "repository": "ghcr.io/acme/demo-frontend",
                "service": "frontend",
            },
        ]
    }
    assert set(repositories) == {"backend", "frontend"}


def test_collect_requires_complete_exact_service_result_set(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    _, repositories = _plan(project, tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    _run(
        "result",
        "--service",
        "backend",
        "--repository",
        repositories["backend"],
        "--digest",
        DIGESTS["backend"],
        "--deployment-sha",
        SHA,
        "--output",
        str(results / "backend.json"),
    )

    incomplete = _run(
        "collect",
        "--expected-repositories-json",
        json.dumps(repositories),
        "--deployment-sha",
        SHA,
        "--results-dir",
        str(results),
        "--github-output",
        str(tmp_path / "collect.output"),
        check=False,
    )

    assert incomplete.returncode == 2
    assert "missing=['frontend']" in incomplete.stderr

    _run(
        "result",
        "--service",
        "unexpected",
        "--repository",
        "ghcr.io/acme/unexpected",
        "--digest",
        "sha256:" + "d" * 64,
        "--deployment-sha",
        SHA,
        "--output",
        str(results / "unexpected.json"),
    )
    unexpected = _run(
        "collect",
        "--expected-repositories-json",
        json.dumps(repositories),
        "--deployment-sha",
        SHA,
        "--results-dir",
        str(results),
        "--github-output",
        str(tmp_path / "unexpected.output"),
        check=False,
    )
    assert unexpected.returncode == 2
    assert "Unexpected image result" in unexpected.stderr


def test_same_complete_image_map_is_applied_to_stage_and_production(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    _, repositories = _plan(project, tmp_path)
    results = tmp_path / "results"
    results.mkdir()
    _record_results(results, repositories)
    output = tmp_path / "collect.output"
    _run(
        "collect",
        "--expected-repositories-json",
        json.dumps(repositories),
        "--deployment-sha",
        SHA,
        "--results-dir",
        str(results),
        "--github-output",
        str(output),
    )
    image_map_json = _outputs(output)["image_map"]

    for environment in ("stage", "prod"):
        _run(
            "apply",
            "--project-dir",
            str(project),
            "--environment",
            environment,
            "--image-map-json",
            image_map_json,
        )

    expected = json.loads(image_map_json)
    for environment in ("stage", "prod"):
        compose = yaml.safe_load(
            (project / f"deploy/compose.{environment}.yml").read_text(encoding="utf-8")
        )
        assert {
            service: body["image"]
            for service, body in compose["services"].items()
            if service in expected
        } == expected


def test_apply_rejects_missing_or_extra_service(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    complete = {
        "backend": "ghcr.io/acme/demo-backend@" + DIGESTS["backend"],
        "frontend": "ghcr.io/acme/demo-frontend@" + DIGESTS["frontend"],
    }
    for broken in (
        {"backend": complete["backend"]},
        {**complete, "unexpected": "ghcr.io/acme/unexpected@sha256:" + "d" * 64},
    ):
        result = _run(
            "apply",
            "--project-dir",
            str(project),
            "--environment",
            "stage",
            "--image-map-json",
            json.dumps(broken),
            check=False,
        )
        assert result.returncode == 2
        assert "Incomplete image map" in result.stderr
