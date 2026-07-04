from __future__ import annotations

import json

import pytest

from nfind import cli
from nfind.extract import iter_extract_rows, render_extract_row, select_list_field


class TestSelectListField:
    def test_sole_list_field_is_chosen(self):
        record = {"path": "a.py", "todos": [{"line": 1}]}

        assert select_list_field(record, None) == "todos"

    def test_no_list_field_returns_none(self):
        record = {"path": "a.py", "version": "2.3.1"}

        assert select_list_field(record, None) is None

    def test_multiple_list_fields_raise_naming_candidates(self):
        record = {"path": "a.py", "todos": [], "urls": []}

        with pytest.raises(ValueError, match="todos, urls"):
            select_list_field(record, None)

    def test_named_field_overrides_ambiguity(self):
        record = {"path": "a.py", "todos": [{"line": 1}], "urls": []}

        assert select_list_field(record, "todos") == "todos"

    def test_named_field_absent_on_record_returns_none(self):
        record = {"path": "a.py", "version": "2.3.1"}

        assert select_list_field(record, "todos") is None


class TestRenderExtractRow:
    def test_line_anchored_text(self):
        row = render_extract_row("src/app.py", {"line": 42, "text": "handle retry"})

        assert row == "src/app.py:42\thandle retry"

    def test_line_and_col(self):
        row = render_extract_row("src/app.py", {"line": 42, "col": 5, "text": "x"})

        assert row == "src/app.py:42:5\tx"

    def test_named_value_field_without_line(self):
        row = render_extract_row("pkg/package.json", {"version": "2.3.1"})

        assert row == "pkg/package.json\tversion=2.3.1"

    def test_text_then_extra_fields(self):
        row = render_extract_row("a.py", {"line": 7, "text": "todo", "kind": "bug"})

        assert row == "a.py:7\ttodo, kind=bug"

    def test_scalar_element(self):
        row = render_extract_row("config.ini", "http://example.com")

        assert row == "config.ini\thttp://example.com"

    def test_missing_line_omits_prefix(self):
        row = render_extract_row("a.py", {"text": "no line here"})

        assert row == "a.py\tno line here"


class TestIterExtractRows:
    def test_explodes_across_records(self):
        records = [
            {"path": "a.py", "todos": [{"line": 1, "text": "x"}, {"line": 2, "text": "y"}]},
            {"path": "b.py", "todos": [{"line": 3, "text": "z"}]},
        ]

        rows = list(iter_extract_rows(records, None))

        assert rows == ["a.py:1\tx", "a.py:2\ty", "b.py:3\tz"]

    def test_record_without_list_field_degrades_to_one_line(self):
        records = [{"path": "a.py", "version": "1.0"}]

        assert list(iter_extract_rows(records, None)) == ["a.py\tversion=1.0"]

    def test_record_with_no_extras_degrades_to_path(self):
        assert list(iter_extract_rows([{"path": "a.py"}], None)) == ["a.py"]


class TestEmitExtract:
    _RECORDS = [{"path": "a.py", "todos": [{"line": 1, "text": "x"}, {"line": 2, "text": "y"}]}]

    def test_extract_text_output(self, capsys):
        cli._emit(self._RECORDS, as_json=False, fields=False, print0=False, extract=True)

        out = capsys.readouterr().out
        assert out == "a.py:1\tx\na.py:2\ty\n"

    def test_extract_print0_separates_rows_with_nul(self, capsys):
        cli._emit(self._RECORDS, as_json=False, fields=False, print0=True, extract=True)

        out = capsys.readouterr().out
        assert out == "a.py:1\tx\0a.py:2\ty\0"

    def test_json_stays_nested_even_with_extract(self, capsys):
        cli._emit(self._RECORDS, as_json=True, fields=False, print0=False, extract=True)

        out = capsys.readouterr().out
        assert '"results"' in out
        assert '"todos"' in out
        assert "\t" not in out

    def test_ambiguous_field_raises(self):
        records = [{"path": "a.py", "todos": [], "urls": []}]

        with pytest.raises(ValueError, match="--extract-field"):
            cli._emit(records, as_json=False, fields=False, print0=False, extract=True)

    def test_max_items_emits_complete_rows_and_warns(self, capsys):
        cli._emit(
            self._RECORDS,
            as_json=False,
            fields=False,
            print0=False,
            extract=True,
            max_items=1,
        )

        captured = capsys.readouterr()
        assert captured.out == "a.py:1\tx\n"
        assert "max-items" in captured.err

    def test_max_output_bytes_never_emits_partial_row(self, capsys):
        cli._emit(
            self._RECORDS,
            as_json=False,
            fields=False,
            print0=False,
            extract=True,
            max_output_bytes=len(b"a.py:1\tx\n"),
        )

        captured = capsys.readouterr()
        assert captured.out == "a.py:1\tx\n"
        assert "max-output-bytes" in captured.err

    def test_json_limits_records_and_stays_valid(self, capsys):
        records = [{"path": "a"}, {"path": "b"}]
        cli._emit(
            records,
            as_json=True,
            fields=False,
            print0=False,
            max_results=1,
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "count": 1,
            "results": [{"path": "a"}],
            "truncated": True,
            "truncated_by": ["max-results"],
        }

    def test_json_output_byte_limit_remains_valid(self, capsys):
        records = [{"path": "a", "text": "x" * 500}, {"path": "b"}]
        cli._emit(
            records,
            as_json=True,
            fields=False,
            print0=False,
            max_output_bytes=250,
        )

        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert len(captured.out.encode()) <= 250
        assert payload["truncated"] is True
        assert "max-output-bytes" in payload["truncated_by"]
