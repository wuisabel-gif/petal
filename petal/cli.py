from __future__ import annotations

import argparse
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path

from petal import env, preflight
from petal.config import (
    add_manifest_dep,
    dependency_hash,
    find_workspace_root,
    load_lock,
    load_manifest,
    manifest_hash,
    remove_manifest_dep,
    write_manifest,
)
from petal.discover.workspace import discover_workspace
from petal.installer import InstallerError, execute, uninstall, write_lock
from petal.planner import PlannerConflict, build_plan
from petal.resolve.manager import ResolutionManager
from petal.status import check_status, print_report
from petal.models import Dep, Source


def _cmd_init(args: argparse.Namespace) -> int:
    workspace = find_workspace_root(
        Path(args.workspace) if args.workspace else Path.cwd()
    )
    distro = env.detect_ros_distro()
    interpreter = env.distro_python(distro)
    py_version = env.python_version(interpreter)
    manifest = workspace / "petal.toml"
    if not manifest.exists():
        write_manifest(manifest, ros_distro=distro, python_version=py_version)
    venv = env.ensure_venv(workspace, distro)
    print(f"initialized {workspace}")
    print(f"venv: {venv}")
    print(f"activate: {workspace / '.petal' / 'activate'}")
    return 0


def _cmd_activate(args: argparse.Namespace) -> int:
    workspace = find_workspace_root(
        Path(args.workspace) if args.workspace else Path.cwd()
    )
    manifest_path = workspace / "petal.toml"
    manifest = load_manifest(manifest_path) if manifest_path.exists() else None
    distro = (
        manifest.ros_distro
        if manifest and manifest.ros_distro
        else env.detect_ros_distro()
    )
    shell = getattr(args, "shell", None) or None
    print(env.activation_snippet(workspace, distro, shell), end="")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    workspace = find_workspace_root(
        Path(args.workspace) if args.workspace else Path.cwd()
    )
    shutil.rmtree(workspace / ".petal" / "venv", ignore_errors=True)
    return 0


def _resolve_deps(manager: ResolutionManager, deps: list[Dep]) -> list:
    if hasattr(manager, "preload"):
        manager.preload()
    workers = max(1, min(8, len(deps)))
    if workers == 1:
        return [item for dep in deps if (item := manager.resolve(dep))]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return [item for item in pool.map(manager.resolve, deps) if item]


def _load_lock_or_none(lock_path: Path):
    if not lock_path.exists():
        return None
    try:
        return load_lock(lock_path)
    except Exception:
        return None


def _cmd_sync(args: argparse.Namespace) -> int:
    preflight.assert_ok(preflight.check())
    workspace = find_workspace_root(
        Path(args.workspace) if args.workspace else Path.cwd()
    )
    manifest_path = workspace / "petal.toml"
    manifest = load_manifest(manifest_path) if manifest_path.exists() else None
    distro = (
        manifest.ros_distro
        if manifest and manifest.ros_distro
        else env.detect_ros_distro()
    )
    venv = env.ensure_venv(workspace, distro)

    discovered = discover_workspace(workspace)
    deps = [*(manifest.deps if manifest else []), *discovered.deps]
    lock_path = workspace / "petal.lock"
    lock = _load_lock_or_none(lock_path)
    if (
        not args.dry_run
        and lock
        and manifest_path.exists()
        and lock.manifest_hash == manifest_hash(manifest_path)
        and lock.dependency_hash == dependency_hash(deps)
    ):
        plan = build_plan(lock.resolved)
    else:
        manager = ResolutionManager(ros_distro=distro, venv=venv)
        resolved = _resolve_deps(manager, deps)
        plan = build_plan(resolved)
    execute(
        plan,
        venv,
        frozen=args.frozen,
        dry_run=args.dry_run,
        assume_yes=args.yes or args.frozen,
        assume_no=args.no,
        workspace_root=workspace,
        manifest_path=manifest_path,
    )
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    preflight.assert_ok(preflight.check())
    workspace = find_workspace_root(
        Path(args.workspace) if args.workspace else Path.cwd()
    )
    manifest_path = workspace / "petal.toml"
    if not manifest_path.exists():
        distro = env.detect_ros_distro()
        interpreter = env.distro_python(distro)
        write_manifest(
            manifest_path,
            ros_distro=distro,
            python_version=env.python_version(interpreter),
        )

    manifest = load_manifest(manifest_path)
    distro = manifest.ros_distro or env.detect_ros_distro()
    venv = env.ensure_venv(workspace, distro)
    source_hint = Source.PIP if args.pip else Source.APT if args.apt else None
    dep = _dep_from_add_args(args.name, args.spec or "", source_hint)
    manager = ResolutionManager(ros_distro=distro, venv=venv)
    resolved = [item for item in [manager.resolve(dep)] if item]
    plan = build_plan(resolved)
    proceeded = execute(
        plan,
        venv,
        dry_run=args.dry_run,
        assume_yes=args.yes,
        assume_no=args.no,
    )
    if proceeded:
        add_manifest_dep(
            manifest_path,
            args.name,
            version_spec=args.spec or "",
            source_hint=source_hint,
            apt_pkg=args.spec or "" if args.apt else "",
        )
        _rewrite_lock(workspace, manifest_path, distro, venv)
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    preflight.assert_ok(preflight.check())
    workspace = find_workspace_root(
        Path(args.workspace) if args.workspace else Path.cwd()
    )
    manifest_path = workspace / "petal.toml"
    if not manifest_path.exists():
        print("petal.toml missing; nothing to remove")
        return 0

    manifest = load_manifest(manifest_path)
    distro = manifest.ros_distro or env.detect_ros_distro()
    venv = env.ensure_venv(workspace, distro)
    if not remove_manifest_dep(manifest_path, args.name):
        print(f"{args.name} not present in petal.toml")
        return 0

    uninstall(args.name, venv, dry_run=args.dry_run)
    if not args.dry_run:
        _rewrite_lock(workspace, manifest_path, distro, venv)
    return 0


def _dep_from_add_args(name: str, spec: str, source_hint: Source | None) -> Dep:
    if source_hint == Source.APT:
        return Dep(name=spec or name, source_hint=Source.APT)
    return Dep(name=name, version_spec=spec, source_hint=source_hint)


def _rewrite_lock(
    workspace: Path, manifest_path: Path, distro: str, venv: Path
) -> None:
    manifest = load_manifest(manifest_path)
    discovered = discover_workspace(workspace)
    deps = [*manifest.deps, *discovered.deps]
    manager = ResolutionManager(ros_distro=distro, venv=venv)
    resolved = [item for dep in deps if (item := manager.resolve(dep))]
    plan = build_plan(resolved)
    write_lock(
        workspace / "petal.lock", manifest_path, [*plan.distro, *plan.apt, *plan.pip]
    )


def _cmd_status(args: argparse.Namespace) -> int:
    preflight.assert_ok(preflight.check())
    workspace = find_workspace_root(
        Path(args.workspace) if args.workspace else Path.cwd()
    )
    manifest_path = workspace / "petal.toml"
    manifest = load_manifest(manifest_path) if manifest_path.exists() else None
    distro = (
        manifest.ros_distro
        if manifest and manifest.ros_distro
        else env.detect_ros_distro()
    )
    venv = env.venv_path(workspace)
    if not venv.exists():
        venv = env.ensure_venv(workspace, distro)
    report = check_status(workspace, venv)
    print_report(report)
    return 0 if report.ok else 2


def _skill_source() -> Path:
    candidates: list[Path] = []
    try:
        dist = metadata.distribution("petal-ros")
        candidates.append(Path(dist.locate_file("skills/petal-cli")))
    except metadata.PackageNotFoundError:
        pass
    candidates.append(Path(__file__).resolve().parent.parent / "skills" / "petal-cli")

    for candidate in candidates:
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise FileNotFoundError(
        "could not locate the petal-cli agent skill; reinstall petal-ros"
    )


def _cmd_install_agent_skill(args: argparse.Namespace) -> int:
    target_root = (
        Path(args.target).expanduser()
        if args.target
        else Path.home() / ".agents" / "skills"
    )
    target = target_root / "petal-cli"

    try:
        source = _skill_source()
        if target.exists() or target.is_symlink():
            if not args.force:
                print(f"agent skill already installed: {target}")
                print("use --force to replace it")
                return 0
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()

        target_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    except OSError as exc:
        print(f"petal: failed to install agent skill: {exc}", file=sys.stderr)
        return 1

    print(f"installed petal-cli agent skill to {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="petal")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="detect ROS distro and create workspace venv")
    init.add_argument("--workspace")
    init.set_defaults(func=_cmd_init)

    activate = sub.add_parser(
        "activate",
        help="print shell snippet to activate petal env (source <(petal activate))",
    )
    activate.add_argument("--workspace")
    activate.add_argument(
        "--shell",
        choices=["bash", "zsh", "sh"],
        default=None,
        help="ROS 2-supported shell dialect (default: auto-detect from $SHELL)",
    )
    activate.set_defaults(func=_cmd_activate)

    clean = sub.add_parser("clean", help="remove petal venv")
    clean.add_argument("--workspace")
    clean.set_defaults(func=_cmd_clean)

    sync = sub.add_parser("sync", help="resolve and install workspace dependencies")
    sync.add_argument("--workspace")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--frozen", action="store_true")
    sync_prompt = sync.add_mutually_exclusive_group()
    sync_prompt.add_argument(
        "-y", "--yes", action="store_true", help="install without prompting"
    )
    sync_prompt.add_argument(
        "--no", action="store_true", help="print plan and do not install"
    )
    sync.set_defaults(func=_cmd_sync)

    add = sub.add_parser("add", help="add a dependency to petal.toml and sync it")
    add.add_argument("name")
    add.add_argument("spec", nargs="?", default="")
    add.add_argument("--workspace")
    add.add_argument("--dry-run", action="store_true")
    add_prompt = add.add_mutually_exclusive_group()
    add_prompt.add_argument(
        "-y", "--yes", action="store_true", help="install without prompting"
    )
    add_prompt.add_argument(
        "--no", action="store_true", help="print plan and do not install"
    )
    source = add.add_mutually_exclusive_group()
    source.add_argument("--pip", action="store_true")
    source.add_argument("--apt", action="store_true")
    add.set_defaults(func=_cmd_add)

    remove = sub.add_parser("remove", help="remove a dependency from petal.toml")
    remove.add_argument("name")
    remove.add_argument("--workspace")
    remove.add_argument("--dry-run", action="store_true")
    remove.set_defaults(func=_cmd_remove)

    status = sub.add_parser("status", help="report manifest/lock/install drift")
    status.add_argument("--workspace")
    status.set_defaults(func=_cmd_status)

    skill = sub.add_parser(
        "install-agent-skill", help="install the Petal CLI agent skill"
    )
    skill.add_argument("--target", help="skills directory (default: ~/.agents/skills)")
    skill.add_argument(
        "--force", action="store_true", help="replace an existing petal-cli skill"
    )
    skill.set_defaults(func=_cmd_install_agent_skill)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("petal: cancelled", file=sys.stderr)
        return 130
    except (env.PetalEnvError, PlannerConflict, InstallerError) as exc:
        print(f"petal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
