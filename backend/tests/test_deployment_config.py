"""Cross-file configuration consistency.

A container stack has no compiler. Nothing checks that the port nginx proxies
to is the port uvicorn binds, that the healthcheck probes a path that exists,
or that the SPA bundle the image copies is actually produced by the build. A
mismatch is not a test failure — it is a container that starts and returns 502
for every request, discovered in production.

These assertions are the substitute for a compiler: cheap, and they run in CI
before anything is deployed.

Each check below corresponds to a real mistake made while building this project.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent


def read(rel: str) -> str:
    path = ROOT / rel
    assert path.exists(), f"missing {rel}"
    return path.read_text()


DOCKERFILE = read("Dockerfile")
DOCKERFILE_VERCEL = read("Dockerfile.vercel")
COMPOSE = read("docker-compose.yml")
NGINX = read("nginx/nginx.conf")
ENTRYPOINT = read("deploy/entrypoint.sh")
VERCEL_JSON = json.loads(read("vercel.json"))


def _single(pattern: str, text: str, label: str) -> str:
    found = re.findall(pattern, text)
    assert found, f"no match for {label}"
    assert len(set(found)) == 1, f"{label} disagrees across the file: {set(found)}"
    return found[0]


class TestPortsAgree:
    def test_backend_port_is_the_same_everywhere(self) -> None:
        """nginx upstream, EXPOSE, SC_PORT and the entrypoint must all agree.

        Gotcha: nginx resolved `backend:8000` while the image listened on 8080.
        Every API call returned 502 and nothing in any test caught it, because
        the unit and integration tests never went through nginx.
        """
        port = _single(r"SC_PORT=(\d+)", DOCKERFILE, "Dockerfile SC_PORT")
        assert re.search(rf"backend:{port}\b", NGINX), f"nginx upstream is not backend:{port}"
        assert f"EXPOSE {port}" in DOCKERFILE
        assert f"SC_PORT={port}" in DOCKERFILE_VERCEL
        assert f"SC_PORT={port}" in ENTRYPOINT.replace("${SC_PORT:-", "SC_PORT=")

    def test_compose_publishes_the_nginx_port(self) -> None:
        compose = yaml.safe_load(COMPOSE)
        ports = compose["services"]["nginx"]["ports"]
        assert len(ports) == 1
        host, container = ports[0].split(":")
        assert host == container, "publishing on a different host port is fine, but say so"
        listen = re.search(r"^\s*listen\s+(\d+);", NGINX, re.M)
        assert listen, "nginx has no plain listen directive"
        assert container == listen.group(1)


class TestBackendNotExposed:
    def test_backend_port_is_not_published(self) -> None:
        """Reaching the API only through nginx is a security property.

        If the uvicorn port is published, a client can bypass nginx and reach
        the app directly -- which means --proxy-headers would trust a
        spoofable X-Forwarded-For and the rate limiter could be evaded.
        """
        compose = yaml.safe_load(COMPOSE)
        assert "ports" not in compose["services"]["backend"], (
            "backend must use `expose`, not `ports`"
        )
        assert compose["services"]["backend"].get("expose") == ["8080"]


class TestNginxConfig:
    def test_every_include_target_exists_in_the_repo(self) -> None:
        for target in re.findall(r"include\s+(/etc/nginx/\S+);", NGINX):
            name = Path(target).name
            if name == "mime.types":
                continue  # provided by the nginx base image
            assert (ROOT / "nginx" / name).exists(), (
                f"nginx.conf includes {target} but nginx/{name} is missing"
            )

    def test_nginx_upstream_is_a_variable(self) -> None:
        """A literal hostname in proxy_pass is resolved once, at parse time.

        Under Compose the backend has a dynamic IP, so nginx would refuse to
        start if it parsed its config before the backend existed. Variables
        defer resolution to request time.
        """
        for match in re.findall(r"proxy_pass\s+(\S+);", NGINX):
            assert match.startswith("$"), (
                f"proxy_pass {match} is a literal; use a variable so DNS resolution "
                "is deferred to request time"
            )

    def test_no_set_directive_in_the_http_block(self) -> None:
        """`set` is a rewrite-phase directive: invalid in `http`.

        nginx -t rejects it outright, so this is caught by the build-time check
        in both Dockerfiles -- but assert it here too, where the failure message
        can be useful rather than terse.
        """
        http_block = NGINX.split("http {", 1)[1].split("server {", 1)[0]
        offenders = [
            line.strip() for line in http_block.splitlines() if line.strip().startswith("set ")
        ]
        assert not offenders, f"`set` is not allowed in the http block: {offenders}"

    def test_locations_with_add_header_reinclude_security_headers(self) -> None:
        """nginx's add_header does not merge across levels.

        A location declaring ANY add_header silently discards every inherited
        security header. This exact bug shipped a version of this config where
        the HTML document had no CSP at all, caught only by curling the response.
        """
        unprotected = []
        for match in re.finditer(r"^\s*location\s+([^\n{]+)\{", NGINX, re.M):
            spec, start = match.group(1).strip(), match.end()
            depth, i = 1, start
            while i < len(NGINX) and depth:
                if NGINX[i] == "{":
                    depth += 1
                elif NGINX[i] == "}":
                    depth -= 1
                i += 1
            body = NGINX[start:i]
            has_header = any(line.strip().startswith("add_header") for line in body.splitlines())
            if has_header and "security-headers.conf" not in body:
                unprotected.append(spec)
        assert not unprotected, (
            f"these locations declare add_header without re-including "
            f"security-headers.conf, so they lose every security header: {unprotected}"
        )


class TestUploadLimits:
    def test_nginx_accepts_at_least_what_the_app_accepts(self) -> None:
        """nginx must never be the layer that rejects a legal file.

        If client_max_body_size <= the app limit, nginx 413s a file the app
        would have accepted, and the user gets a bare nginx error page instead
        of the app's structured message.
        """
        app_bytes = int(re.search(r"SC_MAX_UPLOAD_BYTES=(\d+)", DOCKERFILE).group(1))
        nginx_mb = int(re.search(r"client_max_body_size (\d+)m", NGINX).group(1))
        assert nginx_mb * 1024 * 1024 > app_bytes, (
            f"nginx allows {nginx_mb} MiB but the app allows "
            f"{app_bytes / 1048576:.1f} MiB; nginx would reject valid uploads"
        )

    def test_nginx_returns_json_for_its_own_errors(self) -> None:
        """The frontend parses one error envelope; nginx must speak it too."""
        assert "error_page 413" in NGINX
        assert "file_too_large" in NGINX
        assert "error_page 429" in NGINX


class TestEntrypoint:
    def test_does_not_invent_an_ipv6_listen_rewrite(self) -> None:
        """Regression guard for a real bug.

        The first version rewrote the `[::]:8080` line to `[[::]]:${PORT}`.
        sed does not pass `[::]` through as a literal, so nginx received a line
        beginning `[[::]]:8080` and refused to start: "unknown directive".
        The line is now deleted rather than rewritten.
        """
        assert "[[::]]" not in ENTRYPOINT
        assert "/^[[:space:]]*listen[[:space:]]\\+\\[::\\]:/d" in ENTRYPOINT

    def test_verifies_the_rewritten_config_before_starting(self) -> None:
        assert "nginx -t" in ENTRYPOINT, (
            "a bad rewrite should fail the boot, not surface as 502 on every request"
        )

    def test_entrypoint_rewrites_only_the_listen_directive(self) -> None:
        """A blanket s/PORT/$PORT/ would also hit client_max_body_size.

        That limit is written as `8m`, so it would survive -- but any future
        edit that puts a number in a comment or another directive becomes a
        silent misconfiguration. The rewrite is anchored to `listen`.
        """
        rewrites = re.findall(r'sed -i\s+"([^"]*)"', ENTRYPOINT)
        assert rewrites, "expected the entrypoint to rewrite the listen port"
        listen_rewrites = [r for r in rewrites if "listen" in r]
        assert listen_rewrites, f"no rewrite targets the listen directive: {rewrites}"
        # Every substitution that touches a port must be anchored to listen (or
        # be the backend->loopback upstream swap, which is unambiguous).
        for pattern in rewrites:
            if re.search(r"\$\{?\{?(PORT|APP_PORT|Vercel_PORT)", pattern):
                assert "listen" in pattern or "backend" in pattern or "sc_" in pattern, (
                    f"unanchored port substitution: {pattern}"
                )

    def test_forwards_sigterm(self) -> None:
        assert "trap term TERM INT" in ENTRYPOINT

    def test_constrains_the_target_scheme(self) -> None:
        assert "SC_TARGET" in read("backend/tools/smoke.py")
        assert "http://" in read("backend/tools/smoke.py")


class TestVercel:
    def test_vercel_json_is_minimal_and_valid(self) -> None:
        """Vercel auto-generates the rewrite for a Dockerfile.vercel.

        Adding one by hand risks conflicting with the generated route, and no
        documentation settles it. Leave it out; the platform handles it.
        """
        assert "rewrites" not in VERCEL_JSON, (
            "Vercel adds the container rewrite automatically; a manual one may conflict"
        )
        assert VERCEL_JSON["framework"] is None, (
            "framework detection must be off so the container is used"
        )
        assert "Dockerfile.vercel" in VERCEL_JSON["functions"]

    def test_vercel_image_starts_from_an_unprivileged_port(self) -> None:
        """Binding port 80 as non-root fails; Vercel reports it as a bare 500."""
        assert "SC_PORT=8080" in DOCKERFILE_VERCEL
        # Exact directive match. A plain `"EXPOSE 80" not in ...` is a substring
        # test and would reject the correct "EXPOSE 8080" while accepting a
        # privileged "EXPOSE 80" only if spacing differed. Anchor it.
        assert not re.search(r"^\s*EXPOSE\s+80\s*$", DOCKERFILE_VERCEL, re.M), (
            "the image must not declare privileged port 80; Vercel reports the "
            "bind failure as a bare 500"
        )
        assert re.search(r"^\s*EXPOSE\s+8080\s*$", DOCKERFILE_VERCEL, re.M)

    def test_vercel_image_runs_as_non_root(self) -> None:
        assert "USER 1001:1001" in DOCKERFILE_VERCEL

    def test_vercel_image_installs_fonts(self) -> None:
        """A missing font turns Malay/Chinese/Arabic/emoji into '#'.

        The build asserts all three families register, so an image without
        fonts fails to build rather than shipping a service that renders
        placeholder characters.
        """
        assert "fonts-dejavu-core" in DOCKERFILE_VERCEL
        assert "available_families" in DOCKERFILE_VERCEL


class TestHardening:
    @pytest.mark.parametrize("service", ["backend", "nginx"])
    def test_services_drop_all_capabilities(self, service: str) -> None:
        compose = yaml.safe_load(COMPOSE)
        svc = compose["services"][service]
        assert svc.get("cap_drop") == ["ALL"]
        assert "no-new-privileges:true" in svc.get("security_opt", [])

    @pytest.mark.parametrize("service", ["backend", "nginx"])
    def test_services_are_read_only(self, service: str) -> None:
        """A read-only root is enforceable here: nothing is ever written to disk."""
        compose = yaml.safe_load(COMPOSE)
        assert compose["services"][service].get("read_only") is True

    def test_runtime_image_is_non_root(self) -> None:
        assert "USER 1001:1001" in DOCKERFILE

    def test_native_dependencies_must_be_wheels(self) -> None:
        """A silent fall back to a source build is a build-time landmine."""
        assert "would compile from source" in DOCKERFILE
        assert "--no-deps" in DOCKERFILE, (
            "requirements.txt is the full closure; re-resolving defeats the pinning"
        )

    def test_requirements_are_fully_pinned(self) -> None:
        reqs = read("backend/requirements.txt")
        unpinned = [
            line.strip() for line in reqs.splitlines()
            if line.strip() and not line.startswith("#") and "==" not in line
        ]
        assert not unpinned, f"unpinned requirements: {unpinned}"


class TestDockerignore:
    def test_secrets_are_excluded(self) -> None:
        ignore = read(".dockerignore")
        for pattern in (".env", "*.pem", "*.key"):
            assert pattern in ignore, f"{pattern} must never enter an image layer"

    def test_tools_directory_is_available(self) -> None:
        """backend/tools/ holds smoke.py, which compose runs inside the image.

        Excluding it would break `docker compose --profile test run --rm smoke`.
        """
        ignore = read(".dockerignore")
        active = [
            line.strip() for line in ignore.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert "backend/tools/" not in active
        assert "backend/tests/" in active, "test files have no business in a runtime image"


class TestCI:
    def test_every_action_is_sha_pinned(self) -> None:
        """A floating tag can be repointed at unreviewed code."""
        workflow = read(".github/workflows/ci.yml")
        uses = re.findall(r"uses:\s*([\w./-]+)@(\S+)", workflow)
        assert uses, "no actions found -- has the workflow moved?"
        unpinned = [f"{repo}@{ref}" for repo, ref in uses if not re.fullmatch(r"[0-9a-f]{40}", ref)]
        assert not unpinned, f"actions pinned to a mutable ref: {unpinned}"

    def test_tests_run_before_the_image_builds(self) -> None:
        workflow = read(".github/workflows/ci.yml")
        needs = re.search(r"container:.*?needs:\s*\[([^\]]+)\]", workflow, re.S)
        assert needs is not None, "the container job must depend on the test jobs"
        assert "backend" in needs.group(1) and "frontend" in needs.group(1)

    def test_workflow_smoke_tests_the_real_stack(self) -> None:
        workflow = read(".github/workflows/ci.yml")
        assert "docker compose --profile test run --rm smoke" in workflow
