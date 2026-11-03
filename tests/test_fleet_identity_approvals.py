import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fleet_file_deploy as deployment
import fleet_identity_approvals as identity
from fleet_archive import ArchiveError


class IdentityApprovalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "approvals.json"
        self.patch = patch.object(identity, "APPROVALS", self.path)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.node = deployment.fleet.Node({"name": "old", "host": "root@192.0.2.2",
                                           "mesh_ip": "192.0.2.2"})
        self.approval = identity.Approval("old", "192.0.2.2", "new", "aruba,ap-303",
                                          "ssh-ed25519", "SHA256:abc123")

    def test_save_is_private_persistent_and_revocable(self):
        self.assertEqual(identity.load(), [])
        identity.save(self.approval)
        self.assertEqual(identity.load(), [self.approval])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        replacement = identity.Approval("old", "192.0.2.2", "other", "aruba,ap-303",
                                        "ssh-ed25519", "SHA256:different")
        identity.save(replacement)
        self.assertEqual(identity.load(), [replacement])
        self.path.unlink()
        self.assertEqual(identity.load(), [])

    def test_insecure_symlink_and_invalid_file_are_rejected(self):
        self.path.write_text("[]")
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ArchiveError, "owner-only"):
            identity.load()
        self.path.chmod(0o600)
        self.path.write_text('{"unexpected": true}')
        with self.assertRaisesRegex(ArchiveError, "Invalid identity"):
            identity.load()
        target = self.path.with_name("target")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaises(ArchiveError):
            identity.load()

    def test_identify_uses_authenticated_ssh_and_fails_closed(self):
        stdout = json.dumps({"hostname": "new", "board_name": "aruba,ap-303"})
        stderr = "debug1: Server host key: ssh-ed25519 SHA256:abc123\n"
        with patch.object(deployment.fleet, "transport", return_value=(["ssh"], None)), \
             patch.object(identity.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, stdout, stderr)) as run:
            self.assertEqual(identity.identify(deployment.fleet, self.node, "old"), self.approval)
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertIn("lime-config", run.call_args.args[0][-1])
        for result in (subprocess.CompletedProcess([], 255, stdout, stderr),
                       subprocess.CompletedProcess([], 0, stdout, ""),
                       subprocess.CompletedProcess([], 0, "{}", stderr)):
            with self.subTest(result=result), \
                 patch.object(deployment.fleet, "transport", return_value=(["ssh"], None)), \
                 patch.object(identity.subprocess, "run", return_value=result), \
                 self.assertRaises(ArchiveError):
                identity.identify(deployment.fleet, self.node, "old")

    def test_revalidation_rejects_changed_or_revoked_approval(self):
        identity.save(self.approval)
        self.node.identity_approval = self.approval
        with patch.object(identity, "identify", return_value=self.approval):
            identity.verify_nodes(deployment.fleet, [self.node])
        changed = identity.Approval("old", "192.0.2.2", "new", "aruba,ap-303",
                                     "ssh-ed25519", "SHA256:changed")
        with patch.object(identity, "identify", return_value=changed), self.assertRaises(ArchiveError):
            identity.verify_nodes(deployment.fleet, [self.node])
        os.unlink(self.path)
        with patch.object(identity, "identify") as identify, self.assertRaises(ArchiveError):
            identity.verify_nodes(deployment.fleet, [self.node])
        identify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
