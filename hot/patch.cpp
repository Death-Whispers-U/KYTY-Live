// Hot patch: edit and save; Kyty Live rebuilds kyty_hot.dll and the running game picks it up.
// See kyty_hot.h for the macros. Each saved version starts with fresh static state.

#include "kyty_hot.h"

#include <atomic>

// Example: wrap sceKernelWaitEqueue and report how often the game calls it.
// Change the text and save to watch the swap happen while the game runs.

using WaitEqueueFn = KYTY_HOT_ABI int (*)(int64_t eq, void* ev, int num, int* out, const uint32_t* timo);

static std::atomic<uint64_t> g_calls {0};

HOT_FUNC(sceKernelWaitEqueue, int, (int64_t eq, void* ev, int num, int* out, const uint32_t* timo)) {
	static const auto real = hot::original<WaitEqueueFn>(hot::nid("sceKernelWaitEqueue").c_str());
	const auto        n    = ++g_calls;
	if (n % 500 == 0) {
		hot::log("v1: sceKernelWaitEqueue called %llu times", static_cast<unsigned long long>(n));
	}
	return real(eq, ev, num, out, timo);
}
