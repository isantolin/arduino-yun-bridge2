'use strict';
'require view';
'require poll';
'require view.mcubridge.status_client as statusClient';

return view.extend({
	load: function () {
		return statusClient.fetchStatus();
	},

	render: function (statusData) {
		var preEl = E('pre', { 'class': 'cbi-input-textarea', 'style': 'width:100%; font-family:monospace; min-height:200px;' }, [
			statusData ? JSON.stringify(statusData, null, 2) : _('Status not available. The daemon may be stopped, starting up, or the device may have rebooted.')
		]);

		poll.add(function () {
			return statusClient.fetchStatus().then(function (res) {
				preEl.textContent = res ? JSON.stringify(res, null, 2) : _('Status not available. The daemon may be stopped, starting up, or the device may have rebooted.');
			});
		}, 5);

		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [_('McuBridge Daemon Status')]),
			E('p', {}, [
				E('em', {}, [_('Status is queried natively via OpenWrt UBUS (with tmpfs fallback) and updates automatically every 5 seconds.')])
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
