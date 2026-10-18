"""SecretVault unit tests: policy ceilings, scope checks, SSRF floor, masking."""

import json
import unittest

from automation.vault import SecretVault, VaultError, _normalise_policy


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


BASE_POLICY = {
    "headerTemplate": "Bearer {{secret}}",
    "allowedHosts": ["api.example.dev"],
    "allowedPaths": ["/v1"],
    "methods": ["GET"],
}


class SecretVaultTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.vault = SecretVault(clock=self.clock)

    def create(self, secret="s3cret", policy=None):
        """create() answers masked status; the handle is its fullHandle."""
        return self.vault.create(secret, policy or dict(BASE_POLICY))["fullHandle"]

    def test_policy_requires_placeholder_and_hosts(self):
        with self.assertRaises(VaultError):
            # a template without the placeholder would send no credential
            _normalise_policy({"allowedHosts": ["a.dev"], "headerTemplate": "X-Key: 123"})
        with self.assertRaises(VaultError):
            _normalise_policy({"headerTemplate": "Bearer {{secret}}"})
        with self.assertRaises(VaultError):
            _normalise_policy(
                {"allowedHosts": ["10.1.2.3"], "headerTemplate": "{{secret}}"})

    def test_ttl_and_uses_are_clamped_to_safe_ceilings(self):
        policy = _normalise_policy(dict(
            BASE_POLICY, ttlSeconds=10 ** 9, maxUses=10 ** 9))
        self.assertEqual(policy["ttlSeconds"], 43200)
        self.assertEqual(policy["maxUses"], 1000)

    def test_acquire_enforces_scope_and_counts_uses(self):
        handle = self.create(policy=dict(BASE_POLICY, maxUses=1))
        value, policy = self.vault.acquire(handle, "GET", "https://api.example.dev/v1/x")
        self.assertEqual(value, "s3cret")
        self.assertEqual(policy["maxUses"], 1)
        with self.assertRaises(VaultError) as ctx:
            self.vault.acquire(handle, "GET", "https://api.example.dev/v1/x")
        self.assertEqual(ctx.exception.status, 410)

    def test_wrong_host_path_and_method_are_rejected(self):
        handle = self.create()
        cases = (("GET", "https://other.dev/v1"),
                 ("GET", "https://api.example.dev/v2"),
                 ("POST", "https://api.example.dev/v1"))
        for method, url in cases:
            with self.assertRaises(VaultError, msg=url):
                self.vault.acquire(handle, method, url)

    def test_userinfo_in_url_is_rejected(self):
        handle = self.create(policy=dict(BASE_POLICY, allowedPaths=["/"]))
        with self.assertRaises(VaultError):
            self.vault.acquire(handle, "GET", "https://user:pw@api.example.dev/")

    def test_host_resolving_to_loopback_is_refused(self):
        # The DNS-rebinding floor: even with 'localhost' inside the policy,
        # acquire refuses a name that resolves to a private address.
        handle = self.create(policy=dict(BASE_POLICY, allowedHosts=["localhost"]))
        with self.assertRaises(VaultError):
            self.vault.acquire(handle, "GET", "http://localhost/v1")

    def test_expiry_and_revocation(self):
        handle = self.create(policy=dict(BASE_POLICY, ttlSeconds=60))
        self.clock.now += 61
        with self.assertRaises(VaultError) as ctx:
            self.vault.acquire(handle, "GET", "https://api.example.dev/v1")
        self.assertEqual(ctx.exception.status, 410)
        other = self.create(secret="v2")
        self.vault.revoke(other)
        with self.assertRaises(VaultError) as ctx:
            self.vault.acquire(other, "GET", "https://api.example.dev/v1")
        self.assertEqual(ctx.exception.status, 410)
        # revoke wipes the value, so nothing survives in memory either
        self.assertEqual(self.vault._items[other]["value"], "")

    def test_read_only_policy_refuses_write_methods(self):
        handle = self.create(policy=dict(
            BASE_POLICY, methods=["GET", "PUT"], readOnly=True))
        with self.assertRaises(VaultError):
            self.vault.acquire(handle, "PUT", "https://api.example.dev/v1")
        self.vault.acquire(handle, "GET", "https://api.example.dev/v1")

    def test_status_never_contains_the_value_and_masks_the_handle(self):
        handle = self.create(secret="topsecret")
        status = self.vault.status(handle)
        # fullHandle echoes what the caller already put in the URL; the masked
        # 'handle' field is the one that reaches logs and reports.
        public = dict(status)
        public.pop("fullHandle")
        text = json.dumps(public, ensure_ascii=False, default=str)
        self.assertNotIn("topsecret", text)
        self.assertNotIn(handle, text)
        self.assertTrue(status["handle"].endswith("…"))
        self.assertEqual(status["status"], "ready")
        self.assertIn("digest", status)
        self.assertEqual(len(status["digest"]), 12)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
