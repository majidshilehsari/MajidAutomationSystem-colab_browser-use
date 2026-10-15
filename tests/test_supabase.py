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
from automation.agent_store import AgentStore
from automation.supabase import (SupabaseError, SupabaseSync, inject_settings,
                                 mask_key, parse_connection, restore_on_boot)

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
                     lambda: self.api.route_supabase_restore(body=b"{}"),
                     lambda: self.api.route_supabase_connect(body=b"{}"),
                     lambda: self.api.route_supabase_history()):
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


def make_jwt(claims):
    import base64

    def part(obj):
        raw = base64.urlsafe_b64encode(json.dumps(obj).encode()).decode()
        return raw.rstrip("=")
    return "%s.%s.%s" % (part({"alg": "HS256"}), part(claims), part({"sig": 1}))


def make_empty_dir():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "automation"), exist_ok=True)
    os.makedirs(os.path.join(tmp, "flows"), exist_ok=True)
    return tmp


class FakeAgent:
    """Minimal stand-in: the connect route needs .store and .save_settings."""

    def __init__(self, store):
        self.store = store

    def save_settings(self, data, actor="operator"):
        for key, value in data.items():
            secret = key in SECRET_SETTINGS
            if secret and value in ("", None):
                self.store.delete_setting(key, actor=actor)
            else:
                self.store.set_setting(key, value, secret=secret, actor=actor)
        return self.store.list_settings()


class ParseConnectionTests(unittest.TestCase):
    def test_jwt_carries_its_own_url(self):
        jwt = make_jwt({"role": "service_role",
                        "iss": "https://refxyz.supabase.co/auth/v1"})
        self.assertEqual(parse_connection(jwt),
                         {"url": "https://refxyz.supabase.co", "key": jwt})

    def test_jwt_ref_claim_fallback(self):
        jwt = make_jwt({"role": "service_role", "ref": "refabc"})
        self.assertEqual(parse_connection(jwt)["url"], "https://refabc.supabase.co")

    def test_url_and_key_in_one_blob_either_order(self):
        a = parse_connection("https://x.supabase.co sb_secret_abc")
        b = parse_connection("sb_secret_abc,\nhttps://x.supabase.co/")
        self.assertEqual(a, {"url": "https://x.supabase.co", "key": "sb_secret_abc"})
        self.assertEqual(a, b)

    def test_key_without_url_is_a_clear_400(self):
        for bad in ("", "   ", "sb_secret_abc", "https://x.supabase.co"):
            with self.assertRaises(SupabaseError) as ctx:
                parse_connection(bad)
            self.assertEqual(ctx.exception.status, 400)
        with self.assertRaises(SupabaseError) as ctx:
            parse_connection("sb_secret_abc")
        self.assertIn("نشانی", str(ctx.exception))


class InjectSettingsTests(unittest.TestCase):
    def test_creates_table_upserts_and_flags_secrets(self):
        tmp = tempfile.mkdtemp()
        try:
            db = os.path.join(tmp, "agent.db")
            inject_settings(db, {"supabase.url": "https://x.supabase.co",
                                 "supabase.key": ("sb-secret-x", True)})
            inject_settings(db, {"supabase.key": ("sb-secret-y", True)})
            conn = sqlite3.connect(db)
            rows = dict(conn.execute(
                "SELECT key, value || ':' || is_secret FROM settings").fetchall())
            conn.close()
            self.assertEqual(rows["supabase.url"], "https://x.supabase.co:0")
            self.assertEqual(rows["supabase.key"], "sb-secret-y:1")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_restore_clears_stale_wal_files(self):
        src = make_data_dir()
        dst = make_empty_dir()
        try:
            archive = SupabaseSync(src).build_archive()
            db = os.path.join(dst, "automation", "agent.db")
            for suffix in ("", "-wal", "-shm"):
                with open(db + suffix, "w", encoding="utf-8"):
                    pass
            SupabaseSync(dst).restore(archive)
            # a stale WAL replayed onto the fresh db could corrupt it
            self.assertFalse(os.path.exists(db + "-wal"))
            self.assertFalse(os.path.exists(db + "-shm"))
            self.assertGreater(os.path.getsize(db), 0)
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)

    def test_restore_injects_credentials_into_the_staged_db(self):
        src = make_data_dir()
        dst = make_empty_dir()
        try:
            archive = SupabaseSync(src).build_archive()
            count = SupabaseSync(dst).restore(
                archive, inject={"supabase.key": ("sb-new", True),
                                 "supabase.enabled": "1"})
            self.assertGreater(count, 0)
            conn = sqlite3.connect(os.path.join(dst, "automation", "agent.db"))
            row = conn.execute("SELECT value, is_secret FROM settings"
                               " WHERE key = 'supabase.key'").fetchone()
            conn.close()
            self.assertEqual(row, ("sb-new", 1))
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)


class ConnectRouteTests(unittest.TestCase):
    CONN = "https://proj.supabase.co sb-secret-0123456789abcdef"

    def setUp(self):
        self.empty = make_empty_dir()
        self.store = AgentStore(self.empty)
        self.sync = SupabaseSync(self.empty)
        self.sync.start = lambda: None  # no background thread in tests
        self.api = AutomationApi(None, FlowStore(self.empty), data_dir=self.empty,
                                 token="tok", supabase=self.sync,
                                 agent=FakeAgent(self.store))

    def tearDown(self):
        shutil.rmtree(self.empty, ignore_errors=True)

    def _archive_from_a_populated_dir(self):
        src = make_data_dir()
        try:
            return SupabaseSync(src).build_archive()
        finally:
            shutil.rmtree(src, ignore_errors=True)

    def test_connect_on_empty_data_restores_and_restarts(self):
        archive = self._archive_from_a_populated_dir()
        fake = FakeUrlopen({
            ("GET", "/storage/v1/bucket/"): FakeResponse(b"{}"),
            ("GET", "snapshot-latest"): FakeResponse(archive),
        })
        body = json.dumps({"connection": self.CONN}).encode()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("automation.api._restart_later") as restart:
            status, _headers, raw = self.api.route_supabase_connect(body=body)
        self.assertEqual(status, 200)
        data = json.loads(raw.decode())
        self.assertTrue(data["restarting"])
        self.assertGreater(data["restored"], 0)
        restart.assert_called_once()
        # the credentials were stored…
        self.assertEqual(self.store.get_setting("supabase.url"),
                         "https://proj.supabase.co")
        # …and injected into the restored db so they survive the restart
        conn = sqlite3.connect(os.path.join(self.empty, "automation", "agent.db"))
        row = conn.execute("SELECT value, is_secret FROM settings"
                           " WHERE key = 'supabase.key'").fetchone()
        conn.close()
        self.assertEqual(row, ("sb-secret-0123456789abcdef", 1))
        # the flow library came back too
        self.assertTrue(os.path.exists(os.path.join(self.empty, "flows", "google.json")))

    def test_connect_never_touches_a_machine_that_has_data(self):
        populated = make_data_dir()
        shutil.rmtree(self.empty, ignore_errors=True)
        self.empty = populated
        store = AgentStore(populated)
        sync = SupabaseSync(populated)
        started = []
        sync.start = lambda: started.append(True)
        api = AutomationApi(None, FlowStore(populated), data_dir=populated,
                            token="tok", supabase=sync, agent=FakeAgent(store))
        fake = FakeUrlopen({("GET", "/storage/v1/bucket/"): FakeResponse(b"{}")})
        body = json.dumps({"connection": self.CONN}).encode()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("automation.api._restart_later") as restart:
            status, _headers, raw = api.route_supabase_connect(body=body)
        try:
            data = json.loads(raw.decode())
            self.assertEqual(status, 200)
            self.assertFalse(data["restarting"])
            self.assertEqual(data["restored"], 0)
            restart.assert_not_called()
            self.assertTrue(started)  # the periodic sync took over instead
            self.assertTrue(os.path.exists(os.path.join(populated, "flows", "google.json")))
            self.assertEqual(store.get_setting("supabase.bucket"), "mas-snapshots")
            # no snapshot download was ever attempted
            self.assertFalse([c for c in fake.calls if "snapshot-latest" in c["url"]])
        finally:
            shutil.rmtree(populated, ignore_errors=True)

    def test_wrong_key_is_refused_before_anything_is_stored(self):
        fake = FakeUrlopen({("GET", "/storage/v1/bucket/"): 401})
        body = json.dumps({"connection": self.CONN}).encode()
        with mock.patch("urllib.request.urlopen", fake):
            with self.assertRaises(ApiError) as ctx:
                self.api.route_supabase_connect(body=body)
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("کلید", str(ctx.exception))
        self.assertIsNone(self.store.get_setting("supabase.key"))

    def test_connect_without_agent_is_409(self):
        api = AutomationApi(None, FlowStore(self.empty), data_dir=self.empty,
                            token="tok", supabase=SupabaseSync(self.empty))
        body = json.dumps({"connection": self.CONN}).encode()
        with self.assertRaises(ApiError) as ctx:
            api.route_supabase_connect(body=body)
        self.assertEqual(ctx.exception.status, 409)

    def test_bad_connection_text_is_a_400(self):
        body = json.dumps({"connection": "sb_secret_only"}).encode()
        with self.assertRaises(ApiError) as ctx:
            self.api.route_supabase_connect(body=body)
        self.assertEqual(ctx.exception.status, 400)

    def test_first_ever_connect_without_a_snapshot_just_configures(self):
        fake = FakeUrlopen({("GET", "/storage/v1/bucket/"): FakeResponse(b"{}")})
        body = json.dumps({"connection": self.CONN}).encode()
        with mock.patch("urllib.request.urlopen", fake), \
                mock.patch("automation.api._restart_later") as restart:
            status, _headers, raw = self.api.route_supabase_connect(body=body)
        data = json.loads(raw.decode())
        self.assertEqual(status, 200)
        self.assertFalse(data["restarting"])
        self.assertEqual(data["restored"], 0)
        restart.assert_not_called()
        self.assertEqual(self.store.get_setting("supabase.url"),
                         "https://proj.supabase.co")

    def test_status_reports_whether_local_data_is_empty(self):
        with mock.patch.dict(os.environ, ENV):
            _status, _headers, raw = self.api.route_supabase_status()
        self.assertTrue(json.loads(raw.decode())["dataEmpty"])
        populated = make_data_dir()
        try:
            api2 = AutomationApi(None, FlowStore(populated), data_dir=populated,
                                 token="tok", supabase=SupabaseSync(populated),
                                 agent=FakeAgent(AgentStore(populated)))
            with mock.patch.dict(os.environ, ENV):
                _status, _headers, raw2 = api2.route_supabase_status()
            self.assertFalse(json.loads(raw2.decode())["dataEmpty"])
        finally:
            shutil.rmtree(populated, ignore_errors=True)


class HistoryTests(unittest.TestCase):
    """The operator can list the kept versions and restore any of them."""

    def setUp(self):
        self.tmp = make_data_dir()
        self.sync = SupabaseSync(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_history_maps_names_and_filters_junk(self):
        rows = json.dumps([
            {"name": "snapshot-20261015-101010.tar.gz", "size": 20480,
             "created_at": "2026-10-15T10:10:10Z"},
            {"name": "unexpected.txt", "size": 5, "created_at": ""},
            {"name": "snapshot-../evil.tar.gz", "size": 5, "created_at": ""},
            "not-even-a-dict",
        ]).encode()
        fake = FakeUrlopen({("POST", "/object/list/"): FakeResponse(rows)})
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake):
            history = self.sync.list_history(self.sync.config())
        self.assertEqual([row["name"] for row in history],
                         ["history/snapshot-20261015-101010.tar.gz"])
        self.assertEqual(history[0]["size"], 20480)
        self.assertEqual(fake.calls[0]["method"], "POST")
        self.assertIn(b'"prefix": "history/"', fake.calls[0]["data"])

    def test_list_history_survives_a_missing_bucket(self):
        fake = FakeUrlopen({("POST", "/object/list/"): 404})
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake):
            self.assertEqual(self.sync.list_history(self.sync.config()), [])

    def test_download_refuses_anything_that_is_not_a_snapshot_name(self):
        with mock.patch.dict(os.environ, ENV):
            cfg = self.sync.config()
            for bad in ("../x.tar.gz", "history/x/../../y.tar.gz",
                        "snapshot-latest.tar.gz.bak", "latest.tar.gz",
                        "history/snapshot-a b.tar.gz", "history/snapshot-"):
                with self.assertRaises(SupabaseError) as ctx:
                    self.sync.download(cfg, bad)
                self.assertEqual(ctx.exception.status, 400)


class HistoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = make_data_dir()
        self.sync = SupabaseSync(self.tmp)
        self.api = AutomationApi(None, FlowStore(self.tmp), data_dir=self.tmp,
                                 token="tok", supabase=self.sync)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_history_needs_configuration(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("SUPABASE_")}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ApiError) as ctx:
                self.api.route_supabase_history()
        self.assertEqual(ctx.exception.status, 409)

    def test_restore_by_name_fetches_exactly_that_version(self):
        src = make_data_dir()
        try:
            archive = SupabaseSync(src).build_archive()
        finally:
            shutil.rmtree(src, ignore_errors=True)
        name = "history/snapshot-20261015-101010.tar.gz"
        fake = FakeUrlopen({
            ("GET", name): FakeResponse(archive),
            ("POST", "/object/list/"): FakeResponse(json.dumps([
                {"name": name.split("/", 1)[1], "size": len(archive),
                 "created_at": "2026-10-15T10:10:10Z"}]).encode()),
        })
        with mock.patch.dict(os.environ, ENV), \
                mock.patch("urllib.request.urlopen", fake), \
                mock.patch("automation.api._restart_later") as restart:
            status, _headers, raw = self.api.route_supabase_history()
            self.assertEqual(status, 200)
            listed = json.loads(raw.decode())["history"]
            self.assertEqual(listed[0]["name"], name)
            status, _headers, raw = self.api.route_supabase_restore(
                body=json.dumps({"name": name}).encode())
        self.assertEqual(status, 200)
        payload = json.loads(raw.decode())
        self.assertTrue(payload["restarting"])
        self.assertGreater(payload["restored"], 0)
        restart.assert_called_once()
        self.assertTrue(any(name in call["url"] and call["method"] == "GET"
                            for call in fake.calls),
                        "the chosen version was never downloaded")

    def test_restore_rejects_a_hostile_name(self):
        with mock.patch.dict(os.environ, ENV):
            with self.assertRaises(ApiError) as ctx:
                self.api.route_supabase_restore(body=b'{"name": "../evil"}')
        self.assertEqual(ctx.exception.status, 400)
