#!/usr/bin/env python3
"""Fail-closed image handoff used by the example GitHub Actions pipeline."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any

SERVICE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]*")
IMAGE_PATTERN = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
REGISTRY_PREFIX_PATTERN = re.compile(
    r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9]+(?:[._/-][a-z0-9]+)*"
)
REPOSITORY_PATTERN = re.compile(
    r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9]+(?:[._/-][a-z0-9]+)*"
)
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


class ContractError(RuntimeError):
    """The immutable image handoff contract is invalid."""


def _load_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise ContractError("PyYAML is required for image contract plan/apply") from exc

    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def construct_mapping(
        loader: UniqueKeyLoader,
        node: Any,
        deep: bool = False,
    ) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in result:
                raise ContractError(f"Duplicate YAML key: {key!r}")
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    try:
        return yaml.load(  # noqa: S506 - loader subclasses SafeLoader
            path.read_text(encoding="utf-8"),
            Loader=UniqueKeyLoader,  # noqa: S506 - loader subclasses SafeLoader
        )
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ContractError(f"Unable to load {path}") from exc


def _normalized_relative(value: Any, *, field: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value != str(path)
        or "\\" in value
    ):
        raise ContractError(f"{field} must be a normalized project-relative POSIX path")
    return path


def _project_path(project: Path, relative: PurePosixPath, *, field: str) -> Path:
    path = project.joinpath(*relative.parts).resolve(strict=False)
    try:
        path.relative_to(project)
    except ValueError as exc:
        raise ContractError(f"{field} escapes the project") from exc
    return path


def _configuration(project: Path) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    document = _load_yaml(project / ".deploy/images.yml")
    if not isinstance(document, dict) or set(document) != {
        "schema_version",
        "services",
        "environments",
    }:
        raise ContractError(
            "images.yml must contain only schema_version, services and environments"
        )
    if document["schema_version"] != 1:
        raise ContractError("images.yml schema_version must be 1")
    raw_services = document["services"]
    raw_environments = document["environments"]
    if not isinstance(raw_services, dict) or not raw_services:
        raise ContractError("images.yml must declare at least one image service")
    if not isinstance(raw_environments, dict) or set(raw_environments) != {
        "stage",
        "prod",
    }:
        raise ContractError("images.yml must declare exactly stage and prod environments")

    services: dict[str, dict[str, str]] = {}
    image_services: dict[str, str] = {}
    for service, raw in sorted(raw_services.items()):
        if not isinstance(service, str) or SERVICE_PATTERN.fullmatch(service) is None:
            raise ContractError(f"Invalid image service: {service!r}")
        if not isinstance(raw, dict) or set(raw) != {"context", "dockerfile", "image"}:
            raise ContractError(f"Image service {service!r} has invalid fields")
        image = raw["image"]
        if not isinstance(image, str) or IMAGE_PATTERN.fullmatch(image) is None:
            raise ContractError(f"Image service {service!r} has an invalid image name")
        existing_service = image_services.get(image)
        if existing_service is not None:
            raise ContractError(
                f"Image services {existing_service!r} and {service!r} share "
                f"repository name {image!r}"
            )
        image_services[image] = service
        context_relative = _normalized_relative(
            raw["context"], field=f"{service} context"
        )
        dockerfile_relative = _normalized_relative(
            raw["dockerfile"], field=f"{service} dockerfile"
        )
        context = _project_path(project, context_relative, field=f"{service} context")
        dockerfile = _project_path(
            project,
            PurePosixPath(*context_relative.parts, *dockerfile_relative.parts),
            field=f"{service} dockerfile",
        )
        if not context.is_dir() or not dockerfile.is_file():
            raise ContractError(f"Image service {service!r} build inputs are missing")
        try:
            dockerfile.relative_to(context)
        except ValueError as exc:
            raise ContractError(
                f"Image service {service!r} dockerfile escapes its context"
            ) from exc
        services[service] = {
            "context": context.relative_to(project).as_posix(),
            "dockerfile": dockerfile.relative_to(project).as_posix(),
            "image": image,
        }

    environments: dict[str, str] = {}
    for environment in ("stage", "prod"):
        raw = raw_environments[environment]
        if not isinstance(raw, dict) or set(raw) != {"compose"}:
            raise ContractError(f"{environment} image environment must contain only compose")
        relative = _normalized_relative(raw["compose"], field=f"{environment} compose")
        compose = _project_path(project, relative, field=f"{environment} compose")
        if not compose.is_file():
            raise ContractError(f"{environment} Compose file is missing")
        environments[environment] = compose.relative_to(project).as_posix()
    return services, environments


def _json_mapping(value: str, *, label: str) -> dict[str, str]:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ContractError(f"{label} must be valid JSON") from exc
    if not isinstance(document, dict) or not document:
        raise ContractError(f"{label} must be a non-empty object")
    if any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in document.items()
    ):
        raise ContractError(f"{label} keys and values must be strings")
    return document


def _write_output(path: Path, name: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ContractError(f"GitHub output {name} must be single-line")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(f"{name}={value}\n")


def plan(project: Path, registry_prefix: str, deployment_sha: str, output: Path) -> None:
    if REGISTRY_PREFIX_PATTERN.fullmatch(registry_prefix) is None:
        raise ContractError(
            "registry prefix must be lowercase host/namespace without tag or digest"
        )
    if SHA_PATTERN.fullmatch(deployment_sha) is None:
        raise ContractError("deployment SHA must be 40 lowercase hexadecimal characters")
    services, _ = _configuration(project)
    repositories = {
        service: f"{registry_prefix}/{configuration['image']}"
        for service, configuration in services.items()
    }
    matrix = {
        "include": [
            {
                "service": service,
                "context": configuration["context"],
                "dockerfile": configuration["dockerfile"],
                "repository": repositories[service],
            }
            for service, configuration in services.items()
        ]
    }
    _write_output(output, "matrix", json.dumps(matrix, sort_keys=True, separators=(",", ":")))
    _write_output(
        output,
        "repositories",
        json.dumps(repositories, sort_keys=True, separators=(",", ":")),
    )
    _write_output(output, "deployment_sha", deployment_sha)


def record_result(
    service: str,
    repository: str,
    digest: str,
    deployment_sha: str,
    output: Path,
) -> None:
    if SERVICE_PATTERN.fullmatch(service) is None:
        raise ContractError("result service is invalid")
    if REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ContractError("result repository is invalid")
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ContractError("result digest is invalid")
    if SHA_PATTERN.fullmatch(deployment_sha) is None:
        raise ContractError("result deployment SHA is invalid")
    document = {
        "deployment_sha": deployment_sha,
        "immutable_reference": f"{repository}@{digest}",
        "service": service,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ContractError(f"Refusing to overwrite image result for {service}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")


def collect_results(
    expected_json: str,
    deployment_sha: str,
    results: Path,
    output: Path,
) -> None:
    expected = _json_mapping(expected_json, label="expected repositories")
    if SHA_PATTERN.fullmatch(deployment_sha) is None:
        raise ContractError("deployment SHA must be 40 lowercase hexadecimal characters")
    collected: dict[str, str] = {}
    for path in sorted(results.glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContractError(f"Invalid image result file: {path.name}") from exc
        if not isinstance(document, dict) or set(document) != {
            "deployment_sha",
            "immutable_reference",
            "service",
        }:
            raise ContractError(f"Invalid image result contract: {path.name}")
        service = document["service"]
        reference = document["immutable_reference"]
        if not isinstance(service, str) or not isinstance(reference, str):
            raise ContractError(f"Invalid image result values: {path.name}")
        if service in collected:
            raise ContractError(f"Duplicate image result for service {service!r}")
        if document["deployment_sha"] != deployment_sha:
            raise ContractError(f"Image result SHA mismatch for service {service!r}")
        repository = expected.get(service)
        if repository is None or not reference.startswith(repository + "@"):
            raise ContractError(f"Unexpected image result for service {service!r}")
        digest = reference.removeprefix(repository + "@")
        if DIGEST_PATTERN.fullmatch(digest) is None:
            raise ContractError(f"Invalid digest for service {service!r}")
        collected[service] = reference
    if set(collected) != set(expected):
        missing = sorted(set(expected) - set(collected))
        extra = sorted(set(collected) - set(expected))
        raise ContractError(f"Incomplete image result set; missing={missing}, extra={extra}")
    _write_output(
        output,
        "image_map",
        json.dumps(collected, sort_keys=True, separators=(",", ":")),
    )
    _write_output(output, "deployment_sha", deployment_sha)


def apply_map(project: Path, environment: str, image_map_json: str) -> None:
    if environment not in {"stage", "prod"}:
        raise ContractError("image map environment must be stage or prod")
    services, environments = _configuration(project)
    image_map = _json_mapping(image_map_json, label="image map")
    if set(image_map) != set(services):
        missing = sorted(set(services) - set(image_map))
        extra = sorted(set(image_map) - set(services))
        raise ContractError(f"Incomplete image map; missing={missing}, extra={extra}")
    for service, reference in image_map.items():
        if "@" not in reference:
            raise ContractError(f"Image map reference for {service!r} is not immutable")
        repository, digest = reference.rsplit("@", 1)
        if (
            REPOSITORY_PATTERN.fullmatch(repository) is None
            or DIGEST_PATTERN.fullmatch(digest) is None
        ):
            raise ContractError(f"Image map reference for {service!r} is invalid")

    compose_path = project / environments[environment]
    document = _load_yaml(compose_path)
    compose_services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(compose_services, dict):
        raise ContractError("Deployment Compose must contain services")
    for service, reference in image_map.items():
        body = compose_services.get(service)
        if not isinstance(body, dict) or not isinstance(body.get("image"), str):
            raise ContractError(f"Compose service {service!r} must define one image")
        body["image"] = reference

    import yaml

    compose_path.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--project-dir", type=Path, required=True)
    plan_parser.add_argument("--registry-prefix", required=True)
    plan_parser.add_argument("--deployment-sha", required=True)
    plan_parser.add_argument("--github-output", type=Path, required=True)
    result_parser = subparsers.add_parser("result")
    result_parser.add_argument("--service", required=True)
    result_parser.add_argument("--repository", required=True)
    result_parser.add_argument("--digest", required=True)
    result_parser.add_argument("--deployment-sha", required=True)
    result_parser.add_argument("--output", type=Path, required=True)
    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--expected-repositories-json", required=True)
    collect_parser.add_argument("--deployment-sha", required=True)
    collect_parser.add_argument("--results-dir", type=Path, required=True)
    collect_parser.add_argument("--github-output", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--project-dir", type=Path, required=True)
    apply_parser.add_argument("--environment", required=True)
    apply_parser.add_argument("--image-map-json", required=True)
    args = parser.parse_args(arguments)
    try:
        if args.command == "plan":
            plan(
                args.project_dir.resolve(),
                args.registry_prefix,
                args.deployment_sha,
                args.github_output,
            )
        elif args.command == "result":
            record_result(
                args.service,
                args.repository,
                args.digest,
                args.deployment_sha,
                args.output,
            )
        elif args.command == "collect":
            collect_results(
                args.expected_repositories_json,
                args.deployment_sha,
                args.results_dir,
                args.github_output,
            )
        else:
            apply_map(args.project_dir.resolve(), args.environment, args.image_map_json)
    except ContractError as exc:
        print(f"image contract error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
