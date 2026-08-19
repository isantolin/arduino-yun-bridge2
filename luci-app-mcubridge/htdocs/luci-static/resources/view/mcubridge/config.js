'use strict';
'require view';
'require form';
'require uci';

return view.extend({
	render: function() {
		var m, s, o;

		m = new form.Map('mcubridge', _('McuBridge Configuration'),
			_('Configure the McuBridge daemon which proxies RPC frames between the MCU and the Cloud. Runtime status snapshots are written to /tmp/mcubridge_status.json (tmpfs) and are cleared on reboot.'));

		s = m.section(form.TypedSection, 'general', _('Daemon Settings'));
		s.anonymous = true;
		s.addremove = false;

		o = s.option(form.Flag, 'enabled', _('Enable Daemon'));
		o.rmempty = false;
		o.default = '1';

		o = s.option(form.Flag, 'debug', _('Enable Debug Logging'));
		o.rmempty = false;
		o.default = '0';

		o = s.option(form.Value, 'serial_port', _('Serial Port'));
		o.placeholder = '/dev/ttyATH0';
		o.rmempty = false;

		o = s.option(form.ListValue, 'serial_baud', _('Serial Baud Rate'));
		['2400', '4800', '9600', '19200', '38400', '57600', '115200', '230400', '460800', '500000', '921600', '1000000'].forEach(function(b) {
			o.value(b);
		});
		o.default = '115200';
		o.rmempty = false;

		o = s.option(form.ListValue, 'serial_safe_baud', _('Safe Serial Baud Rate'),
			_('Initial baudrate for negotiation. Use 115200 for safety.'));
		['2400', '4800', '9600', '19200', '38400', '57600', '115200', '230400', '460800', '500000', '921600', '1000000'].forEach(function(b) {
			o.value(b);
		});
		o.default = '115200';
		o.rmempty = false;

		o = s.option(form.Value, 'cloud_host', _('Cloud Host'));
		o.placeholder = '127.0.0.1';
		o.rmempty = false;

		o = s.option(form.Value, 'cloud_port', _('Cloud Port'));
		o.datatype = 'port';
		o.placeholder = '8443';
		o.rmempty = false;

		o = s.option(form.Value, 'cloud_user', _('Cloud Username'));
		o.rmempty = true;

		o = s.option(form.Value, 'cloud_pass', _('Cloud Password'));
		o.password = true;
		o.rmempty = true;

		o = s.option(form.Flag, 'cloud_tls', _('Enable TLS/SSL'),
			_('Strongly recommended. Disabling TLS sends credentials and payloads in plaintext.'));
		o.rmempty = false;
		o.default = '1';

		o = s.option(form.Flag, 'cloud_tls_insecure', _('Disable TLS Hostname Verification'),
			_('Equivalent to insecure TLS. Allows connecting via IP even when the certificate CN/SAN is a DNS name. Less secure; use only for trusted/self-hosted gateways.'));
		o.depends('cloud_tls', '1');
		o.rmempty = true;
		o.default = '0';

		o = s.option(form.Value, 'cloud_cafile', _('CA File Path'));
		o.placeholder = '/etc/ssl/certs/ca-certificates.crt';
		o.depends('cloud_tls', '1');
		o.rmempty = true;
		o.validate = function(section_id, value) {
			if (!value || value === '') return true;
			if (!value.startsWith('/')) return _('CA file path must be absolute.');
			return true;
		};

		o = s.option(form.Value, 'cloud_certfile', _('Client Certificate Path'),
			_('Optional. Only required if your Cloud Gateway enforces client certificates (mTLS).'));
		o.placeholder = '/etc/mcubridge/client.crt';
		o.depends('cloud_tls', '1');
		o.rmempty = true;

		o = s.option(form.Value, 'cloud_keyfile', _('Client Key Path'),
			_('Optional. Only required if your Cloud Gateway enforces client certificates (mTLS).'));
		o.placeholder = '/etc/mcubridge/client.key';
		o.depends('cloud_tls', '1');
		o.rmempty = true;

		o = s.option(form.Value, 'topic_prefix', _('Topic Prefix'),
			_('Base prefix used for messages (for example br/d/<pin>).'));
		o.placeholder = 'br';
		o.rmempty = false;
		o.validate = function(section_id, value) {
			if (!value || value === '') return _('Topic prefix cannot be empty.');
			if (/[#+]/.test(value)) return _('Topic prefix cannot contain wildcards.');
			return true;
		};

		o = s.option(form.Value, 'cloud_spool_dir', _('Cloud Spool Directory'),
			_('Directory used to spool messages when the Cloud Gateway is unavailable. Keep this on /tmp (tmpfs) or an external mount to avoid Flash wear.'));
		o.placeholder = '/tmp/mcubridge/spool';
		o.rmempty = false;
		o.validate = function(section_id, value) {
			if (!value || value === '') return _('Spool directory cannot be empty.');
			if (!value.startsWith('/')) return _('Spool directory must be an absolute path.');
			if (value.startsWith('/tmp') || value.startsWith('/run') || value.startsWith('/var/run') || value.startsWith('/mnt')) return true;
			return _('For Flash safety, use a path under /tmp, /run, /var/run, or /mnt.');
		};

		o = s.option(form.Value, 'file_system_root', _('File System Root'),
			_('Directory exposed for MCU file operations. Use /tmp for tmpfs (Flash-safe) or /mnt/<device> for external storage.'));
		o.placeholder = '/tmp/yun_files';
		o.rmempty = false;

		o = s.option(form.Value, 'process_timeout', _('Process Timeout (s)'));
		o.datatype = 'uinteger';
		o.placeholder = '10';
		o.rmempty = false;

		o = s.option(form.Value, 'allowed_commands', _('Allowed Shell Commands'),
			_('Space separated whitelist for shell execution (leave empty to disable).'));
		o.placeholder = 'date uptime';
		o.rmempty = true;

		var cloud_acl_options = [
			['cloud_allow_file_read', _('Allow file reads'), _('Accept requests that read files via br/fs/read.')],
			['cloud_allow_file_write', _('Allow file writes'), _('Accept requests that write files via br/fs/write.')],
			['cloud_allow_file_remove', _('Allow file deletes'), _('Accept requests that delete files via br/fs/remove.')],
			['cloud_allow_datastore_get', _('Allow datastore get'), _('Allow clients to read key/value pairs via br/datastore/get.')],
			['cloud_allow_datastore_put', _('Allow datastore put'), _('Allow clients to modify key/value pairs via br/datastore/put.')],
			['cloud_allow_mailbox_read', _('Allow mailbox read'), _('Permit reads from the MCU mailbox.')],
			['cloud_allow_mailbox_write', _('Allow mailbox write'), _('Permit writes into the MCU mailbox queue.')],
			['cloud_allow_shell_run', _('Allow shell run'), _('Allow synchronous shell execution via br/sh/run.')],
			['cloud_allow_shell_run_async', _('Allow shell run_async'), _('Allow asynchronous shell execution via br/sh/run_async.')],
			['cloud_allow_shell_poll', _('Allow shell poll'), _('Allow polling of asynchronous shell jobs via br/sh/poll.')],
			['cloud_allow_shell_kill', _('Allow shell kill'), _('Allow canceling asynchronous shell jobs via br/sh/kill.')],
			['cloud_allow_console_input', _('Allow console input'), _('Permit writes to br/console/in to reach the MCU console.')],
			['cloud_allow_digital_write', _('Allow digital write'), _('Allow writes to br/d/<pin>/write.')],
			['cloud_allow_digital_read', _('Allow digital read'), _('Allow reads via br/d/<pin>/read.')],
			['cloud_allow_digital_mode', _('Allow digital mode'), _('Allow access to br/d/<pin>/mode.')],
			['cloud_allow_analog_write', _('Allow analog write'), _('Allow writes to br/a/<pin>/write.')],
			['cloud_allow_analog_read', _('Allow analog read'), _('Allow reads via br/a/<pin>/read.')]
		];

		cloud_acl_options.forEach(function(opt) {
			o = s.option(form.Flag, opt[0], opt[1], opt[2]);
			o.rmempty = false;
			o.default = '1';
		});

		o = s.option(form.Value, 'serial_shared_secret', _('Serial Shared Secret'),
			_('Shared secret for serial authentication (BRIDGE_SERIAL_SHARED_SECRET).'));
		o.password = true;
		o.rmempty = false;

		return m.render();
	}
});
