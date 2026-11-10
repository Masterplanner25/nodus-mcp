# nodus-mcp — Claude instructions

## What this is, and where it sits

The MCP (Model Context Protocol) library for the [Nodus](https://github.com/Masterplanner25/Nodus)
ecosystem. It is a **companion package**, not a standalone project:

- **The language lives at `C:\dev\Coding Language`** (repo `Masterplanner25/Nodus`). Its
  `CLAUDE.md` is the authority for the runtime, the embedding API, capabilities, `.nd`
  syntax and the ecosystem's policies. This repo's entry there is in
  `docs/ecosystem/COMPANION_REPOS.md`.
- **It is a registered dependent of nodus-lang.** The language's release Gate 10a
  (`tools/check_dependent_suites.py`, registry `tools/nodus_lang_dependents.json`) runs this
  suite before every nodus-lang upload, with `PYTHONPATH` pointed at the language's `src/`.
  It has caught a defect here before: 0.1.4 shipped two disagreeing version strings and the
  5.15.0 gate found it (see *Version* below).
- **`tools/check_publish_drift.py`** in the language repo compares this package's PyPI
  release with `main`, and prints `ok — N files identical to X.Y.Z` when they match.
- **Published to PyPI** as `nodus-mcp`. The release notes are on GitHub, and `CHANGELOG.md`
  is the record.

### Two packages in one distribution

`pyproject.toml` (`where = ["src"]`) ships both:

| Package | What | Built on |
|---|---|---|
| `src/nodus_mcp/` | The Nodus-native MCP protocol library: `McpClient`, `McpServer`, stdio and HTTP transports, the `nodus-mcp` CLI, and the `.nd` module (`nodus_mcp/nd/index.nd`, registered through the `nodus.nd` entry point) | Its own JSON-RPC implementation and `httpx`. It does **not** use the official `mcp` SDK |
| `src/nodus_mcp_aindy/` | `NodusServer` (exposes a `ToolRegistry` over MCP) and the client side, `MCPClientAdapter` and `discover_tools` | The **official `mcp` SDK**, plus `starlette`/`uvicorn` for the HTTP transports |

**Most outside demand lands on `nodus_mcp_aindy`.** Its main consumer is **aindy-runtime**
(`C:\dev\aindy-runtime`):
- Its `mcp-server` command *is* a `NodusServer`, in `AINDY/platform_layer/mcp_server.py`.
- Its client side uses `MCPClientAdapter`, in `mcp_client.py`.
- It pins `nodus-mcp` through its `[mcp]` extra, at three sites: `pyproject.toml`,
  `AINDY/requirements.txt`, and the CI `Install MCP extra` step.

When that project asks for something here, it arrives as a handoff document naming its
decision (DEC-…) and feature request (FR-…).

**Sibling, not the same thing:** `nodus-mcp-server` (`C:\dev\nodus-mcp-server`) is a
separate MCP server exposing the Nodus *runtime* (memory, goals, workflows, exec). Both repos
contain an SDK-major port and a streamable-HTTP transport. **Fix a transport bug in both** so
they do not drift.

### Dependencies — what is declared and what is not

- Declared: `nodus-lang>=4.0.0` (uncapped, by ecosystem policy; see below) and `httpx`.
- **Not declared: `mcp`, `starlette`, `uvicorn`.** `nodus_mcp_aindy` imports them lazily or
  behind `try/except ImportError`, and consumers install them. aindy-runtime lists `mcp`
  in its own extra for exactly this reason. Tests that need them `importorskip` or
  `skipif(not _MCP_AVAILABLE)`. **A run without the SDK skips them silently**, so read
  the `skipped` count.
- No module under `src/` imports `nodus` directly. The coupling to nodus-lang is the
  `nodus.nd` entry point, and `tests/test_install_roundtrip.py` checks it resolves through
  `ModuleLoader`.

## Running the tests

nodus-mcp is installed **editable** in the language repo's `.venv`, which has mcp 1.x.
Gate 10a runs the suite the same way:

```powershell
cd C:\dev\nodus-mcp
$env:PYTHONPATH = "C:/dev/Coding Language/src"
& "C:/dev/Coding Language/.venv/Scripts/python.exe" -m pytest tests -q
```

Without `PYTHONPATH` you get whatever nodus-lang that `.venv` has installed, which lags
`src/` by many releases. See the top of the language repo's `CLAUDE.md`.

### Both mcp SDK majors, every transport change

`nodus_mcp_aindy.server` picks one branch per SDK major at import (`_SDK_V2`):
- 2.x registers handlers with `add_request_handler`, and they take `(ctx, params)`.
- 1.x uses the decorator API.

**A change to `NodusServer` or the client adapter is not tested until it has run under
both.** The e2e tests print which branch ran (`test_which_branch_ran_is_visible`), so a
green run on one major is not mistaken for both.

**This box has no ready-made 2.x venv.** Build one, and expect this:

- **Windows Application Control blocks unfamiliar native DLLs.** In a fresh venv,
  pydantic-core 2.50.4's `_pydantic_core` failed with `DLL load failed ... An Application
  Control policy has blocked this file`. Every mcp test then **skipped**, and pytest
  reported success. Pinning `pydantic-core==2.50.2` with `pydantic==2.14.2`, a build the
  policy already allows (aindy-apps-monolith's venv has it), fixed it. Check
  `python -c "import mcp.server"` before trusting a run.
- Recipe (2026-11-09):
  `py -3.11 -m venv <scratch>/v2`, then
  `pip install -e C:/dev/nodus-mcp "mcp==2.3.0" uvicorn pytest`, then
  `pip install "pydantic==2.14.2" "pydantic-core==2.50.2"`.
- **mcp 2.x's client is built on `httpx2`, not `httpx`.** A test passing its own client to
  `streamable_http_client` must build the matching one, as
  `tests/test_streamable_http_end_to_end.py` does.

### Known flake

Two port-binding tests in **`test_phase_m.py`** fail when something else holds the ports.
This is recorded in the language repo's `tools/dependent_flakes.json`, which *classifies* a
red run and never passes one. The check that tells a flake from a regression is a **serial
full-suite re-run**. A green run of the file on its own is weaker evidence than it looks.
**Never run two suites at once.**

## Version — two sources, keep them equal

`pyproject.toml` `version` **and** `src/nodus_mcp/__init__.py` `__version__`. Bump both in
the same commit. 0.1.4 shipped with `__init__.py` still on 0.1.3, so `nodus-mcp version`
reported the wrong release. PyPI is immutable, which is why 0.1.5 exists.
`tests/test_invariants.py::test_version_is_not_dev` asserts they agree, and nothing ran it
between that tag and nodus-lang's Gate 10a.

The CLI command is **`nodus-mcp version`**, a subcommand. `--version` is an argparse error.

## Release

1. Version bump in both sources, plus a `CHANGELOG.md` entry, in the PR that makes the
   change. Date the entry when you release; if that needs its own PR, open one.
2. From a clean tree: `rm -rf build dist src/*.egg-info`, then `python -m build`, then
   `python -m twine check dist/*`. `build/` survives a rebuild and can leak stale files.
3. **Install the built wheel** into a venv with the SDK, copy the e2e test files to a
   directory **outside the repo**, and run them there, so the import resolves to
   `site-packages`. Check the printed `__file__`.
4. `python -m twine upload dist/*`. `~/.pypirc` holds an account-scoped token. **Never write
   a token into any repo file.**
5. Confirm with `pip install --no-cache-dir nodus-mcp==X.Y.Z`. That is the only
   authoritative check, because PyPI's JSON API and simple index lag an upload.
6. **Only after PyPI succeeds:** `gh release create vX.Y.Z --target <FULL 40-char sha>`,
   with notes extracted from the CHANGELOG and the wheel and sdist attached. A short sha
   gets `HTTP 422 target_commitish is invalid`. Releases in this ecosystem become
   immutable, and a tag used by one cannot be reused, so get the target right before
   creating it.
7. Run `tools/check_publish_drift.py` in the language repo. It should report this package
   `ok` at the new version.
8. If a consumer asked for the release, tell them. aindy-runtime raises its pin at its three
   sites (see above).

Write release notes and any file another tool reads with the editor tools, not a
PowerShell `Out-File` that adds a UTF-8 BOM.

## PR workflow

`main` is branch-protected: a PR is required, with 0 approvals and **no required status
checks**. **There is no CI in this repo.** The local runs under both SDK majors are the only
test gate, so state them in the PR body. Branch, push, `gh pr create`, then
`gh pr merge --squash --delete-branch`.

## Gotchas (each cost time at least once)

- **Don't `Mount` an app whose session manager lives in its lifespan.** Starlette never runs
  a mounted app's lifespan, so `StreamableHTTPSessionManager` never starts and every request
  fails with `Task group is not initialized`. `streamable_http_app()` therefore takes
  `routes=`/`middleware=`, and the endpoint is an exact `Route` (a `Mount` would also turn
  `/mcp` into a redirect to `/mcp/`). nodus-mcp-server shipped this bug twice.
- **The SSE transport is two endpoints**: `/sse` (GET stream) and `/messages/` (POST back).
  Without the mount the session never initialises (#7). The SSE handler must return a
  `Response()`, because returning `None` is a Starlette `TypeError` after every session
  (#11).
- **`auth_hook` identity comes from the request scope, never a contextvar.** On mcp 1.x in
  **stateful** streamable HTTP, a contextvar set by middleware is *stale*: every call sees
  the request that opened the session. `scope["user"]`/`scope["auth"]` reach the hook as
  `context["user"]`/`context["auth"]` and are right on both majors and in both modes.
  `test_middleware_contextvar_vs_scope` asserts the whole table.
- **`security_settings=None` means no `Host`/`Origin` checking, on both SDK majors.** Only
  `run(transport="streamable-http")` on a loopback bind turns DNS-rebinding protection on by
  default. Behind a proxy, the host passes settings that list its public name.
- **mcp 2.x renamed `Tool.inputSchema` to `input_schema`.** A `getattr` on the old name
  returned `{}` for every tool with no error (#11). Read under either name.
- **An e2e test beats a route test.** Nothing ran the SSE app against a real client until
  0.1.4; `test_run_sse_app_mounts_messages_endpoint` only inspected routes. Every
  transport has a test that serves it under uvicorn on a free port and drives it with the
  SDK client.
- Design and tech-debt records: `docs/design/` (00–06, with transports in `03`) and
  `docs/governance/TECH_DEBT.md` (TD-001 to TD-010).

## Policy inherited from the ecosystem

- **Do not cap `nodus-lang`.** The language repo's suite and Gate 10a catch a real break. A
  cap turns every nodus-lang major into a two-repo release train, and the `<5.0.0` cap
  that held aindy-runtime on 4.2.0 is why it was floated in 0.1.3.
- **A commit or PR body that says `fix`/`close`/`resolve` near `#N` closes that issue on
  merge**, even after "not". Write "Filed (open):" instead.
