# Cookbook — recipes by profession

← [Home](index.md)

A gallery of real-world prompts grouped by who might use them. Every recipe here is
something `find` or `grep` **can't** express on its own: it reads file *contents* or
*structure*, computes a value, or relates files across a tree. The prompt is free-form —
copy one and adapt the wording to your needs.

For the mechanics (output modes, runtimes, images, piping) see the
[Examples](examples.md) page. This is the "what can I actually ask?" catalog — a trimmed
selection of the broadest-appeal professions.

- [Web developers](#web-developers)
- [Data scientists & data engineers](#data-scientists--data-engineers)
- [AI engineers](#ai-engineers)
- [Authors](#authors)
- [Photographers](#photographers)
- [DevOps & infrastructure](#devops--infrastructure)
- [Solo entrepreneurs](#solo-entrepreneurs)
- [`--extract` — rows from inside files](#extract--rows-from-inside-files)

---

## Web developers

*HTML, CSS, JS/TS, assets, config.*

```bash
# HTML — structure & correctness
nfind "HTML files missing a lang attribute on the <html> element"
nfind "HTML files that contain more than one <h1> element"
nfind "HTML files missing a <meta charset> declaration"
nfind "HTML files whose <title> text is longer than 60 characters"
nfind "HTML files that use deprecated tags such as <center>, <font>, or <marquee>"
nfind "HTML files that embed base64-encoded images inline"
nfind "HTML files with <form> elements that have no action attribute"
nfind "HTML files that contain <img> tags with no alt attribute"
nfind "HTML files with anchor tags whose visible text is 'click here' or 'read more'"
nfind "HTML files that load a script with a crossorigin attribute but no integrity hash"

# CSS / SCSS — quality & hygiene
nfind "CSS files that contain !important declarations"
nfind "CSS files that use vendor prefixes such as -webkit-, -moz-, or -ms-"
nfind "CSS files that define pixel font sizes instead of rem or em"
nfind "SCSS files that use the deprecated @import rule instead of @use or @forward"
nfind "CSS files that contain a * { } universal selector rule"

# JavaScript / TypeScript — code quality
nfind "JavaScript files that contain var declarations"
nfind "JavaScript files that call eval()"
nfind "JavaScript files that call document.write()"
nfind "JavaScript files that import from a path going up more than two levels (../../../)"
nfind "JavaScript files larger than 500 KB that are not minified (filename does not contain .min)"
nfind "TypeScript files that use the any type, using tree-sitter-typescript"
nfind "regular .tsx files that define a React function component returning JSX, using tree-sitter-typescript"
nfind "TypeScript files that export a default, using ts-morph"

# package.json & lock files
nfind "package.json files where the same package appears in both dependencies and devDependencies"
nfind "package.json files that pin a dependency to an exact version (no ^ or ~ prefix)"
nfind "package-lock.json files that resolve a package from a git URL rather than the npm registry"
nfind "yarn.lock files that contain packages resolved over http:// instead of https://"

# Config & environment
nfind "tsconfig.json files that set strict to false"
nfind ".env files that contain values longer than 30 characters that look like secrets"
nfind "Webpack config files that enable source maps in production mode"
nfind "Dockerfile files that expose port 80 or 443"

# Assets
nfind "PNG or JPEG files larger than 500 KB outside directories named 'originals' or 'raw'"
nfind "WebP images that have no JPEG or PNG file with the same stem in the same folder"
nfind "SVG files in directories named 'icons' that contain a <script> element"
```

## Data scientists & data engineers

*Notebooks, tabular data, pipelines.*

```bash
# Jupyter notebooks — state & quality
nfind "Jupyter notebooks that have no output in any cell (never been run)"
nfind "Jupyter notebooks that contain cells with error or traceback outputs"
nfind "Jupyter notebooks with more than 100 cells"
nfind "Jupyter notebooks that contain code cells with TODO comments"
nfind "Jupyter notebooks that import pandas but contain no pd.read_ calls"
nfind "Jupyter notebooks that have output cells larger than 1 MB in total"
nfind "Jupyter notebooks that import matplotlib but never call plt.show() or savefig()"

# CSV / TSV — integrity & encoding
nfind "CSV files that contain a column mixing numeric and non-numeric values (dirty types), using csv"
nfind "CSV files with inconsistent column counts across rows"
nfind "CSV files whose first row appears to be data rather than a header (all values are numeric)"
nfind "CSV files encoded in something other than UTF-8"
nfind "CSV files with duplicate column names in the header row"

# Parquet / Avro / ORC — schema & size
nfind "Parquet files with more than 100 row groups, using pyarrow"
nfind "Parquet files whose schema contains a column of type binary, using pyarrow"
nfind "Parquet files that have no column statistics (uncompressed footer), using pyarrow"
nfind "Avro files whose schema declares more than 20 fields, using fastavro"

# Python data science scripts — anti-patterns
nfind "Python files that import pandas and call df.iterrows()"
nfind "Python files that call pickle.load() or pickle.loads()"
nfind "Python files that use pd.read_csv() with no dtype argument"
nfind "Python files that call train_test_split with no random_state argument"
nfind "Python files that contain hardcoded absolute paths (strings starting with /home/ or /Users/)"
nfind "Python files that call pd.DataFrame.append() (deprecated since pandas 2.0)"

# SQL — content & quality
nfind "SQL files that contain SELECT * queries"
nfind "SQL files that contain DROP TABLE or DROP DATABASE statements"
nfind "SQL files that define more than 10 CTEs in a single query"
nfind "SQL files that have no WHERE clause on a DELETE or UPDATE statement"

# SQLite & JSON
nfind "SQLite databases that contain more than 20 tables, using sqlite3"
nfind "SQLite databases with no indexes defined on any table, using sqlite3"
nfind "JSONL files where any line fails to parse as JSON"
nfind "JSON files that look like GeoJSON (contain a 'type': 'FeatureCollection' key)"

# Pipeline & config
nfind "Airflow DAG Python files that set schedule_interval to None"
nfind "YAML files that appear to be GitHub Actions workflows and have no timeout-minutes set on any job"
nfind "Pickle files that raise an exception when loaded (corrupted or incompatible protocol), using pickle"
```

## AI engineers

*Models, datasets, prompts, pipelines.*

```bash
# Model artifacts — checkpoints & weights
nfind "PyTorch .pt or .pth checkpoint files larger than 500 MB"
nfind "PyTorch checkpoint files that contain an 'optimizer_state_dict' key, using torch"
nfind "SafeTensors files larger than 1 GB"
nfind "GGUF model files (extension .gguf) larger than 4 GB"
nfind "HuggingFace model directories that contain both a config.json and pytorch_model.bin"
nfind "adapter_config.json files that set 'peft_type' to 'LORA' (LoRA adapter configs)"

# Training & fine-tuning datasets
nfind "JSONL files where every line contains both a 'prompt' and a 'completion' key (OpenAI fine-tune format)"
nfind "JSONL files where any line contains a 'messages' key (chat-format training data)"
nfind "JSONL files where any line has a 'completion' value longer than 4000 characters"
nfind "CSV files whose header contains both 'question' and 'answer' columns (QA dataset format)"
nfind "JSONL dataset files that contain fewer than 100 lines (too small for fine-tuning)"

# LLM application code — SDK usage patterns
nfind "Python files that import openai and call client.chat.completions.create without a timeout argument"
nfind "Python files that call openai.ChatCompletion.create (deprecated v0 API)"
nfind "Python files that hardcode an OpenAI or Anthropic API key as a string literal"
nfind "Python files that import langchain and use LLMChain (deprecated pattern)"
nfind "Python files that call model.generate() or pipeline() from the transformers library"

# Prompt templates & evaluation
nfind "text or Markdown files inside directories named 'prompts' or 'system_prompts'"
nfind "YAML files that contain a top-level 'system_prompt' or 'system_message' key"
nfind "Python files that define a multiline string variable whose name contains 'PROMPT' or 'SYSTEM'"
nfind "CSV files whose header contains both 'expected' and 'actual' or 'predicted' columns"

# Vector stores & embeddings
nfind "Python files that import chromadb or pinecone or weaviate or qdrant_client"
nfind "NumPy .npy or .npz files larger than 100 MB (likely embedding caches)"
nfind "FAISS index files (extension .faiss or .index) larger than 200 MB"
```

## Authors

*Manuscripts, documents, ebooks, bibliography.*

```bash
# Markdown — structure & front matter
nfind "Markdown files with no YAML front matter"
nfind "Markdown files that contain placeholder text such as [TODO], [TK], or [PLACEHOLDER]"
nfind "Markdown files that contain the phrase 'lorem ipsum'"
nfind "Markdown files that contain footnote markers but no footnote definitions"
nfind "Markdown files that contain broken image references (the image path does not exist)"
nfind "Markdown files whose word count is under 100 (likely stubs or empty chapters)"

# Plain text — encoding & hygiene
nfind "text files that contain Windows-style line endings (CRLF)"
nfind "text files that contain non-breaking spaces (Unicode U+00A0)"
nfind "text files that contain smart quotes mixed with straight quotes"

# DOCX — content & metadata
nfind "DOCX files that contain tracked changes, using python-docx"
nfind "DOCX files that contain comments, using python-docx"
nfind "DOCX files with fewer than 100 words (likely empty or stub chapters), using python-docx"
nfind "DOCX files whose author metadata field is empty, using python-docx"
nfind "DOCX files, with their word count" --json

# EPUB — structure & metadata
nfind "EPUB files that are missing a cover image"
nfind "EPUB files whose metadata contains no ISBN"
nfind "EPUB files that are missing a table of contents (NCX or Nav document)"
nfind "EPUB files that reference a CSS stylesheet not present in the archive"

# Versioning, backups & bibliography
nfind "files whose names contain 'draft' or 'wip'"
nfind "directories that contain both a 'draft' and a 'final' file with the same stem"
nfind "BibTeX .bib files that contain entries with no year field"
nfind "BibTeX .bib files with duplicate citation keys"
nfind "Markdown files that contain citation markers like [@ref] but no .bib file in the same directory"
```

## Photographers

*Image libraries, EXIF, sidecars.*

```bash
# Discovery & cross-file relationships
nfind "JPEG files whose pixel dimensions exceed 8000 on the long side, using pillow"
nfind "RAW files larger than 50 MB that have no JPEG or HEIC preview with the same stem in the same folder"
nfind "HEIC files that have no JPEG with the same stem in the same folder"
nfind "directories that contain both RAW files and JPEG files with the same stems (already converted)"

# EXIF — camera & lens
nfind "JPEG files with no EXIF data at all, using pillow"
nfind "JPEG files shot with a camera model that contains 'iPhone', using pillow"
nfind "JPEG files with an ISO value above 6400, using pillow"
nfind "JPEG files with a shutter speed slower than 1/30 s (motion blur risk), using pillow"
nfind "JPEG files whose EXIF orientation tag is not 1 (rotated but not baked in), using pillow"

# EXIF — time & GPS
nfind "JPEG files that have no DateTimeOriginal EXIF tag, using pillow"
nfind "JPEG files whose DateTimeOriginal hour is between 0 and 5 (night shots), using pillow"
nfind "JPEG files that have GPS coordinates in their EXIF data, using pillow"
nfind "JPEG files that are missing GPS EXIF tags but were taken after 2015 (likely a phone with location off), using pillow"

# Duplicates, sidecars & edits
nfind "JPEG files whose filename is a default camera name pattern such as IMG_, DSC_, or DSCF_ followed by digits"
nfind "directories that contain more than 500 JPEG or RAW files (unorganised bulk imports)"
nfind "RAW files (extension .cr2, .cr3, .nef, .arw) that have no .xmp sidecar with the same stem"
nfind ".xmp sidecar files that have no matching RAW or JPEG file with the same stem (orphaned sidecars)"
nfind "PNG files that have an alpha channel but contain no transparent pixels, using pillow"
```

## DevOps & infrastructure

*Kubernetes, Terraform, CI/CD, Docker.*

```bash
# Kubernetes manifests — safety & correctness
nfind "YAML files that look like Kubernetes Deployment or StatefulSet manifests with no resource limits set"
nfind "YAML files that look like Kubernetes manifests that set image: to a tag containing 'latest'"
nfind "YAML files that look like Kubernetes Pod or Deployment specs that set privileged: true in a securityContext"
nfind "YAML files that look like Kubernetes Deployment manifests with no liveness or readiness probe defined"
nfind "YAML files that look like Kubernetes manifests that define a container running as root (runAsUser: 0)"
nfind "YAML files that look like Kubernetes Secret manifests that store data as plain text rather than base64"

# Terraform — hygiene & security
nfind "Terraform .tf files that contain a hardcoded AWS account ID (12-digit number in a string)"
nfind "Terraform .tf files that define an aws_s3_bucket resource with no server_side_encryption_configuration block"
nfind "Terraform .tf files that define an aws_security_group rule with cidr_blocks = [\"0.0.0.0/0\"] for port 22 or 3389"
nfind "Terraform .tf files that have no required_version constraint in a terraform block"
nfind ".tfvars files that contain values that look like secrets (long alphanumeric strings or strings containing 'password', 'secret', or 'key')"

# GitHub Actions & GitLab CI
nfind "YAML files that look like GitHub Actions workflows and use a third-party action pinned to a branch name rather than a commit SHA"
nfind "YAML files that look like GitHub Actions workflows that set an env: variable whose name contains 'SECRET', 'PASSWORD', or 'TOKEN'"
nfind "YAML files that look like GitHub Actions workflows with no permissions: block at the job or workflow level"
nfind "YAML files that look like GitHub Actions workflows that run on pull_request_target with a checkout step (potential code injection)"

# Dockerfile & Docker Compose
nfind "Dockerfile files that use FROM with a base image tag of 'latest' rather than a pinned version"
nfind "Dockerfile files that run as root and have no USER instruction before the final CMD or ENTRYPOINT"
nfind "Dockerfile files with no HEALTHCHECK instruction"
nfind "Docker Compose YAML files that define a service with privileged: true"
nfind "Docker Compose YAML files that hardcode a password or secret in an environment: block rather than using a secrets: reference"

# Ansible
nfind "Ansible YAML playbook files that use become: yes without specifying become_user"
nfind "Ansible YAML task files that use the shell or command module with a command containing sudo"
```

## Solo entrepreneurs

*Invoices, contracts, bookkeeping, clients, leads.*

```bash
# Invoices & receipts — completeness and tax hygiene (using pypdf)
nfind "PDF files that mention 'invoice' but contain no tax, VAT, or GST line, using pypdf"
nfind "PDF invoices that contain no invoice number (no 'Invoice #', 'No.', or 'Rechnungsnr' label), using pypdf"
nfind "PDF files that mention 'invoice' and 'due' but whose due date is in the past (overdue), using pypdf"
nfind "PDF receipts that contain no extractable text (scanned images that bookkeeping software can't read), using pypdf"
nfind "PDF invoices, with the invoice number, date, and total amount extracted from each, using pypdf" --json

# Bookkeeping spreadsheets & exports (using openpyxl / csv)
nfind "XLSX spreadsheets that contain a cell with a formula error (#REF!, #DIV/0!, or #VALUE!), using openpyxl"
nfind "CSV exports from Stripe or PayPal that contain a 'refund' or 'chargeback' row, using csv"
nfind "CSV bank or payment statement files that contain negative amounts (outgoing or reversed transactions), using csv"

# Contracts & agreements — signed, dated, protected
nfind "DOCX or PDF files that mention 'agreement' or 'contract' but contain an unfilled signature placeholder (a line of underscores or '[Signature]'), using pypdf"
nfind "DOCX contracts that still contain tracked changes or comments (not finalised), using python-docx"
nfind "PDF files that contain the word 'confidential' or 'NDA' but are not password-protected, using pypdf"

# Clients & projects — cross-file relationships
nfind "client directories that contain a signed contract PDF but no invoice file with a matching stem (delivered work never billed)"
nfind "directories that contain a 'proposal' or 'quote' file but no later 'invoice' file with the same stem (leads that never converted)"

# Leads, mailing lists & CRM exports (using csv)
nfind "CSV mailing-list or CRM exports that contain duplicate email addresses, using csv"
nfind "CSV contact exports that contain malformed email addresses (no @ or no domain), using csv"

# Money-related secrets — credential hygiene
nfind "files that contain a live Stripe secret key (a string starting with 'sk_live_')"
nfind ".env or config files that contain an API key or secret for a payment, email, or hosting provider"
```

## Extract — rows from inside files

The `--extract` flag turns the recipe inside out. Where every recipe above returns a subset of **files**, `--extract` explodes a
list-valued field into many **rows per file** — the TODO, the function, the field, the
citation itself. It needs parsing or judgment a `grep` pattern can't express, and the
payload rides in named fields (`line` appears only when a row is line-anchored). See
[Output modes](output-modes.md) for the row shape.

```bash
# Code — structural matches grep can't express (parsed, not pattern-matched)
nfind --extract "every place an exception is caught and silently swallowed, with the file, line, and enclosing function, using tree-sitter-python" ./src
nfind --extract "all SQL queries built by string concatenation or f-strings (injection risk), with file, line, and the offending expression, using tree-sitter-python" ./src
nfind --extract "every public function or method with no docstring, with its name, line, and signature, using tree-sitter-python" ./src
nfind --extract "all TODO/FIXME/HACK comments, each classified as bug, cleanup, or perf, with file, line, and text" ./src

# Secrets & risk — the row is the finding, with provenance
nfind --extract "every string that looks like a live credential (Stripe sk_live_, AWS AKIA, JWT, private-key header), with file, line, and which provider it belongs to" .
nfind --extract "all PDF text that looks like a Social Security or credit-card number, with the file and page, using pypdf" ./discovery

# Data — schema and values pulled out as rows
nfind --extract "every distinct column header across all CSV files, with the column name and which files contain it, using csv" ./data
nfind --extract "each Parquet column, with its name, type, and the file it belongs to, using pyarrow" ./warehouse
nfind --extract "every cell holding a formula error (#REF!, #DIV/0!, #VALUE!), with file, sheet, and cell address, using openpyxl" ./books

# AI engineering — pull the prompts and model calls out of the code
nfind --extract "every model name passed to an openai, anthropic, or litellm call, with file, line, and the model string, using tree-sitter-python" ./src
```

Counting or answering is left to the pipe you already use — `nfind --extract … | wc -l`
counts matches; `--json` shows the same records nested instead of streamed.
