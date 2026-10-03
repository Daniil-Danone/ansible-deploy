import subprocess
import tempfile
from pathlib import Path

ALLOY_IMAGE = "grafana/alloy:v1.10.2"
ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / "ansible/roles/collector/templates/config.alloy.j2"


def render_fixture() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{ app_environment }}": "stage",
        "{{ ansible_hostname }}": "stage-host",
        "{{ collector_push_url }}": "https://grafana.example.com/loki/api/v1/push",
        "{{ collector_username }}": "alloy",
        "{% raw %}": "",
        "{% endraw %}": "",
    }
    for source, replacement in replacements.items():
        text = text.replace(source, replacement)
    if "{%" in text or "{{ " in text:
        raise RuntimeError("Alloy template contains an unhandled Jinja expression")
    return text


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-alloy-") as directory:
        config = Path(directory) / "config.alloy"
        config.write_text(render_fixture(), encoding="utf-8", newline="\n")
        mount = f"{config.parent.resolve()}:/work:ro"
        for arguments in (
            ["fmt", "--test", "/work/config.alloy"],
            ["validate", "/work/config.alloy"],
        ):
            subprocess.run(  # noqa: S603 - fixed Docker command and pinned image
                [  # noqa: S607 - standard Docker executable
                    "docker",
                    "run",
                    "--rm",
                    "-v",
                    mount,
                    ALLOY_IMAGE,
                    *arguments,
                ],
                cwd=ROOT,
                check=True,
                timeout=120,
            )


if __name__ == "__main__":
    main()
