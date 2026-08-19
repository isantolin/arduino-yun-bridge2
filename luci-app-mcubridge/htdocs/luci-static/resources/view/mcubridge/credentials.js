'use strict';
'require view';
'require fs';
'require ui';

function redactSecretLines(output) {
	if (!output) return '';
	var redacted = output.replace(/SERIAL_SECRET=[^\r\n]*/g, 'SERIAL_SECRET=[redacted]');
	redacted = redacted.replace(/CLOUD_PASSWORD=[^\r\n]*/g, 'CLOUD_PASSWORD=[redacted]');
	redacted = redacted.replace(/CLOUD_PASS=[^\r\n]*/g, 'CLOUD_PASS=[redacted]');
	return redacted;
}

return view.extend({
	render: function() {
		var rotateOutput = E('pre', { 'style': 'font-family:monospace; min-height:50px;' });
		var snippetSection = E('div', { 'style': 'display:none; margin-top:10px;' });
		var snippetPre = E('pre', { 'style': 'font-family:monospace;' });
		var smokeOutput = E('pre', { 'style': 'font-family:monospace; min-height:50px;' });

		var copyBtn = E('button', {
			'type': 'button',
			'class': 'cbi-button cbi-button-action',
			'click': function() {
				var text = snippetPre.textContent;
				if (text && navigator.clipboard) {
					navigator.clipboard.writeText(text).then(function() {
						ui.addNotification(null, E('p', {}, [ _('Snippet copied to clipboard!') ]), 'info');
					}).catch(function(err) {
						ui.addNotification(null, E('p', {}, [ _('Failed to copy to clipboard: ') + err ]), 'warning');
					});
				}
			}
		}, [ _('Copy snippet') ]);

		snippetSection.appendChild(E('h4', {}, [ _('Arduino Secret Template') ]));
		snippetSection.appendChild(E('p', {}, [ _('Paste this snippet into your sketch (before including Bridge.h) so the MCU matches the new daemon secret.') ]));
		snippetSection.appendChild(copyBtn);
		snippetSection.appendChild(snippetPre);

		var rotateBtn = E('button', {
			'class': 'cbi-button cbi-button-apply',
			'click': function() {
				rotateOutput.textContent = _('Rotating credentials...');
				snippetSection.style.display = 'none';
				snippetPre.textContent = '';

				fs.exec('/usr/bin/mcubridge-rotate-credentials').then(function(res) {
					var raw = (res.stdout || '') + (res.stderr || '');
					rotateOutput.textContent = redactSecretLines(raw) || _('Credentials rotated successfully.');

					var match = raw.match(/SERIAL_SECRET=([0-9a-fA-F]+)/);
					if (match && match[1]) {
						var secret = match[1];
						var snippet = '#define BRIDGE_SERIAL_SHARED_SECRET "' + secret + '"\n' +
							'#define BRIDGE_SERIAL_SHARED_SECRET_LEN (sizeof(BRIDGE_SERIAL_SHARED_SECRET) - 1)';
						snippetPre.textContent = snippet;
						snippetSection.style.display = '';
					}
				}).catch(function(err) {
					rotateOutput.textContent = _('Error executing credential rotation: ') + err;
				});
			}
		}, [ _('Rotate now') ]);

		var smokeBtn = E('button', {
			'class': 'cbi-button cbi-button-action',
			'click': function() {
				smokeOutput.textContent = _('Running smoke test...');
				fs.exec('/usr/bin/mcubridge-hw-smoke').then(function(res) {
					var raw = (res.stdout || '') + (res.stderr || '');
					smokeOutput.textContent = raw || _('Smoke test completed.');
				}).catch(function(err) {
					smokeOutput.textContent = _('Error running smoke test: ') + err;
				});
			}
		}, [ _('Run smoke test') ]);

		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [ _('Credentials & TLS') ]),
			E('p', {}, [ _('Manage shared secrets and TLS material without leaving LuCI.') ]),

			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, [ _('Rotate Credentials') ]),
				E('p', {}, [ _('This regenerates the serial shared secret (serial_shared_secret) and the Cloud password, writes them to UCI, and restarts the daemon.') ]),
				rotateBtn,
				rotateOutput,
				snippetSection
			]),

			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, [ _('TLS Material') ]),
				E('p', {}, [ _('Upload new CA/certificate/key files via SCP or the provided helper:') ]),
				E('pre', { 'style': 'font-family:monospace;' }, [
					'scp tls_bundle.tar.gz root@<mcu>:/tmp/\n' +
					"ssh root@<mcu> 'mkdir -p /etc/mcubridge/tls && tar -xzf /tmp/tls_bundle.tar.gz -C /etc/mcubridge/tls && chmod 600 /etc/mcubridge/tls/*'"
				]),
				E('p', {}, [ _('Point the daemon to the new paths using the main configuration page.') ])
			]),

			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, [ _('Hardware Smoke Test') ]),
				E('p', {}, [ _('Runs /usr/bin/mcubridge-hw-smoke to verify cloud gateway round trips and service health.') ]),
				smokeBtn,
				smokeOutput
			])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
