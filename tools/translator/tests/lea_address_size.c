#ifdef MAIN_WRAPPER
#include <stdio.h>
#endif

typedef unsigned long long u64;
typedef long long i64;
typedef u64 (*Calculate)(u64, u64, u64, unsigned char*);

int puts(const char*);
void exit(int);

#define CAPTURE_FLAGS "setb %[cf]\n\tsetp %[pf]\n\tsete %[zf]\n\tsets %[sf]\n\tseto %[of]\n\t"
#define FLAG_OUTPUTS [cf] "=m"(flags[0]), [pf] "=m"(flags[1]), [zf] "=m"(flags[2]), [sf] "=m"(flags[3]), [of] "=m"(flags[4])
#define LEA_CASE(NAME, SIZE, REGISTER, PREFIX, ADDRESS) \
    static u64 NAME(u64 base, u64 index, u64 destination, unsigned char* flags) { \
        __asm__ volatile("cmpq %%rcx, %%rax\n\t" PREFIX "lea" SIZE " " ADDRESS ", %%" REGISTER "\n\t" CAPTURE_FLAGS \
                         : "+&d"(destination), FLAG_OUTPUTS : "a"(base), "c"(index) : "cc"); \
        return destination; \
    }
#define LEA_SET(NAME, ADDRESS) \
    LEA_CASE(NAME##q, "q", "rdx", "", ADDRESS) \
    LEA_CASE(NAME##l, "l", "edx", "", ADDRESS) \
    LEA_CASE(NAME##w, "w", "dx", "", ADDRESS)

LEA_SET(sub32, "-1(%%eax)")
LEA_SET(sum32, "(%%eax,%%ecx,1)")
LEA_SET(scale2, "2147483647(%%eax,%%ecx,2)")
LEA_SET(scale4, "-2147483648(%%eax,%%ecx,4)")
LEA_SET(scale8, "127(%%eax,%%ecx,8)")
LEA_SET(index32, "-1(,%%ecx,8)")
LEA_SET(sub64, "-1(%%rax)")
LEA_SET(sum64, "-2147483648(%%rax,%%rcx,4)")
LEA_CASE(fs32, "q", "rdx", ".byte 0x64\n\t", "-1(%%eax)")
LEA_CASE(gs32, "q", "rdx", ".byte 0x65\n\t", "-1(%%eax)")
LEA_CASE(fs64, "q", "rdx", ".byte 0x64\n\t", "-2147483648(%%rax,%%rcx,4)")
LEA_CASE(gs64, "q", "rdx", ".byte 0x65\n\t", "-2147483648(%%rax,%%rcx,4)")

typedef struct {
    Calculate calculate;
    unsigned address_bits, destination_bits, base, scale;
    i64 displacement;
} Operation;

#define OPERATION_SET(NAME, ADDRESS_BITS, BASE, SCALE, DISPLACEMENT) \
    {NAME##q, ADDRESS_BITS, 64, BASE, SCALE, DISPLACEMENT}, \
    {NAME##l, ADDRESS_BITS, 32, BASE, SCALE, DISPLACEMENT}, \
    {NAME##w, ADDRESS_BITS, 16, BASE, SCALE, DISPLACEMENT}

static u64 checksum = 0xcbf29ce484222325ull;

static void mix(u64 value) {
    checksum ^= value;
    checksum *= 0x100000001b3ull;
    checksum ^= checksum >> 29;
}

static unsigned subtraction_flags(u64 left, u64 right) {
    const u64 result = left - right;
    unsigned parity = 0;
    for (unsigned bit = 0; bit < 8; ++bit) parity ^= (unsigned)(result >> bit) & 1;
    return (left < right) | ((parity == 0) << 1) | ((result == 0) << 2) | ((unsigned)(result >> 63) << 3) |
           ((unsigned)(((left ^ right) & (left ^ result)) >> 63) << 4);
}

static void check(const Operation* operation, u64 base, u64 index) {
    u64 expected = (operation->base ? base : 0) + index * operation->scale + (u64)operation->displacement;
    if (operation->address_bits == 32) expected &= 0xffffffffull;
    const u64 previous = 0xa5a55a5a00110022ull ^ (base << 7) ^ index;
    if (operation->destination_bits == 32) expected &= 0xffffffffull;
    if (operation->destination_bits == 16) expected = (previous & ~0xffffull) | (expected & 0xffffull);
    unsigned char flags[5];
    const u64 actual = operation->calculate(base, index, previous, flags);
    const unsigned expected_flags = subtraction_flags(base, index);
    unsigned actual_flags = 0;
    for (unsigned bit = 0; bit < 5; ++bit) actual_flags |= (unsigned)flags[bit] << bit;
    if (actual != expected || actual_flags != expected_flags) {
#ifdef MAIN_WRAPPER
        fprintf(stderr, "address_bits=%u destination_bits=%u base=%016llx index=%016llx scale=%u displacement=%lld "
                        "actual=%016llx expected=%016llx actual_flags=%02x expected_flags=%02x\n",
                operation->address_bits, operation->destination_bits, base, index, operation->scale, operation->displacement,
                actual, expected, actual_flags, expected_flags);
#endif
        puts("lea address-size mismatch");
        exit(1);
    }
    mix(actual);
    mix(actual_flags);
}

static void run(void) {
    static const u64 values[] = {0, 1, 0x7fffffffull, 0x80000000ull, 0xffffffffull, 0x100000000ull,
                                 0x8000000000000000ull, ~0ull, 0x123456789abcdef0ull, 0x7fff0010ull};
    static const Operation operations[] = {
        OPERATION_SET(sub32, 32, 1, 0, -1),
        OPERATION_SET(sum32, 32, 1, 1, 0),
        OPERATION_SET(scale2, 32, 1, 2, 2147483647),
        OPERATION_SET(scale4, 32, 1, 4, -2147483648ll),
        OPERATION_SET(scale8, 32, 1, 8, 127),
        OPERATION_SET(index32, 32, 0, 8, -1),
        OPERATION_SET(sub64, 64, 1, 0, -1),
        OPERATION_SET(sum64, 64, 1, 4, -2147483648ll),
        {fs32, 32, 64, 1, 0, -1}, {gs32, 32, 64, 1, 0, -1},
        {fs64, 64, 64, 1, 4, -2147483648ll}, {gs64, 64, 64, 1, 4, -2147483648ll},
    };
    for (unsigned operation = 0; operation < sizeof(operations) / sizeof(operations[0]); ++operation) {
        for (unsigned base = 0; base < sizeof(values) / sizeof(values[0]); ++base) {
            for (unsigned index = 0; index < sizeof(values) / sizeof(values[0]); ++index) {
                check(&operations[operation], values[base], values[index]);
            }
        }
    }
    char text[17];
    for (unsigned index = 0; index < 16; ++index) text[index] = "0123456789abcdef"[(checksum >> (60 - 4 * index)) & 15];
    text[16] = 0;
    puts(text);
    exit((int)(checksum & 0x7f));
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
