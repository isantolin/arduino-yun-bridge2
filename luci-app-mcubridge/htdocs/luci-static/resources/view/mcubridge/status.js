'use strict';
'require view';
'require rpc';
'require fs';
'require poll';

var callMcuStatus = rpc.declare({
	object: 'mcubridge',
	method: 'status',
	expect: { '': {} },
	reject: true
});

function getFallbackStatus() {
	return fs.read('/tmp/mcubridge_status.json').then(function(content) {
		if (!content || content.trim() === '') return null;
		try {
			return JSON.parse(content);
		} catch (e) {
			return null;
		}
	}).catch(function() {
		return null;
	});
}

function fetchStatus() {
	return callMcuStatus().then(function(res) {
		if (!res || typeof res !== 'object' || Array.isArray(res) || Object.keys(res).length === 0) {
			return getFallbackStatus();
		}
		return res;
	}).catch(function() {
		return getFallbackStatus();
	});
}

return view.extend({
	load: function() {
		return fetchStatus();
	},

	render: function(statusData) {
		var preEl = E('pre', { 'class': 'cbi-input-textarea', 'style': 'width:100%; font-family:monospace; min-height:200px;' }, [
			statusData ? JSON.stringify(statusData, null, 2) : _('Status not available. The daemon may be stopped, starting up, or the device may have rebooted.')
		]);

		poll.add(function() {
			return fetchStatus().then(function(res) {
				preEl.textContent = res ? JSON.stringify(res, null, 2) : _('Status not available. The daemon may be stopped, starting up, or the device may have rebooted.');
			});
		}, 5);

		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [ _('McuBridge Daemon Status') ]),
			E('p', {}, [
				E('em', {}, [ _('Status is queried natively via OpenWrt UBUS (with tmpfs fallback) and updates automatically every 5 seconds.') ])
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
