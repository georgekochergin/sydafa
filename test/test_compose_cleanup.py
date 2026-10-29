"""Exercise real Compose orphan/volume removal without starting database engines."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from uuid import uuid4


@unittest.skipUnless(os.environ.get("SYDAFA_TEST_COMPOSE") == "1", "explicit Docker integration test")
class ComposeCleanupTests(unittest.TestCase):
    """Verify preservation, complete removal and isolation using actual Docker resources."""

    def test_cleanup(self):
        project = "sydafa-cleanup-" + uuid4().hex[:10]
        prefix = project
        image = "sydafa:latest"

        def docker(*args):
            return subprocess.check_output(["docker", *args], text=True).strip()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.json"
            volumes = {
                kind: {"name": f"{prefix}-data-{kind}-{kind}"}
                for kind in ("mssql", "oracle")
            }
            model = {
                "services": {"sydafa": {
                    "image": image,
                    "command": ["sleep", "300"],
                    "environment": {"SYDAFA_CONTAINER_PREFIX": prefix},
                    "volumes": ["/var/run/docker.sock:/var/run/docker.sock"] + [
                        f"{kind}:/source-volumes/{kind}" for kind in volumes
                    ],
                }},
                "volumes": volumes,
            }
            path.write_text(json.dumps(model))
            compose = ("compose", "-p", project, "-f", str(path))
            foreign = project + "-foreign"
            names = [f"{prefix}-src-{kind}-{kind}" for kind in volumes]
            try:
                docker("volume", "create", foreign)
                docker("run", "-d", "--name", foreign, "--label", f"com.docker.compose.project={foreign}",
                       "-v", f"{foreign}:/data", image, "sleep", "300")
                docker(*compose, "up", "-d")
                code = '''
import os
from functools import partial
from unittest.mock import patch
from app.container_manager import _get_docker_client, _run_container
from app.models import SourceConfig
# Образ движка читается runtime-провайдером из env в момент сборки ContainerSpec
os.environ["SYDAFA_MSSQL_IMAGE"] = "sydafa:latest"
os.environ["SYDAFA_ORACLE_IMAGE"] = "sydafa:latest"
client = _get_docker_client()
client.containers.run = partial(client.containers.run, command=["sleep", "300"])
for kind, port in (("mssql", 1433), ("oracle", 1521)):
    _run_container(client, kind, SourceConfig(name=kind, type=kind, host=kind, port=port))
'''
                docker(*compose, "exec", "-T", "sydafa", "python", "-c", code)
                reset = '''
from pathlib import Path
from app.container_manager import _get_docker_client, reset_data_volume
client = _get_docker_client()
root = Path("/source-volumes/mssql")
(root / ".reset-probe").write_text("data")
parent = client.containers.get(__import__("socket").gethostname())
mount = next(m for m in parent.attrs["Mounts"] if m["Destination"] == str(root))
reset_data_volume(client, client.volumes.get(mount["Name"]))
assert not list(root.iterdir())
'''
                docker(*compose, "exec", "-T", "sydafa", "python", "-c", reset)
                docker(*compose, "down", "--remove-orphans")
                for name in names:
                    self.assertFalse(docker("ps", "-aq", "--filter", f"name=^{name}$"))
                for volume in volumes.values():
                    docker("volume", "inspect", volume["name"])
                docker(*compose, "up", "-d")
                docker(*compose, "exec", "-T", "sydafa", "python", "-c", code)
                docker(*compose, "down", "--remove-orphans", "--volumes")
                self.assertFalse(docker("ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"))
                self.assertFalse(docker("network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"))
                for volume in volumes.values():
                    self.assertFalse(docker("volume", "ls", "-q", "--filter", f"name=^{volume['name']}$"))
                self.assertEqual(docker("inspect", "-f", "{{.State.Running}}", foreign), "true")
                docker("volume", "inspect", foreign)
                print("PASS Compose cleanup: preserve volumes, remove orphans/volumes/network, isolate foreign resources; FAIL=0")
            finally:
                for name in names + [foreign]:
                    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
                subprocess.run(["docker", *compose, "down", "--remove-orphans", "--volumes"], capture_output=True)
                subprocess.run(["docker", "volume", "rm", foreign], capture_output=True)


if __name__ == "__main__":
    unittest.main()
