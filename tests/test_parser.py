"""Unit tests for ingestion/parser.py — no DB or network required."""

from pathlib import Path

import pytest

from ingestion.parser import parse_file, parse_repo

FIXTURES = Path(__file__).parent / "fixtures" / "sample_repo"
V1 = FIXTURES / "v1"
V2 = FIXTURES / "v2"


class TestParseFile:
    def test_returns_expected_keys(self):
        result = parse_file(str(V1 / "main.py"))
        assert "path" in result
        assert "line_count" in result
        assert "functions" in result
        assert "classes" in result
        assert "imports" in result

    def test_finds_functions(self):
        result = parse_file(str(V1 / "main.py"))
        names = [f["name"] for f in result["functions"]]
        assert "run" in names
        assert "create_user" in names
        assert "display_items" in names

    def test_finds_classes(self):
        result = parse_file(str(V1 / "models.py"))
        names = [c["name"] for c in result["classes"]]
        assert "User" in names
        assert "Item" in names

    def test_finds_class_methods(self):
        result = parse_file(str(V1 / "models.py"))
        methods = [f for f in result["functions"] if f.get("class_name")]
        method_names = [m["name"] for m in methods]
        assert "deactivate" in method_names
        assert "to_dict" in method_names

    def test_class_bases_extracted(self):
        result = parse_file(str(V1 / "models.py"))
        # User and Item don't have bases, but the structure should be present
        for cls in result["classes"]:
            assert "bases" in cls
            assert isinstance(cls["bases"], list)

    def test_finds_imports(self):
        result = parse_file(str(V1 / "main.py"))
        assert "config" in result["imports"]
        assert "models" in result["imports"]
        assert "utils" in result["imports"]

    def test_extracts_docstrings(self):
        result = parse_file(str(V1 / "utils.py"))
        slugify = next(f for f in result["functions"] if f["name"] == "slugify")
        assert slugify["docstring"] is not None
        assert "slug" in slugify["docstring"].lower()

    def test_extracts_calls(self):
        result = parse_file(str(V1 / "main.py"))
        display = next(f for f in result["functions"] if f["name"] == "display_items")
        assert "paginate" in display["calls"]
        assert "slugify" in display["calls"]

    def test_source_hash_present(self):
        result = parse_file(str(V1 / "utils.py"))
        for fn in result["functions"]:
            assert "source_hash" in fn
            assert isinstance(fn["source_hash"], str)
            assert len(fn["source_hash"]) == 64  # SHA-256 hex

    def test_line_count(self):
        result = parse_file(str(V1 / "config.py"))
        assert result["line_count"] > 0

    def test_syntax_error_raises(self, tmp_path):
        bad = tmp_path / "bad.py"
        bad.write_text("def broken(:\n")
        with pytest.raises(SyntaxError):
            parse_file(str(bad))


class TestParseRepo:
    def test_finds_all_v1_files(self):
        results = parse_repo(str(V1))
        paths = [Path(r["path"]).name for r in results]
        assert "main.py" in paths
        assert "utils.py" in paths
        assert "config.py" in paths
        assert "models.py" in paths

    def test_finds_all_v2_files(self):
        results = parse_repo(str(V2))
        paths = [Path(r["path"]).name for r in results]
        assert "api.py" in paths  # new file in v2

    def test_skips_pycache(self, tmp_path):
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "cached.py").write_text("x = 1")
        (tmp_path / "good.py").write_text("x = 1")
        results = parse_repo(str(tmp_path))
        paths = [r["path"] for r in results]
        assert len(results) == 1
        assert "good.py" in paths[0]

    def test_v2_has_different_files_than_v1(self):
        v1_names = {Path(r["path"]).name for r in parse_repo(str(V1))}
        v2_names = {Path(r["path"]).name for r in parse_repo(str(V2))}
        assert "models.py" in v1_names
        assert "models.py" not in v2_names  # deleted in v2
        assert "api.py" not in v1_names
        assert "api.py" in v2_names  # added in v2

    def test_source_hash_changes_between_versions(self):
        """hash_password changed between v1 and v2 — hashes should differ."""
        v1 = parse_repo(str(V1))
        v2 = parse_repo(str(V2))
        v1_hp = next(
            f for r in v1 for f in r["functions"] if f["name"] == "hash_password"
        )
        v2_hp = next(
            f for r in v2 for f in r["functions"] if f["name"] == "hash_password"
        )
        assert v1_hp["source_hash"] != v2_hp["source_hash"]
