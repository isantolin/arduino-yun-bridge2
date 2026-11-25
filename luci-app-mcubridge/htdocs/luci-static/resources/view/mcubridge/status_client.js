'use strict';
'require baseclass';
'require rpc';
'require fs';

var callMcuStatus = rpc.declare({
	object: 'mcubridge',
	method: 'status',
	expect: { '': {} },
	reject: true
});

function readFallbackStatus() {
	return fs.read('/tmp/mcubridge_status.json').then(function (content) {
		if (!content || content.trim() === '') return null;
		try {
			return JSON.parse(content);
		} catch (e) {
			return null;
		}
	}).catch(function () {
		return null;
	});
}

function extractCapabilities(data) {
	if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
	var caps = (data.bridge && data.bridge.capabilities) || data.capabilities || null;
	return (caps && Object.keys(caps).length > 0) ? caps : null;
}

return baseclass.extend({
	fetchStatus: function () {
		return callMcuStatus().then(function (res) {
			if (!res || typeof res !== 'object' || Array.isArray(res) || Object.keys(res).length === 0) {
				return readFallbackStatus();
			}
			return res;
		}).catch(function () {
			return readFallbackStatus();
		});
	},

	fetchCapabilities: function () {
		return this.fetchStatus().then(function (res) {
			var caps = extractCapabilities(res);
			if (caps) return caps;
			return readFallbackStatus().then(extractCapabilities);
		});
	}
});
