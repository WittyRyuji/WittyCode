from __future__ import annotations

import json

from desktop.core.diffs import (
    MAX_DIFF_LINES,
    diff_for_new_file,
    diff_from_contents,
    diff_from_edit_result,
    diff_from_fragments,
    diffs_from_patch,
    input_diffs,
    result_diff,
)


def kinds(diff):
    return [line.kind for line in diff.lines]


def test_contents_diff_counts_and_numbers_lines():
    old = "a\nb\nc\nd\ne\n"
    new = "a\nb\nC\nd\ne\nf\n"
    diff = diff_from_contents("x.txt", old, new)
    assert (diff.added, diff.removed) == (2, 1)
    assert not diff.is_new_file
    deleted = [line for line in diff.lines if line.kind == "del"]
    added = [line for line in diff.lines if line.kind == "add"]
    assert [(line.text, line.old) for line in deleted] == [("c", 3)]
    assert [(line.text, line.new) for line in added] == [("C", 3), ("f", 6)]
    assert [line.text for line in diff.lines if line.kind == "ctx"][:2] == ["a", "b"]


def test_identical_contents_give_an_empty_diff():
    diff = diff_from_contents("x", "same\n", "same\n")
    assert (diff.added, diff.removed, diff.lines) == (0, 0, ())


def test_distant_changes_are_separated_by_a_gap_marker():
    old = "\n".join(str(i) for i in range(40))
    new = old.replace("\n2\n", "\nTWO\n").replace("\n37\n", "\nTHIRTY-SEVEN\n")
    diff = diff_from_contents("x", old, new, context=1)
    assert kinds(diff).count("gap") == 1
    assert (diff.added, diff.removed) == (2, 2)


def test_missing_old_content_marks_a_new_file():
    assert diff_from_contents("n", None, "x\n").is_new_file


def test_new_file_diff_is_all_additions():
    diff = diff_for_new_file("n.txt", "one\ntwo\n")
    assert kinds(diff) == ["add", "add"]
    assert (diff.added, diff.removed, diff.is_new_file) == (2, 0, True)
    assert [line.new for line in diff.lines] == [1, 2]


def test_fragment_diff_has_no_line_numbers():
    diff = diff_from_fragments("f", "old line\nkeep", "new line\nkeep")
    assert (diff.added, diff.removed) == (1, 1)
    assert all(line.old is None and line.new is None for line in diff.lines)


def test_large_diffs_are_truncated_with_a_count():
    diff = diff_for_new_file("big", "\n".join("x" for _ in range(MAX_DIFF_LINES + 50)))
    assert len(diff.lines) == MAX_DIFF_LINES
    assert diff.truncated == 50
    assert diff.added == MAX_DIFF_LINES + 50


def test_edit_result_json_is_parsed_with_gaps():
    payload = {
        "success": True,
        "file_path": "D:\\p\\a.txt",
        "linesAdded": 1,
        "linesRemoved": 1,
        "diffLines": [
            {"type": "context", "content": "keep", "lineNumber": {"old": 1, "new": 1}},
            {"type": "removed", "content": "old", "lineNumber": {"old": 2}},
            {"type": "added", "content": "new", "lineNumber": {"new": 2}},
            {"type": "context", "content": "far away", "lineNumber": {"old": 30, "new": 30}},
        ],
    }
    diff = diff_from_edit_result(json.dumps(payload))
    assert diff is not None
    assert diff.path == "D:\\p\\a.txt"
    assert (diff.added, diff.removed) == (1, 1)
    assert kinds(diff) == ["ctx", "del", "add", "gap", "ctx"]


def test_edit_result_that_is_not_a_diff_is_ignored():
    assert diff_from_edit_result("plain text") is None
    assert diff_from_edit_result('{"success": true}') is None
    assert diff_from_edit_result("[1, 2]") is None


def test_result_diff_only_for_edit_like_tools():
    payload = json.dumps({"diffLines": [{"type": "added", "content": "x", "lineNumber": {"new": 1}}]})
    assert result_diff("Edit", payload, {"file_path": "f"}) is not None
    assert result_diff("Read", payload, {}) is None
    assert result_diff("Edit", "not json", {}) is None


def test_apply_patch_text_is_split_per_file():
    patch = (
        "*** Begin Patch\n*** Update File: a.py\n@@\n-old\n+new\n context\n"
        "*** Add File: b.py\n+first\n+second\n*** End Patch"
    )
    first, second = diffs_from_patch(patch)
    assert (first.path, first.added, first.removed, first.is_new_file) == ("a.py", 1, 1, False)
    assert (second.path, second.added, second.is_new_file) == ("b.py", 2, True)


def test_unified_diff_text_is_supported():
    patch = "--- a/x.txt\n+++ b/x.txt\n@@ -1,2 +1,2 @@\n keep\n-old\n+new\n"
    (diff,) = diffs_from_patch(patch)
    assert diff.path == "x.txt"
    assert (diff.added, diff.removed) == (1, 1)


def test_input_diffs_by_tool():
    (edit,) = input_diffs("Edit", {"file_path": "f", "old_str": "a", "new_str": "b"})
    assert (edit.added, edit.removed) == (1, 1)
    (create,) = input_diffs("Create", {"file_path": "f", "content": "x\ny"})
    assert create.is_new_file and create.added == 2
    assert input_diffs("Edit", {"file_path": "f"}) == ()
    assert input_diffs("Read", {"file_path": "f"}) == ()
    assert len(input_diffs("ApplyPatch", {"input": "*** Begin Patch\n*** Add File: z\n+1\n*** End Patch"})) == 1