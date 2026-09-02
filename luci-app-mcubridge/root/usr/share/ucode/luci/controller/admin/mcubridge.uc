// McuBridge LuCI API controller for pin operations
import * as ubus from 'ubus';

return {
	action_pin: function(pin) {
		let body = http.content();
		let parsed = json(body);
		let state = (parsed?.state ?? 'ON');
		let p = +(pin ?? 13);
		let val = (state == 'ON' || state == 'HIGH' || state == '1') ? 'ON' : 'OFF';
		let intVal = (val == 'ON') ? 1 : 0;

		let u = ubus.connect();
		if (u) {
			u.call('mcubridge', 'digital_write', { pin: p, value: intVal });
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
