#include "prx/libkernel/include/Api.hpp"
#include "prx/libkernel/DirectMemory/DirectMemory.hpp"
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <limits>
#include <source_location>
#include <system_error>
#include <vector>
#include <unistd.h>
#if defined(__APPLE__)
#include <libproc.h>
#include <mach/mach.h>
#else
#include <dirent.h>
#endif

namespace {

void Require(bool condition, std::source_location location = std::source_location::current()) {
    if (!condition) {
        std::fprintf(stderr, "Direct memory lifecycle check failed at %s:%u\n", location.file_name(), static_cast<unsigned>(location.line()));
        std::abort();
    }
}

std::vector<int> OpenDescriptors() {
    std::vector<int> descriptors;
#if defined(__APPLE__)
    const int capacity = ::proc_pidinfo(::getpid(), PROC_PIDLISTFDS, 0, nullptr, 0);
    constexpr int extraBytes = static_cast<int>(16 * sizeof(proc_fdinfo));
    Require(capacity > 0 && capacity <= std::numeric_limits<int>::max() - extraBytes && capacity % sizeof(proc_fdinfo) == 0);
    const int bufferBytes = capacity + extraBytes;
    std::vector<proc_fdinfo> entries(static_cast<std::size_t>(bufferBytes) / sizeof(proc_fdinfo));
    const int bytes = ::proc_pidinfo(::getpid(), PROC_PIDLISTFDS, 0, entries.data(), bufferBytes);
    Require(bytes > 0 && bytes < bufferBytes && bytes % sizeof(proc_fdinfo) == 0);
    for (std::size_t index = 0; index < static_cast<std::size_t>(bytes) / sizeof(proc_fdinfo); ++index)
        descriptors.push_back(entries[index].proc_fd);
#else
    auto* directory = ::opendir("/proc/self/fd");
    Require(directory != nullptr);
    const int listing = ::dirfd(directory);
    errno = 0;
    while (const auto* entry = ::readdir(directory)) {
        if (entry->d_name[0] == '.') continue;
        char* end = nullptr;
        const long descriptor = std::strtol(entry->d_name, &end, 10);
        Require(end != entry->d_name && *end == '\0' && descriptor >= 0 && descriptor <= std::numeric_limits<int>::max());
        if (descriptor != listing) descriptors.push_back(static_cast<int>(descriptor));
    }
    Require(errno == 0);
    Require(::closedir(directory) == 0);
#endif
    std::sort(descriptors.begin(), descriptors.end());
    return descriptors;
}

void RequireDescriptors(const std::vector<int>& expected) {
    const auto actual = OpenDescriptors();
    if (actual != expected)
        std::fprintf(stderr, "Open descriptors changed: expected %zu, found %zu\n", expected.size(), actual.size());
    Require(actual == expected);
}

std::int64_t Allocate(std::size_t bytes) {
    std::int64_t physical = -1;
    Require(sceKernelAllocateMainDirectMemory(bytes, 0, 0, &physical) == 0);
    return physical;
}

unsigned char* Map(std::int64_t physical, std::size_t bytes, int protection = 3) {
    void* address = nullptr;
    Require(sceKernelMapDirectMemory(&address, bytes, protection, 0, physical, 0) == 0);
    Require(address != nullptr);
    return static_cast<unsigned char*>(address);
}

void ExerciseLifetime(bool unmapFirst) {
    const auto physical = Allocate(PS5_PAGE_SIZE);
    auto* first = Map(physical, PS5_PAGE_SIZE);
    auto* alias = Map(physical, PS5_PAGE_SIZE);
    Require(first != alias);
    Require(first[0] == 0 && alias[PS5_PAGE_SIZE - 1] == 0);
    first[0] = 37;
    alias[PS5_PAGE_SIZE - 1] = 91;
    Require(alias[0] == 37 && first[PS5_PAGE_SIZE - 1] == 91);
    Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(first), PS5_PAGE_SIZE) == 0);
    Require(alias[0] == 37);
    if (unmapFirst) Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(alias), PS5_PAGE_SIZE) == 0);
    Require(sceKernelReleaseDirectMemory(physical, PS5_PAGE_SIZE) == 0);
}

void CheckRepeatedLifetimes() {
    const auto baseline = OpenDescriptors();
    for (int round = 0; round < 2048; ++round) {
        ExerciseLifetime((round & 1) != 0);
        if ((round + 1) % 256 == 0) RequireDescriptors(baseline);
    }
    std::puts("2048 direct memory allocation, alias and release cycles retained no descriptors");
}

void CheckPartialReleaseKeepsBacking() {
    const auto baseline = OpenDescriptors();
    const auto physical = Allocate(PS5_PAGE_SIZE * 2);
    const auto allocated = OpenDescriptors();
    Require(allocated == baseline);
    auto* first = Map(physical, PS5_PAGE_SIZE * 2);
    auto* alias = Map(physical, PS5_PAGE_SIZE * 2);
    first[PS5_PAGE_SIZE] = 53;
    Require(alias[PS5_PAGE_SIZE] == 53);
    Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(first), PS5_PAGE_SIZE * 2) == 0);
    RequireDescriptors(allocated);
    first = Map(physical, PS5_PAGE_SIZE * 2);
    Require(first[PS5_PAGE_SIZE] == 53);
    Require(sceKernelReleaseDirectMemory(physical, PS5_PAGE_SIZE) == 0);
    RequireDescriptors(allocated);
    alias[PS5_PAGE_SIZE] = 79;
    auto* tail = Map(physical + PS5_PAGE_SIZE, PS5_PAGE_SIZE);
    Require(tail[0] == 79 && first[PS5_PAGE_SIZE] == 79);
    Require(sceKernelReleaseDirectMemory(physical + PS5_PAGE_SIZE, PS5_PAGE_SIZE) == 0);
    RequireDescriptors(baseline);
    std::puts("Partial release retained live aliases and final release restored the descriptor baseline");
}

#if defined(__APPLE__)
std::uint64_t VirtualBytes() {
    mach_task_basic_info_data_t info{};
    mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
    Require(::task_info(mach_task_self(), MACH_TASK_BASIC_INFO, reinterpret_cast<task_info_t>(&info), &count) == KERN_SUCCESS);
    return info.virtual_size;
}

void ExerciseLargeBackingLifetime() {
    constexpr std::size_t bytes = 8u << 20;
    const auto physical = Allocate(bytes);
    auto* first = Map(physical, bytes, 1);
    auto* tail = Map(physical + bytes - PS5_PAGE_SIZE, PS5_PAGE_SIZE);
    Require(first[0] == 0 && tail[PS5_PAGE_SIZE - 1] == 0);
    tail[PS5_PAGE_SIZE - 1] = 91;
    Require(first[bytes - 1] == 91);
    Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(first), bytes) == 0);
    Require(sceKernelMunmap(reinterpret_cast<std::uint64_t>(tail), PS5_PAGE_SIZE) == 0);
    Require(sceKernelReleaseDirectMemory(physical, bytes) == 0);
}

void CheckBackingVirtualMemoryIsReleased() {
    ExerciseLargeBackingLifetime();
    const auto descriptors = OpenDescriptors();
    const auto before = VirtualBytes();
    for (int round = 0; round < 64; ++round) ExerciseLargeBackingLifetime();
    const auto after = VirtualBytes();
    constexpr std::uint64_t allowance = 8u << 20;
    std::printf("direct memory backing VM: before=%llu after=%llu allowance=%llu allocated=%llu\n", static_cast<unsigned long long>(before), static_cast<unsigned long long>(after), static_cast<unsigned long long>(allowance), static_cast<unsigned long long>(64u * (8u << 20)));
    Require(after <= before || after - before <= allowance);
    RequireDescriptors(descriptors);
}

void CheckFailedConstructionLeavesNoResources() {
    const auto baseline = OpenDescriptors();
    const auto before = VirtualBytes();
    for (int round = 0; round < 32; ++round) {
        bool rejected = false;
        try {
            CreateDirectMemoryBacking(0, std::size_t{1} << 60, 0);
        } catch (const std::system_error& error) {
            Require(error.code() == std::errc::not_enough_memory);
            rejected = true;
        }
        Require(rejected);
    }
    RequireDescriptors(baseline);
    const auto after = VirtualBytes();
    constexpr std::uint64_t allowance = 8u << 20;
    Require(after <= before || after - before <= allowance);
    ExerciseLifetime(true);
    RequireDescriptors(baseline);
    std::puts("Failed backing allocation retained no resources and left allocation usable");
}
#endif

}

int main() {
    Require(sceKernelEnableDmemAliasing() == 0);
    ExerciseLifetime(true);
    CheckRepeatedLifetimes();
    CheckPartialReleaseKeepsBacking();
#if defined(__APPLE__)
    CheckBackingVirtualMemoryIsReleased();
    CheckFailedConstructionLeavesNoResources();
#endif
}
