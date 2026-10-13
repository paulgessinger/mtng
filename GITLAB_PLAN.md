# GitLab support and report boundaries

## Direction

GitLab is the primary expansion target, including CERN and other self-hosted
instances. Keep a small `Forge` protocol and shared rendering models, without
introducing a plugin framework. Existing GitHub configurations and output remain
compatible. GitHub-shaped model names are retained to avoid unrelated churn.

## Implemented on `generic`

- `models.py` defines normalized issues, PRs/MRs, users, labels and reviews.
  `forge/github.py` and `forge/gitlab.py` translate API payloads. Collection and
  rendering do not inspect backend-specific payloads.
- `Repository` keeps `name` and adds `forge` (default `github`) and optional
  `host` (default host per backend). GitLab paths can contain nested groups.
  HTTPS hosts are normalized; URL subpaths and GitHub Enterprise are rejected.
- Collection accepts a mapping of `(forge, host)` to backend instances. Duplicate
  display keys fail early with instructions to set distinct `display_name`s.
- GitLab credentials use host-specific keychain entries. Existing `github-token`
  entries still work. `--token` requires one backend/host; `GITLAB_TOKEN` requires
  one GitLab host. Mixed GitHub/GitLab reports resolve tokens independently.
- `auth login`, `check` and `status` support `--host`. GitLab validates through
  `/api/v4/user`. Redirects are disabled on authenticated GitLab requests.
- GitLab transport preserves encoded project/tag paths, paginates, sets a timeout
  and retries rate-limit/502/503/504 responses up to three total attempts.
- Cache keys include backend, origin and a credential digest, never raw tokens.
  The HTTP cache stores JSON responses and validators, not pickled models. GitHub
  review fetching follows pagination instead of returning only its first page.
- GitLab supports merged/open MRs, open issues, label filters and release
  descriptions containing local `!123` references or full MR URLs. Release
  collection resolves data only; it does not select report sections or layout.
- GitLab reports use MR terminology and `!` references, preserving custom title
  overrides. The GitHub Action remains GitHub-only and rejects GitLab configs
  before forwarding its GitHub token.

## Deliberate semantics and limits

- GitLab uses `updated_after` to obtain merge candidates, then filters inclusively
  on `merged_at`. It must not use `updated_before=end`, which loses MRs updated
  after the reporting window. GitHub retains existing calendar-date searches;
  harmonizing time-window semantics is a separate compatibility decision.
- Label filtering is local for GitLab to preserve exact names, including commas
  and reserved API values, and all-required include/any-match exclude semantics.
- GitLab reviews include approvals and assigned reviewers. REST reviewer `state`
  is account state, not a review decision. Missing approvals endpoints (404/405)
  produce unavailable review data; other failures remain errors. Reviewer totals
  are unknown if any MR's review data is unavailable.
- GitLab additions/deletions/file/commit counts remain `None`; fetching paginated
  diffs and commits is deferred. Existing single-assignee models display the
  first GitLab assignee when several are assigned.
- Tests use synthetic documented payloads, isolated caches, mixed-backend
  collection/auth checks, and a GitLab LaTeX snapshot. Live GitLab testing is
  opt-in through `GITLAB_TOKEN`, `MTNG_GITLAB_TEST_PROJECT`, and optional
  `MTNG_GITLAB_TEST_HOST`. Actual CERN deployment compatibility requires that
  smoke test; no minimum GitLab server version is claimed yet.

## Follow-up: report modes and presentation

Keep the boundary explicit during the follow-up:

1. A forge resolves releases and returns normalized data plus availability
   information. It never decides which slides a report contains.
2. A report mode determines required data, grouping, section selection and
   defaults. Existing date-window and release behavior remain until this layer
   is redesigned deliberately.
3. Templates render the resulting report. Improve layouts after the intended
   modes and their defaults are agreed.

Start the modes work by defining the purposes of meeting/date-window and release
reports, their required sections, and how explicit configuration overrides mode
presets. Do not add more interacting mode flags to the forge interface. Open-item
review enrichment now follows report requirements; merged/release reports retain
the data needed for statistics. Versioned collection snapshots support offline
rendering and presentation overrides without changing mode semantics.

Other possible follow-ups: full diff statistics, comment/change-request review
history, and broader self-hosted version coverage.

## API references

- [Merge requests](https://docs.gitlab.com/api/merge_requests/): time filters,
  payloads and reviewer account state.
- [Approvals](https://docs.gitlab.com/api/merge_request_approvals/): approvals and
  edition differences.
- [Issues](https://docs.gitlab.com/api/issues/) and
  [releases](https://docs.gitlab.com/api/releases/).
- [REST pagination](https://docs.gitlab.com/api/rest/).
