# Open Terminal workspace

You run inside the Open Terminal container, as its normal unprivileged user.
Work in the directory selected for this chat. Ask before accessing unrelated
projects, credentials, or private files. Do not treat the shared home directory
as an empty disposable workspace.

Shell commands require approval except basic inspection explicitly allowed by
the configuration. Plain `git diff` and normal viewing options are allowed.
Output-writing (`--output`), explicit external helpers (`--ext-diff`), text
conversion (`--textconv`), outside-index comparisons (`--no-index`), and order
files (`-O`) require approval. Matching includes common option prefixes and can
also ask on filenames that resemble those options; do not work around a prompt.
Prefer `--no-ext-diff --no-textconv` when reviewing an untrusted repository,
because plain Git can run configured helpers implicitly. These convenience
rules are guardrails, not a complete Git sandbox or exhaustive option parser.
Never bypass approvals through another tool. Do not use sudo,
install host-control tools, modify the OpenCode installation, or read/export
authentication state. Ask before deleting files, pushing commits, publishing,
or changing system configuration.

No host MCP servers are configured. Only Alloy, tier-1 through tier-4, and
Alloy's session-model plugin are explicitly shared read-only. Do not assume
other host paths, Docker access, agents, skills, plugins, credentials, or
persistent memory are available. Selecting Alloy opts into its tier-worker
workflow; other primary agents work directly unless the user requests delegation.
Preserve existing user work. Run relevant checks and report what was actually
verified. Keep answers concise.

Select the model deliberately in the client. This configuration does not set a
default model; OpenCode/client defaults can still apply until one is selected.
Do not assume a free cloud model is local or appropriate for private code.

The managed opencode.json, AGENTS.md, agents directory, and shared plugin are
read-only. The enclosing configuration directory is writable for OpenCode's
own runtime metadata such as service.json, isolated in the terminal home.
Human-run setup can use `opencode auth login mistral` normally; `--standalone`
is optional. Authentication is a human task; never read/export credentials or
attempt interactive sign-in autonomously.
