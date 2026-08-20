# Vision

Jernerics is a personal research tool that smooths over the mechanical pain of
running experiments — deploying them to HPC reproducibly and collecting results
without tying the experiment to a library. It is not a product.

## Why this exists

Two frustrations, in the order they arrived:

1. **Reproducibility didn't survive the trip to HPC.** Locally, Nix gives
   bit-for-bit reproducible projects. A local Nix install on the cluster was not
   the same standard. So jernerics began as a way to *make deployment easy* — to
   get the reproducibility Nix gives you locally onto a scheduler you don't
   control, via containers (Apptainer/Docker).

2. **Tracking libraries asked for too much or gave too little.** wandb is SaaS —
   the data leaves your machine, which wasn't worth it and left me uneasy about
   where my data lives. mlflow is clunky and heavyweight, and its dashboard
   didn't show the views this kind of project actually needs. So jernerics
   collects its own data, keeps it on disk you own, and doesn't lock the
   experiment into any one tracking library.

## Who it's for

Primarily one person. But because the whole point is reproducibility, the
project must stay *adoptable* — if only its author can run it, it fails its own
thesis. Users who aren't the author are valuable precisely because they expose
assumptions the author's own usage hides. The goal is **a tool that smooths the
mechanical parts of research**, not a product or a business.

## Principles

The load-bearing design rules. New work should be checked against these.

1. **Grow with real need, not ahead of it.** No speculative features. Everything
   in the tree should trace to a concrete downstream need. This is the lesson
   from the recent trim: features designed without a target became bloat. When in
   doubt, leave it out.

2. **The trial is a plain function.** `trial(config, tracker)` — no subclass, no
   decorator on the experiment body, no framework lock-in. Optuna owns the
   search; you own the function.

3. **Local and cluster are the same code.** One `trial.py` + `config.py` runs
   in-process (`jernerics local`) and on Slurm/Pueue. The backend is a
   deployment target, never a rewrite.

4. **Tracking is self-contained and yours.** One HTTP process, SQLite on disk,
   artifacts on disk. No external database, no object store, no SaaS. Ingest is
   idempotent so live-stream and replay can overlap safely.

5. **Deploy over a network you control.** Tailscale Funnel + bearer key, not a
   hosted endpoint. The server is yours.

6. **One source of truth for paths.** PathResolver resolves every path;
   generated scripts never hardcode host paths. The container sees `/work`
   (source) and `/cache` (ephemeral).

## Non-goals

Largely TBD — the project defines these as needs clarify. One is explicit:

- **No speculative features.** If a feature exists only because it *might* be
  useful, it shouldn't exist yet.

That discipline is the guardrail against the bloat that was just removed. Other
non-goals get written down the first time saying "no" to something would
otherwise be relitigated.

## Direction

Near-term, three thrusts:

- **Robustness** — make the system as reliable as the work it runs demands.
  Retry/heartbeat and the post-hook are the sharp edges.
- **Observability** — the read surface is now driven by real need. The CLI
  (`runs`, `summary`, `diff`, `trace`, `query`, `replay`) serves two audiences:
  humans doing quick check-ins, and agents reasoning about data (via
  `--json`). The typed `TrackingClient` serves programs. The dashboard —
  monitoring, cross-sweep analysis, artifact and stored-log views — is
  mounted read-only on the server behind an API-key login. What's there now
  earned its place through dogfooding against real experiment data.
- **Ergonomics** — make the CLI and trial-authoring UX as smooth as the
  mechanical parts of research allow.

## Related docs

- `CONTEXT.md` — what each term means (vocabulary).
- `README.md` — how to use jernerics.
- `AGENTS.md` — how to work in the repo.
