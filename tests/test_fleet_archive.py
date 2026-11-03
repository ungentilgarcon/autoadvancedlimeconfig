import io
import tarfile
import unittest

from fleet_archive import (
    ArchiveError,
    Change,
    Entry,
    compare,
    merge_change,
    packages_from_text,
    read_backup,
)


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for item in entries:
            path, data = item[:2]
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = item[2] if len(item) > 2 else 0o600
            tar.addfile(info, io.BytesIO(data))
    return output.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_compare_add_modify_and_delete(self):
        original = read_backup(archive([("./etc/config/system", b"one\n"),
                                        ("etc/dropbear/key", b"old\n")]))
        edited = read_backup(archive([("etc/config/system", b"two\n"),
                                      ("etc/new", b"new\n")]))
        self.assertEqual([(c.path, c.operation) for c in compare(original, edited)], [
            ("etc/config/system", "modify"), ("etc/dropbear/key", "delete"),
            ("etc/new", "add"),
        ])

    def test_traversal_and_duplicates_rejected(self):
        for names in (
            [("../etc/config/system", b"x")],
            [("/etc/config/system", b"x")],
            [("etc/config/a", b"x"), ("./etc/config/a", b"y")],
            [("opt/../etc/config/system", b"x")],
            [("usr/sbin/a", b"x"), ("./usr/sbin/a", b"y")],
        ):
            with self.subTest(names=names), self.assertRaises(ArchiveError):
                read_backup(archive(names))

    def test_changed_symlink_rejected(self):
        original = {"etc/test": Entry("link", link="../secret")}
        with self.assertRaises(ArchiveError):
            compare(original, {"etc/test": Entry("file", b"x")})

    def test_compare_new_and_modified_tools(self):
        original = read_backup(archive([
            ("usr/sbin/existing", b"old\n", 0o755),
            ("opt/service/config", b"old\n", 0o600),
            ("usr/bin/outside-scope", b"old\n", 0o755),
        ]))
        edited = read_backup(archive([
            ("usr/sbin/existing", b"new\n", 0o755),
            ("usr/sbin/newtool", b"#!/bin/sh\n", 0o755),
            ("opt/service/config", b"new\n", 0o600),
            ("opt/service/tool", b"#!/bin/sh\n", 0o755),
            ("usr/bin/outside-scope", b"new\n", 0o755),
        ]))
        changes = compare(original, edited)
        self.assertEqual([(c.path, c.operation) for c in changes], [
            ("opt/service/config", "modify"), ("opt/service/tool", "add"),
            ("usr/sbin/existing", "modify"), ("usr/sbin/newtool", "add"),
        ])
        for change in (changes[1], changes[3]):
            self.assertEqual(change.edited.mode, 0o755)
            self.assertEqual(merge_change(change, None).mode, 0o755)

    def test_mode_only_changes_and_merge_conflicts(self):
        original = read_backup(archive([("usr/sbin/tool", b"data", 0o644)]))
        edited = read_backup(archive([("usr/sbin/tool", b"data", 0o755)]))
        changes = compare(original, edited)
        self.assertEqual(len(changes), 1)
        self.assertEqual(merge_change(changes[0], Entry("file", b"data", 0o644)).mode, 0o755)
        self.assertEqual(merge_change(changes[0], Entry("file", b"changed", 0o644)),
                         Entry("file", b"changed", 0o755))
        with self.assertRaises(ArchiveError):
            merge_change(changes[0], Entry("file", b"data", 0o700))

    def test_archive_preserves_special_mode_bits_for_review(self):
        original = read_backup(archive([]))
        edited = read_backup(archive([("usr/sbin/tool", b"data", 0o4755)]))
        self.assertEqual(compare(original, edited)[0].edited.mode, 0o4755)

    def test_three_way_merge_preserves_unrelated_node_change(self):
        before = b"hostname original\n\nchannel 36\n\nother value\n"
        edited = b"hostname original\n\nchannel 44\n\nother value\n"
        target = b"hostname node-two\n\nchannel 36\n\nother value\n"
        change = Change("etc/config/lime", Entry("file", before, 0o600),
                        Entry("file", edited, 0o600))
        merged = merge_change(change, Entry("file", target, 0o640))
        self.assertEqual(merged, Entry("file",
                                     b"hostname node-two\n\nchannel 44\n\nother value\n",
                                     0o640))

    def test_overlapping_changes_fail(self):
        change = Change("etc/config/lime", Entry("file", b"channel 36\n", 0o600),
                        Entry("file", b"channel 44\n", 0o600))
        with self.assertRaises(ArchiveError):
            merge_change(change, Entry("file", b"channel 149\n", 0o600))

    def test_add_and_delete_refuse_conflicts(self):
        file = Entry("file", b"secret\n", 0o600)
        addition = Change("etc/new", None, file)
        deletion = Change("etc/old", file, None)
        self.assertEqual(merge_change(addition, None), file)
        self.assertIsNone(merge_change(deletion, file))
        with self.assertRaises(ArchiveError):
            merge_change(addition, file)
        with self.assertRaises(ArchiveError):
            merge_change(deletion, Entry("file", b"different\n", 0o600))

    def test_packages(self):
        self.assertEqual(packages_from_text("bmx7, lime-proto-bmx7, bmx7"),
                         ["bmx7", "lime-proto-bmx7"])
        for value in ("bmx7,", "-x", "one; echo x", "one two"):
            with self.subTest(value=value), self.assertRaises(ArchiveError):
                packages_from_text(value)


if __name__ == "__main__":
    unittest.main()
