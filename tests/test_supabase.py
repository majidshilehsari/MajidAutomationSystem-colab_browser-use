"""Round-15 Supabase snapshot sync tests.

Everything runs against a fake ``urllib.request.urlopen``: no network, no
Supabase account.  The security-relevant promises under test:

* secrets.env / vnc.pass never enter an archive;
* a hostile archive member (``../``) can never escape the data dir;
* the key is masked in every status output;
* the app boots and syncs are refused cleanly when nothing is configured.
"""

import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
import urllib.error
from unittest import mock

from automation.api import ApiError, AutomationApi, FlowStore
from automation.agent import PLAIN_SETTINGS, SECRET_SETTINGS
from automation.supabase import (SupabaseError, SupabaseSync, mask_key,
                                 restore_on_boot)

ENV = {
    "SUPABASE_URL": "https://proj.supabase.co",
    "SUPABASE_KEY": "sb-secret-0123456789abcdef",
}


class FakeResponse:
    def __init__(self, body=b"", status=200):
        self._body = body
        self.status = status
        self.headers = {}

    def read(self, size=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeUrlopen:
    """(method, url-fragment) -> response | HTTP status code to raise."""

    def __init__(self, routes=None):
        self.calls = []
        self.routes = dict(routes or {})

    def __call__(self, request, timeout=0):
        url = request.full_url
        method = request.get_method()
        self.calls.append({
            "method": method, "url": url, "data": request.data,
            "headers": dict(request.headers), "timeout": timeout,
        })
        for (route_method, fragment), response in self.routes.items():
            if route_method == method and fragment in url:
                if isinstance(response, int):
                    raise urllib.error.HTTPError(url, response, "err", {}, None)
                return response() if callable(response) else response
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)


def make_data_dir(with_db=True):
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "automation"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "flows"), exist_ok=True)
    if with_db:
        conn = sqlite3.connect(os.path.join(tmp, "automation", "agent.db"))
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT)")
        conn.execute("INSERT INTO notes (body) VALUES ('سلام')")
        conn.commit()
        conn.close()
    with open(os.path.join(tmp, "flows", "google.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": "google", "steps": []}, fh)
    with open(os.path.join(tmp, "flows", "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("not a flow")
    with open(os.path.join(tmp, "secrets.env"), "w", encoding="utf-8") as fh:
        fh.write("AUTOMATION_TOKEN=nope")
    return tmp


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_data_dir()
        self.sync = SupabaseSync(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def names(self, archive):
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            return sorted(tar.getnames())

    def test_archive_holds_db_flows_and_meta_only(self):
        names = self.names(self.sync.build_archive())
        self.assertIn(os.path.join("automation", "agent.db"), names)
        self.assertIn("flows/google.json", names)
        self.assertIn("meta.json", names)
        self.assertNotIn("flows/notes.txt", names)      # json only
        self.assertNotIn("secrets.env", names)          # never archived
        self.assertFalse(any("vnc.pass" in n for n in names))

    def test_extract_safe_refuses_path_traversal(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = b"evil"
            info = tarfile.TarInfo("../evil.txt")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with self.assertRaises(SupabaseError):
            SupabaseSync.extract_safe(buf.getvalue(), self.tmp)

    def test_restore_round_trip_into_a_fresh_dir(self):
        archive = self.sync.build_archive()
        fresh = tempfile.mkdtemp()
        try:
            other = SupabaseSync(fresh)
            count = other.restore(archive)
            self.assertGreaterEqual(count, 3)
            self.assertTrue(os.path.exists(os.path.join(fresh, "automation", "agent.db")))
            conn = sqlite3.connect(os.path.join(fresh, "automation", "agent.db"))
            row = conn.execute("SELECT body FROM notes").fetchone()
            conn.close()
            self.assertEqual(row[0], "سلام")
            with open(os.path.join(fresh, "flows", "google.json"),
                      encoding="utf-8") as fh:
                self.assertEqual(json.load(fh)["name"], "google")
        finally:
            shutil.rmtree(fresh, ignore_errors=True)


class SyncCycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_data_dir()
        self.sync = SupabaseSync(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sync_now_uploads_latest_plus_history_and_skips_unchanged(self):
        fake = FakeUrlopen({
            ("GET", "/storage/v1/bucket/mas-snapshots"): FakeResponse(b"{}"),
            ("POST", "/storage/v1/object/mas-snapshots/snapshot-latest"): FakeResponse(b"{}"),
            ("POST", "/storage/v1/object/mas-snapshots/history/"): FakeResponse(b"{}"),
            ("POST", "/storage/v1/object/list/mas-snapshots"): FakeResponse(b"[]"),
        })
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake):
            result = self.sync.sync_now()
            self.assertIn("uploaded", result)
            again = self.sync.sync_now()
            self.assertIn("skipped", again)
            forced = self.sync.sync_now(force=True)
            self.assertIn("uploaded", forced)
            status = self.sync.status()
            self.assertEqual(status["uploads"], 2)
            self.assertNotIn(ENV["SUPABASE_KEY"], json.dumps(status))
            self.assertIn("…", status["key"])
        methods = [(c["method"], c["url"]) for c in fake.calls]
        self.assertTrue(any(m == "POST" and "snapshot-latest" in u for m, u in methods))
        self.assertTrue(any(m == "POST" and "/history/" in u for m, u in methods))
        upload = next(c for c in fake.calls
                      if c["method"] == "POST" and "snapshot-latest" in c["url"])
        self.assertEqual(upload["headers"].get("Authorization"),
                         "Bearer " + ENV["SUPABASE_KEY"])
        self.assertEqual(upload["headers"].get("X-upsert"), "true")

    def test_sync_refused_when_not_configured(self):
        with self.assertRaises(SupabaseError) as ctx:
            self.sync.sync_now()
        self.assertEqual(ctx.exception.status, 409)

    def test_disabled_by_env_switch(self):
        with mock.patch.dict(os.environ, dict(ENV, SUPABASE_SYNC="off")):
            self.assertFalse(self.sync.config()["enabled"])

    def test_history_pruning_keeps_five(self):
        listing = json.dumps([{"name": "snapshot-%02d.tar.gz" % i}
                              for i in range(8)]).encode()
        fake = FakeUrlopen({
            ("GET", "/storage/v1/bucket/mas-snapshots"): FakeResponse(b"{}"),
            ("POST", "/storage/v1/object/mas-snapshots/"): FakeResponse(b"{}"),
            ("POST", "/storage/v1/object/list/mas-snapshots"): FakeResponse(listing),
            ("DELETE", "/storage/v1/object/mas-snapshots"): FakeResponse(b"{}"),
        })
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake):
            self.sync.sync_now(force=True)
        deletes = [c for c in fake.calls if c["method"] == "DELETE"]
        self.assertEqual(len(deletes), 1)
        prefixes = json.loads(deletes[0]["data"].decode())["prefixes"]
        self.assertEqual(len(prefixes), 3)  # 8 - KEEP(5)


class BootRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_data_dir(with_db=False)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_env_no_action(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("SUPABASE_")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(restore_on_boot(self.tmp))

    def test_fresh_dir_restores_snapshot(self):
        source = make_data_dir()
        try:
            archive = SupabaseSync(source).build_archive()
        finally:
            shutil.rmtree(source, ignore_errors=True)
        fake = FakeUrlopen({
            ("GET", "/storage/v1/object/mas-snapshots/snapshot-latest"):
                FakeResponse(archive),
        })
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake):
            result = restore_on_boot(self.tmp)
        self.assertEqual(result, {"restored": 3})
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "automation", "agent.db")))

    def test_existing_local_data_is_never_overwritten(self):
        with open(os.path.join(self.tmp, "automation", "agent.db"), "wb") as fh:
            fh.write(b"local-wins")
        fake = FakeUrlopen({})  # any network call would 404 -> error path
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake):
            result = restore_on_boot(self.tmp)
        self.assertEqual(result, {"skipped": "local data present"})
        with open(os.path.join(self.tmp, "automation", "agent.db"), "rb") as fh:
            self.assertEqual(fh.read(), b"local-wins")

    def test_paused_project_or_dead_network_never_blocks_boot(self):
        def boom(*args, **kwargs):
            raise OSError("network down")
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", boom):
            result = restore_on_boot(self.tmp)
        self.assertIn("error", result)  # logged, swallowed, boot continues


class ApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_data_dir()
        self.sync = SupabaseSync(self.tmp)
        self.api = AutomationApi(None, FlowStore(self.tmp), data_dir=self.tmp,
                                 token="tok", supabase=self.sync)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_is_masked_and_sync_needs_configuration(self):
        with mock.patch.dict(os.environ, ENV):
            status, _headers, body = self.api.route_supabase_status()
        self.assertEqual(status, 200)
        text = body.decode()
        self.assertNotIn(ENV["SUPABASE_KEY"], text)
        self.assertIn("…", json.loads(text)["key"])
        bare = AutomationApi(None, FlowStore(self.tmp), data_dir=self.tmp,
                             token="tok", supabase=SupabaseSync(self.tmp))
        env = {k: v for k, v in os.environ.items() if not k.startswith("SUPABASE_")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ApiError) as ctx:
                bare.route_supabase_sync(body=b"{}")
        self.assertEqual(ctx.exception.status, 409)

    def test_restore_without_snapshot_is_404(self):
        fake = FakeUrlopen({})  # every GET 404s -> "no snapshot yet"
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(ApiError) as ctx:
                self.api.route_supabase_restore(body=b"{}")
        self.assertEqual(ctx.exception.status, 404)

    def test_agent_key_is_refused(self):
        self.api._last_verdict = "agent"
        for call in (lambda: self.api.route_supabase_status(),
                     lambda: self.api.route_supabase_sync(body=b"{}"),
                     lambda: self.api.route_supabase_restore(body=b"{}")):
            with self.assertRaises(ApiError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 403)

    def test_settings_accept_the_new_keys(self):
        self.assertIn("supabase.key", SECRET_SETTINGS)
        for key in ("supabase.url", "supabase.bucket", "supabase.enabled",
                    "supabase.everyMin"):
            self.assertIn(key, PLAIN_SETTINGS)

    def test_mask_key_helper(self):
        self.assertEqual(mask_key(""), "")
        self.assertEqual(mask_key("short"), "…")
        masked = mask_key("sb-secret-0123456789abcdef")
        self.assertTrue(masked.startswith("sb-sec") and masked.endswith("cdef"))
        self.assertIn("…", masked)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
