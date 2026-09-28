# Target Hermes Kanban CLI help (M0 fixture snapshot)

Captured from the pinned installed CLI v0.21.5+3816.gbac0c45 (upstream bac0c45d). For each heading below, run `$HERMES_M0_CLI kanban <heading> --help`; this snapshot retains the output starting at `usage: hermes kanban` (launcher warnings before that line are excluded). `HERMES_M0_CLI` was `/home/ocadmin/.hermes/installs/a7aef1ff6a8fec87/environments/0a783c660bcc4d4d956dd71c2c88fbe4/venv/bin/hermes`. These are read-only `--help` invocations; recheck against the host before M1/M2.

## create

```text
usage: hermes kanban create [-h] [--body BODY] [--body-file PATH]
                            [--assignee ASSIGNEE] [--parent PARENT]
                            [--workspace WORKSPACE] [--branch BRANCH]
                            [--project PROJECT] [--tenant TENANT]
                            [--priority PRIORITY] [--triage]
                            [--idempotency-key IDEMPOTENCY_KEY]
                            [--max-runtime MAX_RUNTIME]
                            [--created-by CREATED_BY] [--skill SKILLS]
                            [--max-retries N] [--model MODEL_OVERRIDE]
                            [--provider PROVIDER_OVERRIDE]
                            [--completion-contract CONTRACT] [--goal]
                            [--goal-max-turns N]
                            [--initial-status {blocked,running}] [--json]
                            title

positional arguments:
  title                 Task title

options:
  -h, --help            show this help message and exit
  --body BODY           Optional opening post
  --body-file PATH      Read the opening post from a file ('-' = stdin), so
                        bodies with embedded newlines or flag-like lines
                        survive shell quoting. Mutually exclusive with --body.
  --assignee ASSIGNEE   Profile name to assign
  --parent PARENT       Parent task id (repeatable)
  --workspace WORKSPACE
                        scratch | worktree | worktree:<path> | dir:<path>
                        (default: scratch; an explicit 'scratch' also opts out
                        of a project-scoped board's project)
  --branch BRANCH       Branch name for worktree tasks, e.g. wt/t6-wire
  --project PROJECT     Link to a project (id or slug). Anchors the task's
                        worktree under the project's primary repo with a
                        deterministic branch. See `hermes project list`.
  --tenant TENANT       Tenant namespace
  --priority PRIORITY   Priority tiebreaker
  --triage              Park in triage — a specifier will flesh out the spec
                        and promote to todo
  --idempotency-key IDEMPOTENCY_KEY
                        Dedup key. If a non-archived task with this key
                        exists, its id is returned instead of creating a
                        duplicate.
  --max-runtime MAX_RUNTIME
                        Per-task runtime cap. Accepts seconds (300) or
                        durations (90s, 30m, 2h, 1d). When exceeded, the
                        dispatcher SIGTERMs (then SIGKILLs) the worker and re-
                        queues the task.
  --created-by CREATED_BY
                        Author name recorded on the task (default: user)
  --skill SKILLS        Skill to force-load into the worker (repeatable). The
                        kanban lifecycle is already injected automatically.
                        Example: --skill translation --skill github-code-
                        review
  --max-retries N       Per-task override for the consecutive-failure circuit
                        breaker. Trip on the Nth failure — e.g. --max-retries
                        1 blocks on the first failure (no retries), --max-
                        retries 3 allows two retries. Omit to use the
                        dispatcher's kanban.failure_limit config (default 2).
  --model MODEL_OVERRIDE
                        Pin the worker to this model (passed as -m <model>)
                        without changing the profile's configured model.
                        Combine with --provider when the model belongs to a
                        different backend than the profile's default.
  --provider PROVIDER_OVERRIDE
                        Provider the --model belongs to (passed as --provider
                        <name> to the worker). Requires --model.
  --completion-contract CONTRACT
                        local-only (default), OWNER/REPO for publication, or
                        exact GitHub PR URL; required CI gates done.
  --goal                Run the worker in a goal loop: after each turn a judge
                        checks the response against the card title/body and,
                        if not done, the worker keeps going in the same
                        session until the judge agrees it's complete (or the
                        turn budget runs out, which blocks the card for
                        review). Best for open-ended cards one shot rarely
                        finishes.
  --goal-max-turns N    Turn budget for --goal workers (default 20). Ignored
                        without --goal.
  --initial-status {blocked,running}
                        Initial card status. Use 'blocked' for cards that
                        require immediate human ops (R3 gate) to skip the
                        brief running-to-blocked transition.
  --json                Emit JSON output
```

## show

```text
usage: hermes kanban show [-h] [--json] [--state-type {status,outcome}]
                          [--state-name VALUE]
                          task_id

positional arguments:
  task_id

options:
  -h, --help            show this help message and exit
  --json
  --state-type {status,outcome}
                        With --state-name: filter listed runs by task_runs
                        column
  --state-name VALUE    With --state-type: keep runs whose column equals this
                        value
```

## list

```text
usage: hermes kanban list [-h] [--mine] [--assignee ASSIGNEE]
                          [--status {archived,blocked,done,ready,review,running,scheduled,todo,triage}]
                          [--tenant TENANT] [--session SESSION] [--archived]
                          [--json]
                          [--sort {assignee,completed-desc,created,created-desc,priority,priority-desc,status,title,updated}]
                          [--workflow-template-id ID] [--step-key KEY]

options:
  -h, --help            show this help message and exit
  --mine                Filter by $HERMES_PROFILE as assignee
  --assignee ASSIGNEE
  --status {archived,blocked,done,ready,review,running,scheduled,todo,triage}
  --tenant TENANT
  --session SESSION     Filter by originating chat/agent session id (set on
                        tasks created from inside an ACP loop)
  --archived            Include archived tasks
  --json
  --sort {assignee,completed-desc,created,created-desc,priority,priority-desc,status,title,updated}
                        Sort order for listed tasks (default: priority)
  --workflow-template-id ID
                        Restrict to tasks with this workflow_template_id
  --step-key KEY        Restrict to tasks with this current_step_key
```

## runs

```text
usage: hermes kanban runs [-h] [--json] [--state-type {status,outcome}]
                          [--state-name VALUE]
                          task_id

positional arguments:
  task_id

options:
  -h, --help            show this help message and exit
  --json
  --state-type {status,outcome}
                        With --state-name: filter runs by task_runs column
  --state-name VALUE    With --state-type: keep runs whose column equals this
                        value
```

## comment

```text
usage: hermes kanban comment [-h] [--author AUTHOR] [--max-len MAX_LEN]
                             task_id text [text ...]

positional arguments:
  task_id
  text               Comment body

options:
  -h, --help         show this help message and exit
  --author AUTHOR    Author name (default: $HERMES_PROFILE or 'user')
  --max-len MAX_LEN  Trim the stored comment body to this many characters
```

## block

```text
usage: hermes kanban block [-h] [--ids IDS [IDS ...]]
                           [--kind {capability,dependency,needs_input,transient}]
                           task_id [reason ...]

positional arguments:
  task_id
  reason                Reason (also appended as a comment)

options:
  -h, --help            show this help message and exit
  --ids IDS [IDS ...]   Additional task ids to block with the same reason
                        (bulk mode)
  --kind {capability,dependency,needs_input,transient}
                        Typed block reason. 'dependency' waits in todo (auto-
                        promoted when parents finish, no human);
                        'needs_input'/'capability' go to blocked for a human;
                        'transient' marks a maybe-flaky failure. Repeated
                        same-kind re-blocks after unblock route the task to
                        triage to break unblock loops. Omit for a generic
                        block.
```

## unblock

```text
usage: hermes kanban unblock [-h] [--reason REASON] task_ids [task_ids ...]

positional arguments:
  task_ids

options:
  -h, --help       show this help message and exit
  --reason REASON  Optional reason/note — recorded as a comment before
                   unblocking. Quote multi-word reasons.
```

## request-review

```text
usage: hermes kanban request-review [-h] [--summary SUMMARY]
                                    [--reviewer REVIEWER]
                                    [--metadata METADATA] [--force]
                                    task_id

positional arguments:
  task_id

options:
  -h, --help           show this help message and exit
  --summary SUMMARY    What was implemented and how it was verified — shown to
                       the reviewer.
  --reviewer REVIEWER  Optional reviewer profile; reassigns the task before
                       review dispatch.
  --metadata METADATA  JSON object with structured reviewer handoff facts.
  --force              Override the live-claim guard: move a running, claimed
                       task to review even without owning its run (clears the
                       worker's claim).
```

## request-changes

```text
usage: hermes kanban request-changes [-h] task_id reason [reason ...]

positional arguments:
  task_id
  reason      Concrete changes required before re-review

options:
  -h, --help  show this help message and exit
```

## reopen-review

```text
usage: hermes kanban reopen-review [-h] [--reason REASON]
                                   task_ids [task_ids ...]

positional arguments:
  task_ids

options:
  -h, --help       show this help message and exit
  --reason REASON  Optional reason/note — recorded as a comment before
                   reopening. Quote multi-word reasons.
```

## link

```text
usage: hermes kanban link [-h] parent_id child_id

positional arguments:
  parent_id
  child_id

options:
  -h, --help  show this help message and exit
```

## complete

```text
usage: hermes kanban complete [-h] [--result RESULT] [--summary SUMMARY]
                              [--metadata METADATA] [--force]
                              task_ids [task_ids ...]

positional arguments:
  task_ids             One or more task ids (only --result applies to all of
                       them)

options:
  -h, --help           show this help message and exit
  --result RESULT      Result summary
  --summary SUMMARY    Structured handoff summary for downstream tasks. Falls
                       back to --result if omitted.
  --metadata METADATA  JSON dict of structured facts (e.g. '{"changed_files":
                       [...], "tests_run": 12}'). Stored on the closing run.
  --force              Override the live-claim guard: complete a running,
                       claimed task even without owning its run (closes the
                       worker's run).
```

## archive

```text
usage: hermes kanban archive [-h] [--rm PURGE_IDS [PURGE_IDS ...]]
                             [task_ids ...]

positional arguments:
  task_ids              Task ids to archive (default mode)

options:
  -h, --help            show this help message and exit
  --rm PURGE_IDS [PURGE_IDS ...]
                        Permanently delete already-archived task ids from the
                        board
```

## reclaim

```text
usage: hermes kanban reclaim [-h] [--reason REASON] task_id

positional arguments:
  task_id

options:
  -h, --help       show this help message and exit
  --reason REASON  Human-readable reason (recorded on the reclaimed event)
```

## dispatch

```text
usage: hermes kanban dispatch [-h] [--dry-run] [--max MAX]
                              [--failure-limit FAILURE_LIMIT] [--json]

options:
  -h, --help            show this help message and exit
  --dry-run             Don't actually spawn processes; just print what would
                        happen
  --max MAX             Cap number of spawns this pass
  --failure-limit FAILURE_LIMIT
                        Auto-block a task after this many consecutive non-
                        success attempts (spawn_failed, timed_out, or crashed;
                        default: 2)
  --json
```
