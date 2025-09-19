# OpenWRT Integration Package for Bridge v2

This directory contains scripts and config files to ensure Bridge v2 and YunBridge v2 work on modern OpenWRT.

## Files
- `bridge-v2.init`: Init script to start/stop YunBridge daemon
- `99-bridge-ttyath0.conf`: UCI config for serial port
- `bridge-v2.files`: List of files for package manager

## Installation
1. Copy all files to your OpenWRT device in the appropriate locations:
   - `/usr/bin/yunbridge` (daemon)
   - `/etc/init.d/bridge-v2` (init script)
   - `/etc/config/bridge-ttyath0` (serial config)
   - `/www/cgi-bin/led13` (CGI script)
2. Make scripts executable:
   ```sh
   chmod +x /etc/init.d/bridge-v2 /www/cgi-bin/led13
   ```
3. Enable and start the service:
   ```sh
   /etc/init.d/bridge-v2 enable
   /etc/init.d/bridge-v2 start
   ```

## Notes
- Ensure `/dev/ttyATH0` exists and is not used by other processes.
- Check `/etc/inittab` and `/etc/config/system` for serial port conflicts.
- Use UCI config to adjust baudrate if needed.

---
