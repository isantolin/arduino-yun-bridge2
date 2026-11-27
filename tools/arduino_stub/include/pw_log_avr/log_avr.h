#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*pw_log_avr_LogSink)(uint8_t level, const uint8_t* log_data, size_t size);

void pw_log_avr_SetLogSink(pw_log_avr_LogSink sink);
void pw_log_avr_HandleLog(uint8_t level, const uint8_t* log_data, size_t size);

#ifdef __cplusplus
}
#endif
