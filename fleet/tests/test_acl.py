"""With `private` false, the folders and files the receiver makes in the copy follow the
copy's default ACL, as folders and files made there by hand would.

Say an ACL gives one more account read access to the copy, with a default ACL for new
entries. Folders made 0700 and files made 0600 leave a default ACL's mask at nothing, so
that account's entry has no effect on them, and files built in staging/ (no ACL there)
and moved in have no ACL at all. These tests check that neither happens."""
import os
import shutil
import stat
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FLEET = os.path.dirname(HERE)
sys.path.insert(0, FLEET)
sys.path.insert(0, HERE)

import fleet_stream as fs                       # noqa: E402
from fakecollector import FakeCollector         # noqa: E402
from test_safety import ReceiverCase, wait_for  # noqa: E402

ACCESS, DEFAULT = "system.posix_acl_access", "system.posix_acl_default"
USER_OBJ, USER, GROUP_OBJ, MASK, OTHER = 0x01, 0x02, 0x04, 0x10, 0x20
NOID = 0xFFFFFFFF
OTHER_UID = 65534          # the account the ACL lets in; any uid will do


def blob(*entries):
    """An ACL in its xattr form: version 2, then (tag, perm, id) entries."""
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", t, p, i) for t, p, i in entries)


def entries(data):
    return [struct.unpack_from("<HHI", data, off) for off in range(4, len(data), 8)]


# What `setfacl -d -m u:<uid>:r-x` leaves on a 700 folder.
SHARE = blob((USER_OBJ, 7, NOID), (USER, 5, OTHER_UID), (GROUP_OBJ, 0, NOID),
             (MASK, 5, NOID), (OTHER, 0, NOID))


def effective(path):
    """What the ACL on `path` gives OTHER_UID: its entry, limited by the mask."""
    try:
        e = entries(os.getxattr(path, ACCESS))
    except OSError:
        return 0
    mask = next((p for t, p, _ in e if t == MASK), 7)
    return next((p & mask for t, p, i in e if t == USER and i == OTHER_UID), 0)


def share(folder, case):
    """Put SHARE on `folder` as its default ACL, or skip where the disk has no ACLs."""
    try:
        os.setxattr(folder, DEFAULT, SHARE)
    except OSError as e:
        case.skipTest("no ACLs on this disk: %s" % e)


class TestAclForNew(unittest.TestCase):
    def test_masks_like_the_kernel(self):
        got = {(t, i): p for t, p, i in entries(fs.acl_for_new(SHARE, 0o666))}
        self.assertEqual(got, {(USER_OBJ, NOID): 6, (USER, OTHER_UID): 5, (GROUP_OBJ, NOID): 0,
                               (MASK, NOID): 4, (OTHER, NOID): 0})
        got = {(t, i): p for t, p, i in entries(fs.acl_for_new(SHARE, 0o777))}
        self.assertEqual(got[(USER_OBJ, NOID)], 7)
        self.assertEqual(got[(MASK, NOID)], 5)

    def test_without_a_mask_the_group_entry_takes_the_group_bits(self):
        acl = blob((USER_OBJ, 7, NOID), (GROUP_OBJ, 5, NOID), (OTHER, 5, NOID))
        got = {t: p for t, p, _ in entries(fs.acl_for_new(acl, 0o640))}
        self.assertEqual(got, {USER_OBJ: 6, GROUP_OBJ: 4, OTHER: 0})


class TestFollowTheDefaultAcl(unittest.TestCase):
    def setUp(self):
        self.old_umask = os.umask(0o077)          # what the stream's main() sets
        self.tmp = tempfile.mkdtemp(prefix="fleet-acl-")
        self.basedir = os.path.join(self.tmp, "data")
        self.mirror = os.path.join(self.basedir, "mirror")
        os.mkdir(self.basedir, 0o700)
        os.mkdir(self.mirror, 0o700)
        share(self.mirror, self)

    def tearDown(self):
        os.umask(self.old_umask)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_folders_and_files_follow_it(self):
        b = fs.SafeBase.open(self.basedir, private=False)
        os.close(b.open_append("mirror/h1/2026-11-16/h1.exits.jsonl"))
        b.close()
        day = os.path.join(self.mirror, "h1", "2026-11-16")
        self.assertEqual(effective(os.path.join(self.mirror, "h1")), 5)
        self.assertEqual(effective(day), 5)
        self.assertEqual(effective(os.path.join(day, "h1.exits.jsonl")), 4)

    def test_a_file_moved_in_from_staging_follows_it(self):
        """A sealed day's `.gz` is built in staging/ and moved into the copy."""
        b = fs.SafeBase.open(self.basedir, private=False)
        os.close(b.open_new("staging/h1/2026-11-15/h1.exits.jsonl.gz.tmp"))
        b.rename("staging/h1/2026-11-15/h1.exits.jsonl.gz.tmp", "mirror/h1/2026-11-15/h1.exits.jsonl.gz")
        b.close()
        moved = os.path.join(self.mirror, "h1", "2026-11-15", "h1.exits.jsonl.gz")
        self.assertEqual(effective(moved), 4)
        self.assertEqual(stat.S_IMODE(os.stat(moved).st_mode), 0o640)

    def test_without_a_default_acl_new_entries_stay_owner_only(self):
        """state/ and staging/ have no default ACL: the umask gives 0700 and 0600, as before."""
        b = fs.SafeBase.open(self.basedir, private=False)
        os.close(b.open_new("state/x.json"))
        b.close()
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.basedir, "state")).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.basedir, "state", "x.json")).st_mode),
                         0o600)

    def test_private_keeps_new_files_private(self):
        """With `private` (the default) nothing changes: the entry has no effect on them."""
        b = fs.SafeBase.open(self.basedir)
        os.close(b.open_append("mirror/h1/2026-11-16/h1.exits.jsonl"))
        os.close(b.open_new("staging/h1/2026-11-15/h1.exits.jsonl.gz.tmp"))
        b.rename("staging/h1/2026-11-15/h1.exits.jsonl.gz.tmp", "mirror/h1/2026-11-15/h1.exits.jsonl.gz")
        b.close()
        self.assertEqual(effective(os.path.join(self.mirror, "h1", "2026-11-16", "h1.exits.jsonl")), 0)
        self.assertEqual(effective(os.path.join(self.mirror, "h1", "2026-11-15", "h1.exits.jsonl.gz")), 0)


class TestReceiverFollowsIt(ReceiverCase):
    def test_the_live_files_follow_the_copys_default_acl(self):
        mirror = os.path.join(self.basedir, "mirror")
        os.mkdir(mirror, 0o700)
        share(mirror, self)
        c = FakeCollector(self.src)
        c.append("exits", b'{"w":1}\n' * 10)
        self.start(cfg_extra={"private": False})
        self.assertTrue(wait_for(self.same))
        files = [os.path.join(d, f) for d, _, names in os.walk(self.mdir) for f in names]
        self.assertTrue(files)
        for f in files:
            self.assertEqual(effective(f), 4, f)


if __name__ == "__main__":
    unittest.main()
