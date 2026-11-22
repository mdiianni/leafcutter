#!/usr/bin/env python3
"""
Read-only guard for leafcutter analysers.

Scans every analyse_*.py (plus the shared cache/box utility modules) for calls on
AWS SDK (boto3) client/resource objects whose method name implies a mutating AWS API
action (Create*, Put*, Update*, Delete*, Start*, Stop*, Attach*, Enable*, etc).

leafcutter's core trust guarantee is that it never modifies AWS resources — it only
describes/reads state. This check exists to catch an accidental or AI-suggested
mutating call before it merges, not to be a perfect static analyser.

It uses the AST (not a text/grep scan) so that mutating verbs appearing inside
docstrings or recommendation strings (e.g. "call sagemaker.delete_app() yourself")
are correctly ignored — only real Call nodes on tracked client variables count.

Exit code 0 = clean, 1 = violation(s) found.
"""
from __future__ import annotations

import ast
import glob
import sys

# AWS API verb prefixes that imply a mutating/state-changing call. boto3 method
# names mirror the AWS API action names closely enough that a prefix match on the
# snake_case method name is a reliable signal.
MUTATING_PREFIXES = (
    "create_", "put_", "update_", "modify_", "delete_", "terminate_", "stop_",
    "start_", "tag_", "untag_", "attach_", "detach_", "enable_", "disable_",
    "reboot_", "revoke_", "authorize_", "associate_", "disassociate_",
    "register_", "deregister_", "accept_", "reject_", "cancel_", "purge_",
    "run_", "invoke_", "send_", "restore_", "reset_", "copy_", "import_",
    "set_", "replace_", "reinstate_", "transfer_", "reboot_",
)

# Method names that would otherwise match a prefix above but are explicitly
# read-only / safe (extend this list with justification in the PR, never remove
# an entry without one).
ALLOWED_EXCEPTIONS: set[str] = {
    # GetMetricData-style reads sometimes named with these verbs in botocore paginators
}

TARGET_GLOBS = ["analyse_*.py", "cache_utils.py", "pricing_cache.py", "box_utils.py"]


def find_client_vars(tree: ast.AST) -> set[str]:
    """Find variable names assigned from a boto3 .client(...)/.resource(...) call."""
    client_vars: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr in ("client", "resource"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        client_vars.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        client_vars.add(target.attr)
    return client_vars


def find_violations(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=path)
    client_vars = find_client_vars(tree)
    if not client_vars:
        return []

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        method_name = func.attr
        if method_name in ALLOWED_EXCEPTIONS:
            continue
        if not method_name.startswith(MUTATING_PREFIXES):
            continue

        obj = func.value
        obj_name = None
        if isinstance(obj, ast.Name):
            obj_name = obj.id
        elif isinstance(obj, ast.Attribute):
            obj_name = obj.attr

        if obj_name in client_vars:
            violations.append(f"{path}:{node.lineno}: {obj_name}.{method_name}(...)")
    return violations


def main() -> int:
    files: list[str] = []
    for pattern in TARGET_GLOBS:
        files.extend(sorted(glob.glob(pattern)))

    all_violations: list[str] = []
    for path in files:
        all_violations.extend(find_violations(path))

    if all_violations:
        print("❌ Read-only guard failed — mutating AWS API call(s) detected:\n")
        for v in all_violations:
            print(f"  {v}")
        print(
            "\nleafcutter must never call AWS APIs that create, modify, or delete "
            "resources. If this is a false positive, add a justified entry to "
            "ALLOWED_EXCEPTIONS in scripts/check_readonly.py and explain why in the PR."
        )
        return 1

    print(f"✅ Read-only guard passed — scanned {len(files)} file(s), no mutating calls found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
