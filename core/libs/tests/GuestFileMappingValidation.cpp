#include "prx/libkernel/include/Api.hpp"
#include "prx/libkernel/DirectMemory/DirectMemory.hpp"
#include "prx/libkernel/File/include/File.hpp"
#include "prx/libkernel/File/include/FileFlags.hpp"
#include "prx/libc/include/Api.hpp"
#include "prx/libc/include/GuestAllocations.hpp"
#include <array>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <stdexcept>
#include <string_view>
#include <sys/mman.h>
#include <unistd.h>

namespace {

constexpr std::size_t Page = PS5_PAGE_SIZE;
constexpr int Shared = 1;
constexpr int Private = 2;
constexpr int Fixed = 0x10;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

void Cleanup(int result) {
    if (result == 0) return;
    std::fprintf(stderr, "file mapping cleanup failed: 0x%x\n", static_cast<unsigned>(result));
    std::abort();
}

class TemporaryFile {
public:
    TemporaryFile() : descriptor(::mkstemp(path.data())) {
        Require(descriptor >= 0, "create mapping test file");
    }
    ~TemporaryFile() {
        Cleanup(::close(descriptor));
        Cleanup(::unlink(path.data()));
    }
    TemporaryFile(const TemporaryFile&) = delete;
    TemporaryFile& operator=(const TemporaryFile&) = delete;
    const char* Path() const { return path.data(); }
    void Seed() const {
        std::array<unsigned char, Page> bytes{};
        for (std::size_t page = 0; page < 3; ++page) {
            bytes.fill(static_cast<unsigned char>(0x61 + page * 0x10));
            Require(::pwrite(descriptor, bytes.data(), bytes.size(), static_cast<off_t>(page * Page)) == static_cast<ssize_t>(bytes.size()), "seed mapping test file");
        }
    }

private:
    std::array<char, 34> path{"anyps5-file-validation-XXXXXX"};
    int descriptor;
};

class Descriptor {
public:
    Descriptor(const char* path, int flags) : descriptor(sceKernelOpen(path, flags, 0)) {
        Require(descriptor >= 0, "open mapping test descriptor");
    }
    ~Descriptor() { Cleanup(sceKernelClose(descriptor)); }
    Descriptor(const Descriptor&) = delete;
    Descriptor& operator=(const Descriptor&) = delete;
    int Get() const { return descriptor; }
    unsigned char Byte(std::size_t offset) const {
        unsigned char byte = 0;
        Require(sceKernelPread(descriptor, &byte, 1, static_cast<std::int64_t>(offset)) == 1, "read mapped file byte");
        return byte;
    }

private:
    int descriptor;
};

class Allocation {
public:
    Allocation() {
        Require(sceKernelAllocateMainDirectMemory(Page * 3, 0, 0, &physical) == 0, "allocate original direct memory");
    }
    ~Allocation() { Cleanup(sceKernelReleaseDirectMemory(physical, Page * 3)); }
    Allocation(const Allocation&) = delete;
    Allocation& operator=(const Allocation&) = delete;
    std::int64_t Physical() const { return physical; }

private:
    std::int64_t physical = -1;
};

class Mapping {
public:
    explicit Mapping(std::int64_t physical) {
        Require(sceKernelMapDirectMemory(&address, Page * 3, 3, 0, physical, 0) == 0 && address != nullptr, "map original direct memory");
    }
    ~Mapping() {
        if (address != nullptr) Cleanup(sceKernelMunmap(reinterpret_cast<std::uint64_t>(address), Page * 3));
    }
    Mapping(const Mapping&) = delete;
    Mapping& operator=(const Mapping&) = delete;
    unsigned char* Data(std::size_t offset = 0) const { return static_cast<unsigned char*>(address) + offset; }
    void Unmap() {
        Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(address), Page * 3) == 0, "unmap replaced file view");
        address = nullptr;
    }

private:
    void* address = nullptr;
};

struct Snapshot {
    VirtualQueryInfo query;
    GuestAllocations::Range allocation;
};

Snapshot Capture(const void* address) {
    Snapshot snapshot{};
    Require(sceKernelVirtualQuery(address, 0, &snapshot.query, sizeof(snapshot.query)) == 0, "query original mapping");
    const auto allocation = GuestAllocations::GuestAllocationsLookup_nid_postfix(reinterpret_cast<std::uintptr_t>(address), false);
    Require(allocation != nullptr, "find original allocation");
    snapshot.allocation = *allocation;
    return snapshot;
}

void RequirePreserved(const void* address, const Snapshot& before) {
    VirtualQueryInfo after{};
    const bool queried = sceKernelVirtualQuery(address, 0, &after, sizeof(after)) == 0;
    const auto& query = before.query;
    Require(queried && after.start == query.start && after.end == query.end && after.offset == query.offset &&
            after.protection == query.protection && after.memory_type == query.memory_type && after.is_direct == query.is_direct &&
            after.is_flexible == query.is_flexible && after.is_committed == query.is_committed,
            "rejected file mapping changed original mapping metadata");
    const auto allocation = GuestAllocations::GuestAllocationsLookup_nid_postfix(reinterpret_cast<std::uintptr_t>(address), false);
    const auto& original = before.allocation;
    Require(allocation != nullptr && allocation->address == original.address && allocation->bytes == original.bytes &&
            allocation->allocationAddress == original.allocationAddress && allocation->allocationBytes == original.allocationBytes &&
            allocation->backingSerial == original.backingSerial && allocation->readable == original.readable && allocation->writable == original.writable,
            "rejected file mapping changed original allocation ownership");
}

void CheckRejectedReplacement(int kind, bool overlap) {
    TemporaryFile file;
    file.Seed();
    Descriptor writer(file.Path(), SCE_KERNEL_O_WRONLY);
    Descriptor reader(file.Path(), SCE_KERNEL_O_RDONLY);
    Descriptor readWrite(file.Path(), SCE_KERNEL_O_RDWR);
    Allocation allocation;
    Mapping mapped(allocation.Physical());
    Mapping alias(allocation.Physical());
    for (std::size_t page = 0; page < 3; ++page) {
        mapped.Data(page * Page)[0] = static_cast<unsigned char>(0x31 + page);
        mapped.Data(page * Page)[Page - 1] = static_cast<unsigned char>(0x41 + page);
    }
    Require(sceKernelMprotect(mapped.Data(Page), Page, 1) == 0, "protect original middle page");
    if (overlap) Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(mapped.Data(Page * 2)), Page) == 0, "open a hole after the original middle page");
    const std::size_t survivingPages = overlap ? 2 : 3;
    std::array<Snapshot, 3> before{};
    for (std::size_t page = 0; page < survivingPages; ++page) before[page] = Capture(mapped.Data(page * Page));
    const auto rejected = [&](int protection) {
        *__error_nid_postfix() = 0;
        const auto result = mmap_nid_postfix(mapped.Data(Page), Page * 2, protection, kind | Fixed, writer.Get(), Page);
        const int error = *__error_nid_postfix();
        std::fprintf(stderr, "file mapping rejection kind=%d protection=%d overlap=%d result=%p errno=%d\n", kind, protection, overlap, result, error);
        Require(result == MAP_FAILED && error == 13, "write-only file mapping did not return EACCES");
        for (std::size_t page = 0; page < survivingPages; ++page) {
            RequirePreserved(mapped.Data(page * Page), before[page]);
            Require(mapped.Data(page * Page)[0] == 0x31 + page && mapped.Data(page * Page)[Page - 1] == 0x41 + page,
                    "rejected file mapping changed original bytes");
        }
    };
    for (const int protection : {2, 4, 6, 1, 3, 5, 7}) rejected(protection);
#ifdef __linux__
    rejected(0);
#endif
    alias.Data(Page)[0] = 0x54;
    Require(mapped.Data(Page)[0] == 0x54, "rejected file mapping broke a surviving alias");
    Require(sceKernelMprotect(mapped.Data(Page), Page, 3) == 0, "restore original write permission");
    mapped.Data(Page)[1] = 0x65;
    Require(alias.Data(Page)[1] == 0x65, "original mapping could not be written after rejection");
    const int descriptor = kind == Shared ? readWrite.Get() : reader.Get();
    Require(mmap_nid_postfix(mapped.Data(Page), Page * 2, 3, kind | Fixed, descriptor, Page) == mapped.Data(Page), "valid file replacement failed after rejection");
    Require(mapped.Data(Page)[0] == 0x71 && mapped.Data(Page * 2)[0] == 0x81, "valid replacement used the wrong file offset");
    Require(alias.Data(Page)[0] == 0x54 && alias.Data(Page * 2)[0] == 0x33, "valid file replacement changed the original physical alias");
    mapped.Data(Page)[3] = 0x91;
    mapped.Data(Page * 2)[4] = 0xa2;
    Require(reader.Byte(Page + 3) == (kind == Shared ? 0x91 : 0x71) &&
            reader.Byte(Page * 2 + 4) == (kind == Shared ? 0xa2 : 0x81), "valid replacement lost shared or private file semantics");
    mapped.Unmap();
    Require(alias.Data(Page)[0] == 0x54 && alias.Data(Page)[1] == 0x65, "unmapping the file replacement damaged the surviving physical alias");
}

void CheckControls() {
    TemporaryFile file;
    file.Seed();
    Descriptor writer(file.Path(), SCE_KERNEL_O_WRONLY);
    Descriptor reader(file.Path(), SCE_KERNEL_O_RDONLY);
    for (const int kind : {Shared, Private}) {
        errno = 0;
        void* native = ::mmap(nullptr, Page, PROT_NONE, kind == Shared ? MAP_SHARED : MAP_PRIVATE, writer.Get(), 0);
        const int nativeError = errno;
        *__error_nid_postfix() = 0;
        void* guest = mmap_nid_postfix(nullptr, Page, 0, kind, writer.Get(), 0);
        const int guestError = *__error_nid_postfix();
        const bool same = (native == MAP_FAILED) == (guest == MAP_FAILED) &&
            (native != MAP_FAILED || (nativeError == EACCES && guestError == 13));
        if (native != MAP_FAILED) Cleanup(::munmap(native, Page));
        if (guest != MAP_FAILED) Cleanup(munmap_nid_postfix(guest, Page));
        Require(same, "PROT_NONE write-only mapping no longer matches the host contract");
        std::fprintf(stderr, "file mapping PROT_NONE kind=%d native=%p native_errno=%d guest=%p guest_errno=%d\n", kind, native, nativeError, guest, guestError);
        auto* readable = static_cast<unsigned char*>(mmap_nid_postfix(nullptr, Page, 1, kind, reader.Get(), Page));
        Require(readable != MAP_FAILED, "valid read-only file mapping failed");
        const bool correct = readable[0] == 0x71 && readable[Page - 1] == 0x71;
        Cleanup(munmap_nid_postfix(readable, Page));
        Require(correct, "valid read-only file mapping returned incorrect bytes");
    }
    int ends[2] = {-1, -1};
    Require(::pipe(ends) == 0, "create unsupported mapping descriptor");
    bool preserved = true;
    for (const int kind : {Shared, Private}) {
        for (int protection = 0; protection < 8; ++protection) {
            *__error_nid_postfix() = 0;
            void* mapped = mmap_nid_postfix(nullptr, Page, protection, kind, ends[1], 0);
            const int error = *__error_nid_postfix();
            preserved = preserved && mapped == MAP_FAILED && error == ((protection & 1) != 0 ? 13 : 45);
            if (mapped != MAP_FAILED) Cleanup(munmap_nid_postfix(mapped, Page));
        }
    }
    Cleanup(::close(ends[0]));
    Cleanup(::close(ends[1]));
    Require(preserved, "unsupported descriptor error precedence changed");
}

}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: guest_file_mapping_validation_tests <case>\n");
        return 2;
    }
    try {
        const std::string_view test = argv[1];
        if (test == "shared_covered") CheckRejectedReplacement(Shared, false);
        else if (test == "shared_overlap") CheckRejectedReplacement(Shared, true);
        else if (test == "private_covered") CheckRejectedReplacement(Private, false);
        else if (test == "private_overlap") CheckRejectedReplacement(Private, true);
        else if (test == "controls") CheckControls();
        else throw std::invalid_argument("unknown file mapping validation case");
        std::printf("file mapping validation %s passed\n", argv[1]);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "file mapping validation failed [%s]: %s\n", argv[1], error.what());
        return 1;
    }
}
