#include "prx/libkernel/include/Api.hpp"
#include "prx/libkernel/DirectMemory/DirectMemory.hpp"
#include "prx/libc/include/Api.hpp"
#include "prx/libc/include/GuestAllocations.hpp"
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace {

constexpr std::size_t Page = PS5_PAGE_SIZE;
constexpr int Fixed = 0x10;
constexpr int NoOverwrite = 0x80;
constexpr int NoCoalesce = 0x400000;
constexpr int InvalidFlag = 0x100;

void Require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

void Cleanup(int result) {
    if (result == 0) return;
    std::fprintf(stderr, "fixed mapping cleanup failed: 0x%x\n", static_cast<unsigned>(result));
    std::abort();
}

class Allocation {
public:
    explicit Allocation(std::size_t bytes) : bytes(bytes) {
        Require(sceKernelAllocateMainDirectMemory(bytes, 0, 0, &physical) == 0, "allocate direct memory");
    }
    ~Allocation() {
        for (std::size_t offset = 0; offset < bytes; offset += Page)
            Cleanup(sceKernelReleaseDirectMemory(Physical(offset), Page));
    }
    Allocation(const Allocation&) = delete;
    Allocation& operator=(const Allocation&) = delete;
    std::int64_t Physical(std::size_t offset = 0) const { return physical + static_cast<std::int64_t>(offset); }
    void SplitBacking() {
        Require(sceKernelReleaseDirectMemory(physical, bytes) == 0, "release backing before splitting");
        for (std::size_t offset = 0; offset < bytes; offset += Page) {
            std::int64_t allocated = -1;
            Require(sceKernelAllocateDirectMemory(Physical(offset), Physical(offset + Page), Page, 0, 0, &allocated) == 0 && allocated == Physical(offset), "allocate adjacent independent backing");
        }
    }

private:
    std::size_t bytes;
    std::int64_t physical = -1;
};

class Mapping {
public:
    Mapping(std::int64_t physical, std::size_t bytes, std::size_t alignment = 0) : bytes(bytes) {
        Require(sceKernelMapDirectMemory(&address, bytes, 3, 0, physical, alignment) == 0 && address != nullptr, "map direct memory");
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
            "rejected fixed request changed original mapping metadata");
    const auto allocation = GuestAllocations::GuestAllocationsLookup_nid_postfix(reinterpret_cast<std::uintptr_t>(address), false);
    const auto& original = before.allocation;
    Require(allocation != nullptr && allocation->address == original.address && allocation->bytes == original.bytes &&
            allocation->allocationAddress == original.allocationAddress && allocation->allocationBytes == original.allocationBytes &&
            allocation->backingSerial == original.backingSerial && allocation->readable == original.readable && allocation->writable == original.writable,
            "rejected fixed request changed original allocation ownership");
}

enum class Operation { Direct, Flexible, Reserve };
enum class Invalid { Flags, Alignment, AddressAlignment };

void CheckRejectedReplacement(Operation operation, Invalid invalid, bool overlap) {
    Allocation original(Page * 3);
    Allocation replacement(Page * 2);
    replacement.SplitBacking();
    Mapping mapped(original.Physical(), Page * 3, Page * 2);
    Mapping alias(original.Physical(), Page * 3);
    Mapping replacementAlias(replacement.Physical(), Page * 2);
    for (std::size_t page = 0; page < 3; ++page) {
        mapped.Data(page * Page)[0] = static_cast<unsigned char>(0x31 + page);
        mapped.Data(page * Page)[Page - 1] = static_cast<unsigned char>(0x41 + page);
    }
    replacementAlias.Data()[0] = 0x71;
    replacementAlias.Data(Page)[0] = 0x72;
    Require(sceKernelMprotect(mapped.Data(Page), Page, 1) == 0, "protect original middle page");
    if (overlap) Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(mapped.Data(Page * 2)), Page) == 0, "open a hole after the original middle page");
    const std::size_t survivingPages = overlap ? 2 : 3;
    std::array<Snapshot, 3> before{};
    for (std::size_t page = 0; page < survivingPages; ++page) before[page] = Capture(mapped.Data(page * Page));
    const int flags = Fixed | (invalid == Invalid::Flags ? InvalidFlag : 0);
    const std::size_t alignment = invalid == Invalid::Alignment ? Page * 3 : invalid == Invalid::AddressAlignment ? Page * 2 : 0;
    void* address = mapped.Data(Page);
    bool rejected = false;
    try {
        int result = 0;
        if (operation == Operation::Direct)
            result = sceKernelMapDirectMemory(&address, Page * 2, 3, flags, replacement.Physical(), alignment);
        else if (operation == Operation::Flexible)
            result = sceKernelMapFlexibleMemory(&address, Page * 2, 3, flags);
        else
            result = sceKernelReserveVirtualRange(&address, Page * 2, flags, alignment);
        Require(result == 0, "invalid fixed request returned an unexpected kernel error");
    } catch (const std::invalid_argument& error) {
        std::fprintf(stderr, "fixed request rejected: %s\n", error.what());
        rejected = true;
    }
    Require(rejected, "invalid fixed request was accepted");
    Require(address == mapped.Data(Page), "rejected fixed request changed its output address");
    for (std::size_t page = 0; page < survivingPages; ++page) {
        RequirePreserved(mapped.Data(page * Page), before[page]);
        Require(mapped.Data(page * Page)[0] == 0x31 + page && mapped.Data(page * Page)[Page - 1] == 0x41 + page,
                "rejected fixed request changed original bytes");
    }
    alias.Data(Page)[0] = 0x54;
    Require(mapped.Data(Page)[0] == 0x54, "rejected fixed request broke a live physical alias");
    Require(sceKernelMprotect(mapped.Data(Page), Page, 3) == 0, "restore original write permission");
    mapped.Data(Page)[1] = 0x65;
    Require(alias.Data(Page)[1] == 0x65, "original mapping could not be written after rejection");
    address = mapped.Data(Page);
    Require(sceKernelMapDirectMemory(&address, Page * 2, 3, Fixed | NoCoalesce, replacement.Physical(), 0) == 0 && address == mapped.Data(Page), "valid replacement failed after rejection");
    Require(mapped.Data(Page)[0] == 0x71 && mapped.Data(Page * 2)[0] == 0x72, "replacement did not span both physical backing blocks");
    Require(alias.Data(Page)[0] == 0x54 && alias.Data(Page * 2)[0] == 0x33, "valid replacement changed the original backing alias");
    mapped.Data(Page)[1] = 0x9a;
    mapped.Data(Page * 2)[2] = 0x8b;
    Require(replacementAlias.Data()[1] == 0x9a && replacementAlias.Data(Page)[2] == 0x8b, "valid replacement lost physical sharing");
    mapped.Unmap();
    Require(alias.Data(Page)[0] == 0x54 && replacementAlias.Data()[1] == 0x9a, "unmapping the replacement damaged surviving aliases");
}

void CheckControls() {
    Allocation allocation(Page);
    Mapping mapped(allocation.Physical(), Page);
    mapped.Data()[0] = 0x5a;
    const auto before = Capture(mapped.Data());
    void* address = mapped.Data();
    Require(sceKernelMapDirectMemory(&address, Page, 3, Fixed | NoOverwrite, allocation.Physical(), 0) == SCE_KERNEL_ERROR_ENOMEM,
            "no-overwrite did not reject a live mapping");
    Require(address == mapped.Data(), "no-overwrite changed its output address");
    RequirePreserved(mapped.Data(), before);
    const auto failed = reinterpret_cast<void*>(std::numeric_limits<std::uintptr_t>::max());
    Require(mmap_nid_postfix(mapped.Data() + 1, Page, 3, 0x1012, -1, 0) == failed && *__error_nid_postfix() == 22,
            "unaligned POSIX fixed map did not report EINVAL");
    Require(mmap_nid_postfix(reinterpret_cast<void*>(~std::uintptr_t{Page - 1}), Page * 2, 3, 0x1012, -1, 0) == failed && *__error_nid_postfix() == 22,
            "overflowing POSIX fixed map did not report EINVAL");
    RequirePreserved(mapped.Data(), before);
    Require(mapped.Data()[0] == 0x5a, "rejected POSIX map changed live bytes");
    void* flexible = nullptr;
    Require(sceKernelMapFlexibleMemory(&flexible, Page, 3, 0x8000) == 0 && flexible != nullptr, "accepted flexible-memory heap flag was rejected");
    static_cast<unsigned char*>(flexible)[0] = 0x7b;
    Require(mmap_nid_postfix(flexible, Page, 3, 0x1012, -1, 0) == flexible, "accepted POSIX anonymous fixed map was rejected");
    Require(static_cast<unsigned char*>(flexible)[0] == 0, "valid anonymous replacement did not zero its pages");
    Cleanup(sceKernelMunmap(reinterpret_cast<std::uint64_t>(flexible), Page));
}

}

int main(int argc, char** argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: guest_fixed_mapping_validation_tests <case>\n");
        return 2;
    }
    try {
        const std::string_view test = argv[1];
        if (test == "direct_flags_covered") CheckRejectedReplacement(Operation::Direct, Invalid::Flags, false);
        else if (test == "direct_flags_overlap") CheckRejectedReplacement(Operation::Direct, Invalid::Flags, true);
        else if (test == "direct_alignment_covered" || test == "direct_alignment_overlap") {
            const bool overlap = test == "direct_alignment_overlap";
            CheckRejectedReplacement(Operation::Direct, Invalid::Alignment, overlap);
            CheckRejectedReplacement(Operation::Direct, Invalid::AddressAlignment, overlap);
        } else if (test == "flexible_flags_covered") CheckRejectedReplacement(Operation::Flexible, Invalid::Flags, false);
        else if (test == "flexible_flags_overlap") CheckRejectedReplacement(Operation::Flexible, Invalid::Flags, true);
        else if (test == "reserve_alignment") {
            CheckRejectedReplacement(Operation::Reserve, Invalid::Alignment, false);
            CheckRejectedReplacement(Operation::Reserve, Invalid::AddressAlignment, false);
        } else if (test == "controls") CheckControls();
        else throw std::invalid_argument("unknown fixed mapping validation case");
        std::printf("fixed mapping validation %s passed\n", argv[1]);
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "fixed mapping validation failed [%s]: %s\n", argv[1], error.what());
        return 1;
    }
}
