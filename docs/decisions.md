# Decision log

Non-obvious choices in this project, each recorded with the alternatives that were rejected and
the evidence that settled it. Before reversing behavior an entry covers, read it — several choices
look arbitrary without the reasoning, and undoing them reintroduces problems that are already
known.

An entry earns its place when both are true: a future reader might reasonably undo the decision
without knowing why it was made, and there is no single code site where the reasoning would fit.
If a comment beside the line (or a column doc) would reach that reader, write that instead.

Entries are chronological and self-contained: a short heading (kept stable — cross-references
link to it), one bold sentence stating the decision with its date and originating issue, then each
rejected alternative and why it lost, with the key evidence inline.

```
### Short stable handle

**The thing that was decided.** (YYYY-MM-DD, #N)
Rejected: an alternative. Why it lost, with the evidence that settled it.
Rejected: another alternative, when there was more than one.
```

If a decision is reversed, add a new entry and append **Superseded by [handle](#anchor).** to the
old one — its reasoning is the record of why the code once looked the way it did.

---

### Comment channels

**Pull request conversation comments and review comments are counted separately; the thread
search corpus folds them together.** (2026-08-06,
[#11](https://github.com/jayqi/ghtriage/issues/11); 2026-08-07,
[#13](https://github.com/jayqi/ghtriage/issues/13))
Rejected: a single combined comment count. GitHub's `/issues/comments` endpoint returns PR
conversation comments and `/pulls/comments` returns only inline review comments; in the sample
repository, 61% of pull requests had conversation comments and no review comments at all, so a
combined or review-only count reports them as silent.
Rejected: keeping that separation in the thread documents. Whether a PR got discussion or code
review is a fact about engagement and stays split in `pull_request_activity`; a search corpus
just wants all the words.

### Bot counts

**Split counts carry the total under the unqualified name, with a `non_bot_` companion.**
(2026-08-06, [#11](https://github.com/jayqi/ghtriage/issues/11))
Rejected: making `comment_count` mean the non-bot count. `issues.comments` is GitHub's own total
sitting in the raw table under a near-identical name, and excluding bots from the unqualified
name is itself the judgment the project avoids. The companion counts non-bots rather than bots
because `WHERE non_bot_comment_count = 0` is the query people actually want and needs no
arithmetic.

### No AI-identification column

**There is no AI-identification column; `author_type` is as far as the data supports.**
(2026-08-06, [#11](https://github.com/jayqi/ghtriage/issues/11))
Rejected: a curated list of agent logins. GitHub distinguishes `Bot` from `User`, not AI from
CI — `github-actions[bot]` and `Copilot` are both `Bot`, and machine accounts that are not
GitHub Apps are typed `User`. Separating them needs a maintained, repo-specific list, which goes
stale and is a judgment. If it is ever wanted, `.ghtriage/config.toml` is the right home, not
the database.

### Views, not tables

**The activity views are views, not materialized tables.** (2026-08-06,
[#11](https://github.com/jayqi/ghtriage/issues/11))
Rejected: `CREATE TABLE AS`, which would need invalidating whenever a pull changed the
underlying data. Scanning both views fully took 38 ms on the sample repository.
[Thread tables](#thread-tables) is the one place this is deliberately reversed.

### Missing-data degradation

**Derived objects degrade to typed NULLs and empty relations when optional upstream data is
absent, via one declaration-driven padding combinator applied to every source a derived object
reads — base tables included.** (2026-08-06, [#11](https://github.com/jayqi/ghtriage/issues/11);
mechanism simplified 2026-08-08)
Rejected: skipping creation whenever anything is missing. dlt only creates a column or child
table once a record carrying that field arrives, so a young or sparse repository is missing
several. Degrading keeps the column set identical everywhere, so a query written against one
database works against another. Only a missing *base* table skips an object.
Rejected: probing mechanisms — padding CTEs inside the SQL, a stand-in dict for absent tables,
per-slot column probes deciding real-vs-stand-in, and `requires` tuples gating whole objects —
each skip path needing its own drop logic, stderr note, and tests. Padding by
`UNION ALL BY NAME` against a declaration of every column the SQL reads makes a missing column a
non-event without probing.

### SQL templates and column docs

**Derived objects are plain SQL templates with format slots; column docs live in a separate
dict.** (2026-08-06, [#11](https://github.com/jayqi/ghtriage/issues/11))
Rejected: a record-per-column composition layer colocating SQL and docs. Colocation does not
actually enforce anything — an edited expression can leave an adjacent doc stale just as easily.
`test_view_docs_match_view_columns` pins the enforceable half — every column has exactly one doc
and no doc outlives its column — and the SQL stays readable and pasteable into `ghtriage query`.
A doc whose *text* drifts from its expression is caught only by review, and has already happened
once.

### Table naming

**Tables are named for the domain (`pull_requests`, `conversation_comments`), not for GitHub's
REST paths.** (2026-08-06, [#11](https://github.com/jayqi/ghtriage/issues/11))
Rejected: mirroring the endpoint names (`pulls`, `issue_comments`, `pull_comments`). `pulls`
collided with the `ghtriage pull` command, and `issue_comments` actively misleads: it holds
conversation comments on issues *and* pull requests. The dlt resource `name` and `endpoint.path`
are separate, so renaming touched no API calls. Spelled out rather than abbreviated because
nothing else in the schema is abbreviated.

### Full-text document key

**GitHub's `id` is the full-text document key, not dlt's `_dlt_id`, and its uniqueness is pinned
by a test on the source declaration, not a runtime check.** (2026-08-07,
[#13](https://github.com/jayqi/ghtriage/issues/13); runtime check removed 2026-08-08)
Rejected: `_dlt_id`, which #13 proposed as the natural key. dlt assigns it at extract time
rather than deriving it from content, so an edited row's `_dlt_id` changes on the pull that
picks the edit up — no good as a handle that outlives a pull. `id` is GitHub's permanent
identifier, dlt's merge key, and a column agents already select.
Rejected: a per-pull full-table scan re-verifying key uniqueness before indexing. Uniqueness is
load-bearing — `create_fts_index` validates nothing, and rows sharing a key all silently report
the first one's score — but a broken merge key means the whole database is wrong, not just BM25
scores. A test pins `primary_key: id` + `write_disposition: merge` on every resource
declaration: the layer that provides the guarantee is the layer that gets the test.

### Thread tables

**Thread tables are materialized tables, not views.** (2026-08-07,
[#13](https://github.com/jayqi/ghtriage/issues/13))
Rejected: views, per [Views, not tables](#views-not-tables). That entry rejected materialization
for the invalidation cost; here it is already paid: DuckDB cannot index a view at all, and an
FTS index has no incremental update — it is rebuilt from scratch on every pull regardless — so
the table is rebuilt in the same step for 5–13 ms.

### Stopwords and tokenization

**The default `ignore` pattern is kept (digits are not searchable); the stopword list is not
(`stopwords='none'`).** (2026-08-07, [#13](https://github.com/jayqi/ghtriage/issues/13))
Rejected: an `ignore` pattern that keeps digits (`'(\.|[^a-z0-9])+'`), which makes `404`
findable but stops `python` matching `python3.11` — it tokenizes as `python3` + `11`. Exact
codes and versions are what `LIKE` and `regexp_matches` already do well; full-text search
complements the SQL filters rather than replacing them.
Rejected: the default 571-word English stopword list, which on software text eats domain
vocabulary — `get`, `old`, and, because filtering applies to *stemmed* tokens, every word whose
Porter stem collides with the list (`use` → `us`) — so its damage cannot be enumerated by
reading it. A filtered term returns a confident empty result, and one such word inside a
`conjunctive := 1` query empties the whole result set. BM25's IDF weighting already down-ranks
ubiquitous terms; the accepted cost is inflated match counts for queries carrying very common
words, which the README folds into "read the ranking, not the match count."
Rejected: a curated minimal list. It would have to be curated in stem space, and it is a
maintained judgment of the kind this project avoids (see
[No AI-identification column](#no-ai-identification-column)).

### No search command

**There is no `search` subcommand and no SQL macro wrapper.** (2026-08-07,
[#13](https://github.com/jayqi/ghtriage/issues/13))
Rejected: a `search_issues(q)` table macro. It works and composes with `WHERE`, but macros are
invisible to `information_schema.tables` and reject `COMMENT ON`, so the schema-transparency
mechanism the rest of the project relies on cannot document them. The raw syntax is surfaced
through `ghtriage schema` instead.

### Post-load failure handling

**Every step after the load is attempted, reported, and survived; `run_pull` owns that policy,
and no step leaves behind an object it could not rebuild this pull.** (2026-08-08, follow-up to
[#13](https://github.com/jayqi/ghtriage/issues/13))
Rejected: each builder guarding itself. A module swallowing its own failures is deciding what a
pull is worth, which is the orchestrator's call. The builders raise, or return a message for any
single object they could not build; `run_pull` collects both and exits 0, because the raw tables
landed and they are what a pull is for. The CLI follows any warnings with a pointer at
`ghtriage pull --full`, the recovery path for every post-load state.
Rejected: indexing whichever declared columns happen to exist. A search that silently covers
only `title` because `body` was absent returns plausible, wrong-by-omission results — the
failure class this project defends hardest against. An index covers all its declared columns or
is not built; a failed build falls into the drop-and-warn path, so a later search errors on a
missing relation instead of scoring the previous pull's text. This is also what lets
`ghtriage schema` report indexes from the declaration: a present index always matches it
exactly.
Rejected: cascading drops of every derived object when the derive step failed wholesale. The
per-object paths already drop what they could not rebuild; the wholesale case leaves the user
one known, cheap command from clean.

### Six index corpora

**All six full-text indexes stay: the base title/body indexes are not subsumed by the thread
indexes.** (2026-08-10, post-review follow-up to
[#13](https://github.com/jayqi/ghtriage/issues/13))
Rejected: dropping `fts_github_issues` and `fts_github_pull_requests` as redundant with the
thread indexes. A thread document mixes what an issue is *about* with everything ever said in
it — pasted stack traces, tangents, cross-references — so BM25 over threads answers "was this
mentioned?" while title+body is the only corpus that answers "is this what the issue is about?"
without that noise. Both are real triage questions, and the two extra indexes cost two dict
entries and tens of milliseconds per pull.
Rejected: prose warning that pairing a table with the wrong index misbehaves. A base table and
its thread table hold the same ids over different text *by design* — same entities, different
corpora — so crossing them is a corpus choice to make deliberately, not an anomaly to detect. An
earlier "wrong index returns nothing" diagnostic was factually false for exactly these pairs
(every id hits) and taught the false contrapositive that a non-empty result proves the right
index. The README documents which corpus answers which question.

### Propagated join keys

**Child tables carry their parent's GitHub key — `issues__labels.issue_number` joins to
`issues.number` — and that is the documented join; the `_dlt_*` columns stay in the database
as plumbing and are hidden from `ghtriage schema`.** (2026-08-12,
[#15](https://github.com/jayqi/ghtriage/issues/15))
Rejected: documenting `_dlt_parent_id = _dlt_id` as the child-table join. It publishes loader
bookkeeping as API — the same objection that made GitHub's `id` the full-text document key (see
[Full-text document key](#full-text-document-key)) — and a reader who joins on it learns nothing
about the entity. dlt's normalizer propagates the parent's own key into every child table,
including ones it creates later, so the public join costs a config block rather than per-table
work.
Rejected: a derived view per child table exposing the key. That fixes presentation while the raw
tables a query actually reaches still lack the column, and it adds an object per array.
Rejected: propagating `id` alongside `number`. The database is single-repo by construction
(`_ghtriage_meta` holds one repo) and `number` is the handle everywhere else — the derived views,
the README examples, how people refer to issues — with `id` still one join away. Comments have no
number, so `id` is the propagated key there.
Rejected: keeping `_dlt_list_idx` visible. Hiding it is a real loss and taken deliberately: it is
the only record of GitHub's original array order, but nothing depends on that order, the views
sort by name, and label or assignee order carries no meaning on GitHub.
Rejected: an `--internal` escape hatch on `schema`, and with it the `include_internal` parameter
`get_tables()` had carried unused since #3. `information_schema` through `ghtriage query` is the
ground truth the command summarizes, so a display flag is surface without a user; adding one
takes five minutes if a user ever appears.
Rejected: switching the derived views' internal joins to the propagated key. `derived.py` still
joins children on `_dlt_parent_id = _dlt_id`, deliberately: the normalizer guarantees that link
independent of the propagation config, so the views cannot be broken by a change to it. The
propagated key is the documented surface; the views are plumbing and may join on plumbing.

### Generation stamp, not migrations

**A single integer stamped into `_ghtriage_meta` is compared before every incremental pull; any
mismatch refuses the pull, before the load, and points at `ghtriage pull --full`.** (2026-08-12,
[#15](https://github.com/jayqi/ghtriage/issues/15))
Rejected: backfilling the propagated columns with an `UPDATE` from the `_dlt_` link. It works
once, and then every future shape change needs its own backfill; the database is a disposable
cache that `pull --full` rebuilds in minutes, so the state is worth making unreachable rather
than repairable. Without the check, an incremental pull populates the new columns only for
children of recently-touched parents, and the newly documented join silently drops every label
row belonging to an untouched issue.
Rejected: comparing the package version. It bumps on every release, so every release would demand
a rebuild, and it is unreliable under editable installs where the version does not move at all.
Rejected: warning after the pull. The mixed database already exists by then, and the warning
scrolls past while the wrong joins happen later and quietly.
Rejected: auto-escalating a mismatch to a full pull. It silently deletes the database and spends
minutes and API quota the user did not ask for.

### Config file is not committable

**`.ghtriage/` is ignored in full — `config.toml` included — reversing the whitelist that let it
be committed.** (2026-08-13, auth and configuration UX)
Rejected: keeping `!config.toml` in the directory's self-managed `.gitignore` so a repository can
share a checked-in default. Its one repo-wide key, `repo`, answers a question the git `origin`
remote already answers for anyone who cloned the repository, so the committed file would say
nothing new; and the file now also carries `[auth] use_gh_token`, a per-user preference about a
per-user credential that has no business in a team's version control. The whole directory is a
disposable per-user cache, and a cache with one committable file inside it is a rule everyone has
to remember.
Rejected: migration logic for directories created before the change. `_ensure_local_gitignore`
still only writes when the file is absent, so an existing directory keeps the old whitelist until
its `.gitignore` is deleted — acceptable for a directory `pull --full` rebuilds in minutes, and
cheaper than a rewrite path that has to decide whether a hand-edited ignore file is stale.

### Hand-edited config, no config commands

**`config.toml` is scaffolded as fully commented-out boilerplate and hand-edited from there; the
only value ghtriage writes is `[auth] use_gh_token`, set by `auth setup` through tomlkit's
comment-preserving round-trip.** (2026-08-13, auth and configuration UX)
Rejected: `ghtriage config set` / `config get`. The schema is two keys; a command pair to edit
them is more surface than the thing being edited, and it would own the file's formatting forever
— the scaffolded comments explaining each key are the actual documentation and a naive writer
destroys them. The gh fallback is the one exception because it is set as a side effect of a menu
choice, not by someone who has the file open.
Rejected: writing the file only when a value is needed. Scaffolding it at directory creation is
what makes hand-editing discoverable: the keys, their types, and their defaults are already in
front of the reader. Every key is commented out, so a fresh scaffold parses as an empty config
and changes nothing.
Rejected: an `init` command whose job is the scaffolding. Both entry points into the tool create
the directory already — `auth setup` eagerly at command start, `pull` when it writes — so `init`
would be a command that only ever runs `mkdir`.
Rejected: ignoring unrecognized keys, as a stricter loader's alternative. A hand-edited file makes
typos the expected failure, and a silently ignored `repoo = ` is a silent wrong answer: the pull
targets the wrong repository with no signal. Unknown keys and tables warn on stderr — which is
also what surfaces a leftover old-style `[repo]` table after the rename to top-level `repo`. Type
errors stay loud, since a `repo` that is not a string cannot be honored at all.
