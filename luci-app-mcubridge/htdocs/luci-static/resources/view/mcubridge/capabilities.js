'use strict';
'require view';
'require poll';
'require view.mcubridge.status_client as statusClient';

return view.extend({
	load: function () {
		return statusClient.fetchCapabilities();
	},

	renderBool: function (value) {
		if (value === true) {
			return E('span', { 'style': 'color:green; font-weight:bold;' }, [_('Yes')]);
		}
		return E('span', { 'style': 'color:red;' }, [_('No')]);
	},

	renderContent: function (caps, container) {
		while (container.firstChild) {
			container.removeChild(container.firstChild);
		}

		if (!caps || Object.keys(caps).length === 0) {
			container.appendChild(E('div', { 'class': 'alert-message warning' }, [
				_('Not available (Handshake pending or legacy firmware)')
			]));
			return;
		}

		var features = [
			{ key: 'watchdog', label: _('Watchdog Timer'), desc: _('MCU expects heartbeat') },
			{ key: 'eeprom', label: _('EEPROM'), desc: _('Non-volatile memory') },
			{ key: 'dac', label: _('True DAC'), desc: _('Real analog output') },
			{ key: 'hw_serial1', label: _('HW Serial 1'), desc: _('Extra UART available') },
			{ key: 'fpu', label: _('FPU'), desc: _('Floating Point Unit') },
			{ key: 'i2c', label: _('I2C (Wire)'), desc: _('SDA/SCL Hardware Support') },
			{ key: 'logic_3v3', label: _('3.3V Logic'), desc: _('IO Voltage Level') },
			{ key: 'big_buffer', label: _('Large Serial Buffer'), desc: _('RX Buffer > 64 bytes') },
			{ key: 'spi', label: _('SPI Bus'), desc: _('Hardware SPI peripheral') },
			{ key: 'sd', label: _('SD Card'), desc: _('Storage peripheral support') },
			{ key: 'clock_sync', label: _('Clock Synchronization'), desc: _('Microsecond-precision MPU-MCU clock sync') }
		];

		var featRows = features.map(function (feat) {
			var val = caps[feat.key] !== undefined ? caps[feat.key] : caps['has_' + feat.key];
			return E('div', { 'class': 'cbi-value' }, [
				E('label', { 'class': 'cbi-value-title' }, [feat.label]),
				E('div', { 'class': 'cbi-value-field' }, [
					this.renderBool(val),
					E('span', { 'class': 'cbi-value-description' }, [' ' + feat.desc])
				])
			]);
		}, this);

		var featSection = E('div', { 'class': 'cbi-section' }, featRows);

		container.appendChild(E('h3', { 'class': 'cbi-section-title' }, [_('Feature Flags')]));
		container.appendChild(featSection);
	},

	render: function (initialCaps) {
		var container = E('div', { 'id': 'caps-container' });
		this.renderContent(initialCaps, container);

		var self = this;
		poll.add(function () {
			return statusClient.fetchCapabilities().then(function (caps) {
				self.renderContent(caps, container);
			});
		}, 5);

		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [_('Device Capabilities')]),
			E('p', {}, [
				E('em', {}, [_('These features are auto-detected during the serial handshake phase.')])
			]),
			container
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
