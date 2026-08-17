# Instructions for an agent working in this repository

You are running on the owner's own machine, which is the only place their email
exists. Everything here is designed around that: the data never leaves, and you
are the one who can reach it.

## What this is

A personal knowledge base built from the owner's email, in two layers.

**Layer one is the mail itself.** Full-text search over every imported message.
It needs no model, no API key, and no analysis, and it is available minutes
after an import. This is what to set up first and what to fall back on always.

**Layer two is distilled experience.** Cases, rules, and prior attempts, each
checked against the original text. It costs model calls and only covers threads
that have been analyzed. Treat it as an upgrade, never as a prerequisite.

## Setting it up

When the owner points you at an export, run this and read the output:

```bash
bash scripts/setup-linux.sh <export-directory> [database-path]
```

It stops at the first real problem rather than failing later somewhere less
obvious. Two of those problems matter more than they look:

- **A CSV export with no full body column.** It imports cleanly and every
  message is silently truncated to a preview. Nothing downstream recovers it.
  If `check-source` reports this, stop and tell the owner to re-export with the
  body field, or to export raw JSON from Graph instead. Do not proceed.
- **Cloud placeholder files.** They report a real size while holding no
  content. Importing works but blocks on a download per file. Get them pinned
  locally first.

Put the database on a volume with room. It lands at roughly three times the
size of the export, because each message is kept as the original record, the
body, and the cleaned body. `EMAIL_KB_DB` sets the path once.

Then confirm with `python -m email_kb doctor`. `can_search_email` is the one
that matters first; `can_answer_from_experience` will be false until analysis
has run, and that is expected.

## Using it

Prefer the MCP tools over reading files by hand. Generate the client config
with `python -m email_kb mcp-config --client cursor`, or query directly:

```bash
python -m email_kb search "the question"          # the mail itself
python -m email_kb thread "<thread-id>"           # one whole conversation
python -m email_kb search --mode cases "..."      # past problems like this one
python -m email_kb search --mode rules "..."      # rules that may apply
python -m email_kb search --mode prior "..."      # was this already tried
```

Search works in Chinese and English. Start with the mail itself; reach for
cases and rules only when the question is "what should I do" rather than "what
happened".

When you use a result, read its `advisories`. An `outcome_state` of `unknown`
means nobody recorded whether the approach worked, so it must never be
presented as proven.

## Rules

**Never commit the database or anything derived from the mail.** This
repository is public. `messages` holds message bodies and `analysis_runs` holds
the full model output of every run, so publishing the database publishes the
mailbox. `.gitignore` covers `data/`, `*.db`, and `evaluation/*.toml`, and
`doctor` reports if the database ends up somewhere tracked. Keep it outside the
repository entirely.

**Never paste email content into a commit message, a PR description, an issue,
or a test fixture.** Test data must be synthetic. When reporting a problem,
share the output of `stats`, `report`, `doctor`, or `--dry-run`, which are
counts only. The output of `search`, `thread`, `case`, `rules`, and `review`
contains message text and must stay local.

**Do not run `analyze` or `induce` on the full corpus without asking.** They
call a model for every thread. Run `--dry-run` first, show the owner the
estimated call count, and start with `--limit 20`.

**Two blind extraction passes must use different models** for the agreement
score to mean anything. The same model twice measures sampling noise.

## Working on the code

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
```

Read `docs/PLAN_REVIEW.md` before proposing architectural changes. It records
which parts of the product plan were judged sound, which were not, and why the
execution order was rearranged. In particular, the reason evidence checks and
program-decided status exist is that a model reporting its own confidence was
found to be gating whether knowledge counted as verified.
