#ifdef MAIN_WRAPPER
#include <stdio.h>
#endif

typedef unsigned long long u64;
typedef void (*Release)(void*, u64, u64, u64*, u64*, unsigned char*);

int puts(const char*);
void exit(int);

#define CAPTURE_FLAGS "setb %[cf]\n\tsetp %[pf]\n\tsete %[zf]\n\tsets %[sf]\n\tseto %[of]\n\t"
#define FLAG_OUTPUTS [cf] "=m"(flags[0]), [pf] "=m"(flags[1]), [zf] "=m"(flags[2]), [sf] "=m"(flags[3]), [of] "=m"(flags[4])
#define LEAVE_CASE(NAME, BYTES) \
    static void NAME(void* frame, u64 left, u64 right, u64* stack, u64* base, unsigned char* flags) { \
        u64 stack_result, base_result; \
        __asm__ volatile("movq %%rbp, %%r8\n\tmovq %%rsp, %%r9\n\tmovq %%rsi, %%rbp\n\tcmpq %%rcx, %%rax\n\t" \
                         BYTES "\n\tmovq %%rsp, %%rdx\n\tmovq %%rbp, %%rbx\n\tmovq %%r8, %%rbp\n\tmovq %%r9, %%rsp\n\t" CAPTURE_FLAGS \
                         : "=&d"(stack_result), "=&b"(base_result), FLAG_OUTPUTS \
                         : "S"(frame), "a"(left), "c"(right) : "r8", "r9", "cc", "memory"); \
        *stack = stack_result; \
        *base = base_result; \
    }

LEAVE_CASE(plain, ".byte 0xc9")
LEAVE_CASE(word, ".byte 0x66,0xc9")
LEAVE_CASE(address32, ".byte 0x67,0xc9")
LEAVE_CASE(word_address32, ".byte 0x66,0x67,0xc9")
LEAVE_CASE(rex64, ".byte 0x48,0xc9")
LEAVE_CASE(word_rex0, ".byte 0x66,0x40,0xc9")
LEAVE_CASE(word_rex64, ".byte 0x66,0x48,0xc9")
LEAVE_CASE(word_address32_rex64, ".byte 0x66,0x67,0x48,0xc9")

typedef struct {
    Release release;
    unsigned bits;
} Operation;

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

static void check(const Operation* operation, unsigned offset, u64 value, u64 left, u64 right) {
    unsigned char storage[16];
    unsigned char* frame = storage + offset;
    for (unsigned byte = 0; byte < 8; ++byte) frame[byte] = (unsigned char)(value >> (8 * byte));
    const u64 frame_address = (u64)frame;
    if ((frame_address >> 32) == 0) {
        puts("leave frame address lacks upper bits");
        exit(1);
    }
    const u64 preserved = operation->bits == 16 ? frame_address & ~0xffffull : 0;
    const u64 expected_base = operation->bits == 16 ? preserved | (value & 0xffffull) : value;
    const u64 expected_stack = frame_address + operation->bits / 8;
    u64 actual_stack, actual_base;
    unsigned char flags[5];
    operation->release(frame, left, right, &actual_stack, &actual_base, flags);
    const unsigned expected_flags = subtraction_flags(left, right);
    unsigned actual_flags = 0;
    for (unsigned bit = 0; bit < 5; ++bit) actual_flags |= (unsigned)flags[bit] << bit;
    if (actual_stack != expected_stack || actual_base != expected_base || actual_flags != expected_flags) {
#ifdef MAIN_WRAPPER
        fprintf(stderr, "bits=%u offset=%u frame=%016llx value=%016llx left=%016llx right=%016llx "
                        "actual_stack=%016llx expected_stack=%016llx actual_base=%016llx expected_base=%016llx "
                        "actual_flags=%02x expected_flags=%02x\n",
                operation->bits, offset, frame_address, value, left, right, actual_stack, expected_stack,
                actual_base, expected_base, actual_flags, expected_flags);
#endif
        puts("leave operand-size mismatch");
        exit(1);
    }
    mix(actual_stack - frame_address);
    mix(actual_base ^ preserved);
    mix(actual_flags);
}

static void run(void) {
    static const u64 values[] = {0, 1, 0x7fffull, 0x8000ull, 0xffffull, 0x10000ull, 0xffffffffull, 0x100000000ull,
                                 0x7fffffffffffffffull, 0x8000000000000000ull, ~0ull, 0x123456789abcdef0ull};
    static const u64 comparisons[][2] = {{0, 0}, {0, 1}, {1, 0}, {0x8000000000000000ull, 1},
                                         {0x7fffffffffffffffull, ~0ull}, {~0ull, 1}, {0x80, 0x7f}, {0x100, 1}};
    static const unsigned offsets[] = {0, 1, 3, 7};
    static const Operation operations[] = {{plain, 64}, {word, 16}, {address32, 64}, {word_address32, 16},
                                           {rex64, 64}, {word_rex0, 16}, {word_rex64, 64}, {word_address32_rex64, 64}};
    for (unsigned operation = 0; operation < sizeof(operations) / sizeof(operations[0]); ++operation) {
        for (unsigned offset = 0; offset < sizeof(offsets) / sizeof(offsets[0]); ++offset) {
            for (unsigned value = 0; value < sizeof(values) / sizeof(values[0]); ++value) {
                for (unsigned comparison = 0; comparison < sizeof(comparisons) / sizeof(comparisons[0]); ++comparison) {
                    check(&operations[operation], offsets[offset], values[value], comparisons[comparison][0], comparisons[comparison][1]);
                }
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
