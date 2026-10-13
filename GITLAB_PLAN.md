# Plan: GitLab support

Goal: let `mtng` collect merge requests and issues from GitLab (gitlab.com and
self-hosted instances such as gitlab.cern.ch) as well as GitHub, with the same
slides.

## Phase 1 — Forge abstraction (done, uncommitted)

The GitHub fetch code now sits behind a `Forge` protocol. There is no change in
behaviour: the snapshot test passes byte-for-byte and the live tests pass.

- `models.py`: forge-neutral pydantic models (`Issue`, `PullRequest`, `Review`,
  `User`, `Label`). Templates and `stats.py` read only these, so a new backend
  just has to fill them.
- `forge/base.py`: the `Forge` protocol (`merged_prs`, `release_prs`,
  `open_prs`, `open_issues(include_prs=)`), plus `memoize` and
  `bounded_gather`. Cache keys use the instance's `cache_id` instead of
  pickling the client. `CACHE_VERSION` is now 4.
- `forge/github.py`: `GitHubForge`, plus the GitHub release-tag and URL
  parsing.
- `collect.py`: orchestration and enrichment only. `collect_repositories(...,
  forge=...)`.
- `tests/fake_forge.py`: answers calls by what they ask for, not by call order.

Still GitHub-specific: token resolution, validation and keyring in `cli.py`
(handled in Phase 3).

## Phase 2 — Configuration

- Add the forge to `Repository` in `spec.py`, e.g. `forge: "github" | "gitlab"`
  and `host: str | None` (default `github.com` / `gitlab.com`). Alternative: a
  single `url` field (`https://gitlab.cern.ch/group/sub/project`) that both are
  derived from. Decide which one before implementing.
- `extra="forbid"` applies, so update the schema, `mtng schema` output and the
  README together.
- GitLab project paths can be nested (`group/subgroup/project`). Don't assume
  `owner/repo` anywhere, including in release URL parsing.
- `action.py` stays GitHub-only.

## Phase 3 — Auth

- Tokens are per host. Resolution order per forge: `--token` (only
  unambiguous when every repo in the spec uses the same host), then an env var
  (`GH_TOKEN`, `GITLAB_TOKEN`), then the keyring entry keyed by host.
- `mtng auth login --host gitlab.cern.ch`. The existing keyring username
  `github-token` must keep working.
- Validation: GitLab sends the token as `PRIVATE-TOKEN` and checks it against
  `GET /api/v4/user`. A personal access token with the `read_api` scope is
  enough.
- Move validation onto the forge (`current_user()`) so `cli.py` doesn't branch
  on the forge type.
- `generate` builds one forge per `(forge, host)` and passes it per repo, so
  `collect_repositories` takes a forge lookup instead of a single forge.

## Phase 4 — `GitLabForge`

Client: [gidgetlab](https://gitlab.com/beenje/gidgetlab) (async, mirrors
gidgethub's `getitem`/`getiter`), or plain aiohttp if it turns out to be
unmaintained. Project paths are URL-encoded as the project id
(`group%2Fproject`). `cache_id = f"gitlab:{host}"`.

| Method | Endpoint | Notes |
|---|---|---|
| `merged_prs` | `GET /projects/:id/merge_requests?state=merged&updated_after=&updated_before=` | No server-side filter on merge date: fetch by update time, then filter on `merged_at` in Python. |
| `open_prs` | `…/merge_requests?state=opened` | `draft` exists, so the WIP logic is unchanged. |
| `open_issues` | `GET /projects/:id/issues?state=opened&created_after=&created_before=` | `include_prs` means merging in open MRs, as GitHub does. |
| `release_prs` | `GET /projects/:id/releases/:tag` | Parse `!123` and `…/-/merge_requests/123` from `description`, then fetch each MR. |
| labels | `labels=a,b` / `not[labels]=a,b` | Server-side on all list endpoints. |

Mapping to the models:

- `number` ← `iid` (not `id`). `html_url` ← `web_url`. `user` ← `author`
  (`username`, `web_url`). `body` ← `description`. `labels` arrive as strings
  and need wrapping in `Label`.
- `url` must be unique per item, because `open_issues` dedupes on it. Use the
  API URL of the MR/issue.
- `Issue.pull_request` is how `is_pr` works. Set it for MRs merged into issue
  lists.

### Lossy parts

- **Reviews.** GitLab has no review objects with
  APPROVED/COMMENTED/CHANGES_REQUESTED. Combine
  `GET …/merge_requests/:iid/approvals` (`approved_by` → `APPROVED`) and the MR
  `reviewers` state (`requested_changes` → `CHANGES_REQUESTED`; only on recent
  GitLab versions). There's no equivalent for "comment by". Either drop it, or
  approximate it from non-system notes (an extra request per MR). Start by
  dropping it.
- **Release stats.** The MR payload has `changes_count` but no
  additions/deletions. Line counts need `…/diffs` per MR (paginated and slow),
  and commit counts need `…/commits`. Start with those fields as `None`: the
  bento tiles already handle missing values. Make the full counts opt-in later
  if they're wanted.

## Phase 5 — Templates

- Show `!` instead of `#` for MRs (`\prstr` in `macros.tex`), based on the
  forge. That means exposing the forge kind in the repo data dict or on the
  spec.
- Optionally use "MR" instead of "PR" in the default frame titles for GitLab
  repos (`DEFAULT_FRAME_TITLES`). Only do this if it doesn't complicate the
  title overrides.

## Phase 6 — Tests

- JSON fixtures from a real GitLab project in `tests/ref/gitlab/`, and unit
  tests for the payload → model mapping, especially reviews and `merged_at`
  filtering.
- A snapshot test rendering a GitLab repo through `FakeForge`, to pin the `!`
  prefix.
- Live tests gated on `GITLAB_TOKEN`, like the existing `GH_TOKEN` ones.
- A spec with a GitHub repo and a GitLab repo side by side, to check per-repo
  forge selection and auth.

## Order and effort

1. Commit Phase 1 on its own.
2. Phases 2 + 3 together (config and auth have to agree on host handling):
   about half a day.
3. Phase 4 with the lossy parts stubbed, plus Phase 6 fixtures: about a day.
4. Phase 5: an hour or two.
5. Optional follow-ups: comment-based reviews and full diff stats.

## Open questions

- Config shape: separate `forge` + `host` fields, or a single `url`?
- Is gitlab.cern.ch the only target? (It affects how much to test against
  older GitLab versions without reviewer states.)
- Are release line counts wanted for GitLab, given the cost of fetching each
  MR's diffs?
