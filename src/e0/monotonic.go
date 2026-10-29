package main

/*
#include <stdint.h>
#include <time.h>

static int e0_monotonic_ns(int64_t *value) {
	struct timespec ts;
	if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) {
		return -1;
	}
	*value = ((int64_t)ts.tv_sec * 1000000000LL) + (int64_t)ts.tv_nsec;
	return 0;
}
*/
import "C"

import "fmt"

func monotonicNanoseconds() (int64, error) {
	var value C.int64_t
	if C.e0_monotonic_ns(&value) != 0 {
		return 0, fmt.Errorf("clock_gettime(CLOCK_MONOTONIC) failed")
	}
	return int64(value), nil
}
