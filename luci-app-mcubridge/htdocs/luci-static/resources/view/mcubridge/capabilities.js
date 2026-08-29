'use strict';
'require view';
'require fs';
'require poll';

return view.extend({
	load: function() {
		return fs.read('/tmp/mcubridge_status.json').then(function(content) {
			if (!content || content.trim() === '') return null;
			try {
				var parsed = JSON.parse(content);
				return (parsed && parsed.bridge && parsed.bridge.capabilities) || null;
			} catch (e) {
				return null;
			}
		}).catch(function() {
			return null;
		});
	},

	renderBool: function(value) {
		if (value === true) {
			return E('span', { 'style': 'color:green; font-weight:bold;' }, [ _('Yes') ]);
		}
		return E('span', { 'style': 'color:red;' }, [ _('No') ]);
	},

	renderContent: function(caps, container) {
		while (container.firstChild) {
			container.removeChild(container.firstChild);
		}

		if (!caps) {
			container.appendChild(E('div', { 'class': 'alert-message warning' }, [
				_('Not available (Handshake pending or legacy firmware)')
			]));
			return;
		}

		var features = [
			{ key: 'has_watchdog', label: _('Watchdog Timer'), desc: _('MCU expects heartbeat') },
			{ key: 'has_eeprom', label: _('EEPROM'), desc: _('Non-volatile memory') },
			{ key: 'has_dac', label: _('True DAC'), desc: _('Real analog output') },
			{ key: 'has_hw_serial1', label: _('HW Serial 1'), desc: _('Extra UART available') },
			{ key: 'has_fpu', label: _('FPU'), desc: _('Floating Point Unit') },
			{ key: 'has_i2c', label: _('I2C (Wire)'), desc: _('SDA/SCL Hardware Support') },
			{ key: 'is_3v3_logic', label: _('3.3V Logic'), desc: _('IO Voltage Level') },
			{ key: 'has_large_buffer', label: _('Large Serial Buffer'), desc: _('RX Buffer > 64 bytes') },
			{ key: 'debug_frames', label: _('Debug Frames'), desc: _('Verbose frame logging') },
			{ key: 'debug_io', label: _('Debug I/O'), desc: _('Verbose GPIO logging') }
		];

		var hwSection = E('div', { 'class': 'cbi-section' }, [
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, [ _('Architecture ID') ]),
				E('div', { 'class': 'cbi-value-field' }, [ String(caps.arch || '0') ])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, [ _('Protocol Version') ]),
				E('div', { 'class': 'cbi-value-field' }, [ String(caps.protocol_ver || '0') ])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, [ _('Digital Pins') ]),
				E('div', { 'class': 'cbi-value-field' }, [ String(caps.digital || '0') ])
			]),
			E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, [ _('Analog Inputs') ]),
				E('div', { 'class': 'cbi-value-field' }, [ String(caps.analog || '0') ])
			])
		]);

		var featRows = features.map(function(feat) {
			return E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, [ feat.label ]),
				E('div', { 'class': 'cbi-value-field' }, [
					this.renderBool(caps[feat.key]),
					E('span', { 'class': 'cbi-value-description' }, [ ' ' + feat.desc ])
				])
			]);
		}, this);

		var featSection = E('div', { 'class': 'cbi-section' }, featRows);

		container.appendChild(E('h3', { 'class': 'cbi-section-title' }, [ _('Identified Hardware') ]));
		container.appendChild(hwSection);
		container.appendChild(E('h3', { 'class': 'cbi-section-title' }, [ _('Feature Flags') ]));
		container.appendChild(featSection);
	},

	render: function(initialCaps) {
		var container = E('div', { 'id': 'caps-container' });
		this.renderContent(initialCaps, container);

		var self = this;
		poll.add(function() {
			return fs.read('/tmp/mcubridge_status.json').then(function(content) {
				if (!content || content.trim() === '') {
					self.renderContent(null, container);
					return;
				}
				try {
					var parsed = JSON.parse(content);
					var caps = (parsed && parsed.bridge && parsed.bridge.capabilities) || null;
					self.renderContent(caps, container);
				} catch (e) {
					self.renderContent(null, container);
				}
			}).catch(function() {
				self.renderContent(null, container);
			});
		}, 5);

		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [ _('Device Capabilities') ]),
			E('p', {}, [
				E('em', {}, [ _('These features are auto-detected during the serial handshake phase.') ])
			]),
			container
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
