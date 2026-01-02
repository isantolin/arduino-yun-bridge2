# YunBridge Credential Rotation

Secure deployments require the MCU, the Linux daemon, and the MQTT broker to agree on the same shared materials. This guide explains how to regenerate those secrets with the provided tooling and how to keep the Arduino firmware synchronized without leaking credentials into version control.

## When to rotate

- **Before shipping a device**: The repository ships with placeholder secrets so the bridge works immediately after flashing. Rotate them before exposing the hardware outside of a lab network.
- **After servicing hardware**: Any time the MCU or daemon image is reflashed on an untrusted bench.
- **On a schedule**: Align with your org’s security cadence (e.g., every 90 days) so MQTT credentials and serial secrets do not become stale.

## Quick rotation from your workstation

```sh
# Rotate credentials on a remote Yun and print the sketch snippet.
./tools/rotate_credentials.sh --host <yun-ip>
```

What happens:

1. The script connects via SSH (default user `root`) and runs `/usr/bin/yunbridge-rotate-credentials` on the device.
2. The helper writes the regenerated secrets to UCI (`/etc/config/yunbridge`) and restarts the daemon.
3. The script captures the freshly generated serial shared secret (stored in `yunbridge.general.serial_shared_secret`, also printed as `SERIAL_SECRET=...`) and prints a ready-to-paste snippet:

   ```c
   #define BRIDGE_SERIAL_SHARED_SECRET "<hex-secret>"
   ```

   Drop that line near the top of every sketch (before `#include <Bridge.h>`).
4. Optional: pass `--emit-sketch-snippet path/to/secret.h` if you prefer the helper to write the snippet to a header that you `#include` from your sketch sources.

### Rotating without SSH

You can run the same helper locally to update bootstrap images or CI artifacts by pointing `--local` to the UCI config directory inside your rootfs (for example, `build_dir/root-ath79/etc/config`):

```sh
sudo ./tools/rotate_credentials.sh --local build/rootfs/etc/config \
  --emit-sketch-snippet my_project/BridgeSecret.inc
```

The CLI sets `UCI_CONFIG_DIR` to the provided path, invokes `openwrt-yun-core/scripts/yunbridge-rotate-credentials`, and (optionally) drops a snippet file that you can include from multiple sketches.

## LuCI workflow

The **Services → YunBridge → Credentials & TLS** page now shows an "Arduino Secret Template" block once the rotation succeeds. Use the **Copy snippet** button to paste the synchronized `BRIDGE_SERIAL_SHARED_SECRET` line directly into your sketch (before `#include <Bridge.h>`) or into a local header that you manage inside your project.

## Verifying the new material

1. Rebuild or re-upload your Arduino sketch so it includes the updated `#define BRIDGE_SERIAL_SHARED_SECRET "..."` line (or the header where you stored that snippet).
2. Run `tools/hardware_smoke_test.sh --host <yun>` or use the LuCI "Run smoke test" button to confirm Linux ↔ MCU communication still succeeds.
3. Check `/var/log/yun-bridge.log` or the LuCI status panel for `handshake` entries. Any `serial handshake rejected` messages typically mean the MCU firmware did not pick up the new header yet.

## Operational checklist

- Track which devices have been rotated by tagging them in your asset inventory or by storing the `SERIAL_SECRET=...` line that `tools/rotate_credentials.sh` prints during automation runs.
- If you mirror secrets into another system (e.g., provisioning service), parse the `SERIAL_SECRET=...` line that the CLI prints or call the LuCI endpoint (`/admin/services/yunbridge/rotate_credentials`) and use the `serial_secret` field in its JSON response.
- Keep any snippet/header file with `BRIDGE_SERIAL_SHARED_SECRET` out of version control (or encrypt it) so each device preserves its unique material.
- Store TLS assets separately from the credential file. Use the commands documented in the LuCI page or `3_install.sh` to regenerate the `/etc/yunbridge/tls/` directory whenever you rotate client certificates.

Following this workflow keeps the MCU and daemon secrets aligned and makes rotations a repeatable, scriptable process that you can embed in CI, provisioning scripts, or LuCI itself.
