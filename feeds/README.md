# Local McuBridge Feed (Generated)

This directory is intentionally kept empty in Git. During a build, `1_compile.sh`
runs `tools/sync_feed_overlay.sh` to create symlinks to the canonical package
sources from the repository root into this feed so the OpenWrt SDK can consume
them via `src-link mcubridge ...` (no source copying).

Any files created here after running the sync script are ignored from version
control. Remove the directory contents if you need a clean tree before running
the script again.
