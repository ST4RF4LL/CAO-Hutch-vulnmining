# Hutch project registry

Hutch models source targets as:

`application project root -> adaptive directory tree -> microservice Git repository leaves -> flow runs and reports`

The application root does not need to be a Git repository. Hutch recursively walks
the complete project hierarchy and retains directory branches that lead to Git
repositories. It does not assign business-domain meaning to a fixed directory depth.
Each Git repository is a microservice leaf, so discovery stops at that repository
boundary. Hidden directories, symlinks, and common generated dependency directories
are skipped.

The dashboard reads `~/.hutch/projects/projects.json` by default:

```json
{
  "projects": [
    {
      "id": "commerce-platform",
      "name": "Commerce Platform",
      "root": "/absolute/path/to/commerce-platform"
    }
  ]
}
```

Project IDs must match `[A-Za-z0-9_-]{1,64}`. Roots may be unavailable temporarily;
the dashboard preserves the configured project and marks the directory unavailable.

Use another registry with either:

```bash
python3 scripts/run_hutch_dashboard.py --projects-file /path/to/projects.json
```

or the `HUTCH_PROJECTS_FILE` environment variable. Runs whose target is outside every
configured root remain visible as backward-compatible standalone repository projects.

## Deleting flow records

The project overview and flow detail page can delete finished Hutch run records.
Deletion does not remove the registered CAO flow definition. Only runs in `launching`
or `running` state are protected; prepared records have not started CAO execution and
can be removed. A deleted run directory is moved to
`~/.hutch/runs/.trash/<run-id>-<timestamp>` so an operator can restore it manually.

For every dashboard request, Hutch reconciles `launching` and `running` records with
CAO's live session list. If the recorded CAO session no longer exists, the effective
status is `orphaned`; the original status remains available as `raw_status` and the
record becomes deletable. If CAO is unavailable, Hutch keeps the active-state delete
protection instead of guessing that the run is stale.

## Markdown reports

Text deliverables ending in `.md` open in rendered mode and can be switched back to
the original source. The built-in renderer supports headings, paragraphs, emphasis,
links, lists and task lists, blockquotes, fenced code, horizontal rules, and tables.
It creates DOM nodes directly and never injects artifact content as HTML; unsafe link
schemes and embedded HTML are rendered as inert text.
