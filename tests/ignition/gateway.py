"""A local Ignition gateway run by igdev, configured for the simulator over Native REST.

Only what the integration test needs: the igdev lifecycle, a disposable API token, and
the REST calls that create resources, import tags and import a project.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA = "/usr/local/bin/ignition/data"
RESOURCES = f"{DATA}/config/resources/core/ignition"
TOKEN_NAME = "gws-integration"
SECURITY_LEVEL = "GwsIntegration"


class GatewayError(RuntimeError):
    pass


def igdev(*args: str, timeout: float = 900) -> dict[str, Any]:
    """Run an igdev command in machine mode and return its data, or raise with its code."""
    exe = os.environ.get("IGDEV", "igdev")
    done = subprocess.run(
        [exe, *args, "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    try:
        envelope = json.loads(done.stdout)
    except json.JSONDecodeError as e:
        raise GatewayError(f"igdev {' '.join(args)}: {done.stderr[-800:]}") from e
    if not envelope.get("ok"):
        steps = "; ".join(r.get("command", "") for r in envelope.get("remediation", []))
        raise GatewayError(f"igdev {' '.join(args)}: {envelope.get('code')} {steps}")
    data: dict[str, Any] = envelope.get("data", {})
    return data


def _sh(*words: str) -> str:
    done = subprocess.run(list(words), capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise GatewayError(f"{' '.join(words)}: {done.stderr[-800:]}")
    return done.stdout


def _wait(what: str, seconds: float, ready: Any, interval: float = 2.0) -> Any:
    deadline = time.monotonic() + seconds
    while True:
        result = ready()
        if result:
            return result
        if time.monotonic() > deadline:
            raise GatewayError(f"{what}: not ready after {seconds:g} s")
        time.sleep(interval)


@dataclass
class Gateway:
    url: str
    container: str
    network_gateway: str
    """The host's address as the gateway container sees it."""
    token: str = ""

    # --- lifecycle -------------------------------------------------------------------------

    @classmethod
    def up(cls) -> Gateway:
        """`igdev setup`, then `gateway reset`: every run gets a gateway rebuilt from scratch
        (a fresh volume, so a fresh trial period and no state from an earlier run). Then find
        the container."""
        setup = igdev("setup")
        igdev("gateway", "reset", "--timeout", "10m")
        return cls._find(setup["namespace"])

    @classmethod
    def attach(cls, token: str) -> Gateway:
        """The gateway igdev already runs, kept as an earlier run left it, with that run's API
        token. Not `igdev setup`: it finds the running gateway's ports taken and moves them."""
        gateway = cls._find(igdev("gateway", "status")["namespace"])
        gateway.token = token
        return gateway

    @classmethod
    def _find(cls, namespace: str) -> Gateway:
        igdev("gateway", "wait", "--timeout", "10m")
        url = igdev("gateway", "url")["url"]
        container = _sh(
            "docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={namespace}"
        ).split()[0]
        network = _sh(
            "docker",
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}",
            container,
        ).split()[0]
        gateway = cls(url, container, network)
        gateway.wait_running()
        return gateway

    def wait_running(self) -> None:
        def running() -> bool:
            try:
                with urllib.request.urlopen(self.url + "/StatusPing", timeout=5) as r:
                    return bool(json.loads(r.read()).get("state") == "RUNNING")
            except (OSError, ValueError):
                return False

        _wait("gateway RUNNING", 600, running, 3)

    # --- API token -------------------------------------------------------------------------

    def bootstrap_token(self) -> str:
        """Install a disposable API token with full access, then restart the gateway.

        Ignition stores the SHA-256 of the token's 32 key bytes (unpadded Base64URL); the
        token is `<name>:<key>`. Its security level is added under Authenticated and granted
        gateway access, read and write."""
        raw = secrets.token_bytes(32)
        key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        work = Path(tempfile.mkdtemp(prefix="gws-ign-"))
        try:
            for name in ("security-levels", "security-properties"):
                _sh("docker", "cp", f"{self.container}:{RESOURCES}/{name}", str(work / name))
            levels_path = work / "security-levels" / "config.json"
            levels = json.loads(levels_path.read_text())
            auth = next(x for x in levels["securityLevels"] if x["name"] == "Authenticated")
            auth["children"] = [c for c in auth["children"] if c["name"] != SECURITY_LEVEL]
            auth["children"].append({"name": SECURITY_LEVEL, "description": "", "children": []})
            levels_path.write_text(json.dumps(levels, indent=2))
            path = [
                {"name": "Authenticated", "children": [{"name": SECURITY_LEVEL, "children": []}]}
            ]
            props_path = work / "security-properties" / "config.json"
            props = json.loads(props_path.read_text())
            for field in ("accessPermissions", "readPermissions", "writePermissions"):
                props[field] = {"type": "AnyOf", "securityLevels": path}
            props_path.write_text(json.dumps(props, indent=2))
            token_dir = work / "api-token" / TOKEN_NAME
            token_dir.mkdir(parents=True)
            (token_dir / "config.json").write_text(
                json.dumps(
                    {
                        "profile": {
                            "type": "basic-token",
                            "secureChannelRequired": False,
                            "securityLevels": [
                                {
                                    "name": "Authenticated",
                                    "description": "",
                                    "children": [
                                        {"name": SECURITY_LEVEL, "description": "", "children": []}
                                    ],
                                }
                            ],
                            "timestamp": int(time.time() * 1000),
                        },
                        "settings": {"tokenHash": digest},
                    }
                )
            )
            (token_dir / "resource.json").write_text(
                json.dumps(
                    {
                        "scope": "A",
                        "version": 1,
                        "restricted": False,
                        "overridable": True,
                        "files": ["config.json"],
                        "attributes": {"uuid": str(uuid.uuid4()), "enabled": True},
                    }
                )
            )
            _sh("docker", "exec", self.container, "mkdir", "-p", f"{RESOURCES}/api-token")
            for name in ("security-levels", "security-properties"):
                _sh("docker", "cp", f"{work / name}/.", f"{self.container}:{RESOURCES}/{name}/")
            _sh("docker", "cp", str(token_dir), f"{self.container}:{RESOURCES}/api-token/")
            _sh("docker", "exec", self.container, "chown", "-R", "2003:0", f"{RESOURCES}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        igdev("gateway", "restart")
        self.wait_running()
        self.token = f"{TOKEN_NAME}:{key}"
        _wait(
            "authenticated REST",
            300,
            lambda: self.request("GET", "/data/api/v1/gateway-info")[0] == 200,
            3,
        )
        return self.token

    # --- REST ------------------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: dict[str, str] | None = None,
        raw: bytes | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, Any]:
        url = self.url + path
        if query:
            url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if self.token:
            req.add_header("X-Ignition-API-Token", self.token)
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, payload = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
        except OSError:
            return 0, None
        try:
            return status, json.loads(payload) if payload else None
        except ValueError:
            return status, payload.decode(errors="replace")

    def create(self, resource_type: str, resource: dict[str, Any]) -> Any:
        status, body = self.request("POST", f"/data/api/v1/resources/{resource_type}", [resource])
        if status not in (200, 201):
            _, schema = self.request("GET", f"/data/api/v1/resources/type/{resource_type}")
            raise GatewayError(f"create {resource_type}: {status} {body} schema={schema}")
        return body

    def update(self, resource_type: str, resource: dict[str, Any]) -> Any:
        status, body = self.request("PUT", f"/data/api/v1/resources/{resource_type}", [resource])
        if status != 200:
            raise GatewayError(f"update {resource_type}: {status} {str(body)[:600]}")
        return body

    def find(self, resource_type: str, name: str) -> tuple[int, Any]:
        quoted = urllib.parse.quote(name, safe="")
        return self.request("GET", f"/data/api/v1/resources/find/{resource_type}/{quoted}")

    def delete(self, resource_type: str, name: str) -> None:
        """Delete a named resource; one that does not exist is already gone."""
        status, found = self.find(resource_type, name)
        if status == 404:
            return
        if status != 200:
            raise GatewayError(f"find {resource_type} {name}: {status}")
        quoted = urllib.parse.quote(name, safe="")
        path = f"/data/api/v1/resources/{resource_type}/{quoted}/{found['signature']}"
        status, body = self.request("DELETE", path)
        if status not in (200, 204):
            raise GatewayError(f"delete {resource_type} {name}: {status} {str(body)[:600]}")

    def import_tags(self, provider: str, document: dict[str, Any]) -> Any:
        def attempt() -> Any:
            status, body = self.request(
                "POST",
                "/data/api/v1/tags/import",
                raw=json.dumps(document).encode(),
                content_type="application/octet-stream",
                query={"provider": provider, "type": "json", "collisionPolicy": "o"},
            )
            text = json.dumps(body)
            return body if status == 200 and "Bad" not in text else None

        return _wait(f"tag import into {provider}", 180, attempt, 3)

    def import_project(self, name: str, source: Path) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    z.write(path, path.relative_to(source).as_posix())
        status, body = self.request(
            "POST",
            f"/data/api/v1/projects/import/{name}",
            raw=buffer.getvalue(),
            content_type="application/zip",
            query={"overwrite": "true"},
        )
        if status not in (200, 201):
            raise GatewayError(f"project import: {status} {body}")
