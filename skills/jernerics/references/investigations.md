# Investigations

An **investigation** is a named, project-unique group of member sweeps plus a
factor/outcome pair — the unit for "compare these sweeps along this factor,
judged by this metric". Investigations live on the tracking server; nothing
about the sweeps themselves changes.

- **Factor** — the comparison axis: a logged param (`manual_param`), a config
  source, or a sweep-name token (the candidates preview offers).
- **Outcome** — the value key the comparison is judged by (a `log_value` key).
- **Replicate factor** (optional) — a second factor treated as replicates
  rather than a comparison axis.
- **Coverage** — per investigation: member count, how many members carry the
  outcome, completed vs invalid members, last activity.

## Commands

All read the tracking server via `JERNERICS_TRACKING_SERVER` (or
`[tool.jernerics] tracking_server`); all accept `--json`.

```bash
jernerics investigation list                       # this project's investigations
jernerics investigation list --include-archived

jernerics investigation preview [sweep_id ...]     # coverage BEFORE creating

jernerics investigation create <name> \
  --factor <factor> --outcome <value_key> [sweep_id ...] \
  [--replicate-factor <factor>]

jernerics investigation show <ref>                 # record + coverage + Selection
jernerics investigation members set|add|remove <ref> <sweep_id ...>
jernerics investigation archive <ref>              # hide from default list
jernerics investigation restore <ref>
```

- A `<ref>` is the investigation id or its project-unique name.
- `create` is idempotent per name: re-creating an existing name re-creates
  the record with the given members.
- `preview` answers "is this membership sane?" before you commit: candidate
  factors (with member counts), candidate outcome keys, and divergence
  warnings — unknown sweeps, sweeps from another project, and members whose
  git hash or config source diverge from the rest.

## Selection tokens

`investigation show` prints the investigation's **Selection** — the project
plus its member sweep ids — as an opaque token
(`jernerics_schema.encode_selection`). Hand that token to the dashboard or
decode it in a notebook (`decode_selection`) to query exactly the member
sweeps, the same way the tracking client does.

## Programmatic

`ProjectHandle` (`jernerics.tracking`) exposes the same surface:
`investigations()`, `investigation(id)`, `investigation_preview(sweep_ids)`,
`create_investigation(name, factor, outcome, members=..., replicate_factor=...)`,
`set/add/remove_investigation_members`, `archive_investigation`,
`restore_investigation`, and `investigation_selection(id)` for the
materialized `Selection`.
