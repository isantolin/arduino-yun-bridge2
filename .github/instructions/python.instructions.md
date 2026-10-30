---
applyTo: "**/*.py,**/*.pyi"
---

Use the Python version and dependency requirements defined by pyproject.toml and requirements/. Inspect tox.ini for validation environments. Prefer existing standard-library and project-library APIs over hand-written equivalents. Use modern typed Python consistent with configured linters and type checkers. Keep asynchronous code non-blocking and use structured concurrency only when its failure/cancellation semantics fit the operation. Add behavior-focused tests with meaningful assertions. Do not impose system-global package installation; use the repository's supported environment tooling.