#ifndef ETL_PROFILE_H
#define ETL_PROFILE_H

/**
 * @file etl_profile.h
 * @brief ETL configuration profile for Arduino MCU Bridge.
 * 
 * This file configures the Embedded Template Library (ETL) to work
 * in restricted embedded environments like Arduino AVR.
 * 
 * [SIL-2 COMPLIANCE]
 * - Disables C++ exceptions (not supported on AVR)
 * - Disables dynamic memory allocation
 * - Ensures deterministic behavior
 */

#ifndef ETL_NO_CPP_EXTENSIONS
#define ETL_NO_CPP_EXTENSIONS
#endif

#ifndef ETL_THROW_EXCEPTIONS
#define ETL_THROW_EXCEPTIONS 0
#endif

#define ETL_VERBOSE_ERRORS 0
#define ETL_CHECK_PUSH_POP 0

// Use generic profile for GCC
#include <etl/profiles/gcc_generic.h>

#endif
