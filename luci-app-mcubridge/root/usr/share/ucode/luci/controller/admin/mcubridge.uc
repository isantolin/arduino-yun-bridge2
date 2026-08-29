// McuBridge LuCI API controller for pin operations
import { popen } from 'fs';

return {
	action_pin: function(pin) {
		let body = http.content();
		let parsed = json(body);
		let state = (parsed?.state ?? 'ON');
		let p = +(pin ?? 13);
		let val = (state == 'ON' || state == 'HIGH' || state == '1') ? 'ON' : 'OFF';

		let payload = sprintf('{"state": "%s"}', val);
		let cmd = sprintf('printf \'%%s\' \'%s\' | REQUEST_METHOD=POST PATH_INFO=/pin/%d CONTENT_LENGTH=%d /www/cgi-bin/mcubridge-pin 2>/dev/null', payload, p, length(payload));
		let proc = popen(cmd, 'r');
		if (proc) {
			proc.read('all');
			proc.close();
		}

		http.prepare_content('application/json; charset=UTF-8');
		http.write_json({
			status: 'ok',
			state: val,
			data: {
				pin: p,
				state: val
			}
		});
	}
};
