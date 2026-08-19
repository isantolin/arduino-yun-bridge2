'use strict';
'require view';
'require fs';
'require poll';

return view.extend({
	load: function() {
		return fs.readfile('/tmp/mcubridge_status.json').then(function(content) {
			if (!content || content.trim() === '') {
				return null;
			}
			try {
				return JSON.parse(content);
			} catch (e) {
				return { status: 'error', message: 'Failed to parse JSON status file: ' + e };
			}
		}).catch(function(err) {
			return null;
		});
	},

	render: function(statusData) {
		var preEl = E('pre', { 'class': 'cbi-input-textarea', 'style': 'width:100%; font-family:monospace; min-height:200px;' }, [
			statusData ? JSON.stringify(statusData, null, 2) : _('Status file not found or is empty. The daemon may be stopped, starting up, or the device may have rebooted (the status file lives on /tmp tmpfs).')
		]);

		poll.add(function() {
			return fs.readfile('/tmp/mcubridge_status.json').then(function(content) {
				if (!content || content.trim() === '') {
					preEl.textContent = _('Status file not found or is empty. The daemon may be stopped, starting up, or the device may have rebooted (the status file lives on /tmp tmpfs).');
					return;
				}
				try {
					var parsed = JSON.parse(content);
					preEl.textContent = JSON.stringify(parsed, null, 2);
				} catch (e) {
					preEl.textContent = 'Error parsing status JSON: ' + e;
				}
			}).catch(function() {
				preEl.textContent = _('Status file not found or is empty. The daemon may be stopped, starting up, or the device may have rebooted (the status file lives on /tmp tmpfs).');
			});
		}, 5);

		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [ _('McuBridge Daemon Status') ]),
			E('p', {}, [
				E('em', {}, [ _('Status snapshot is read from /tmp/mcubridge_status.json (tmpfs) and updates automatically every 5 seconds.') ])
			]),
			E('div', { 'class': 'cbi-section' }, [
				preEl
			])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
