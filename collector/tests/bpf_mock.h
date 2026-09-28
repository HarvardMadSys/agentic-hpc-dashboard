/* Just enough of BCC's C dialect to compile a SECTION of ebpfm's generated BPF
 * program with the host C compiler and drive its probes from a unit test.
 *
 * Not a BPF runtime: no verifier, no rewriter, no per-CPU anything. What it
 * does reproduce is the part a test can get wrong without noticing -- map
 * semantics (lookup/update/delete, a hash that refuses a NEW key when full,
 * arrays that never miss) and bpf_probe_read as a plain copy, so byte order,
 * struct offsets and the probe-to-probe hand-offs run for real. The program's
 * pointers are ordinary userspace pointers here, which is what lets a test lay
 * out a fake `struct sock` at the BTF offsets and hand it to a probe.
 *
 * Used by test_ebpfm.py; nothing in the collector includes it. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int32_t s32;
typedef int64_t s64;

#define __always_inline inline __attribute__((always_inline))
#define TASK_COMM_LEN 16

struct pt_regs { unsigned long parm1, parm2, rc; };
#define PT_REGS_PARM1(x) ((x)->parm1)
#define PT_REGS_PARM2(x) ((x)->parm2)
#define PT_REGS_RC(x) ((x)->rc)

static u64 mock_pid_tgid;
static u64 bpf_get_current_pid_tgid(void) { return mock_pid_tgid; }
static u64 bpf_ktime_get_ns(void) { return 0; }

/* The kernel helper zero-fills and fails on a bad source; a NULL is the only
 * bad source a test can hand it. */
static int bpf_probe_read(void *dst, u32 size, const void *src)
{
    if (!src) {
        memset(dst, 0, size);
        return -1;
    }
    memcpy(dst, src, size);
    return 0;
}

/* BPF_HASH: linear-scan slots; update() of a NEW key fails once `sz` are in
 * use, exactly the case a full kernel map returns -E2BIG for. */
#define BPF_HASH(name, kt, vt, sz)                                              \
    static struct { int used; kt k; vt v; } name##_slots[sz];                   \
    static int name##_find(const void *k)                                       \
    {                                                                           \
        for (int i = 0; i < (sz); i++)                                          \
            if (name##_slots[i].used && !memcmp(&name##_slots[i].k, k, sizeof(kt))) \
                return i;                                                       \
        return -1;                                                              \
    }                                                                           \
    static vt *name##_lookup(const void *k)                                     \
    {                                                                           \
        int i = name##_find(k);                                                 \
        return i < 0 ? 0 : &name##_slots[i].v;                                  \
    }                                                                           \
    static int name##_update(const void *k, const void *v)                      \
    {                                                                           \
        int i = name##_find(k);                                                 \
        if (i < 0) {                                                            \
            for (i = 0; i < (sz); i++)                                          \
                if (!name##_slots[i].used)                                      \
                    break;                                                      \
            if (i == (sz))                                                      \
                return -1;                                                      \
            name##_slots[i].used = 1;                                           \
            memcpy(&name##_slots[i].k, k, sizeof(kt));                          \
        }                                                                       \
        memcpy(&name##_slots[i].v, v, sizeof(vt));                              \
        return 0;                                                               \
    }                                                                           \
    static int name##_delete(const void *k)                                     \
    {                                                                           \
        int i = name##_find(k);                                                 \
        if (i < 0)                                                              \
            return -1;                                                          \
        name##_slots[i].used = 0;                                               \
        return 0;                                                               \
    }                                                                           \
    static int name##_count(void)                                               \
    {                                                                           \
        int n = 0;                                                              \
        for (int i = 0; i < (sz); i++)                                          \
            n += name##_slots[i].used;                                          \
        return n;                                                               \
    }                                                                           \
    static struct {                                                             \
        vt *(*lookup)(const void *);                                            \
        int (*update)(const void *, const void *);                              \
        int (*delete)(const void *);                                            \
    } name = { name##_lookup, name##_update, name##_delete }

#define BPF_ARRAY(name, vt, sz)                                                 \
    static vt name##_vals[sz];                                                  \
    static vt *name##_lookup(const void *k)                                     \
    {                                                                           \
        u32 i = *(const u32 *)k;                                                \
        return i < (sz) ? &name##_vals[i] : 0;                                  \
    }                                                                           \
    static struct { vt *(*lookup)(const void *); } name = { name##_lookup }

static int mock_perf_submit(void *ctx, void *data, u32 size)
{
    (void)ctx; (void)data; (void)size;
    return 0;
}
#define BPF_PERF_OUTPUT(name)                                                   \
    static struct { int (*perf_submit)(void *, void *, u32); } name = { mock_perf_submit }
