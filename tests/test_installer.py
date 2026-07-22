"""Tests for `cc-mock init` installer logic (pure over explicit paths)."""

from __future__ import annotations

from pathlib import Path

import pytest

from cc_mock_server import installer


class TestResolveTargets:
    def test_global_targets_under_home_claude(self, tmp_path: Path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        skill, claude_md = installer.resolve_targets("global", home, cwd)
        assert skill == home / ".claude" / "skills" / "cc-mock" / "SKILL.md"
        assert claude_md == home / ".claude" / "CLAUDE.md"

    def test_project_targets_under_cwd(self, tmp_path: Path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        skill, claude_md = installer.resolve_targets("project", home, cwd)
        assert skill == cwd / ".claude" / "skills" / "cc-mock" / "SKILL.md"
        assert claude_md == cwd / "CLAUDE.md"

    def test_unknown_scope_raises(self, tmp_path: Path):
        with pytest.raises(ValueError):
            installer.resolve_targets("nope", tmp_path, tmp_path)


class TestSkillContent:
    def test_skill_has_frontmatter_name_and_description(self):
        assert installer.SKILL_MD.startswith("---\n")
        assert "name: cc-mock" in installer.SKILL_MD
        assert "description:" in installer.SKILL_MD

    def test_skill_teaches_the_poll_respond_loop(self):
        for token in ("cc-mock pending", "cc-mock respond", "HTTP_PROXY", "mode replay"):
            assert token in installer.SKILL_MD


class TestUpsertManagedBlock:
    def test_appends_block_to_existing_content(self):
        existing = "# My Project\n\nSome rules.\n"
        result = installer.upsert_managed_block(existing, installer.render_claude_block())
        assert existing.rstrip() in result
        assert installer.START_MARKER in result
        assert installer.END_MARKER in result

    def test_creates_content_when_empty(self):
        result = installer.upsert_managed_block("", installer.render_claude_block())
        assert result.count(installer.START_MARKER) == 1
        assert result.count(installer.END_MARKER) == 1

    def test_idempotent_replaces_in_place(self):
        base = "# Project\n\nkeep me\n"
        once = installer.upsert_managed_block(base, installer.render_claude_block())
        twice = installer.upsert_managed_block(once, installer.render_claude_block())
        # no duplication on re-run
        assert twice.count(installer.START_MARKER) == 1
        assert twice.count(installer.END_MARKER) == 1
        assert "keep me" in twice

    def test_replacing_updated_block_preserves_surrounding_text(self):
        base = "TOP\n\n" + installer.render_claude_block() + "\n\nBOTTOM\n"
        # simulate a stale/edited body inside the markers (markers intact),
        # then upsert the canonical block back over it.
        changed = base.replace("developing", "developing STALE_EDIT")
        result = installer.upsert_managed_block(changed, installer.render_claude_block())
        assert result.startswith("TOP")
        assert result.rstrip().endswith("BOTTOM")
        assert "STALE_EDIT" not in result  # managed region fully replaced
        assert result.count(installer.START_MARKER) == 1


class TestInstall:
    def test_install_global_writes_both_artifacts(self, tmp_path: Path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        written = installer.install("global", home, cwd)

        skill = home / ".claude" / "skills" / "cc-mock" / "SKILL.md"
        claude_md = home / ".claude" / "CLAUDE.md"
        assert set(written) == {skill, claude_md}
        assert skill.read_text().startswith("---\n")
        assert installer.START_MARKER in claude_md.read_text()

    def test_install_project_scope_uses_repo_paths(self, tmp_path: Path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        installer.install("project", home, cwd)
        assert (cwd / ".claude" / "skills" / "cc-mock" / "SKILL.md").exists()
        assert (cwd / "CLAUDE.md").exists()

    def test_install_preserves_and_does_not_duplicate_on_rerun(self, tmp_path: Path):
        home, cwd = tmp_path / "home", tmp_path / "proj"
        claude_md = home / ".claude" / "CLAUDE.md"
        claude_md.parent.mkdir(parents=True)
        claude_md.write_text("# Existing global rules\n\nkeep this line\n")

        installer.install("global", home, cwd)
        installer.install("global", home, cwd)  # rerun

        text = claude_md.read_text()
        assert "keep this line" in text
        assert text.count(installer.START_MARKER) == 1
        assert text.count(installer.END_MARKER) == 1
