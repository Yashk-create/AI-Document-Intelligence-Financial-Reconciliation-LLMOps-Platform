"""Start the LiteLLM proxy with the cost tracker registered.

Secrets come from the environment (or a local .env that is git-ignored),
never from this file or the YAML config.

    python gateway/start_gateway.py            # uses gateway/litellm_config.yaml
    LITELLM_CONFIG=other.yaml python gateway/start_gateway.py
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load_dotenv(path: Path) -> None:
    """Minimal .env loader so python-dotenv is not required."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_dotenv(HERE.parent / ".env")
    config = Path(os.environ.get("LITELLM_CONFIG", HERE / "litellm_config.yaml"))
    if not config.exists():
        print(f"{config} not found: copy gateway/litellm_config.example.yaml and edit it.")
        return 1

    litellm = shutil.which("litellm")
    if not litellm:
        print("litellm CLI not found: pip install 'litellm[proxy]'")
        return 1

    env = os.environ.copy()
    # tracker_callback.py lives next to this file; LiteLLM imports it by module name.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH")]))
    cmd = [litellm, "--config", str(config),
           "--host", os.environ.get("GATEWAY_HOST", "127.0.0.1"),
           "--port", os.environ.get("GATEWAY_PORT", "4000")]
    return subprocess.call(cmd, env=env)


if __name__ == "__main__":
    sys.exit(main())
