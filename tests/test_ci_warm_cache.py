"""Private cache provisioning and bounded idle maintenance."""

import importlib.util
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[3] / 'scripts/ci-warm-cache.py'
SPEC = importlib.util.spec_from_file_location('ci_warm_cache', SCRIPT)
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


class WarmCache(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'nzfleet' / 'ci-cache'
        self.base.parent.mkdir()
        self.base.mkdir(mode=0o711)
        os.chmod(self.base, 0o711)
        self.owner = os.getuid()
        old_base, old_owner = cache.BASE, cache.BASE_OWNER
        cache.BASE, cache.BASE_OWNER = self.base, self.owner
        self.addCleanup(lambda: setattr(cache, 'BASE', old_base))
        self.addCleanup(lambda: setattr(cache, 'BASE_OWNER', old_owner))
        self.root = cache.exact_root('42')
        self.root.mkdir(mode=0o700)
        os.chmod(self.root, 0o700)

    def test_provisioning_is_idempotent_and_job_owner_can_write(self):
        with patch.object(cache.os, 'geteuid', return_value=0):
            cache.provision(self.root, self.owner)
            cache.provision(self.root, self.owner)
        (self.root / 'job-write').write_text('ready')
        self.assertEqual((self.root / 'job-write').read_text(), 'ready')
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_wrong_owner_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'owner UID'):
            cache.verify(self.root, self.owner + 1)

    def test_world_readable_cache_is_refused(self):
        os.chmod(self.root, 0o755)
        with self.assertRaisesRegex(ValueError, 'mode'):
            cache.verify(self.root, self.owner)

    def test_symlink_cache_root_is_refused(self):
        self.root.rmdir()
        self.root.symlink_to(self.base)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            cache.verify(self.root, self.owner)

    def test_wrong_owner_nonempty_cache_is_preserved(self):
        (self.root / 'valuable').write_text('keep')
        with patch.object(cache.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(ValueError, 'wrong owner and nonempty'):
                cache.provision(self.root, self.owner + 1)
        self.assertEqual((self.root / 'valuable').read_text(), 'keep')

    def test_empty_wrong_owner_changes_only_the_exact_leaf(self):
        with patch.object(cache.os, 'geteuid', return_value=0), \
             patch.object(cache.os, 'chown') as chown, \
             patch.object(cache, 'verify'):
            cache.provision(self.root, self.owner + 1)
        chown.assert_called_once_with(self.root, self.owner + 1, self.owner + 1)

    def test_busy_target_is_preserved_even_over_budget(self):
        import fcntl
        target = self.root / 'target'
        target.mkdir()
        slot = target / 'rust-1'
        slot.mkdir()
        data = slot / 'data'
        data.write_bytes(b'cache data')
        lock = target / 'rust-1.lock'
        lock.touch()
        self.assertGreater(cache.size(self.root), 0)
        with lock.open('r+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(cache, 'TOTAL_LIMIT', 0), self.assertRaisesRegex(ValueError, 'active locks'):
                cache.maintenance(self.root, delete=True)
        self.assertTrue(slot.exists())
        self.assertEqual(data.read_bytes(), b'cache data')

    def test_non_target_data_alone_exceeds_the_whole_root_limit_without_deletion(self):
        for name in ('sccache', 'bin', 'other'):
            path = self.root / name
            path.mkdir()
            (path / 'owned').write_bytes(b'cache data')
        output = io.StringIO()
        with patch.object(cache, 'TOTAL_LIMIT', cache.size(self.root) - 1024), \
             contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, 'non-target data is preserved'):
            cache.maintenance(self.root, delete=True)
        for name in ('sccache', 'bin', 'other'):
            self.assertTrue((self.root / name / 'owned').exists())
        self.assertIn('whole cache:', output.getvalue())
        self.assertIn('sccache:', output.getvalue())
        self.assertIn('bin:', output.getvalue())
        self.assertIn('other:', output.getvalue())

    def test_oldest_idle_slot_is_removed_only_when_total_exceeds_limit(self):
        target = self.root / 'target'
        target.mkdir()
        for area in ('rust', 'e2e'):
            slot = target / f'{area}-1'
            slot.mkdir()
            (slot / 'data').write_text(area)
            (target / f'{area}-1.lock').touch()
        os.utime(target / 'rust-1', ns=(1, 1))
        os.utime(target / 'e2e-1', ns=(2, 2))
        with patch.object(cache, 'TOTAL_LIMIT', cache.size(self.root) - cache.size(target / 'rust-1')):
            cache.maintenance(self.root, delete=True)
        self.assertFalse((target / 'rust-1').exists())
        self.assertTrue((target / 'e2e-1').exists())

    def test_symlink_target_is_preserved(self):
        target = self.root / 'target'
        target.mkdir()
        (target / 'rust-1').symlink_to(self.root)
        (target / 'rust-1.lock').touch()
        with self.assertRaisesRegex(ValueError, 'symlink'):
            cache.maintenance(self.root, delete=True)
        self.assertTrue((target / 'rust-1').is_symlink())

    def test_unknown_repository_id_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'positive decimal'):
            cache.exact_root('../other')

    def test_default_mount_requires_private_rootless_owner(self):
        old_mount = cache.MOUNT
        cache.MOUNT = self.root
        self.addCleanup(lambda: setattr(cache, 'MOUNT', old_mount))
        if self.owner == 0:
            os.chown(self.root, 1, 1)
        with patch.dict(os.environ, {'CI_SELF_HOSTED': 'nzela', 'CI_WARM_OVERRIDE': ''}):
            with self.assertRaisesRegex(ValueError, 'owner UID'):
                cache.mounted()

    def test_root_owned_private_default_mount_is_valid(self):
        if self.owner != 0:
            self.skipTest('requires the pinned root job')
        with patch.object(cache, 'MOUNT', self.root), \
             patch.dict(os.environ, {'CI_SELF_HOSTED': 'nzela', 'CI_WARM_OVERRIDE': ''}):
            cache.mounted()

    def test_operator_override_keeps_writable_legacy_mount(self):
        old_mount = cache.MOUNT
        cache.MOUNT = self.root
        self.addCleanup(lambda: setattr(cache, 'MOUNT', old_mount))
        os.chmod(self.root, 0o755)
        with patch.dict(os.environ, {'CI_SELF_HOSTED': 'nzela', 'CI_WARM_OVERRIDE': 'operator-volume'}):
            cache.mounted()

    def test_missing_absolute_operator_bind_cannot_pass_as_a_public_docker_directory(self):
        old_mount = cache.MOUNT
        cache.MOUNT = self.root
        self.addCleanup(lambda: setattr(cache, 'MOUNT', old_mount))
        os.chmod(self.root, 0o755)
        with patch.dict(os.environ, {'CI_SELF_HOSTED': 'nzela', 'CI_WARM_OVERRIDE': '/operator/cache'}):
            with self.assertRaisesRegex(ValueError, 'mode' if self.owner == 0 else 'owner UID'):
                cache.mounted()


if __name__ == '__main__':
    unittest.main()
