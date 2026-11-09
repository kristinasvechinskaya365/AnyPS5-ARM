#ifndef CORE_LIBS_PRX_LIBC_INCLUDE_GUESTVARARGS_HPP
#define CORE_LIBS_PRX_LIBC_INCLUDE_GUESTVARARGS_HPP

#include "SceTypes.hpp"

#if defined(_WIN32) || defined(__aarch64__)
#define APS5_SYSV_GUEST_ARGS 1
#else
#define APS5_SYSV_GUEST_ARGS 0
#endif

#if defined(_WIN32)
#define APS5_GUEST_VA_START(list, last, gp, fp) \
    __builtin_sysv_va_list list;                \
    __builtin_sysv_va_start(list, last)
#define APS5_GUEST_VA_END(list) __builtin_sysv_va_end(list)
#elif defined(__aarch64__)
namespace LibcDetail {

VaList* GuestVaStart(VaList* list, void* saveArea, unsigned gp, unsigned fp);
}
#define APS5_GUEST_VA_START(list, last, gp, fp)       \
    VaList list##Storage;                            \
    alignas(16) unsigned char list##Save[176];       \
    VaList* list = LibcDetail::GuestVaStart(&list##Storage, list##Save, gp, fp)
#define APS5_GUEST_VA_END(list) static_cast<void>(list)
#endif

#endif
