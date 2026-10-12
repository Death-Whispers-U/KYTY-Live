// Kyty Live hot-patch SDK. Include from exactly one .cpp file that builds kyty_hot.dll.
//
//   HOT_FUNC(sceKernelUsleep, int, (uint32_t usec)) { ... }
//       Replaces the function with that name. The NID is computed from the name.
//   HOT_FUNC_SYMBOL("_ZN3sce2Np...C1Ev", LibContextCtor, void, (void* self)) { ... }
//       For names that are not C identifiers (C++ symbols). The second argument is any label.
//   HOT_FUNC_NID("fzyMKs9kim0", WaitEqueue, int, (...)) { ... }
//       When only the NID is known.
//
//   hot::log("text %d", n)        prints a line into the emulator log ("HotHLE patch: ...").
//   hot::original<Fn>("nid")      the emulator's own function (or its missing-import stub), so a
//                                 patch can wrap it. Call it lazily, inside the patched function.
//   hot::nid("name")              the NID of a name.
//
// Patched functions run on guest threads with the guest (System V) calling convention; the
// macros declare that for you. Guest pointers are host pointers, so structs can be used directly.
// Must match the emulator's src/loader/hotHle.h (ABI_VERSION).

#ifndef KYTY_HOT_H_
#define KYTY_HOT_H_

#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#define KYTY_HOT_ABI __attribute__((sysv_abi))

namespace hot {

constexpr uint32_t ABI_VERSION = 1;

struct Host {
	uint32_t version;
	void (*log)(const char* text);
	void* (*original)(const char* nid);
};

struct Entry {
	const char* nid;
	const char* name;
	void*       func;
};

inline const Host*& host() {
	static const Host* h = nullptr;
	return h;
}

inline void log(const char* format, ...) {
	char    buf[1024];
	va_list args;
	va_start(args, format);
	std::vsnprintf(buf, sizeof(buf), format, args);
	va_end(args);
	if (host() != nullptr) {
		host()->log(buf);
	}
}

inline std::string nid(const char* name) {
	// SHA-1 of name + fixed suffix; the first 8 digest bytes (little-endian) in a base64 variant.
	auto rotl = [](uint32_t x, int n) { return (x << n) | (x >> (32 - n)); };
	static const uint8_t suffix[16] = {0x51, 0x8d, 0x64, 0xa6, 0x35, 0xde, 0xd8, 0xc1,
	                                   0xe6, 0xb0, 0x39, 0xb1, 0xc3, 0xe5, 0x52, 0x30};
	std::vector<uint8_t> m(name, name + std::strlen(name));
	m.insert(m.end(), suffix, suffix + 16);
	const uint64_t bits = uint64_t(m.size()) * 8;
	m.push_back(0x80);
	while (m.size() % 64 != 56) m.push_back(0);
	for (int i = 7; i >= 0; i--) m.push_back(uint8_t(bits >> (i * 8)));

	uint32_t h[5] = {0x67452301u, 0xefcdab89u, 0x98badcfeu, 0x10325476u, 0xc3d2e1f0u};
	for (size_t off = 0; off < m.size(); off += 64) {
		uint32_t w[80];
		for (int i = 0; i < 16; i++)
			w[i] = uint32_t(m[off + i * 4]) << 24 | uint32_t(m[off + i * 4 + 1]) << 16 |
			       uint32_t(m[off + i * 4 + 2]) << 8 | m[off + i * 4 + 3];
		for (int i = 16; i < 80; i++) w[i] = rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
		uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
		for (int i = 0; i < 80; i++) {
			uint32_t f, k;
			if (i < 20) { f = (b & c) | (~b & d); k = 0x5a827999u; }
			else if (i < 40) { f = b ^ c ^ d; k = 0x6ed9eba1u; }
			else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdcu; }
			else { f = b ^ c ^ d; k = 0xca62c1d6u; }
			uint32_t t = rotl(a, 5) + f + e + k + w[i];
			e = d; d = c; c = rotl(b, 30); b = a; a = t;
		}
		h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e;
	}
	uint8_t digest[8];
	for (int i = 0; i < 2; i++)
		for (int j = 0; j < 4; j++) digest[i * 4 + j] = uint8_t(h[i] >> (24 - j * 8));
	uint64_t v = 0;
	std::memcpy(&v, digest, 8);
	static const char codes[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-";
	std::string out(11, '\0');
	for (int i = 0; i < 10; i++) out[i] = codes[(v >> (58 - i * 6)) & 0x3f];
	out[10] = codes[(v & 0xf) * 4];
	return out;
}

template <typename Fn>
Fn original(const char* nid_text) {
	return host() != nullptr ? reinterpret_cast<Fn>(host()->original(nid_text)) : nullptr;
}

struct Registry {
	std::vector<std::string> nids;
	std::vector<std::string> names;
	std::vector<void*>       funcs;
	std::vector<Entry>       entries;

	static Registry& get() {
		static Registry r;
		return r;
	}
};

struct Registrar {
	Registrar(const std::string& nid_text, const char* name, void* func) {
		auto& r = Registry::get();
		r.nids.push_back(nid_text);
		r.names.push_back(name);
		r.funcs.push_back(func);
	}
};

} // namespace hot

#define KYTY_HOT_DEFINE(nid_expr, label, name_text, ret, params)                                   \
	static KYTY_HOT_ABI ret label params;                                                          \
	static ::hot::Registrar kyty_hot_registrar_##label(nid_expr, name_text,                        \
	                                                   reinterpret_cast<void*>(&label));           \
	static KYTY_HOT_ABI ret label params

#define HOT_FUNC(name, ret, params) KYTY_HOT_DEFINE(::hot::nid(#name), name, #name, ret, params)
#define HOT_FUNC_SYMBOL(symbol, label, ret, params)                                                \
	KYTY_HOT_DEFINE(::hot::nid(symbol), label, symbol, ret, params)
#define HOT_FUNC_NID(nid_text, label, ret, params)                                                 \
	KYTY_HOT_DEFINE(std::string(nid_text), label, #label, ret, params)

#ifndef KYTY_HOT_NO_EXPORT
extern "C" __declspec(dllexport) uint32_t kyty_hot_abi_version() {
	return hot::ABI_VERSION;
}

extern "C" __declspec(dllexport) const hot::Entry* kyty_hot_entries(const hot::Host* h,
                                                                     uint32_t*        count) {
	if (h == nullptr || h->version != hot::ABI_VERSION) {
		*count = 0;
		return nullptr;
	}
	hot::host() = h;
	auto& r     = hot::Registry::get();
	r.entries.clear();
	for (size_t i = 0; i < r.nids.size(); i++) {
		r.entries.push_back({r.nids[i].c_str(), r.names[i].c_str(), r.funcs[i]});
	}
	*count = static_cast<uint32_t>(r.entries.size());
	return r.entries.data();
}
#endif

#endif // KYTY_HOT_H_
