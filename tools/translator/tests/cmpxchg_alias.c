typedef unsigned long long u64;
typedef void (*Exchange)(u64*, u64*, u64, unsigned char*);

int puts(const char*);
void exit(int);

#define CAPTURE_FLAGS "setb %[cf]\n\tsetp %[pf]\n\tsete %[zf]\n\tsets %[sf]\n\tseto %[of]\n\t"
#define FLAG_OUTPUTS [cf] "=m"(flags[0]), [pf] "=m"(flags[1]), [zf] "=m"(flags[2]), [sf] "=m"(flags[3]), [of] "=m"(flags[4])
#define EXCHANGE_CASE(BITS, SUFFIX, ACCUMULATOR, DESTINATION, SOURCE) \
    static void alias##BITS(u64* accumulator, u64* destination, u64 source, unsigned char* flags) { \
        (void)destination; \
        u64 value = *accumulator; \
        __asm__ volatile("cmpxchg" SUFFIX " %%" SOURCE ", %%" ACCUMULATOR "\n\t" CAPTURE_FLAGS \
                         : "+&a"(value), FLAG_OUTPUTS : "c"(source) : "cc"); \
        *accumulator = value; \
    } \
    static void separate##BITS(u64* accumulator, u64* destination, u64 source, unsigned char* flags) { \
        u64 value = *accumulator, target = *destination; \
        __asm__ volatile("cmpxchg" SUFFIX " %%" SOURCE ", %%" DESTINATION "\n\t" CAPTURE_FLAGS \
                         : "+&a"(value), "+&b"(target), FLAG_OUTPUTS : "c"(source) : "cc"); \
        *accumulator = value; \
        *destination = target; \
    }

EXCHANGE_CASE(8, "b", "al", "bl", "cl")
EXCHANGE_CASE(16, "w", "ax", "bx", "cx")
EXCHANGE_CASE(32, "l", "eax", "ebx", "ecx")
EXCHANGE_CASE(64, "q", "rax", "rbx", "rcx")

static void high_source(u64* accumulator, u64* destination, u64 source, unsigned char* flags) {
    (void)destination;
    (void)source;
    u64 value = *accumulator;
    __asm__ volatile("cmpxchgb %%ah, %%al\n\t" CAPTURE_FLAGS : "+&a"(value), FLAG_OUTPUTS : : "cc");
    *accumulator = value;
}

static u64 hash = 0xcbf29ce484222325ull;

static void mix(u64 value) {
    hash ^= value;
    hash *= 0x100000001b3ull;
    hash ^= hash >> 29;
}

static u64 write_part(u64 previous, u64 value, unsigned bits) {
    const u64 mask = ~0ull >> (64 - bits);
    return bits >= 32 ? value & mask : (previous & ~mask) | (value & mask);
}

static unsigned subtraction_flags(u64 left, u64 right, unsigned bits) {
    const u64 mask = ~0ull >> (64 - bits);
    const u64 sign = 1ull << (bits - 1);
    left &= mask;
    right &= mask;
    const u64 result = (left - right) & mask;
    unsigned parity = 0;
    for (unsigned bit = 0; bit < 8; ++bit) parity ^= (unsigned)(result >> bit) & 1;
    return (left < right) | ((parity == 0) << 1) | ((result == 0) << 2) | (((result & sign) != 0) << 3) |
           ((((left ^ right) & (left ^ result) & sign) != 0) << 4);
}

static void check(Exchange operation, unsigned bits, int aliased, u64 accumulator, u64 destination, u64 source) {
    const u64 mask = ~0ull >> (64 - bits);
    const u64 compared = aliased ? accumulator : destination;
    const int equal = (accumulator & mask) == (compared & mask);
    const u64 expected_accumulator = aliased ? write_part(accumulator, source, bits) :
                                     equal ? accumulator : write_part(accumulator, destination, bits);
    const u64 expected_destination = aliased || !equal ? destination : write_part(destination, source, bits);
    const unsigned expected_flags = subtraction_flags(accumulator, compared, bits);
    unsigned char flags[5];
    operation(&accumulator, &destination, source, flags);
    unsigned actual_flags = 0;
    for (unsigned index = 0; index < 5; ++index) actual_flags |= (unsigned)flags[index] << index;
    if (accumulator != expected_accumulator || destination != expected_destination || actual_flags != expected_flags) {
        puts(aliased ? "cmpxchg accumulator alias mismatch" : "cmpxchg register exchange mismatch");
        exit(1);
    }
    mix(accumulator);
    mix(destination);
    mix(actual_flags);
}

static void run(void) {
    static const u64 values[] = {0, 1, 0x7f, 0x80, 0x7fff, 0x8000, 0x7fffffff, 0x80000000,
                                 0x7fffffffffffffffull, 0x8000000000000000ull, ~0ull, 0xa5a55a5affff5501ull};
    static Exchange aliases[] = {alias8, alias16, alias32, alias64};
    static Exchange separate[] = {separate8, separate16, separate32, separate64};
    static const unsigned widths[] = {8, 16, 32, 64};
    const unsigned count = sizeof(values) / sizeof(values[0]);
    for (unsigned width = 0; width < 4; ++width) {
        for (unsigned left = 0; left < count; ++left) {
            const u64 source = ~values[(left + 3) % count];
            check(aliases[width], widths[width], 1, values[left], 0, source);
            for (unsigned right = 0; right < count; ++right) {
                check(separate[width], widths[width], 0, values[left], values[right], source);
            }
        }
    }
    for (unsigned index = 0; index < count; ++index) check(high_source, 8, 1, values[index], 0, values[index] >> 8);
    char text[17];
    for (unsigned index = 0; index < 16; ++index) text[index] = "0123456789abcdef"[(hash >> (60 - 4 * index)) & 15];
    text[16] = 0;
    puts(text);
    exit((int)(hash & 0x7f));
}

#ifdef MAIN_WRAPPER
int main(void) {
    run();
    return 0;
}
#else
void _start(void* block) {
    (void)block;
    run();
}
#endif
