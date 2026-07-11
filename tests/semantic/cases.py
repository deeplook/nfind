"""Small, deterministic filesystem cases for semantic evaluations.

The cases commit three equivalent prompts and their shared ground truth.
``materialize_case`` creates the filesystem without model involvement; a separate,
opt-in evaluator can ask a model to generate each filter and compare its relative
results with ``expected``.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TextFile:
    path: str
    contents: str


@dataclass(frozen=True)
class BinaryAsset:
    path: str
    asset: str


CaseEntry = TextFile | BinaryAsset


@dataclass(frozen=True)
class SemanticCase:
    id: str
    query: str
    variants: tuple[str, str]
    entries: tuple[CaseEntry, ...]
    expected: frozenset[str]
    dependencies: tuple[str, ...] = ()

    @property
    def prompts(self) -> tuple[str, str, str]:
        """Return the canonical, conversational, and terse equivalent prompts."""
        return (self.query, *self.variants)


PYTHON_IMPORT_CASE = SemanticCase(
    id="python-files-importing-requests",
    query="Python files that import requests",
    variants=(
        "Which Python modules directly import the requests package?",
        "Find .py files importing requests",
    ),
    entries=(
        TextFile("src/client.py", "import requests\n\nrequests.get('https://example.test')\n"),
        TextFile("src/models.py", "from dataclasses import dataclass\n"),
        TextFile("src/web.js", "import requests from './requests.js';\n"),
        TextFile("README.md", "The requests package is used by the client.\n"),
    ),
    expected=frozenset({"src/client.py"}),
)


MP3_ALBUM_CASE = SemanticCase(
    id="mp3-files-by-id3-album",
    query="MP3 files whose ID3 album is exactly Night Signals",
    variants=(
        "Which MP3 tracks have the ID3 album tag Night Signals?",
        'Find .mp3 files with album tag equal to "Night Signals"',
    ),
    entries=(
        BinaryAsset("music/match.mp3", "night-signals.mp3"),
        BinaryAsset("music/other.mp3", "day-signals.mp3"),
        TextFile("music/notes.txt", "Album: Night Signals\n"),
    ),
    expected=frozenset({"music/match.mp3"}),
    dependencies=("mutagen",),
)


AUDIO_ONLY_DIRECTORY_CASE = SemanticCase(
    id="nonempty-directories-containing-only-audio-files",
    query="Non-empty directories whose direct files are all MP3 or FLAC audio files",
    variants=(
        "Which non-empty folders contain only MP3 or FLAC files directly inside them?",
        "Find non-empty dirs with only direct .mp3/.flac files",
    ),
    entries=(
        TextFile("albums/live/one.mp3", "audio placeholder\n"),
        TextFile("albums/live/two.flac", "audio placeholder\n"),
        TextFile("albums/mixed/one.mp3", "audio placeholder\n"),
        TextFile("albums/mixed/cover.jpg", "image placeholder\n"),
        TextFile("albums/notes/readme.txt", "not audio\n"),
    ),
    expected=frozenset({"albums/live"}),
)


TODO_CONTENT_CASE = SemanticCase(
    id="source-files-containing-todo-comments",
    query="Source files containing a TODO comment, but not documentation mentioning TODO",
    variants=(
        "Which source-code files have TODO comments, excluding documentation?",
        "Find source files with TODO comments; ignore docs",
    ),
    entries=(
        TextFile("src/app.py", "def run():\n    pass  # TODO: retry failures\n"),
        TextFile("src/clean.py", "def done():\n    return True\n"),
        TextFile("docs/process.md", "Use TODO comments to mark unfinished work.\n"),
        TextFile("data/blob.bin", "\x00TODO\xff\n"),
    ),
    expected=frozenset({"src/app.py"}),
)


ASYNC_FUNCTION_CASE = SemanticCase(
    id="python-files-defining-async-functions",
    query="Python files that define at least one async function",
    variants=(
        "Which Python modules contain an async function definition?",
        "Find .py files defining async def",
    ),
    entries=(
        TextFile("src/fetch.py", "async def fetch(url: str) -> bytes:\n    return b''\n"),
        TextFile("src/sync.py", "def fetch(url: str) -> bytes:\n    return b''\n"),
        TextFile("tests/test_words.py", "TEXT = 'async def example(): pass'\n"),
    ),
    expected=frozenset({"src/fetch.py"}),
)


JSON_VALUE_CASE = SemanticCase(
    id="json-files-with-enabled-boolean",
    query='JSON files whose top-level "enabled" value is the boolean true',
    variants=(
        "Which JSON files have a top-level enabled field set to boolean true?",
        "Find .json with root enabled === true",
    ),
    entries=(
        TextFile("config/enabled.json", '{"enabled": true, "name": "live"}\n'),
        TextFile("config/string.json", '{"enabled": "true"}\n'),
        TextFile("config/nested.json", '{"settings": {"enabled": true}}\n'),
        TextFile("config/broken.json", '{"enabled": true,}\n'),
    ),
    expected=frozenset({"config/enabled.json"}),
)


SVG_DIMENSION_CASE = SemanticCase(
    id="svg-images-wider-than-1000-pixels",
    query="SVG images with an explicit width greater than 1000 pixels",
    variants=(
        "Which SVG files explicitly declare a width above 1000 pixels?",
        "Find .svg with width > 1000px",
    ),
    entries=(
        TextFile("images/banner.svg", '<svg width="1200" height="300"></svg>\n'),
        TextFile("images/icon.svg", '<svg width="32" height="32"></svg>\n'),
        TextFile("images/viewbox.svg", '<svg viewBox="0 0 1600 900"></svg>\n'),
        TextFile("images/fake.txt", '<svg width="5000"></svg>\n'),
    ),
    expected=frozenset({"images/banner.svg"}),
)


MULTI_ROOT_CASE = SemanticCase(
    id="same-named-configs-across-roots",
    query='Config files named settings.ini whose mode value is exactly "production"',
    variants=(
        "Which settings.ini files set mode to production?",
        'Find settings.ini where mode == "production"',
    ),
    entries=(
        TextFile("root-a/settings.ini", "mode=production\n"),
        TextFile("root-b/settings.ini", "mode=development\n"),
        TextFile("root-c/nested/settings.ini", "mode = production\n"),
        TextFile("root-c/settings.txt", "mode=production\n"),
    ),
    expected=frozenset({"root-a/settings.ini", "root-c/nested/settings.ini"}),
)


UNICODE_CASE = SemanticCase(
    id="unicode-filenames-and-contents",
    query='Text files whose contents contain the exact word "Grüße"',
    variants=(
        'Which text files contain the exact Unicode word "Grüße"?',
        'Find .txt containing "Grüße" exactly',
    ),
    entries=(
        TextFile("notes/überblick.txt", "Viele Grüße aus Berlin.\n"),
        TextFile("notes/plain.txt", "Viele Gruesse aus Berlin.\n"),
        TextFile("notes/日本語.txt", "Grüße und こんにちは.\n"),
    ),
    expected=frozenset({"notes/überblick.txt", "notes/日本語.txt"}),
)


FILE_SIZE_CASE = SemanticCase(
    id="text-files-larger-than-one-kibibyte",
    query="Regular .txt files larger than 1024 bytes",
    variants=(
        "Which regular files with a .txt extension are bigger than one kibibyte?",
        "Find regular .txt files > 1024 bytes",
    ),
    entries=(
        TextFile("data/large.txt", "x" * 1025),
        TextFile("data/exact.txt", "x" * 1024),
        TextFile("data/small.txt", "x" * 20),
        TextFile("data/large.log", "x" * 2048),
    ),
    expected=frozenset({"data/large.txt"}),
)


CSV_VALUE_CASE = SemanticCase(
    id="csv-files-with-overdue-rows",
    query='CSV files containing a row whose status is "overdue"',
    variants=(
        "Which CSV documents have at least one row with status overdue?",
        'Find .csv with a row where status == "overdue"',
    ),
    entries=(
        TextFile("reports/open.csv", "id,status\n1,paid\n2,overdue\n"),
        TextFile("reports/closed.csv", "id,status\n1,paid\n2,cancelled\n"),
        TextFile("reports/note.csv", 'id,note\n1,"status,overdue"\n'),
        TextFile("reports/readme.txt", "status,overdue\n"),
    ),
    expected=frozenset({"reports/open.csv"}),
)


XML_ATTRIBUTE_CASE = SemanticCase(
    id="xml-items-with-disabled-attribute",
    query='Well-formed XML files containing an item element with disabled="true"',
    variants=(
        'Which valid XML documents contain an item whose disabled attribute equals "true"?',
        'Find well-formed .xml where an item element has disabled="true"',
    ),
    entries=(
        TextFile("xml/disabled.xml", '<root><item disabled="true" /></root>\n'),
        TextFile("xml/enabled.xml", '<root><item disabled="false" /></root>\n'),
        TextFile("xml/text.xml", '<root><item>disabled="true"</item></root>\n'),
        TextFile("xml/broken.xml", '<root><item disabled="true"></root>\n'),
    ),
    expected=frozenset({"xml/disabled.xml"}),
)


MARKDOWN_HEADING_CASE = SemanticCase(
    id="markdown-files-with-install-heading",
    query='Markdown files with a level-two heading exactly named "Installation"',
    variants=(
        "Which Markdown documents have a real H2 titled exactly Installation, outside code fences?",
        'Find .md with an actual "## Installation" heading; ignore fenced code examples',
    ),
    entries=(
        TextFile("docs/guide.md", "# Guide\n\n## Installation\n\nRun the installer.\n"),
        TextFile("docs/intro.md", "# Installation\n\nWelcome.\n"),
        TextFile("docs/details.md", "## Installation details\n"),
        TextFile("docs/code.md", "```markdown\n## Installation\n```\n"),
    ),
    expected=frozenset({"docs/guide.md"}),
)


JAVASCRIPT_EXPORT_CASE = SemanticCase(
    id="javascript-files-with-default-export",
    query="JavaScript files that contain a default export declaration",
    variants=(
        "Which JavaScript modules declare a default export?",
        "Find .js files that syntactically declare export default; ignore comments",
    ),
    entries=(
        TextFile("web/app.js", "export default function app() {}\n"),
        TextFile("web/named.js", "export function helper() {}\n"),
        TextFile("web/common.cjs", "module.exports = function app() {};\n"),
        TextFile("web/comment.js", "// export default is intentionally absent\n"),
    ),
    expected=frozenset({"web/app.js"}),
)


TOML_DEPENDENCY_CASE = SemanticCase(
    id="pyproject-files-depending-on-typer",
    query="pyproject.toml files declaring typer as a project dependency",
    variants=(
        "Which Python project manifests list typer in project dependencies?",
        "Find pyproject.toml with project dependency typer",
    ),
    entries=(
        TextFile(
            "apps/cli/pyproject.toml",
            '[project]\nname = "cli"\ndependencies = ["typer>=0.12", "rich"]\n',
        ),
        TextFile(
            "apps/web/pyproject.toml",
            '[project]\nname = "web"\ndependencies = ["fastapi"]\n',
        ),
        TextFile("apps/note/pyproject.toml", '# dependencies = ["typer"]\n'),
    ),
    expected=frozenset({"apps/cli/pyproject.toml"}),
)


DATED_FILENAME_CASE = SemanticCase(
    id="dated-backups-before-2024",
    query="Backup files with an ISO date in their filename earlier than 2024-01-01",
    variants=(
        "Which .bak files have a filename date before January 1, 2024?",
        "Find dated .bak names with date < 2024-01-01",
    ),
    entries=(
        TextFile("backups/db-2023-12-31.bak", "old\n"),
        TextFile("backups/db-2024-01-01.bak", "threshold\n"),
        TextFile("backups/db-2025-02-10.bak", "new\n"),
        TextFile("backups/db-final.bak", "undated\n"),
        TextFile("backups/db-2023-01-01.txt", "wrong extension\n"),
    ),
    expected=frozenset({"backups/db-2023-12-31.bak"}),
)


PROJECT_DIRECTORY_CASE = SemanticCase(
    id="directories-containing-readme-and-pyproject",
    query="Directories that directly contain both README.md and pyproject.toml",
    variants=(
        "Which folders have both README.md and pyproject.toml as direct children?",
        "Find dirs directly containing README.md + pyproject.toml",
    ),
    entries=(
        TextFile("projects/complete/README.md", "# Complete\n"),
        TextFile("projects/complete/pyproject.toml", '[project]\nname = "complete"\n'),
        TextFile("projects/readme-only/README.md", "# Partial\n"),
        TextFile("projects/nested/README.md", "# Parent\n"),
        TextFile("projects/nested/config/pyproject.toml", '[project]\nname = "nested"\n'),
    ),
    expected=frozenset({"projects/complete"}),
)


PYTHON_DATACLASS_CASE = SemanticCase(
    id="python-files-defining-dataclasses",
    query="Python files defining a class decorated with @dataclass",
    variants=(
        "Which Python modules define at least one dataclass-decorated class?",
        "Find .py defining an @dataclass class",
    ),
    entries=(
        TextFile(
            "models/user.py",
            "from dataclasses import dataclass\n\n@dataclass\nclass User:\n    name: str\n",
        ),
        TextFile("models/plain.py", "class User:\n    name: str\n"),
        TextFile("models/comment.py", "# @dataclass\nclass Comment:\n    pass\n"),
    ),
    expected=frozenset({"models/user.py"}),
)


LOG_TIMESTAMP_CASE = SemanticCase(
    id="logs-with-errors-after-cutoff",
    query="Log files containing an ERROR entry after 2025-06-01T12:00:00Z",
    variants=(
        "Which log files have an ERROR later than noon UTC on June 1, 2025?",
        "Find .log with ERROR timestamp > 2025-06-01T12:00:00Z",
    ),
    entries=(
        TextFile("logs/recent.log", "2025-06-01T12:00:01Z ERROR connection lost\n"),
        TextFile("logs/old.log", "2025-06-01T11:59:59Z ERROR connection lost\n"),
        TextFile("logs/warning.log", "2025-06-02T09:00:00Z WARNING connection slow\n"),
        TextFile("logs/mixed.log", "ERROR 2025-06-03 without standard ordering\n"),
    ),
    expected=frozenset({"logs/recent.log"}),
)


PACKAGE_SCRIPT_CASE = SemanticCase(
    id="package-json-files-with-test-script",
    query='package.json files whose scripts object defines a "test" command',
    variants=(
        "Which package.json manifests define a test script?",
        'Find package.json where scripts has key "test"',
    ),
    entries=(
        TextFile("packages/core/package.json", '{"scripts": {"test": "vitest"}}\n'),
        TextFile("packages/site/package.json", '{"scripts": {"build": "vite build"}}\n'),
        TextFile("packages/string/package.json", '{"description": "run test manually"}\n'),
        TextFile("packages/broken/package.json", '{"scripts": {"test": }}\n'),
    ),
    expected=frozenset({"packages/core/package.json"}),
)


EXAMPLE_CASES = (
    PYTHON_IMPORT_CASE,
    MP3_ALBUM_CASE,
    AUDIO_ONLY_DIRECTORY_CASE,
    TODO_CONTENT_CASE,
    ASYNC_FUNCTION_CASE,
    JSON_VALUE_CASE,
    SVG_DIMENSION_CASE,
    MULTI_ROOT_CASE,
    UNICODE_CASE,
    FILE_SIZE_CASE,
    CSV_VALUE_CASE,
    XML_ATTRIBUTE_CASE,
    MARKDOWN_HEADING_CASE,
    JAVASCRIPT_EXPORT_CASE,
    TOML_DEPENDENCY_CASE,
    DATED_FILENAME_CASE,
    PROJECT_DIRECTORY_CASE,
    PYTHON_DATACLASS_CASE,
    LOG_TIMESTAMP_CASE,
    PACKAGE_SCRIPT_CASE,
)

EXAMPLE_PROMPTS = tuple(
    (case, prompt_index, prompt)
    for case in EXAMPLE_CASES
    for prompt_index, prompt in enumerate(case.prompts)
)


def materialize_case(case: SemanticCase, root: Path) -> None:
    """Create ``case`` below ``root`` using only checked-in specifications/assets."""
    assets = Path(__file__).with_name("assets")
    for entry in case.entries:
        destination = root / entry.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(entry, TextFile):
            destination.write_text(entry.contents)
        else:
            shutil.copyfile(assets / entry.asset, destination)
