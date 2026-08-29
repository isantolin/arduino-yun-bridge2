'use strict';
'require view';
'require dom';

return view.extend({
	render: function() {
		var statusBox = E('div', {
			'id': 'pin-status',
			'class': 'alert-message',
			'style': 'margin-top:15px; padding:12px; font-family:monospace; background:#e9ecef; border-radius:4px;'
		}, [ _('Status: Ready') ]);

		function setPinState(state) {
			statusBox.className = 'alert-message notice';
			statusBox.textContent = _('Sending command: ') + state + '...';

			fetch('/cgi-bin/mcubridge-pin/pin/13', {
				method: 'POST',
				headers: {
					'Content-Type': 'application/json'
				},
				body: JSON.stringify({ state: state })
			})
			.then(function(response) {
				if (!response.ok) {
					throw new Error('HTTP error! status: ' + response.status);
				}
				return response.json();
			})
			.then(function(data) {
				if (data.status === 'ok') {
					var resState = (data.data && data.data.state) ? data.data.state : (data.state || state);
					statusBox.className = 'alert-message success';
					statusBox.textContent = 'LED 13 State: ' + resState + ' (Success)';
				} else {
					statusBox.className = 'alert-message warning';
					statusBox.textContent = 'Error: ' + (data.message || 'Unknown error');
				}
			})
			.catch(function(err) {
				statusBox.className = 'alert-message danger';
				statusBox.textContent = 'API Error: ' + err.message;
			});
		}

		var btnOn = E('button', {
			'class': 'cbi-button cbi-button-positive',
			'style': 'margin-right:10px; padding:8px 24px; font-weight:bold;',
			'click': function() { setPinState('ON'); }
		}, [ _('Turn ON') ]);

		var btnOff = E('button', {
			'class': 'cbi-button cbi-button-negative',
			'style': 'padding:8px 24px; font-weight:bold;',
			'click': function() { setPinState('OFF'); }
		}, [ _('Turn OFF') ]);

		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [ _('McuBridge Web UI - Pin Control') ]),
			E('p', {}, [
				E('em', {}, [ _('Control MCU hardware GPIO pins directly via UNIX domain socket gRPC REST API.') ])
			]),
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, [ _('LED 13 (Digital Output)') ]),
				E('div', { 'style': 'margin-top:10px;' }, [ btnOn, btnOff ]),
				statusBox
			]),
			E('div', { 'class': 'cbi-section', 'style': 'margin-top:20px;' }, [
				E('h3', {}, [ _('Standalone Web UI') ]),
				E('p', {}, [
					_('You can also access the standalone dedicated interface here: '),
					E('a', { 'href': '/mcubridge/index.html', 'target': '_blank' }, [ '/mcubridge/index.html' ])
				])
			])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});

