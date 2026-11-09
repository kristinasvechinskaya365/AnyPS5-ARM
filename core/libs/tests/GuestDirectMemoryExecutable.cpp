#include "prx/libkernel/include/Api.hpp"
#include "prx/libkernel/DirectMemory/DirectMemory.hpp"
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string_view>
#include <system_error>
#if defined(__APPLE__)
#include <mach/mach.h>
#include <mach/mach_vm.h>
#endif

namespace {

constexpr std::size_t Page = PS5_PAGE_SIZE;
constexpr int Read = 1;
constexpr int Write = 2;
constexpr int Execute = 4;
constexpr int Fixed = 0x10;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

void Cleanup(int result) {
    if (result == 0) return;
    std::fprintf(stderr, "direct memory executable cleanup failed: 0x%x\n", static_cast<unsigned>(result));
    std::abort();
}

class Allocation {
public:
    explicit Allocation(std::size_t bytes) : bytes(bytes) {
        Require(sceKernelAllocateMainDirectMemory(bytes, 0, 0, &physical) == 0, "allocate direct memory");
        Require(physical >= 0, "allocation returned a negative physical address");
    }
    ~Allocation() {
        for (std::size_t offset = 0; offset < bytes; offset += Page)
            Cleanup(sceKernelReleaseDirectMemory(physical + static_cast<std::int64_t>(offset), Page));
    }
    Allocation(const Allocation&) = delete;
    Allocation& operator=(const Allocation&) = delete;
    std::int64_t Physical(std::size_t offset = 0) const { return physical + static_cast<std::int64_t>(offset); }
    void Release(std::size_t offset, std::size_t length) {
        Require(sceKernelReleaseDirectMemory(Physical(offset), length) == 0, "release direct memory");
    }

private:
    std::size_t bytes;
    std::int64_t physical = -1;
};

class Mapping {
public:
    Mapping(std::int64_t physical, std::size_t bytes, int protection = Read | Write) : bytes(bytes) {
        Require(sceKernelMapDirectMemory(&address, bytes, protection, 0, physical, 0) == 0, "map direct memory");
        Require(address != nullptr, "mapping returned a null address");
    }
    ~Mapping() {
        if (address != nullptr) Cleanup(sceKernelMunmap(reinterpret_cast<std::uint64_t>(address), bytes));
    }
    Mapping(const Mapping&) = delete;
    Mapping& operator=(const Mapping&) = delete;
    unsigned char* Data(std::size_t offset = 0) const { return static_cast<unsigned char*>(address) + offset; }
    void Unmap() {
        Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(address), bytes) == 0, "unmap direct memory");
        address = nullptr;
    }

private:
    std::size_t bytes;
    void* address = nullptr;
};

void Protect(const void* address, std::size_t bytes, int protection) {
    Require(sceKernelMprotect(address, bytes, protection) == 0, "protect direct memory");
}

void WriteReturn(void* address, std::uint32_t value) {
    std::array<unsigned char, 6> code{0xb8, 0, 0, 0, 0, 0xc3};
    std::memcpy(code.data() + 1, &value, sizeof(value));
    std::memcpy(address, code.data(), code.size());
}

int Call(const void* address) {
    using Function = int (*)();
    Function function;
    static_assert(sizeof(function) == sizeof(address));
    std::memcpy(&function, &address, sizeof(function));
    return function();
}

void CheckTransition() {
    Allocation allocation(Page);
    Mapping mapping(allocation.Physical(), Page);
    WriteReturn(mapping.Data(), 42);
    try {
        Protect(mapping.Data(), Page, Read | Execute);
    } catch (const std::system_error& error) {
        std::fprintf(stderr, "direct memory RW-to-RX failure: error=%d category=%s\n", error.code().value(), error.code().category().name());
        throw;
    }
    Require(Call(mapping.Data()) == 42, "RW-to-RX code returned the wrong value");
    Protect(mapping.Data(), Page, Read | Write);
    WriteReturn(mapping.Data(), 43);
    Protect(mapping.Data(), Page, Read | Execute);
    Require(Call(mapping.Data()) == 43, "RX-to-RW-to-RX rewrite returned stale code");
    Protect(mapping.Data(), Page, Read | Write | Execute);
    WriteReturn(mapping.Data(), 44);
    Require(Call(mapping.Data()) == 44, "same-view RWX rewrite returned stale code");
    Mapping inaccessible(allocation.Physical(), Page, 0);
    Protect(inaccessible.Data(), Page, Read | Execute);
    Require(Call(inaccessible.Data()) == 44, "initially inaccessible alias lost executable bytes");
}

void CheckAliases() {
    Allocation allocation(Page * 3);
    Mapping writable(allocation.Physical(Page), Page);
    Mapping executable(allocation.Physical(Page), Page, Read);
    Require(writable.Data() != executable.Data(), "aliases used the same virtual address");
    Require(executable.Data()[0] == 0 && executable.Data()[Page - 1] == 0, "new direct memory was not zero-filled");
    WriteReturn(writable.Data(), 0x12345678);
    writable.Data()[Page - 1] = 0x5a;
    Require(std::memcmp(writable.Data(), executable.Data(), 6) == 0 && executable.Data()[Page - 1] == 0x5a, "offset aliases did not share bytes");
    Protect(executable.Data(), Page, Read | Execute);
    Require(Call(executable.Data()) == 0x12345678, "executable offset alias returned the wrong value");
    WriteReturn(writable.Data(), 0x13572468);
    Protect(executable.Data(), Page, Read);
    Protect(executable.Data(), Page, Read | Execute);
    Require(Call(executable.Data()) == 0x13572468, "alias rewrite stayed stale after protection transition");
    writable.Unmap();
    Require(Call(executable.Data()) == 0x13572468, "unmapping the writable alias lost executable bytes");
    executable.Unmap();
    Mapping remapped(allocation.Physical(Page), Page, Read);
    Require(remapped.Data()[Page - 1] == 0x5a, "unmapping all views lost the backing bytes");
    Protect(remapped.Data(), Page, Read | Execute);
    Require(Call(remapped.Data()) == 0x13572468, "remapping the backing lost executable bytes");
}

void CheckPartialRelease() {
    Allocation allocation(Page * 3);
    Mapping writable(allocation.Physical(), Page * 3);
    Mapping executable(allocation.Physical(), Page * 3, Read);
    WriteReturn(writable.Data(), 11);
    WriteReturn(writable.Data(Page), 22);
    WriteReturn(writable.Data(Page * 2), 33);
    Protect(executable.Data(), Page * 3, Read | Execute);
    allocation.Release(Page, Page);
    Require(Call(executable.Data()) == 11 && Call(executable.Data(Page * 2)) == 33, "partial physical release damaged surviving executable pages");
    void* missing = nullptr;
    Require(sceKernelMapDirectMemory(&missing, Page, Read, 0, allocation.Physical(Page), 0) == SCE_KERNEL_ERROR_ENOMEM && missing == nullptr, "released physical page remained mappable");
    Mapping tail(allocation.Physical(Page * 2), Page, Read);
    Protect(tail.Data(), Page, Read | Execute);
    Require(Call(tail.Data()) == 33, "partial release lost the backing offset");
    WriteReturn(writable.Data(Page * 2), 44);
    Protect(tail.Data(), Page, Read);
    Protect(tail.Data(), Page, Read | Execute);
    Require(Call(tail.Data()) == 44, "surviving offset alias stopped sharing writes");
    allocation.Release(0, Page);
    writable.Unmap();
    executable.Unmap();
    Require(Call(tail.Data()) == 44, "releasing the prefix lost the final executable alias");
}

void CheckReplacement() {
    Allocation original(Page * 3);
    Allocation replacement(Page);
    Mapping mapped(original.Physical(), Page * 3);
    Mapping oldAlias(original.Physical(Page), Page, Read);
    mapped.Data()[0] = 0x5a;
    mapped.Data()[Page * 3 - 1] = 0xa5;
    WriteReturn(mapped.Data(Page), 51);
    Protect(oldAlias.Data(), Page, Read | Execute);
    void* middle = mapped.Data(Page);
    Require(sceKernelMapDirectMemory(&middle, Page, Read | Write, Fixed, replacement.Physical(), 0) == 0 && middle == mapped.Data(Page), "fixed direct-memory replacement failed");
    WriteReturn(middle, 73);
    Protect(middle, Page, Read | Execute);
    Mapping newAlias(replacement.Physical(), Page, Read);
    Protect(newAlias.Data(), Page, Read | Execute);
    Require(Call(middle) == 73 && Call(newAlias.Data()) == 73, "replacement did not share executable bytes");
    Require(Call(oldAlias.Data()) == 51, "replacement changed the previous backing alias");
    Require(mapped.Data()[0] == 0x5a && mapped.Data()[Page * 3 - 1] == 0xa5, "replacement damaged adjacent physical pages");
    mapped.Unmap();
    Require(Call(oldAlias.Data()) == 51 && Call(newAlias.Data()) == 73, "unmapping a mixed backing range damaged aliases");
}

void CheckRejections() {
    Allocation allocation(Page * 2);
    Mapping writable(allocation.Physical(), Page);
    Mapping readable(allocation.Physical(), Page, Read);
    WriteReturn(writable.Data(), 42);
    void* address = nullptr;
    Require(sceKernelMapDirectMemory(&address, Page, Read | Execute, 0, allocation.Physical(), 0) == SCE_KERNEL_ERROR_EINVAL && address == nullptr, "initial executable direct mapping was not rejected");
    allocation.Release(Page, Page);
    address = writable.Data();
    Require(sceKernelMapDirectMemory(&address, Page, Read | Write, Fixed, allocation.Physical(Page), 0) == SCE_KERNEL_ERROR_ENOMEM && address == writable.Data(), "fixed map accepted released physical memory");
    Require(std::memcmp(writable.Data(), readable.Data(), 6) == 0 && readable.Data()[1] == 42, "rejected fixed map damaged a live alias");
#if defined(__APPLE__)
    Protect(readable.Data(), Page, Read);
    Require(mach_vm_protect(mach_task_self(), reinterpret_cast<mach_vm_address_t>(readable.Data()), Page, TRUE, VM_PROT_READ) == KERN_SUCCESS, "reduce the test view's maximum protection");
    bool rejected = false;
    try {
        Protect(readable.Data(), Page, Read | Execute);
    } catch (const std::system_error& error) {
        Require(error.code() == std::errc::permission_denied, "unexpected maximum-protection failure");
        rejected = true;
    }
    Require(rejected, "protection exceeded the host view's maximum");
    VirtualQueryInfo info{};
    Require(sceKernelVirtualQuery(readable.Data(), 0, &info, sizeof(info)) == 0 && (info.protection & 7) == Read, "failed protection changed the reported rights");
    WriteReturn(writable.Data(), 43);
    Require(readable.Data()[1] == 43, "failed protection damaged another live alias");
#endif
}

}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: guest_direct_memory_executable_tests transition|aliases|partial_release|replacement|rejections\n");
        return 2;
    }
    try {
        Require(sceKernelEnableDmemAliasing() == 0, "enable direct-memory aliases");
        const std::string_view test = argv[1];
        if (test == "transition") CheckTransition();
        else if (test == "aliases") CheckAliases();
        else if (test == "partial_release") CheckPartialRelease();
        else if (test == "replacement") CheckReplacement();
        else if (test == "rejections") CheckRejections();
        else throw std::invalid_argument("unknown direct-memory executable test");
        std::printf("direct memory executable %s passed\n", argv[1]);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "direct memory executable check failed: %s\n", error.what());
        return 1;
    }
}
