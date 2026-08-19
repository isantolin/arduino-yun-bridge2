'use strict';
'require view';

return view.extend({
	render: function() {
		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, [ _('Web UI') ]),
			E('div', { 'id': 'mcubridge-webui', 'style': 'margin-top:15px;' }, [
				E('iframe', {
					'src': '/mcubridge/index.html',
					'style': 'width:100%; height:650px; border:1px solid #ccc; border-radius:4px;'
				})
			])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
