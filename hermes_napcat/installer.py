"""Installer: patches a local Hermes Agent source tree to add NapCat support.

Strategy
--------
1. Locate the Hermes ``gateway`` package (importable or via --hermes-dir).
2. Copy ``adapter.py`` → ``{gateway}/platforms/napcat.py``.
3. Patch ``{gateway}/config.py``:  add ``NAPCAT = "napcat"`` to Platform enum.
4. Patch ``{gateway}/run.py``:     add NapCat branch to ``_create_adapter()``.
5. Patch ``toolsets.py`` (sibling of ``gateway/``): add napcat toolset & include
   it in ``hermes-gateway``.

All patched files are backed up as ``<file>.napcat.bak`` before modification.
Uninstall restores the backups and removes ``gateway/platforms/napcat.py``.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path

# ── Locate Hermes ─────────────────────────────────────────────────────────────

def find_hermes_dir(hint: str | None = None) -> Path:
    """Return the directory that contains the ``gateway`` package."""
    if hint:
        p = Path(hint).resolve()
        if (p / "gateway" / "__init__.py").exists():
            return p
        if (p / "__init__.py").exists() and p.name == "gateway":
            return p.parent
        raise FileNotFoundError(f"Cannot find Hermes gateway package in: {hint}")

    # Try importable gateway
    spec = importlib.util.find_spec("gateway")
    if spec and spec.origin:
        return Path(spec.origin).parent.parent  # gateway/__init__.py → root

    # Try common install locations
    candidates = [
        Path.home() / ".hermes" / "hermes-agent",
        Path("/opt/hermes-agent"),
        Path("/usr/local/hermes-agent"),
    ]
    for p in candidates:
        if (p / "gateway" / "__init__.py").exists():
            return p

    raise FileNotFoundError(
        "Cannot locate Hermes Agent installation.\n"
        "Install it first:\n"
        "  git clone https://github.com/NousResearch/hermes-agent ~/.hermes/hermes-agent\n"
        "  cd ~/.hermes/hermes-agent && pip install -e . --break-system-packages\n"
        "Or specify the path: hermes-napcat setup --hermes-dir /path/to/hermes-agent"
    )


# ── File helpers ──────────────────────────────────────────────────────────────

def _backup(path: Path) -> None:
    bak = path.with_suffix(path.suffix + ".napcat.bak")
    if not bak.exists():
        shutil.copy2(path, bak)


def _restore(path: Path) -> bool:
    bak = path.with_suffix(path.suffix + ".napcat.bak")
    if bak.exists():
        shutil.copy2(bak, path)
        bak.unlink()
        return True
    return False


def _read(path: Path) -> str:
    # newline="" so CRLF/LF pass through unchanged (see _write).
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, content: str) -> None:
    # newline="" so we never rewrite the file's existing line endings
    # (write_text would translate "\n" to CRLF on Windows).
    path.write_text(content, encoding="utf-8", newline="")


def _write_checked(path: Path, content: str) -> None:
    """Write a patched Python file only if it still compiles.

    On a syntax error the target file is left untouched and installation
    aborts with a clear message, instead of leaving the Hermes gateway
    unable to start.
    """
    try:
        compile(content, str(path), "exec")
    except SyntaxError as e:
        raise RuntimeError(
            f"Patch would break {path} (SyntaxError at line {e.lineno}: {e.msg}).\n"
            f"The file was NOT modified. The Hermes source layout has likely "
            f"changed — please update hermes-napcat or report this issue."
        ) from e
    _write(path, content)


# ── Step 1: copy adapter ──────────────────────────────────────────────────────

def _install_adapter(hermes_root: Path) -> None:
    pkg = Path(__file__).parent
    platforms_dir = hermes_root / "gateway" / "platforms"
    tools_dir = hermes_root / "tools"

    # Copy adapter (napcat.py) — rewrites relative imports to absolute
    adapter_src = (pkg / "adapter.py").read_text(encoding="utf-8")
    adapter_src = adapter_src.replace(
        "from .api import",
        "from gateway.platforms.napcat_api import",
    )
    # After install, qq_tool lives in tools/, not gateway/platforms/
    adapter_src = adapter_src.replace(
        "from gateway.platforms import qq_tool as _qq_tool",
        "import tools.qq_tool as _qq_tool",
    )
    dst = platforms_dir / "napcat.py"
    dst.write_text(adapter_src, encoding="utf-8")
    print(f"  [+] Copied adapter        → {dst}")

    # Copy api module as napcat_api.py
    api_dst = platforms_dir / "napcat_api.py"
    shutil.copy2(pkg / "api.py", api_dst)
    print(f"  [+] Copied API client     → {api_dst}")

    # Copy qq_tool.py into tools/
    if tools_dir.exists():
        shutil.copy2(pkg / "qq_tool.py", tools_dir / "qq_tool.py")
        print(f"  [+] Copied QQ tools       → {tools_dir / 'qq_tool.py'}")
    else:
        print(f"  [!] tools/ directory not found — qq_tool.py not installed")


def _uninstall_adapter(hermes_root: Path) -> None:
    platforms_dir = hermes_root / "gateway" / "platforms"
    for name in ("napcat.py", "napcat_api.py"):
        p = platforms_dir / name
        if p.exists():
            p.unlink()
            print(f"  [-] Removed {p}")

    qq_tool = hermes_root / "tools" / "qq_tool.py"
    if qq_tool.exists():
        qq_tool.unlink()
        print(f"  [-] Removed {qq_tool}")


# ── Step 2: patch gateway/config.py ──────────────────────────────────────────

_CONFIG_MARKER = "# napcat-installed"


def _patch_config(hermes_root: Path) -> None:
    path = hermes_root / "gateway" / "config.py"
    _backup(path)
    src = _read(path)

    if _CONFIG_MARKER in src:
        print("  [=] gateway/config.py already patched")
        return

    # Locate the Platform enum class via AST and insert NAPCAT after its
    # last simple assignment (regex over the whole file could match an
    # unrelated assignment in a later class).
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise RuntimeError(f"gateway/config.py does not parse (line {e.lineno})") from e

    platform_cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Platform":
            platform_cls = node
            break
    if platform_cls is None:
        raise RuntimeError("Could not find Platform enum in gateway/config.py")

    last_assign = None
    for stmt in platform_cls.body:
        if isinstance(stmt, ast.Assign):
            last_assign = stmt
    if last_assign is None:
        raise RuntimeError("Platform enum in gateway/config.py has no members")

    lines = src.splitlines(keepends=True)
    insert_idx = last_assign.end_lineno  # 0-based index of the next line
    enum_line = " " * last_assign.col_offset + 'NAPCAT = "napcat"  ' + _CONFIG_MARKER
    src = "".join(lines[:insert_idx]) + enum_line + "\n" + "".join(lines[insert_idx:])

    _write_checked(path, src)
    print("  [+] Patched gateway/config.py (Platform.NAPCAT)")


def _unpatch_config(hermes_root: Path) -> None:
    path = hermes_root / "gateway" / "config.py"
    if _restore(path):
        print("  [-] Restored gateway/config.py")


# ── Step 3: patch gateway/run.py ─────────────────────────────────────────────

_RUN_MARKER = "# napcat-installed"


def _patch_run(hermes_root: Path) -> None:
    path = hermes_root / "gateway" / "run.py"
    _backup(path)
    src = _read(path)

    if _RUN_MARKER in src:
        print("  [=] gateway/run.py already patched")
        return

    # Locate _create_adapter via AST — regex-based indentation guessing broke
    # when upstream's function body contained nested blocks before the first
    # top-level elif/return (see issue: SyntaxError after install).
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise RuntimeError(f"gateway/run.py does not parse (line {e.lineno}) — is the Hermes install corrupted?") from e

    func = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_create_adapter":
            func = node
            break
    if func is None:
        raise RuntimeError("Could not find _create_adapter in gateway/run.py")

    body_indent = " " * func.body[0].col_offset
    inner_indent = body_indent + "    "

    napcat_block = (
        f"{body_indent}elif platform == Platform.NAPCAT:  {_RUN_MARKER}\n"
        f"{inner_indent}from gateway.platforms.napcat import NapCatAdapter, check_napcat_requirements\n"
        f"{inner_indent}if not check_napcat_requirements():\n"
        f"{inner_indent}    logger.warning('NapCat: aiohttp not installed')\n"
        f"{inner_indent}    return None\n"
        f"{inner_indent}return NapCatAdapter(config)\n"
    )

    # Find the last top-level if/elif chain in the function body that
    # dispatches on `platform`, and append our elif right after it.
    dispatch_if = None
    for stmt in func.body:
        if isinstance(stmt, ast.If):
            test_src = ast.get_source_segment(src, stmt.test) or ""
            if "platform" in test_src:
                dispatch_if = stmt
    if dispatch_if is None:
        raise RuntimeError("Could not find adapter dispatch in gateway/run.py")

    # Refuse to append an elif to a chain that ends in a bare `else:`.
    node = dispatch_if
    while node.orelse and len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
        node = node.orelse[0]
    if node.orelse:
        raise RuntimeError(
            "_create_adapter dispatch chain ends in an else clause — "
            "cannot append NapCat branch; please update hermes-napcat."
        )

    lines = src.splitlines(keepends=True)
    insert_idx = dispatch_if.end_lineno  # 0-based index of the line after the chain
    new_src = "".join(lines[:insert_idx]) + "\n" + napcat_block + "".join(lines[insert_idx:])

    _write_checked(path, new_src)
    print("  [+] Patched gateway/run.py (_create_adapter)")


def _unpatch_run(hermes_root: Path) -> None:
    path = hermes_root / "gateway" / "run.py"
    if _restore(path):
        print("  [-] Restored gateway/run.py")


# ── Step 4: patch toolsets.py ────────────────────────────────────────────────

_TOOLSETS_MARKER = "# napcat-installed"
_NAPCAT_TOOLSET_BLOCK = (
    '\n    "hermes-napcat": {  ' + _TOOLSETS_MARKER + '\n'
    '        "description": "QQ (NapCat / OneBot 11) toolset — group management, messaging, files",\n'
    '        "tools": [\n'
    '            "qq_like_user", "qq_get_user_info", "qq_get_group_info",\n'
    '            "qq_get_group_member_info", "qq_mute_group_member", "qq_kick_group_member",\n'
    '            "qq_poke", "qq_recall_message", "qq_set_group_card", "qq_get_friend_list",\n'
    '            "qq_get_group_list", "qq_get_group_member_list", "qq_set_group_admin",\n'
    '            "qq_set_group_name", "qq_set_group_whole_ban", "qq_send_group_notice",\n'
    '            "qq_get_group_honor_info", "qq_send_message", "qq_upload_file",\n'
    '            "qq_forward_message", "qq_set_group_special_title", "qq_leave_group",\n'
    '            "qq_handle_friend_request", "qq_handle_group_request",\n'
    '            "qq_get_group_msg_history", "qq_get_friend_msg_history",\n'
    '            "qq_get_essence_msg_list", "qq_set_essence_msg", "qq_delete_essence_msg",\n'
    '            "qq_set_msg_emoji_like", "qq_ocr_image", "qq_set_friend_remark",\n'
    '            "qq_delete_friend", "qq_get_group_root_files", "qq_get_group_file_url",\n'
    '            "qq_create_group_file_folder", "qq_delete_group_file",\n'
    '            "qq_get_group_notice", "qq_delete_group_notice", "qq_set_group_portrait",\n'
    '            "qq_send_group_forward_msg", "qq_send_private_forward_msg",\n'
    '            "qq_mark_msg_as_read", "qq_get_group_at_all_remain",\n'
    '            "qq_translate_en2zh", "qq_download_file", "qq_set_group_sign",\n'
    '            "qq_set_group_remark",\n'
    '        ],\n'
    '        "includes": [],\n'
    '    },\n'
)


def _patch_toolsets(hermes_root: Path) -> None:
    path = hermes_root / "toolsets.py"
    if not path.exists():
        print("  [!] toolsets.py not found — skipping toolset registration")
        return
    _backup(path)
    src = _read(path)

    if _TOOLSETS_MARKER in src:
        print("  [=] toolsets.py already patched")
        return

    # Add "hermes-napcat" to hermes-gateway includes list
    def _add_to_gateway_includes(text: str) -> str:
        pattern = r'("hermes-gateway".*?"includes"\s*:\s*\[)(.*?)(\])'
        m = re.search(pattern, text, re.DOTALL)
        if m:
            includes_content = m.group(2)
            if '"hermes-napcat"' not in includes_content:
                # Ensure last item ends with a comma before appending
                trimmed = includes_content.rstrip()
                if trimmed and not trimmed.endswith(','):
                    trimmed += ','
                new_includes = trimmed + '\n        "hermes-napcat",  ' + _TOOLSETS_MARKER + "\n    "
                text = text[:m.start(2)] + new_includes + text[m.end(2):]
        return text

    # Insert the napcat toolset block before the closing brace of the
    # TOOLSETS dict, located via AST (rfind("\n}") could hit a later dict).
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise RuntimeError(f"toolsets.py does not parse (line {e.lineno})") from e

    toolsets_dict = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TOOLSETS":
                    toolsets_dict = node.value
    if toolsets_dict is None or not toolsets_dict.values:
        print("  [!] Cannot locate TOOLSETS dict — skipping")
        return

    # Insert after the last entry, just before the dict's closing brace line.
    lines = src.splitlines(keepends=True)
    close_line = toolsets_dict.end_lineno - 1  # 0-based line of the final "}"
    head = "".join(lines[:close_line])
    if head.rstrip().endswith((",", "{")):
        src = head + _NAPCAT_TOOLSET_BLOCK + "".join(lines[close_line:])
    else:
        src = head.rstrip() + ",\n" + _NAPCAT_TOOLSET_BLOCK + "".join(lines[close_line:])
    src = _add_to_gateway_includes(src)

    _write_checked(path, src)
    print("  [+] Patched toolsets.py (hermes-napcat toolset)")


def _unpatch_toolsets(hermes_root: Path) -> None:
    path = hermes_root / "toolsets.py"
    if path.exists() and _restore(path):
        print("  [-] Restored toolsets.py")


# ── Step 5: patch hermes_cli/platforms.py ────────────────────────────────────

_PLATFORMS_MARKER = "# napcat-installed"
_NAPCAT_PLATFORMS_LINE = (
    '    ("napcat",         PlatformInfo(label="🐧 NapCat (QQ)",     default_toolset="hermes-napcat")),  '
    + _PLATFORMS_MARKER
)


def _patch_platforms(hermes_root: Path) -> None:
    path = hermes_root / "hermes_cli" / "platforms.py"
    if not path.exists():
        print("  [!] hermes_cli/platforms.py not found — skipping")
        return
    _backup(path)
    src = _read(path)

    if _PLATFORMS_MARKER in src:
        print("  [=] hermes_cli/platforms.py already patched")
        return

    # Insert napcat entry before the webhook entry
    target = '    ("webhook",'
    if target in src:
        src = src.replace(target, _NAPCAT_PLATFORMS_LINE + "\n" + target)
    else:
        # Fallback: insert after the last PlatformInfo line
        last = list(re.finditer(r'^    \("[^"]+",\s+PlatformInfo', src, re.MULTILINE))
        if not last:
            raise RuntimeError("Cannot find PLATFORMS list in hermes_cli/platforms.py")
        eol = src.index("\n", last[-1].end())
        src = src[:eol + 1] + _NAPCAT_PLATFORMS_LINE + "\n" + src[eol + 1:]

    _write_checked(path, src)
    print("  [+] Patched hermes_cli/platforms.py (napcat platform entry)")


def _unpatch_platforms(hermes_root: Path) -> None:
    path = hermes_root / "hermes_cli" / "platforms.py"
    if path.exists() and _restore(path):
        print("  [-] Restored hermes_cli/platforms.py")


# ── Step 6: patch gateway auth check (_is_user_authorized) ───────────────────
#
# The NapCat adapter enforces its own dm_policy / allow_from at intake, so the
# gateway's env-var allowlist check must not default-deny NapCat events.
# Upstream has moved this check over time:
#   - older Hermes: gateway/run.py, tuple syntax  (Platform.X, Platform.Y)
#   - newer Hermes: gateway/authz_mixin.py, set syntax  {Platform.X, Platform.Y}
# We try each known (file, target, replacement) triple in order.

_RUN_AUTH_MARKER = "# napcat-installed-auth"
_AUTH_PATCH_SITES = [
    (
        Path("gateway") / "authz_mixin.py",
        "if source.platform in {Platform.HOMEASSISTANT, Platform.WEBHOOK}:",
        "if source.platform in {Platform.HOMEASSISTANT, Platform.WEBHOOK, Platform.NAPCAT}:  "
        + _RUN_AUTH_MARKER,
    ),
    (
        Path("gateway") / "run.py",
        "if source.platform in (Platform.HOMEASSISTANT, Platform.WEBHOOK):",
        "if source.platform in (Platform.HOMEASSISTANT, Platform.WEBHOOK, Platform.NAPCAT):  "
        + _RUN_AUTH_MARKER,
    ),
]


def _auth_patch_paths(hermes_root: Path) -> list[Path]:
    return [hermes_root / rel for rel, _, _ in _AUTH_PATCH_SITES]


def _patch_run_auth(hermes_root: Path) -> None:
    for rel, target, replacement in _AUTH_PATCH_SITES:
        path = hermes_root / rel
        if not path.exists():
            continue
        src = _read(path)
        if _RUN_AUTH_MARKER in src:
            print(f"  [=] {rel} auth bypass already patched")
            return
        if target in src:
            # _backup is a no-op if a .napcat.bak already exists from _patch_run
            _backup(path)
            src = src.replace(target, replacement, 1)
            _write_checked(path, src)
            print(f"  [+] Patched {rel} (NapCat auth bypass)")
            return
    print(
        "  [!] Auth check pattern not found in gateway/authz_mixin.py or "
        "gateway/run.py — NapCat messages may be denied by the gateway "
        "allowlist. Set GATEWAY_ALLOW_ALL_USERS=true as a workaround, or "
        "update hermes-napcat."
    )


def _unpatch_run_auth(hermes_root: Path) -> None:
    for rel, target, replacement in _AUTH_PATCH_SITES:
        path = hermes_root / rel
        if not path.exists():
            continue
        src = _read(path)
        if _RUN_AUTH_MARKER not in src:
            continue
        src = src.replace(replacement, target, 1)
        _write(path, src)
        # Drop the stale backup if run.py's was already consumed elsewhere
        bak = path.with_suffix(path.suffix + ".napcat.bak")
        if bak.exists() and rel.name == "authz_mixin.py":
            bak.unlink()
        print(f"  [-] Removed NapCat auth bypass from {rel}")


# ── Step 7: install skill file ────────────────────────────────────────────────

def _install_skill(hermes_root: Path) -> None:
    skill_src = Path(__file__).parent / "skills" / "qq" / "SKILL.md"
    if not skill_src.exists():
        print("  [!] skills/qq/SKILL.md not found in package — skipping")
        return
    dst_dir = hermes_root / "skills" / "qq"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "SKILL.md"
    shutil.copy2(skill_src, dst)
    print(f"  [+] Installed skill       → {dst}")


def _uninstall_skill(hermes_root: Path) -> None:
    dst = hermes_root / "skills" / "qq" / "SKILL.md"
    if dst.exists():
        dst.unlink()
        print(f"  [-] Removed skill ({dst})")
    parent = dst.parent
    if parent.exists() and not any(parent.iterdir()):
        parent.rmdir()


# ── Public API ────────────────────────────────────────────────────────────────

def install(hermes_dir: str | None = None) -> None:
    root = find_hermes_dir(hermes_dir)
    print(f"\nInstalling hermes-napcat into: {root}\n")
    _install_adapter(root)
    _patch_config(root)
    _patch_run(root)
    _patch_toolsets(root)
    _patch_platforms(root)
    _patch_run_auth(root)
    _install_skill(root)
    print("\n✓ Installation complete.\n")
    print("Add the following to ~/.hermes/config.yaml:\n")
    print("  platforms:")
    print("    napcat:")
    print("      enabled: true")
    print("      extras:")
    print('        http_api: "http://127.0.0.1:18801"')
    print('        access_token: ""')
    print('        self_id: "YOUR_QQ_NUMBER"')
    print("        ws_port: 18800")
    print('        dm_policy: "allowlist"')
    print("        allow_from: []")
    print("        admins: []")
    print()
    print("Configure NapCat reverse WebSocket:")
    print('  { "reverseWebSocket": [{ "url": "ws://127.0.0.1:18800" }] }')
    print()


def uninstall(hermes_dir: str | None = None) -> None:
    root = find_hermes_dir(hermes_dir)
    print(f"\nUninstalling hermes-napcat from: {root}\n")
    _uninstall_adapter(root)
    _unpatch_config(root)
    _unpatch_run(root)
    _unpatch_toolsets(root)
    _unpatch_platforms(root)
    _unpatch_run_auth(root)
    _uninstall_skill(root)
    print("\n✓ Uninstall complete.\n")


def status(hermes_dir: str | None = None) -> None:
    root = find_hermes_dir(hermes_dir)
    adapter = root / "gateway" / "platforms" / "napcat.py"
    qq_tool = root / "tools" / "qq_tool.py"
    config_patched = _CONFIG_MARKER in _read(root / "gateway" / "config.py")
    run_patched = _RUN_MARKER in _read(root / "gateway" / "run.py")
    ts_path = root / "toolsets.py"
    toolsets_patched = _TOOLSETS_MARKER in _read(ts_path) if ts_path.exists() else False

    platforms_path = root / "hermes_cli" / "platforms.py"
    platforms_patched = (
        _PLATFORMS_MARKER in _read(platforms_path)
        if platforms_path.exists() else False
    )
    run_auth_patched = any(
        _RUN_AUTH_MARKER in _read(p)
        for p in _auth_patch_paths(root)
        if p.exists()
    )
    skill_installed = (root / "skills" / "qq" / "SKILL.md").exists()

    print(f"\nhermes-napcat status in: {root}")
    print(f"  adapter file:   {'✓' if adapter.exists() else '✗'}")
    print(f"  qq_tool file:   {'✓' if qq_tool.exists() else '✗'}")
    print(f"  config.py:      {'✓' if config_patched else '✗'}")
    print(f"  run.py:         {'✓' if run_patched else '✗'}")
    print(f"  toolsets.py:    {'✓' if toolsets_patched else '✗ (optional)'}")
    print(f"  platforms.py:   {'✓' if platforms_patched else '✗'}")
    print(f"  run.py auth:    {'✓' if run_auth_patched else '✗'}")
    print(f"  skill (qq):     {'✓' if skill_installed else '✗'}")
    all_ok = (adapter.exists() and qq_tool.exists() and config_patched
              and run_patched and platforms_patched and run_auth_patched
              and skill_installed)
    print(f"\n  {'Fully installed' if all_ok else 'Not fully installed'}")
    print()
