import hashlib
import subprocess
from pathlib import Path

from test_runner_cleanup import _require_docker

from deploy_cli.secret_file import secure_secret_permissions, validate_secret_permissions

RUNTIME_IMAGE = "ansible-deploy:local"


def test_restrictive_registry_auth_remains_readable_through_docker_mount(
    tmp_path: Path,
) -> None:
    docker = _require_docker()
    auth = tmp_path / "registry-auth.json"
    content = '{"auths":{"ghcr.io":{"auth":"synthetic"}}}\n'
    auth.write_text(content, encoding="utf-8")
    secure_secret_permissions(auth)
    validate_secret_permissions(auth)
    digest = hashlib.sha256(auth.read_bytes()).hexdigest()

    result = subprocess.run(  # noqa: S603 - resolved Docker executable
        [
            docker,
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "--volume",
            f"{auth}:/run/registry-auth.json:ro",
            RUNTIME_IMAGE,
            "-c",
            f"sha256sum /run/registry-auth.json | grep -q '^{digest} '",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
