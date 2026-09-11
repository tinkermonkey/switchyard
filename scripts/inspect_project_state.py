#!/usr/bin/env python3
"""
Inspect Project State

The operator entry point for `state/projects/` -- what is in it, what no
longer has a config behind it, and how much of it is backup churn.

Two conditions are worth an operator's attention:

  * ORPHANED -- a `state/projects/<name>/` directory that no config in
    `config/projects/` claims, by filename or by declared project.name. The
    orchestrator does not remove these and will not: `github_state.yaml` is the
    only local record of a project's board and column node IDs, a config can go
    missing for reasons that are not a decommission (an unmounted volume, a
    half-finished rename, a file moved aside to pause a project), and a
    reconciliation that cannot see an existing board creates a duplicate of it
    rather than adopting it. Deleting state is therefore a decision with a real
    cost and no undo, which makes it a human's.

    Two were found on the live deployment when this was written --
    `agent_team_ansible` (last touched 2026-08-03) and `switchyard` (the
    orchestrator's own self-hosting state, 2026-03-15) -- both real projects
    whose configs were removed without their state. Neither had been mentioned
    anywhere in six and nine months respectively.

  * BACKUP CHURN -- `github_state_backup_*.yaml` files. Reconciliation writes
    one per project per run and nothing has ever read one; 3,196 of them / 31MB
    had accumulated, 612 for a single project. Counted here for visibility, but
    NOT pruned by anything yet. The age-based sweep that will own them
    (config/retention.py's single RETENTION_DAYS window, applied by
    services/data_retention.py, inspected with
    scripts/inspect_data_retention.py) lands in a separate change and is not
    in this branch -- the count is reported here so the growth is at least
    visible until it does. Deliberately not bounded by count in the meantime:
    ten backups is four days on a busy project and nine months on a quiet one,
    and two mechanisms with two different answers for the same files is what
    the single retention value exists to prevent.

Removal is opt-in, one project at a time, with the name typed out
(`--remove-orphan NAME`), and it refuses any name that still has a config.

Decommissioning a project fully is more than this script: drain its queued
Redis tasks, release its pipeline locks (scripts/release_lock.py), remove its
`state/dev_containers/<name>.yaml`, remove its checkout under `/workspace/`,
and only then remove its state. Removing config first and leaving tasks queued
is what produced the orphaned-task failures seen during #175.

Usage:
    python scripts/inspect_project_state.py
    python scripts/inspect_project_state.py --json
    python scripts/inspect_project_state.py --orphans-only
    python scripts/inspect_project_state.py --remove-orphan agent_team_ansible
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.state_manager import state_manager  # noqa: E402

logger = logging.getLogger(__name__)


def _dir_size_bytes(path: Path) -> int:
    """Total size of the files under `path`. Unreadable entries count as 0."""
    total = 0
    for entry in path.rglob('*'):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _human(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f"{value:.0f}{unit}" if unit == 'B' else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def _configured_projects() -> list:
    """The configured project stems, or [] if they could not be listed.

    The failure is printed rather than discarded. An empty return feeds two
    messages that name a specific cause -- "config/projects/ is gitignored as a
    directory, so a checkout sees zero" -- and one of them guards the only
    destructive operation in this script. If the listing failed for some other
    reason (a permissions gap, a missing mount, an API change), telling the
    operator to "run this inside the orchestrator container" when that is
    exactly where they are is worse than saying nothing.
    """
    try:
        return state_manager.config_manager.list_projects()
    except Exception as e:
        print(f"Could not list project configs: {e!r}", file=sys.stderr)
        return []


def collect() -> dict:
    """Everything the report and the JSON output are both derived from."""
    projects_dir = state_manager.projects_state_dir
    configured = _configured_projects()
    orphaned = set(state_manager.list_orphaned_project_state())

    entries = []
    if projects_dir.exists():
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            name = project_dir.name
            backups = state_manager.list_state_backups(name)
            state_file = project_dir / 'github_state.yaml'
            entries.append({
                'project': name,
                'orphaned': name in orphaned,
                'path': str(project_dir),
                'has_github_state': state_file.exists(),
                'backup_count': len(backups),
                'newest_backup': backups[0].name if backups else None,
                'oldest_backup': backups[-1].name if backups else None,
                'size_bytes': _dir_size_bytes(project_dir),
            })

    return {
        'state_root': str(state_manager.state_root),
        'config_dir': str(getattr(state_manager.config_manager, 'projects_dir', '?')),
        'configured_count': len(configured),
        # Explicit because `orphaned: []` is ambiguous on its own, and the two
        # readings are opposite: "nothing is orphaned" or "orphan detection did
        # not run". Anything parsing this output would read the first.
        'detection_enabled': bool(configured),
        'projects': entries,
        'orphaned': sorted(orphaned),
        'total_backups': sum(e['backup_count'] for e in entries),
        'total_size_bytes': sum(e['size_bytes'] for e in entries),
    }


def report(data: dict, orphans_only: bool) -> None:
    print(f"State root:  {data['state_root']}")
    print(f"Config dir:  {data['config_dir']} "
          f"({data['configured_count']} project config(s))")
    if not data['configured_count']:
        # Without this the report below silently says "nothing is orphaned",
        # which is true but for the wrong reason and hides that the run was
        # useless. config/projects/ is gitignored as a directory, so this is
        # the normal state of any checkout that is not the deployment.
        print()
        print("No project configs are visible from here, so orphaned-state")
        print("detection is disabled for this run. Either config/projects/ is")
        print("empty -- it is gitignored as a directory, so every checkout that")
        print("is not the deployment sees zero -- or listing it failed, in")
        print("which case the reason was printed to stderr above. Run this")
        print("inside the orchestrator container, where it is populated.")
    print()

    entries = [e for e in data['projects'] if e['orphaned']] if orphans_only \
        else data['projects']

    if not entries:
        print("No project state directories found."
              if not orphans_only else "No orphaned project state.")
        return

    width = max(len(e['project']) for e in entries)
    for e in entries:
        flag = 'ORPHANED' if e['orphaned'] else '        '
        missing = '' if e['has_github_state'] else '  (no github_state.yaml)'
        print(f"  {flag}  {e['project']:<{width}}  "
              f"backups={e['backup_count']:>4}  "
              f"{_human(e['size_bytes']):>8}{missing}")

    print()
    print(f"{len(data['orphaned'])} orphaned, "
          f"{data['total_backups']} backups, "
          f"{_human(data['total_size_bytes'])} total")
    print("Backups are not pruned by anything yet -- the shared age-based")
    print("retention sweep that will own them is a separate change. Nothing")
    print("has ever read one; the count above is growth, not state.")

    if data['orphaned']:
        print()
        print("Orphaned state has no config in config/projects/. Nothing reads")
        print("it and nothing will remove it. To remove one, after draining its")
        print("queued tasks and releasing its locks:")
        for name in data['orphaned']:
            print(f"    python scripts/inspect_project_state.py "
                  f"--remove-orphan {name}")


def remove_orphan(name: str) -> int:
    """Remove ONE orphaned project's state directory.

    Re-derives orphan status here rather than trusting the caller: this is the
    one destructive operation in the script, and the check it is guarding
    against -- deleting the live board state of a project that still has a
    config -- is exactly the mistake a stale report would cause.
    """
    project_dir = state_manager.projects_state_dir / name

    if not _configured_projects():
        # Distinguished from "still has a config" deliberately: the two refusals
        # need different next steps, and conflating them tells an operator
        # running from a worktree that their decommissioned project is still
        # configured, which is the opposite of true.
        print(f"Refusing to remove '{name}': no project configs are visible "
              f"from here, so nothing can be established to be orphaned.")
        print(f"Either config/projects/ ("
              f"{getattr(state_manager.config_manager, 'projects_dir', '?')}) "
              f"is empty -- it is gitignored as a directory, so any checkout "
              f"that is not the deployment sees zero configs -- or listing it "
              f"failed, in which case the reason was printed to stderr above.")
        print("Run this inside the orchestrator container.")
        return 1

    orphaned = state_manager.list_orphaned_project_state()
    if name not in orphaned:
        if not project_dir.exists():
            print(f"No state directory for '{name}' at {project_dir}")
        else:
            # Deliberately not phrased as "a config still claims it". Orphan
            # detection also declines wholesale when a config cannot be parsed
            # (see GitHubStateManager.list_orphaned_project_state), and from
            # here the two are one empty list. Asserting the wrong one of them
            # sends an operator off to look for a config file that is not
            # there. The refusal is the same either way; the next step is not.
            print(f"Refusing to remove '{name}': it is not reported as "
                  f"orphaned.")
            print("Either a config in config/projects/ still claims it -- by "
                  "filename or by its declared project.name -- or orphan "
                  "detection declined this run because a config could not be "
                  "read (that is logged as a warning). Run this script with no "
                  "arguments to see which.")
        return 1

    size = _dir_size_bytes(project_dir)
    file_count = sum(1 for p in project_dir.rglob('*') if p.is_file())

    import shutil
    shutil.rmtree(project_dir)
    print(f"Removed {project_dir} ({file_count} file(s), {_human(size)}).")
    print("Remember the rest of a decommission: queued Redis tasks, pipeline")
    print(f"locks, state/dev_containers/{name}.yaml, and /workspace/{name}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect and maintain state/projects/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--json', action='store_true',
                        help='Machine-readable output (report only)')
    parser.add_argument('--orphans-only', action='store_true',
                        help='Only list state with no matching config')
    parser.add_argument('--remove-orphan', metavar='NAME',
                        help='Remove ONE orphaned project state directory')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    if args.remove_orphan:
        return remove_orphan(args.remove_orphan)

    data = collect()
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        report(data, args.orphans_only)
    return 0


if __name__ == '__main__':
    sys.exit(main())
