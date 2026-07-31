"""Remove legacy Antigravity fallback assets from non-Antigravity instances."""

from __future__ import annotations

import sys

import agy_update_freeze
import antigravity_setup
import harness_state


def migrate() -> dict:
    """Apply the F-141 cleanup only for a valid, non-Antigravity selection.

    Missing, invalid, and unreadable selection state must not be interpreted as
    Claude: an older Antigravity instance may not have a valid flag yet.
    """
    state, selected = harness_state.read_selection()
    if state != "valid":
        return {"status": "skipped", "reason": f"selection_{state}"}
    if selected == "agy":
        return {"status": "skipped", "reason": "antigravity_selected"}

    failed_steps = []
    try:
        result = antigravity_setup.uninstall()
    except OSError as exc:
        result = {}
        failed_steps.append(f"uninstall:{type(exc).__name__}")
    try:
        redirect_removed = agy_update_freeze.remove_hosts_redirect()
    except OSError as exc:
        redirect_removed = False
        failed_steps.append(f"freeze_redirect:{type(exc).__name__}")
    removed_files = result.get("removed_files", []) if isinstance(result, dict) else []
    return {
        "status": "partial" if failed_steps else "removed",
        "selected": selected,
        "removed_file_count": len(removed_files),
        "redirect_removed": bool(redirect_removed),
        "failed_steps": failed_steps,
    }


def main() -> int:
    try:
        result = migrate()
    except Exception as exc:  # noqa: BLE001 - startup migration must not abort the add-on
        print(f"[f141-agy-cleanup] cleanup failed: {exc}", file=sys.stderr)
        return 1
    if result["status"] == "skipped":
        print(f"[f141-agy-cleanup] skipped: {result['reason']}")
        return 0
    print(
        "[f141-agy-cleanup] removed unused Antigravity credentials/binary: "
        f"selected={result['selected']} files={result['removed_file_count']} "
        f"freeze_redirect={result['redirect_removed']}"
    )
    if result["status"] == "partial":
        print(
            "[f141-agy-cleanup] incomplete steps; next startup will retry: "
            + ",".join(result["failed_steps"]),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
