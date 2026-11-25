"""Versioned expert catalog CRUD, indexes, frozen versions, permissions and storage safety."""
import os
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from desktop.core.fusion import expert_catalog as catalog_module
from desktop.core.fusion.expert_catalog import (
    MAX_P1_SERIALIZED_UTF8,
    WORK_TYPE_LABELS,
    WORK_TYPES,
    ExpertCatalog,
    ExpertCatalogError,
    ExpertStorageError,
    ProtectedExpertError,
    UnknownExpertError,
    split_tags,
)
from desktop.core.fusion.identity import load
from desktop.core.fusion.inject import (
    FIRST_PACK_SECTION_TOKENS,
    FIRST_PACK_TOKENS,
    AuthorizedLink,
    ReferencedText,
    build_first_pack,
)


def _catalog(tmp_path):
    return ExpertCatalog(tmp_path / "catalog")


def _markdown(body="Do the controlled work.", header="tools: read, exec\n"):
    return f"---\n{header}---\n{body}"


def _full_referenced(ref, limit):
    return ReferencedText("x" * (limit - len(f"ref={ref}\n".encode("utf-8"))), ref)


def _full_link(kind, limit):
    filler = limit - len(f"{kind}:@v1 auth=g".encode("utf-8"))
    return AuthorizedLink(kind, "t" * filler, 1, "g")


def _full_memory(limit):
    overhead = len("- ref=mem:x\n  ".encode("utf-8"))
    return ReferencedText("x" * (limit - overhead), "mem:x")


def test_builtin_identities_are_visible_legacy_resolvable_and_protected(tmp_path):
    catalog = _catalog(tmp_path)
    assert [entry.stable_id for entry in catalog.list()] == ["Coder", "Runner", "QualitySampler", "Observer"]
    assert catalog.snapshot().revision == 0
    coder = catalog.get("coder")
    assert coder.stable_id == "Coder" and coder.builtin and coder.version == 0
    assert coder.work_type == "execute" and coder.md_path == "builtin:Coder" and coder.enabled
    resolved = catalog.resolve("Coder")
    assert resolved.identity == load(None).get("Coder")
    assert catalog.resolve_version("coder", 0).identity == resolved.identity

    for operation in (
        lambda: catalog.update("Coder", name="Changed"),
        lambda: catalog.set_enabled("observer", False),
        lambda: catalog.delete("Runner"),
        lambda: catalog.create(stable_id="coder-copy", name="Coder", work_type="execute", markdown=_markdown()),
    ):
        with pytest.raises(ProtectedExpertError, match="protected"):
            operation()
    with pytest.raises(UnknownExpertError, match="version 0"):
        catalog.resolve_version("Coder", 1)


@pytest.mark.parametrize("work_type, role, tools, reuse", [
    ("execute", "worker", {"read", "edit", "exec"}, True),
    ("manage", "worker", {"read", "edit", "exec"}, True),
    ("review", "worker", {"read", "exec"}, False),
    ("research", "light", {"read", "exec"}, False),
    ("verify", "light", {"read", "exec"}, False),
    ("observe", "light", {"read"}, False),
])
def test_every_stable_work_type_maps_to_existing_identity_permissions(tmp_path, work_type, role, tools, reuse):
    assert WORK_TYPE_LABELS[work_type] in ("执行", "管理", "审核", "信息查找", "运行验证", "观察")
    catalog = _catalog(tmp_path)
    summary = catalog.create(stable_id=f"expert-{work_type}", name=f"Custom {work_type}", work_type=work_type,
                             markdown="---\n---\nUse the controlled ceiling.")
    resolved = catalog.resolve(summary.stable_id)
    assert summary.role == role
    assert resolved.identity.name == f"Custom {work_type}"
    assert resolved.identity.role == role
    assert resolved.identity.tools == tools
    assert resolved.identity.reuse is reuse
    assert resolved.identity.instructions.endswith("Do not delegate or write to long-term memory.")
    assert set(WORK_TYPES) == {"execute", "manage", "review", "research", "verify", "observe"}


def test_observer_is_reserved_and_custom_observation_cannot_be_widened(tmp_path):
    catalog = _catalog(tmp_path)
    with pytest.raises(ProtectedExpertError):
        catalog.create(stable_id="watcher", name="Observer", work_type="observe", markdown=_markdown("Watch.", "tools: read\n"))
    for header in ("tools: read, exec\n", "role: worker\n", "reuse: true\n"):
        with pytest.raises(ValueError, match="cannot exceed"):
            catalog.create(stable_id="watcher", name="Watcher", work_type="observe", markdown=_markdown("Watch.", header))


def test_create_list_get_snapshot_and_work_type_tag_index_are_lightweight(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    summary = catalog.create(
        stable_id="login-implementer", name="登录实现", work_type="execute", tags=" API api 后端\twindows ",
        markdown=_markdown("Implement login carefully."),
    )
    assert summary.version == 1 and summary.enabled and not summary.builtin and not summary.deleted
    assert summary.tags == ("API", "后端", "windows")
    assert summary.md_path.startswith("bodies/e-") and summary.md_path.endswith(".md")
    assert summary.content_hash.startswith("sha256:")
    assert summary.body_tokens > 0
    assert catalog.get("登录实现") == summary
    assert catalog.snapshot().revision == 1
    assert split_tags(" one One\ntwo  three ") == ("one", "two", "three")

    original_open = Path.open

    def no_markdown_read(path, *args, **kwargs):
        if str(path).endswith(".md"):
            raise AssertionError("lightweight index access opened a Markdown body")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_markdown_read)
    assert catalog.get("login-implementer") == summary
    assert summary in catalog.list()
    assert catalog.query(work_type="execute", tags="api windows") == (summary,)
    assert catalog.query(work_type="manage") == ()
    assert catalog.query(tags="missing") == ()
    assert catalog.query(enabled=True) == tuple(entry for entry in catalog.list() if entry.enabled)
    assert catalog.snapshot().entries == catalog.list()


def test_update_enable_disable_and_delete_keep_frozen_versions_resolvable(tmp_path):
    catalog = _catalog(tmp_path)
    created = catalog.create(stable_id="versioned", name="Versioned", work_type="execute", tags="one",
                             markdown=_markdown("Version one body."))
    first = catalog.resolve("versioned")
    assert first.summary.version == 1 and "Version one body." in first.identity.instructions

    updated = catalog.update("versioned", name="Versioned two", tags="two two", markdown=_markdown("Version two body."))
    assert updated.version == 2 and updated.name == "Versioned two" and updated.tags == ("two",)
    assert created.content_hash != updated.content_hash
    assert "Version two body." in catalog.resolve("versioned").identity.instructions
    frozen_one = catalog.resolve_version("versioned", 1)
    assert frozen_one.summary.name == "Versioned" and "Version one body." in frozen_one.identity.instructions

    disabled = catalog.set_enabled("versioned", False)
    assert disabled.version == 3 and not disabled.enabled
    with pytest.raises(ExpertCatalogError, match="disabled"):
        catalog.resolve("versioned")
    assert "Version two body." in catalog.resolve_version("versioned", 2).identity.instructions

    enabled = catalog.set_enabled("versioned", True)
    assert enabled.version == 4 and enabled.enabled
    assert catalog.delete("versioned") is True
    assert catalog.delete("versioned") is False
    assert catalog.get("versioned") is None
    assert all(entry.stable_id != "versioned" for entry in catalog.list())
    with pytest.raises(UnknownExpertError):
        catalog.resolve("versioned")
    assert "Version one body." in catalog.resolve_version("versioned", 1).identity.instructions
    assert "Version two body." in catalog.resolve_version("versioned", 2).identity.instructions
    with pytest.raises(ExpertCatalogError, match="tombstone"):
        catalog.resolve_version("versioned", 5)
    assert catalog.snapshot().revision == 5


def test_update_revalidates_markdown_against_a_changed_work_type_ceiling(tmp_path):
    catalog = _catalog(tmp_path)
    catalog.create(stable_id="ceiling-change", name="Ceiling", work_type="execute",
                   markdown=_markdown("Body.", "tools: read, edit, exec\n"))
    with pytest.raises(ValueError, match="tools cannot exceed"):
        catalog.update("ceiling-change", work_type="review")
    unchanged = catalog.get("ceiling-change")
    assert unchanged.work_type == "execute" and unchanged.version == 1
    updated = catalog.update("ceiling-change", work_type="review",
                             markdown=_markdown("Body.", "tools: read, exec\n"))
    assert updated.version == 2 and updated.role == "worker"
    assert catalog.resolve("ceiling-change").identity.tools == {"read", "exec"}


def test_legacy_crlf_bom_markdown_is_normalized_and_remains_parseable(tmp_path):
    catalog = _catalog(tmp_path)
    raw = ("\ufeff---\r\nname: 旧身份\r\ntools: read, exec\r\ncompact_at: 75\r\nunknown: ignored\r\n---\r\n旧正文。\r\n")
    summary = catalog.create(stable_id="legacy-doc", name="旧身份", work_type="execute", markdown=raw)
    resolved = catalog.resolve("legacy-doc")
    assert summary.version == 1
    assert "\r" not in resolved.markdown and not resolved.markdown.startswith("\ufeff")
    assert resolved.identity.compact_at == 75
    assert resolved.identity.tools == {"read", "exec"}
    assert "旧正文。" in resolved.identity.instructions
    assert catalog.resolve_version("legacy-doc", 1).markdown == resolved.markdown


@pytest.mark.parametrize("stable_id", ["", "../escape", "/absolute", "a/b", "white space", "x" * 65])
def test_stable_id_rejects_paths_whitespace_and_overlong_values(tmp_path, stable_id):
    catalog = _catalog(tmp_path)
    with pytest.raises(ValueError, match="stable_id"):
        catalog.create(stable_id=stable_id, name="Valid", work_type="execute", markdown=_markdown())


@pytest.mark.parametrize("kwargs, problem", [
    ({"work_type": "执行"}, "work_type"),
    ({"name": "bad\nname"}, "name"),
    ({"tags": tuple(f"tag{number}" for number in range(33))}, "at most 32 tags"),
    ({"markdown": "---\n---\n   "}, "must not be empty"),
    ({"markdown": "no front matter"}, "opening front matter"),
])
def test_invalid_user_fields_are_rejected_before_storage(tmp_path, kwargs, problem):
    catalog = _catalog(tmp_path)
    values = {"stable_id": "invalid-test", "name": "Valid", "work_type": "execute", "markdown": _markdown()}
    values.update(kwargs)
    with pytest.raises(ValueError, match=problem):
        catalog.create(**values)
    assert catalog.get("invalid-test") is None


def test_ascii_9000_byte_body_is_rejected_at_save_with_actionable_error(tmp_path):
    catalog = _catalog(tmp_path)
    with pytest.raises(ValueError) as error:
        catalog.create(stable_id="too-long-ascii", name="Too long", work_type="execute",
                       markdown=_markdown("x" * 9000))
    message = str(error.value)
    assert "cannot be dispatched" in message and "serialized P1 costs" in message
    assert "body text 9000" in message and str(MAX_P1_SERIALIZED_UTF8) in message
    assert "Remove at least" in message and "split the expert" in message
    assert catalog.get("too-long-ascii") is None


def test_multibyte_body_uses_the_utf8_upper_bound_not_the_character_estimate(tmp_path):
    catalog = _catalog(tmp_path)
    accepted = catalog.create(stable_id="unicode-fits", name="Unicode fits", work_type="execute",
                              markdown=_markdown("界" * 1000))
    assert accepted.version == 1
    with pytest.raises(ValueError, match="UTF-8 bytes"):
        catalog.create(stable_id="unicode-over", name="Unicode over", work_type="execute",
                       markdown=_markdown("界" * 1400))
    assert catalog.get("unicode-over") is None


def test_body_at_the_conservative_cap_fits_a_worst_case_8192_first_pack(tmp_path):
    catalog = _catalog(tmp_path)
    boundary = len("\nDo not delegate or write to long-term memory.".encode("utf-8"))
    body = "x" * (MAX_P1_SERIALIZED_UTF8 - boundary)
    summary = catalog.create(stable_id="exact-cap", name="Exact cap", work_type="execute", markdown=_markdown(body))
    resolved = catalog.resolve(summary.stable_id)
    assert len(resolved.instructions.encode("utf-8")) == MAX_P1_SERIALIZED_UTF8

    expert_ref = "e" * 19 + ":" + "r" * 160
    pack = build_first_pack(
        resolved.identity,
        expert_ref=expert_ref,
        goal=_full_referenced("goal:x", FIRST_PACK_SECTION_TOKENS["P2"]),
        task=_full_referenced("task:x", FIRST_PACK_SECTION_TOKENS["P3"]),
        authority=_full_referenced("auth:x", FIRST_PACK_SECTION_TOKENS["P4"]),
        acceptance=_full_referenced("acc:x", FIRST_PACK_SECTION_TOKENS["P5"]),
        rule_memory_links=(_full_link("rules", FIRST_PACK_SECTION_TOKENS["P6"]),),
        upstream_links=(_full_link("upstream", FIRST_PACK_SECTION_TOKENS["P7"]),),
        handoff_links=(_full_link("handoff", FIRST_PACK_SECTION_TOKENS["P8"]),),
        memory=(_full_memory(FIRST_PACK_SECTION_TOKENS["P9"]),),
    )
    assert pack.ok and not pack.problems
    assert pack.tokens == FIRST_PACK_TOKENS


def test_legacy_oversize_version_stays_readable_but_new_versions_are_rejected(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    original = catalog_module.MAX_P1_SERIALIZED_UTF8
    monkeypatch.setattr(catalog_module, "MAX_P1_SERIALIZED_UTF8", 10000)
    summary = catalog.create(stable_id="legacy-large", name="Legacy large", work_type="execute",
                             markdown=_markdown("x" * 9000))
    monkeypatch.setattr(catalog_module, "MAX_P1_SERIALIZED_UTF8", original)

    assert "x" * 9000 in catalog.resolve("legacy-large").instructions
    assert "x" * 9000 in catalog.resolve_version("legacy-large", summary.version).instructions
    with pytest.raises(ValueError, match="cannot be dispatched"):
        catalog.update("legacy-large", tags="metadata-only-change")
    assert catalog.delete("legacy-large") is True
    assert "x" * 9000 in catalog.resolve_version("legacy-large", summary.version).instructions


def test_failed_atomic_body_save_rolls_back_index_and_removes_temporary_file(tmp_path, monkeypatch):
    catalog = _catalog(tmp_path)
    original_replace = os.replace

    def fail_markdown_replace(source, target):
        if str(target).endswith(".md"):
            raise OSError("injected replace failure")
        return original_replace(source, target)

    monkeypatch.setattr(catalog_module.os, "replace", fail_markdown_replace)
    with pytest.raises(OSError, match="injected replace failure"):
        catalog.create(stable_id="atomic", name="Atomic", work_type="execute", markdown=_markdown())
    monkeypatch.setattr(catalog_module.os, "replace", original_replace)
    assert catalog.get("atomic") is None
    assert catalog.snapshot().revision == 0
    bodies = tmp_path / "catalog" / "bodies"
    assert list(bodies.glob("*.md")) == []
    assert list(bodies.glob("*.tmp")) == []


def test_immutable_body_tampering_and_controlled_path_tampering_are_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    summary = catalog.create(stable_id="integrity", name="Integrity", work_type="execute", markdown=_markdown())
    body = tmp_path / "catalog" / summary.md_path
    body.write_text("---\n---\nTampered.", encoding="utf-8")
    with pytest.raises(ExpertStorageError, match="integrity"):
        catalog.resolve_version("integrity", 1)

    catalog2 = ExpertCatalog(tmp_path / "second")
    summary2 = catalog2.create(stable_id="path-check", name="Path", work_type="execute", markdown=_markdown())
    with sqlite3.connect(tmp_path / "second" / "experts.sqlite") as db:
        db.execute("UPDATE experts SET md_path = '../outside.md' WHERE id = 'path-check'")
        db.execute("UPDATE versions SET md_path = '../outside.md' WHERE id = 'path-check'")
    with pytest.raises(ExpertStorageError, match="path is invalid"):
        catalog2.resolve("path-check")
    assert summary2.md_path.startswith("bodies/")


def test_body_directory_symlink_escape_is_rejected(tmp_path):
    catalog = _catalog(tmp_path)
    catalog.create(stable_id="linked", name="Linked", work_type="execute", markdown=_markdown())
    bodies = tmp_path / "catalog" / "bodies"
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.rmtree(bodies)
    try:
        bodies.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        linked = subprocess.run(["cmd", "/c", "mklink", "/J", str(bodies), str(outside)],
                                capture_output=True, text=True)
        if linked.returncode:
            pytest.skip(f"this Windows account cannot create a directory link: {error}; {linked.stderr.strip()}")
    with pytest.raises(ExpertStorageError, match="links are not allowed"):
        catalog.resolve("linked")


def test_namespace_collisions_are_rejected_and_deleted_names_can_be_reused(tmp_path):
    catalog = _catalog(tmp_path)
    catalog.create(stable_id="first", name="Shared", work_type="execute", markdown=_markdown())
    for stable_id, name in (("second", "shared"), ("shared", "Other"), ("first", "Other")):
        with pytest.raises(ValueError, match="collide|reserved"):
            catalog.create(stable_id=stable_id, name=name, work_type="execute", markdown=_markdown())
    catalog.delete("first")
    reused = catalog.create(stable_id="second", name="shared", work_type="execute", markdown=_markdown("New body."))
    assert reused.name == "shared"
    with pytest.raises(ValueError, match="reserved"):
        catalog.create(stable_id="first", name="Different", work_type="execute", markdown=_markdown())


def test_concurrent_catalog_instances_do_not_lose_writes(tmp_path):
    root = tmp_path / "catalog"

    def write_batch(worker):
        catalog = ExpertCatalog(root)
        for index in range(8):
            catalog.create(stable_id=f"worker-{worker}-{index}", name=f"Worker {worker}-{index}", work_type="execute",
                           tags=f"worker-{worker} batch", markdown=_markdown(f"Body {worker}-{index}."))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_batch, range(4)))
    catalog = ExpertCatalog(root)
    custom = [entry for entry in catalog.list() if not entry.builtin]
    assert len(custom) == 32
    assert len(catalog.query(tags="batch")) == 32
    assert len(catalog.query(tags="worker-2")) == 8
    assert catalog.snapshot().revision == 32


def test_concurrent_updates_to_one_expert_are_serialized_into_distinct_versions(tmp_path):
    root = tmp_path / "catalog"
    first = ExpertCatalog(root)
    first.create(stable_id="shared-target", name="Shared target", work_type="execute", markdown=_markdown("Initial body."))

    def update(index):
        ExpertCatalog(root).update("shared-target", markdown=_markdown(f"Concurrent body {index}."))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(update, range(8)))
    catalog = ExpertCatalog(root)
    current = catalog.get("shared-target")
    assert current.version == 9
    assert catalog.snapshot().revision == 9
    bodies = {catalog.resolve_version("shared-target", version).markdown for version in range(1, 10)}
    assert len(bodies) == 9
