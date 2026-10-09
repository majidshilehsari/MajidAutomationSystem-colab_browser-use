"""Contract tests for the Hostim deployment track (hostim/).

These tests exist because the Hostim image cannot be built or run in most
development environments, let alone in CI: there is no Docker, no Xvfb and no
Chrome. What CAN be checked without any of that is the set of promises the
deployment makes, which is what this file pins down:

* the container publishes exactly one port, and VNC/CDP stay private,
* no Cloudflare Quick Tunnel exists anywhere in the Hostim path,
* the secrets are generated, stored with mode 0600, and never logged,
* the platform's health check path really answers 200 without a token,
* the Hostim template/compose files agree with the image,
* and the Colab track (install.sh, start_colab_browser.sh, stop script) is
  still exactly what it was - nothing in hostim/ is wired into it.

Everything here is static except the health-route test, which builds the real
AutomationApi. No Docker, no network, no X display required.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTIM_DIR = os.path.join(REPO_ROOT, "hostim")
sys.path.insert(0, REPO_ROOT)

DOCKERFILE = os.path.join(HOSTIM_DIR, "Dockerfile")
ENTRYPOINT = os.path.join(HOSTIM_DIR, "docker-entrypoint.sh")
HEALTHCHECK = os.path.join(HOSTIM_DIR, "healthcheck.sh")
COMPOSE = os.path.join(HOSTIM_DIR, "compose.yaml")
TEMPLATE = os.path.join(HOSTIM_DIR, "hostim-template.yaml")
GUIDE = os.path.join(HOSTIM_DIR, "GUIDE.fa.md")

# Hostim hands a git-source build to BuildKit as "<repo>.git#<commit>", which
# means the build context is the repository root and the Dockerfile name is the
# default "Dockerfile" in that root. The real deployment proved it:
#   failed to read dockerfile: open Dockerfile: no such file or directory
# So the root carries a mirror of hostim/Dockerfile. hostim/ stays canonical.
ROOT_DOCKERFILE = os.path.join(REPO_ROOT, "Dockerfile")
ROOT_DOCKERIGNORE = os.path.join(REPO_ROOT, "Dockerfile.dockerignore")
MIRROR_NOTE = re.compile(
    r"(?ms)^# ===== BEGIN hostim-root-mirror-note =====\n.*?"
    r"^# ===== END hostim-root-mirror-note =====\n")

PUBLIC_PORT = "6080"
VNC_PORT = "5901"
CDP_PORT = "9222"
HEALTH_PATH = "/automation/api/info"


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def lines(path):
    return [line.rstrip("\n") for line in read(path).splitlines()]


def code_lines(path):
    """Non-empty lines that are not comments, so a commented-out example in a
    file cannot satisfy (or break) an assertion."""
    out = []
    for raw in read(path).splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(raw)
    return out


class TestHostimFilesPresent(unittest.TestCase):
    def test_every_hostim_deliverable_exists(self):
        for path in (DOCKERFILE, ENTRYPOINT, HEALTHCHECK, COMPOSE, TEMPLATE, GUIDE):
            self.assertTrue(os.path.isfile(path), "missing deliverable: %s" % path)

    def test_hostim_files_live_in_their_own_directory(self):
        # Separation rule: the Hostim track stays inside hostim/. The only two
        # files it may add to the repository root are the Dockerfile mirror and
        # its ignore file, because Hostim's git build reads "Dockerfile" from
        # the context root. Everything else - the entrypoint, the supervisor,
        # the compose harness, the template, the guide - belongs in hostim/.
        root_entries = set(os.listdir(REPO_ROOT))
        for forbidden in ("docker-entrypoint.sh", "entrypoint.sh", "compose.yaml",
                          "docker-compose.yml", "hostim-template.yaml", ".dockerignore"):
            self.assertNotIn(forbidden, root_entries,
                             "%s belongs in hostim/, not in the repository root" % forbidden)

    def test_the_root_mirror_files_are_the_only_hostim_addition_to_the_root(self):
        tracked = subprocess.run(["git", "-C", REPO_ROOT, "ls-files"],
                                 capture_output=True, text=True).stdout.split()
        added = {p for p in tracked if "/" not in p and p.startswith(("Dockerfile",))}
        self.assertEqual(added, {"Dockerfile", "Dockerfile.dockerignore"},
                         "unexpected Dockerfile-shaped files at the repository root: %s"
                         % sorted(added))

    def test_scripts_are_executable(self):
        for path in (ENTRYPOINT, HEALTHCHECK):
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IXUSR, "%s is not executable" % path)


class TestRootMirrorOfHostimDockerfile(unittest.TestCase):
    """The root Dockerfile must be a byte-for-byte mirror of hostim/Dockerfile.

    Duplication is the price of Hostim's git build reading the default
    "Dockerfile" from the context root. These tests make the duplication safe:
    the two files cannot drift apart without the suite going red.
    """

    def setUp(self):
        self.canonical = read(DOCKERFILE)
        self.mirror = read(ROOT_DOCKERFILE)

    def test_mirror_is_byte_identical_after_stripping_its_note(self):
        stripped = MIRROR_NOTE.sub("", self.mirror)
        self.assertEqual(stripped, self.canonical,
                         "Dockerfile drifted from hostim/Dockerfile; regenerate the "
                         "mirror (see hostim/GUIDE.fa.md, section 'mirror ریشه')")

    def test_note_is_present_and_points_at_the_canonical_file(self):
        self.assertRegex(self.mirror, MIRROR_NOTE,
                         "the root mirror must carry the BEGIN/END note explaining "
                         "why it exists")
        note = MIRROR_NOTE.search(self.mirror).group(0)
        self.assertIn("hostim/Dockerfile", note)
        self.assertIn("TestRootMirrorOfHostimDockerfile", note)

    def test_syntax_directive_is_still_the_first_line(self):
        # BuildKit only honours "# syntax=" when it is the very first line, so
        # the note must be inserted after it, never before.
        self.assertEqual(self.mirror.splitlines()[0], "# syntax=docker/dockerfile:1")
        self.assertEqual(self.canonical.splitlines()[0], "# syntax=docker/dockerfile:1")

    def test_dockerignore_is_an_exact_copy(self):
        self.assertEqual(read(ROOT_DOCKERIGNORE),
                         read(os.path.join(HOSTIM_DIR, "Dockerfile.dockerignore")))

    def test_mirror_exposes_only_the_single_public_port(self):
        exposed = re.findall(r"(?m)^EXPOSE\s+(.+)$", self.mirror)
        self.assertEqual(exposed, [PUBLIC_PORT],
                         "the root mirror must publish exactly one port")

    def test_mirror_runs_as_non_root_behind_tini(self):
        self.assertRegex(self.mirror, r"(?m)^USER\s+automation\s*$")
        self.assertIn('ENTRYPOINT ["/usr/bin/tini", "--", '
                      '"/app/hostim/docker-entrypoint.sh"]', self.mirror)

    def test_mirror_has_no_cloudflare_in_its_code_lines(self):
        pattern = re.compile(r"cloudflared|trycloudflare", re.I)
        for line in self.mirror.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            self.assertIsNone(pattern.search(line),
                              "the root mirror runs Cloudflare: %s" % stripped)

    def test_mirror_does_not_copy_the_colab_launchers(self):
        copies = re.findall(r"(?m)^COPY\s+(\S+)", self.mirror)
        self.assertEqual(copies, ["automation/", "browser_control.sh", "hostim/"])

    def test_mirror_keeps_the_hostim_entrypoint_and_healthcheck(self):
        # The image still runs the hostim/ copies even when built from the root
        # Dockerfile, because COPY hostim/ puts them at /app/hostim/.
        self.assertIn("/app/hostim/docker-entrypoint.sh", self.mirror)
        self.assertIn('CMD ["/app/hostim/healthcheck.sh"]', self.mirror)


class TestRuntimeUserIsRobust(unittest.TestCase):
    """The real Hostim build died here: ubuntu:24.04 already has a stock
    account on uid/gid 1000, so an unconditional `groupadd --gid 1000` exits 4
    with "GID '1000' already exists". These tests pin the fix down."""

    def setUp(self):
        self.body = read(DOCKERFILE)

    def test_uid_1000_is_looked_up_before_it_is_claimed(self):
        guard = self.body.find("if getent passwd 1000")
        groupadd = self.body.find("groupadd --system --gid 1000")
        self.assertNotEqual(guard, -1, "the Dockerfile must check whether uid 1000 is taken")
        self.assertNotEqual(groupadd, -1)
        self.assertLess(guard, groupadd,
                        "groupadd runs before the uid/gid 1000 guard, so it will "
                        "fail again on a base image that ships a stock account")

    def test_gid_1000_is_looked_up_before_it_is_claimed(self):
        guard = self.body.find("if getent group 1000")
        groupadd = self.body.find("groupadd --system --gid 1000")
        self.assertNotEqual(guard, -1)
        self.assertLess(guard, groupadd)

    def test_the_stock_account_is_removed_by_id_not_by_guessed_name(self):
        # The base image's account is "ubuntu" today; nothing should depend on
        # that name staying true.
        self.assertRegex(self.body, r'stock_user="\$\(getent passwd 1000 \| cut -d: -f1\)"')
        self.assertRegex(self.body, r'stock_group="\$\(getent group 1000 \| cut -d: -f1\)"')
        self.assertNotRegex(self.body, r"(?m)^\s*(userdel|groupdel)\s+ubuntu\b",
                            "do not hard-code the stock account's name")

    def test_userdel_has_a_fallback_when_the_home_cannot_be_removed(self):
        self.assertIn('userdel -r "$stock_user" || userdel "$stock_user"', self.body)

    def test_the_user_creation_runs_in_strict_mode_and_is_verified(self):
        block = self.body[self.body.find("if getent passwd 1000"):]
        block = block[:block.find("\n\n")]
        self.assertIn("id automation", block, "prove the account really exists")
        self.assertIn("test -d /home/automation", block, "prove the home directory exists")
        # The RUN that owns this block must be strict, or the guards are theatre.
        self.assertRegex(self.body, r"(?m)^RUN set -eux; \\\n\s*if getent passwd 1000")

    def test_ownership_is_granted_by_name_so_a_uid_change_cannot_strand_files(self):
        self.assertIn("chown -R automation:automation", self.body)
        self.assertNotRegex(self.body, r"chown\s+-R\s+\d+:\d+",
                            "chown by numeric id would break if the uid ever moves")

    def test_user_instruction_and_home_agree_with_the_created_account(self):
        self.assertRegex(self.body, r"(?m)^USER\s+automation\s*$")
        self.assertIn("--home-dir /home/automation", self.body)
        self.assertRegex(self.body, r"(?m)^\s*HOME=/home/automation\s*\\?\s*$")

    def test_the_image_still_does_not_need_root(self):
        # Everything the runtime writes lives under /data and /home/automation,
        # both chowned to the unprivileged account.
        self.assertRegex(self.body, r"chown -R automation:automation /app /data /home/automation")
        self.assertNotRegex(self.body, r"(?m)^USER\s+root\s*$")


class TestShellScriptsParse(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("bash is not available")

    def test_bash_syntax(self):
        for path in (ENTRYPOINT, HEALTHCHECK):
            result = subprocess.run([self.bash, "-n", path],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0,
                             "%s does not parse: %s" % (path, result.stderr))

    def test_scripts_are_strict(self):
        for path in (ENTRYPOINT, HEALTHCHECK):
            self.assertIn("set -euo pipefail", read(path),
                          "%s must run in strict mode" % path)


class TestNoCloudflareInHostimPath(unittest.TestCase):
    """Hostim terminates HTTPS itself, so a Quick Tunnel would only add a
    second hop and a second failure mode. It must not exist in this track."""

    def test_no_cloudflared_anywhere_in_hostim(self):
        pattern = re.compile(r"cloudflared|trycloudflare|cloudflare tunnel", re.I)
        for name in sorted(os.listdir(HOSTIM_DIR)):
            path = os.path.join(HOSTIM_DIR, name)
            if not os.path.isfile(path) or name.endswith(".md"):
                continue
            # Only executable lines count: the files legitimately *explain* in a
            # comment why there is no tunnel here.
            for number, line in enumerate(code_lines(path), start=1):
                self.assertIsNone(
                    pattern.search(line),
                    "%s:%d runs Cloudflare in the Hostim path: %s"
                    % (name, number, line.strip()))

    def test_entrypoint_does_not_install_or_start_a_tunnel(self):
        body = "\n".join(code_lines(ENTRYPOINT))
        self.assertNotIn("cloudflared", body)
        self.assertNotIn("tunnel", body)


class TestSinglePublicPort(unittest.TestCase):
    def test_dockerfile_exposes_only_the_http_port(self):
        exposed = [line.split()[-1] for line in code_lines(DOCKERFILE)
                   if line.strip().startswith("EXPOSE")]
        self.assertEqual(exposed, [PUBLIC_PORT],
                         "the image must publish exactly one port: %s" % PUBLIC_PORT)

    def test_entrypoint_binds_all_interfaces_on_the_http_port(self):
        body = read(ENTRYPOINT)
        self.assertIn("--listen-host 0.0.0.0", body,
                      "the platform routes to the pod IP, not to loopback")
        self.assertIn('--listen-port "$PORT"', body)

    def test_vnc_stays_on_loopback(self):
        body = read(ENTRYPOINT)
        self.assertIn("--vnc-host 127.0.0.1", body)
        self.assertIn("-localhost", body, "x11vnc must listen on loopback only")

    def test_cdp_is_never_published(self):
        for path in (DOCKERFILE, COMPOSE, TEMPLATE, ENTRYPOINT):
            text = read(path)
            # A published port shows up as "5901:5901" / "- 5901" in compose, or
            # as an EXPOSE line in the Dockerfile. Neither may mention these.
            self.assertNotRegex(text, r"(?m)^\s*(EXPOSE|-)\s*.*\b%s\b" % VNC_PORT,
                                "%s publishes the VNC port" % path)
            self.assertNotRegex(text, r"(?m)^\s*(EXPOSE|-)\s*.*\b%s\b" % CDP_PORT,
                                "%s publishes the CDP port" % path)

    def test_compose_publishes_only_the_http_port(self):
        # Matches both "6080:6080" and "${LOCAL_PORT:-6080}:6080".
        mappings = re.findall(r'"([^"]*):(\d+)"', read(COMPOSE))
        self.assertTrue(mappings, "compose should publish the HTTP port for local testing")
        for host_side, container_port in mappings:
            self.assertEqual(container_port, PUBLIC_PORT,
                             "compose publishes container port %s; only %s is public"
                             % (container_port, PUBLIC_PORT))
            self.assertNotIn(VNC_PORT, container_port)
            self.assertNotIn(CDP_PORT, host_side)


class TestImageHygiene(unittest.TestCase):
    def test_runs_as_a_non_root_user(self):
        users = [line.split()[-1] for line in code_lines(DOCKERFILE)
                 if line.strip().startswith("USER ")]
        self.assertEqual(users, ["automation"],
                         "the image must end with a single non-root USER")

    def test_uses_tini_as_pid_one(self):
        entrypoint_lines = [line for line in code_lines(DOCKERFILE)
                            if line.strip().startswith("ENTRYPOINT")]
        self.assertEqual(len(entrypoint_lines), 1)
        self.assertIn("tini", entrypoint_lines[0],
                      "PID 1 must reap the zombies Chrome leaves behind")
        self.assertIn("docker-entrypoint.sh", entrypoint_lines[0])

    def test_refuses_a_non_amd64_build(self):
        # Hostim nodes are linux/amd64; an arm64 image fails with "exec format
        # error" and no logs at all, which is miserable to debug remotely.
        body = read(DOCKERFILE)
        self.assertIn("dpkg --print-architecture", body)
        self.assertIn("amd64", body)

    def test_websockify_module_is_verified_at_build_time(self):
        # automation/server.py imports the module (it drives top_new_client
        # itself); a missing module silently kills the sidebar and the API.
        body = read(DOCKERFILE)
        self.assertIn("import websockify.websocketproxy", body)

    def test_colab_launchers_are_not_copied_into_the_image(self):
        copies = [line for line in code_lines(DOCKERFILE)
                  if line.strip().startswith("COPY ")]
        joined = "\n".join(copies)
        for colab_only in ("install.sh", "start_colab_browser.sh",
                           "stop_colab_browser.sh", "run_tests.sh"):
            self.assertNotIn(colab_only, joined,
                             "%s is Colab-only and must not be in the image" % colab_only)
        self.assertIn("COPY automation/", joined)
        self.assertIn("COPY browser_control.sh", joined)

    def test_has_a_local_healthcheck(self):
        self.assertRegex(read(DOCKERFILE), r"(?m)^HEALTHCHECK ")
        self.assertIn("healthcheck.sh", read(DOCKERFILE))


class TestEntrypointBehaviour(unittest.TestCase):
    def setUp(self):
        self.body = read(ENTRYPOINT)

    def test_traps_sigterm_and_shuts_the_tree_down(self):
        self.assertIn("trap shutdown TERM INT", self.body)
        self.assertIn("stop_all", self.body,
                      "a shared shutdown must run on SIGTERM and on every failure exit")
        # Both the signal path and the fatal path have to clean up.
        shutdown_fn = self.body.split("shutdown() {", 1)[1].split("\n}", 1)[0]
        self.assertIn("stop_all", shutdown_fn)
        fatal_block = self.body.split("stopped unexpectedly", 1)[1][:400]
        self.assertIn("stop_all", fatal_block,
                      "a dead core service must not leave the rest running")

    def test_relaunches_chrome_but_not_the_core_services(self):
        self.assertIn("Chrome is gone; relaunching it", self.body)
        self.assertIn("chrome_alive", self.body)
        self.assertIn("--user-data-dir=$PROFILE_DIR", self.body,
                      "Chrome must be recognised by its profile, like the Colab launcher")

    def test_state_lives_on_the_volume(self):
        for fragment in ('DATA_DIR=${DATA_DIR:-/data}',
                         'PROFILE_DIR="$DATA_DIR/chrome-profile"',
                         'AUTOMATION_DIR="$DATA_DIR/automation"',
                         'LOG_DIR="$DATA_DIR/logs"'):
            self.assertIn(fragment, self.body)

    def test_web_root_is_ephemeral_not_on_the_volume(self):
        # A web root that survives on the volume would keep serving an old
        # noVNC build next to new sidebar assets after an image upgrade.
        self.assertRegex(self.body, r"WEB_ROOT=\$\{WEB_ROOT:-/tmp/")

    def test_chrome_flags_match_the_colab_launcher(self):
        colab = read(os.path.join(REPO_ROOT, "start_colab_browser.sh"))
        for flag in ("--no-sandbox", "--disable-dev-shm-usage", "--use-gl=swiftshader",
                     "--enable-unsafe-swiftshader", "--no-first-run",
                     "--no-default-browser-check"):
            self.assertIn(flag, self.body, "missing Chrome flag: %s" % flag)
            self.assertIn(flag, colab, "Colab launcher changed: %s" % flag)

    def test_refuses_an_unwritable_volume_with_a_useful_message(self):
        self.assertIn("is not writable", self.body)
        self.assertIn("/volumes/", self.body,
                      "the message should point at the Bastion path Hostim documents")


class TestSecretHandling(unittest.TestCase):
    def test_secrets_are_generated_when_absent(self):
        body = read(ENTRYPOINT)
        self.assertIn("openssl rand -hex 8", body, "AUTOMATION_TOKEN generator")
        self.assertIn("openssl rand -hex 4", body, "VNC password generator")

    def test_secrets_file_is_locked_down(self):
        body = read(ENTRYPOINT)
        self.assertIn("umask 077", body)
        self.assertIn('chmod 600 "$SECRETS_FILE"', body)
        self.assertIn('chmod 600 "$VNC_PASS_FILE"', body)

    def test_secrets_are_never_logged(self):
        for line in read(ENTRYPOINT).splitlines():
            if not re.search(r"\blog\b", line):
                continue
            self.assertNotRegex(line, r"\$\{?(AUTOMATION_TOKEN|VNC_PASSWORD)\b",
                                "a secret must never reach the container log: %s" % line)

    def test_token_is_not_passed_on_the_command_line(self):
        # /proc/<pid>/cmdline is world readable, so a --token argument leaks the
        # secret to anything in the container. server.py reads AUTOMATION_TOKEN
        # from the environment instead.
        body = "\n".join(code_lines(ENTRYPOINT))
        self.assertNotIn("--token", body)
        self.assertIn("export AUTOMATION_TOKEN", body)

    def test_environment_overrides_the_generated_secret(self):
        # Hostim can inject AUTOMATION_TOKEN/VNC_PASSWORD as app env vars; that
        # must win over the generated file. The entrypoint sources the file, so
        # the env values have to be captured BEFORE the source and restored
        # after it - otherwise the file silently wins. This shipped broken once;
        # TestSecretPrecedenceIsReal runs the function to prove the behaviour.
        body = read(ENTRYPOINT)
        capture = body.find('local env_token="${AUTOMATION_TOKEN:-}"')
        source = body.find('source "$SECRETS_FILE"')
        restore = body.find('AUTOMATION_TOKEN="${env_token:-$file_token}"')
        self.assertNotEqual(capture, -1, "the env token is never captured")
        self.assertNotEqual(source, -1)
        self.assertNotEqual(restore, -1, "the env token is never restored after sourcing")
        self.assertLess(capture, source, "the env value must be captured before the file is sourced")
        self.assertLess(source, restore, "the env value must be restored after the file is sourced")

    def test_no_literal_secret_is_committed(self):
        literal = re.compile(
            r"(AUTOMATION_TOKEN|VNC_PASSWORD)[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9+/_-]{8,})")
        for name in sorted(os.listdir(HOSTIM_DIR)):
            path = os.path.join(HOSTIM_DIR, name)
            if not os.path.isfile(path):
                continue
            for number, line in enumerate(read(path).splitlines(), start=1):
                for match in literal.finditer(line):
                    value = match.group(2)
                    self.assertTrue(
                        value.startswith("$") or "GENERATE_ME" in value
                        or "openssl" in line or "<" in value,
                        "%s:%d looks like a committed secret: %s" % (name, number, line.strip()))


class TestHealthCheckRoute(unittest.TestCase):
    """The platform's readiness probe cannot authenticate, so the health path
    must be the one route that is open by design. This is a real API call."""

    TOKEN = "test-token-for-hostim-healthcheck"

    @classmethod
    def setUpClass(cls):
        try:
            from automation.api import AutomationApi, FlowStore
            from automation.archive import shot_archive, text_archive
            from automation.detect import Detector
            from automation.engine import AutomationEngine, ControlBackend
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest("automation package not importable: %s" % exc)

        cls.tmp = tempfile.mkdtemp(prefix="hostim-health-")
        cls.data_dir = os.path.join(cls.tmp, "data")
        os.makedirs(cls.data_dir, exist_ok=True)
        backend = ControlBackend(script_path="/nonexistent/browser_control.sh")
        shots = shot_archive(cls.data_dir)
        texts = text_archive(cls.data_dir)
        engine = AutomationEngine(backend, cls.data_dir, shots=shots, texts=texts)
        detector = Detector(backend, cls.data_dir, {"width": 1366, "height": 768})
        cls.api = AutomationApi(engine, FlowStore(cls.data_dir), detector,
                                data_dir=cls.data_dir, token=cls.TOKEN,
                                control_script="/nonexistent/browser_control.sh",
                                shots=shots, texts=texts)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_health_path_needs_no_token(self):
        status, _, payload = self.api.handle("GET", HEALTH_PATH[len("/automation/api"):],
                                             headers={})
        self.assertEqual(status, 200,
                         "%s must answer 200 without a token or Hostim's readiness "
                         "probe can never pass" % HEALTH_PATH)
        self.assertIn(b"authRequired", payload)

    def test_every_other_route_still_needs_the_token(self):
        status, _, _ = self.api.handle("GET", "/status", headers={})
        self.assertEqual(status, 401)

    def test_the_token_still_unlocks_the_api(self):
        status, _, _ = self.api.handle(
            "GET", "/status", headers={"x-automation-token": self.TOKEN})
        self.assertEqual(status, 200)

    def test_healthcheck_script_probes_the_same_path(self):
        body = read(HEALTHCHECK)
        self.assertIn(HEALTH_PATH, body)
        self.assertIn("/vnc.html", body,
                      "noVNC itself must be served, not only the API")


class TestHostimManifests(unittest.TestCase):
    def test_template_uses_the_health_path_and_one_replica(self):
        body = read(TEMPLATE)
        self.assertIn("healthCheckPath: %s" % HEALTH_PATH, body)
        self.assertRegex(body, r"(?m)^\s*replicas:\s*1\s*$")
        self.assertRegex(body, r"(?m)^\s*public:\s*true\s*$")
        self.assertRegex(body, r"(?m)^\s*httpPort:\s*%s\s*$" % PUBLIC_PORT)
        self.assertRegex(body, r"(?m)^\s*mountPath:\s*/data\s*$")
        # The root mirror, because Hostim's git build resolves "Dockerfile" in
        # the build context root. See TestRootMirrorOfHostimDockerfile.
        self.assertIn("dockerfilepath: Dockerfile", body)
        self.assertNotIn("dockerfilepath: hostim/Dockerfile", body)

    def test_template_does_not_deploy_the_stable_colab_branch(self):
        branch = re.search(r"(?m)^\s*branch:\s*(\S+)\s*$", read(TEMPLATE))
        self.assertIsNotNone(branch, "the template must pin a branch")
        self.assertNotEqual(branch.group(1), "colab-stable",
                            "the Hostim track must never build the stable Colab branch")
        self.assertEqual(branch.group(1), "hostim-deploy")

    def test_template_leaves_the_command_override_empty(self):
        # A Command Override replaces ENTRYPOINT and CMD completely, which would
        # drop tini and the whole supervisor.
        self.assertNotRegex(read(TEMPLATE), r"(?m)^\s*command:")

    def test_compose_points_at_the_hostim_dockerfile(self):
        body = read(COMPOSE)
        self.assertIn("dockerfile: hostim/Dockerfile", body)
        self.assertRegex(body, r"(?m)^\s*context:\s*\.\.\s*$",
                         "the build context must be the repository root")
        self.assertIn("/data", body)

    def test_manifests_parse_as_yaml_when_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed; skipping the parse check")
        for path in (COMPOSE, TEMPLATE):
            with self.subTest(path=os.path.basename(path)):
                data = yaml.safe_load(read(path))
                self.assertIsInstance(data, dict)
        template = yaml.safe_load(read(TEMPLATE))
        apps = template["components"]["apps"]
        self.assertEqual(len(apps), 1)
        self.assertEqual(apps[0]["healthCheckPath"], HEALTH_PATH)
        self.assertEqual(apps[0]["replicas"], 1)


class TestColabTrackIsUntouched(unittest.TestCase):
    """The Hostim track is additive. The Colab launcher, its ports, its noVNC
    path and its Cloudflare Quick Tunnel must be exactly as they were."""

    def setUp(self):
        self.start = read(os.path.join(REPO_ROOT, "start_colab_browser.sh"))
        self.stop = read(os.path.join(REPO_ROOT, "stop_colab_browser.sh"))
        self.install = read(os.path.join(REPO_ROOT, "install.sh"))

    def test_colab_launcher_keeps_its_ports_and_tunnel(self):
        for fragment in ("VNC_PORT=5901", "NOVNC_PORT=6080", "CDP_PORT=9222",
                         "--listen-host 127.0.0.1",
                         "cloudflared tunnel --url",
                         "trycloudflare",
                         "/usr/share/novnc"):
            self.assertIn(fragment, self.start,
                          "Colab behaviour changed: %s is gone" % fragment)

    def test_colab_scripts_do_not_reference_the_hostim_track(self):
        for name, body in (("start_colab_browser.sh", self.start),
                           ("stop_colab_browser.sh", self.stop),
                           ("install.sh", self.install)):
            self.assertNotIn("hostim", body.lower(),
                             "%s must not depend on the Hostim track" % name)

    def test_runtime_artifacts_stay_out_of_git(self):
        ignore = read(os.path.join(REPO_ROOT, ".gitignore"))
        for entry in (".runtime/", "screen_shots/", "node_modules/", "__pycache__/"):
            self.assertIn(entry, ignore)

    def test_nothing_runtime_shaped_is_committed(self):
        tracked = subprocess.run(
            ["git", "-C", REPO_ROOT, "ls-files"],
            capture_output=True, text=True).stdout.split()
        if not tracked:  # pragma: no cover - only outside a git checkout
            self.skipTest("not a git checkout")
        for path in tracked:
            self.assertFalse(path.startswith(".runtime/"), path)
            self.assertFalse(path.startswith("screen_shots/"), path)
            self.assertFalse(path.startswith("hostim/") and
                             ("secrets" in os.path.basename(path)
                              or path.endswith(".pass") or path.endswith(".log")),
                             "a runtime secret or log is tracked: %s" % path)


class TestGuide(unittest.TestCase):
    def setUp(self):
        if not os.path.isfile(GUIDE):
            self.skipTest("hostim/GUIDE.fa.md is not written yet")
        self.body = read(GUIDE)

    def test_guide_names_the_stable_colab_branch(self):
        self.assertIn("colab-stable", self.body,
                      "the Colab instructions must name the stable branch")

    def test_guide_documents_the_cell_by_cell_colab_path(self):
        self.assertRegex(self.body, r"سلول\s*۱|Cell 1")
        for marker in ("install.sh", "start_colab_browser.sh"):
            self.assertIn(marker, self.body)

    def test_guide_states_what_is_unverified(self):
        self.assertIn("تأییدنشده", self.body)

    def test_guide_does_not_send_the_reader_to_a_tunnel(self):
        self.assertNotIn("cloudflared tunnel --url", self.body)


class TestSecretPrecedenceIsReal(unittest.TestCase):
    """Runs the real load_or_create_secrets in bash and checks what it produces.

    Static assertions cannot catch this class of bug: the previous version of
    the function contained the text "AUTOMATION_TOKEN=${AUTOMATION_TOKEN:-}" and
    still let the secrets file overwrite an env var the platform had injected,
    because `source "$SECRETS_FILE"` assigns those very same names. So the
    function is extracted verbatim from the entrypoint and executed.
    """

    def setUp(self):
        self.bash = shutil.which("bash")
        if not self.bash:
            self.skipTest("bash is not available")
        if not shutil.which("openssl"):
            self.skipTest("openssl is not available")
        if not shutil.which("sed"):
            self.skipTest("sed is not available")
        self.tmp = tempfile.mkdtemp(prefix="hostim-secrets-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.secrets = os.path.join(self.tmp, "secrets.env")
        self.vncpass = os.path.join(self.tmp, "vnc.pass")
        self.function = subprocess.run(
            ["sed", "-n", "/^load_or_create_secrets()/,/^}/p", ENTRYPOINT],
            capture_output=True, text=True).stdout
        self.assertIn("openssl rand", self.function,
                      "could not extract load_or_create_secrets from the entrypoint")

    def run_it(self, env=None):
        """Execute the real function with stubs for log/x11vnc; return state."""
        script = "\n".join([
            "set -uo pipefail",
            # Mirror the real log() but send it to stderr, so the function's own
            # logging can be inspected for secret leaks while stdout stays free
            # for the values this harness prints on purpose.
            'log() { printf \'LOG: %s\\n\' "$*" >&2; }',
            "x11vnc() { :; }",           # no real VNC binary in a test run
            'SECRETS_FILE="%s"' % self.secrets,
            'VNC_PASS_FILE="%s"' % self.vncpass,
            self.function,
            "load_or_create_secrets",
            'printf "%s\\n%s\\n" "$AUTOMATION_TOKEN" "$VNC_PASSWORD"',
        ])
        environment = dict(os.environ)
        environment.pop("AUTOMATION_TOKEN", None)
        environment.pop("VNC_PASSWORD", None)
        environment.update(env or {})
        result = subprocess.run([self.bash, "-c", script], capture_output=True,
                                text=True, env=environment)
        self.assertEqual(result.returncode, 0,
                         "load_or_create_secrets failed: %s" % result.stderr)
        token, password = result.stdout.strip().split("\n")
        stored = {}
        if os.path.exists(self.secrets):
            for line in read(self.secrets).splitlines():
                if "=" in line:
                    key, value = line.split("=", 1)
                    stored[key] = value
        return token, password, stored, result.stderr

    def test_first_boot_generates_and_locks_down_the_file(self):
        token, password, stored, _ = self.run_it()
        self.assertRegex(token, r"^[0-9a-f]{16}$")
        self.assertRegex(password, r"^[0-9a-f]{8}$")
        self.assertEqual(stored, {"AUTOMATION_TOKEN": token, "VNC_PASSWORD": password})
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets).st_mode), 0o600)

    def test_second_boot_reuses_the_file_unchanged(self):
        first = self.run_it()[:2]
        before = read(self.secrets)
        second = self.run_it()[:2]
        self.assertEqual(first, second, "secrets were regenerated on restart")
        self.assertEqual(read(self.secrets), before, "the file was rewritten for no reason")

    def test_env_wins_over_an_existing_file(self):
        self.run_it()
        stored_token = read(self.secrets).split("AUTOMATION_TOKEN=")[1].split("\n")[0]
        token, password, stored, _ = self.run_it(
            {"VNC_PASSWORD": "rotatedpw"})
        self.assertEqual(password, "rotatedpw",
                         "the secrets file overwrote the platform-provided VNC_PASSWORD")
        self.assertEqual(token, stored_token,
                         "the token must stay the one already on the volume")
        self.assertEqual(stored["VNC_PASSWORD"], "rotatedpw",
                         "the file was not updated to the password actually in use")

    def test_env_can_rotate_both_secrets_at_once(self):
        self.run_it()
        token, password, stored, _ = self.run_it(
            {"AUTOMATION_TOKEN": "a" * 16, "VNC_PASSWORD": "b" * 8})
        self.assertEqual((token, password), ("a" * 16, "b" * 8))
        self.assertEqual(stored, {"AUTOMATION_TOKEN": "a" * 16, "VNC_PASSWORD": "b" * 8})

    def test_env_alone_on_a_fresh_volume_is_kept_and_persisted(self):
        token, password, stored, _ = self.run_it({"AUTOMATION_TOKEN": "c" * 16})
        self.assertEqual(token, "c" * 16)
        self.assertRegex(password, r"^[0-9a-f]{8}$", "the password should still be generated")
        self.assertEqual(stored["AUTOMATION_TOKEN"], "c" * 16)

    def test_the_function_logs_the_path_but_never_the_values(self):
        token, password, _, logged = self.run_it()
        self.assertIn("LOG: secrets:", logged, "the function did not log at all")
        self.assertIn(self.secrets, logged, "the log should say where the file is")
        self.assertNotIn(password, logged, "the VNC password reached the container log")
        self.assertNotIn(token, logged, "the automation token reached the container log")
        self.assertIn("hostim exec", logged,
                      "the log should tell the operator how to read the secrets")

    def test_a_rotated_password_reaches_x11vnc_and_the_file(self):
        # x11vnc -storepasswd is what makes the password real; prove the value
        # handed to it is the effective one, not the stale file value.
        script = "\n".join([
            "set -uo pipefail",
            "log() { :; }",
            'x11vnc() { printf "X11VNC %s\\n" "$*" >&2; }',
            'SECRETS_FILE="%s"' % self.secrets,
            'VNC_PASS_FILE="%s"' % self.vncpass,
            self.function,
            "load_or_create_secrets",
        ])
        environment = dict(os.environ)
        environment.pop("AUTOMATION_TOKEN", None)
        environment.pop("VNC_PASSWORD", None)
        subprocess.run([self.bash, "-c", script], capture_output=True,
                       text=True, env=environment)
        environment["VNC_PASSWORD"] = "rotated99"
        result = subprocess.run([self.bash, "-c", script], capture_output=True,
                                text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-storepasswd rotated99', result.stderr,
                      "x11vnc was not given the rotated password")
        self.assertIn("VNC_PASSWORD=rotated99", read(self.secrets))


class TestDesktopUsability(unittest.TestCase):
    """Persian input and NumLock, both reported missing on the real deployment.

    Chrome drew Persian as empty boxes (no Arabic-script font in the image),
    there was no "fa" X layout to type it with (noVNC forwards raw keys, so the
    X layout decides the character, not the operator's own OS layout), and a
    fresh Xvfb session starts with NumLock off, which turns the keypad into
    arrow keys.
    """

    def setUp(self):
        self.dockerfile = read(DOCKERFILE)
        self.mirror = read(ROOT_DOCKERFILE)
        self.entrypoint = read(ENTRYPOINT)
        start = self.dockerfile.find("# Persian (and any other non-Latin)")
        end = self.dockerfile.find("# Google Chrome")
        self.assertTrue(0 < start < end, "the font layer could not be located")
        self.font_layer = self.dockerfile[start:end]
        start = self.entrypoint.find("configure_keyboard() {")
        end = self.entrypoint.find("start_chrome() {")
        self.assertTrue(0 < start < end, "configure_keyboard could not be located")
        self.keyboard = self.entrypoint[start:end]

    def test_image_installs_the_keyboard_and_numlock_tools(self):
        for package in ("x11-xkb-utils", "numlockx", "fontconfig"):
            self.assertRegex(self.dockerfile,
                             r"(?m)^\s*%s \\\s*$" % re.escape(package),
                             "%s is missing from the apt list" % package)

    def test_persian_font_coverage_is_verified_instead_of_assumed(self):
        # Ubuntu 24.04 has no dedicated Persian font package, so the layer must
        # prove the result rather than trust a package name.
        self.assertIn("fc-list ':lang=fa'", self.font_layer)
        self.assertIn("has_persian", self.font_layer)
        self.assertIn("exit 1", self.font_layer,
                      "a missing Persian font must fail the build, not ship boxes")
        self.assertIn("no Persian-capable font", self.font_layer)

    def test_font_install_does_not_bet_on_a_single_package_name(self):
        for candidate in ("fonts-noto-core", "fonts-freefont-ttf", "fonts-kacst"):
            self.assertIn(candidate, self.font_layer)
        # A candidate that disappears from the archive must not break the build.
        self.assertIn('|| echo "SKIP:', self.font_layer)
        # And the loop must stop as soon as coverage exists, so the image does
        # not grow by installing every candidate.
        self.assertIn("break", self.font_layer)

    def test_entrypoint_offers_a_persian_layout_with_a_familiar_toggle(self):
        self.assertIn("XKB_LAYOUTS=${XKB_LAYOUTS:-us,fa}", self.entrypoint)
        self.assertIn("XKB_OPTIONS=${XKB_OPTIONS:-grp:alt_shift_toggle}", self.entrypoint)
        self.assertIn("setxkbmap", self.keyboard)

    def test_entrypoint_turns_numlock_on_by_default(self):
        self.assertIn("NUMLOCK=${NUMLOCK:-on}", self.entrypoint)
        self.assertIn("numlockx on", self.keyboard)

    def test_every_keyboard_knob_is_overridable_without_a_rebuild(self):
        for knob in ("XKB_MODEL", "XKB_LAYOUTS", "XKB_OPTIONS", "NUMLOCK"):
            self.assertRegex(self.entrypoint, r"(?m)^%s=\$\{%s:-" % (knob, knob),
                             "%s cannot be overridden by an env var" % knob)

    def test_keyboard_setup_runs_after_x_is_up_and_before_chrome(self):
        main = self.entrypoint[self.entrypoint.find("main() {"):]
        self.assertLess(main.find("start_xvfb"), main.find("configure_keyboard"))
        self.assertLess(main.find("configure_keyboard"), main.find("start_chrome"))

    def test_keyboard_setup_cannot_stop_the_container(self):
        # A missing convenience must never take the desktop down with it.
        self.assertNotIn("die ", self.keyboard)
        self.assertIn("command -v setxkbmap", self.keyboard)
        self.assertIn("command -v numlockx", self.keyboard)
        self.assertIn("WARN", self.keyboard)

    def test_the_root_mirror_carries_the_same_layers(self):
        for marker in ("fc-list ':lang=fa'", "numlockx", "x11-xkb-utils"):
            self.assertIn(marker, self.mirror,
                          "the root mirror is stale: %s is missing" % marker)

    def test_the_unicode_type_path_is_documented_in_the_shared_script(self):
        script = read(os.path.join(REPO_ROOT, "browser_control.sh"))
        self.assertIn("*[![:ascii:]]*", script,
                      "browser_control.sh must route non-ASCII text to the clipboard")
        self.assertIn("xclip -selection clipboard", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)
