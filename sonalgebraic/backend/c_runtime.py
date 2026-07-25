RUNTIME = r'''
#ifndef _WIN32
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200112L
#endif
#endif
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <limits.h>
#include <sys/stat.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifdef SA_ENABLE_NET
#include <winsock2.h>
#include <ws2tcpip.h>
#endif
#include <windows.h>
#ifdef SA_ENABLE_TLS
#ifndef SECURITY_WIN32
#define SECURITY_WIN32
#endif
#include <security.h>
#include <schannel.h>
#endif
#include <direct.h>
#ifdef SA_ENABLE_DESKTOP
#include <shellapi.h>
#endif
#ifdef SA_ENABLE_NET
#include <winhttp.h>
#endif
#if defined(_MSC_VER) && defined(SA_ENABLE_NET)
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "ws2_32.lib")
#endif
#if defined(_MSC_VER) && defined(SA_ENABLE_TLS)
#pragma comment(lib, "secur32.lib")
#endif
#if defined(_MSC_VER) && defined(SA_ENABLE_DESKTOP)
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "shell32.lib")
#endif
#else
#include <unistd.h>
#include <signal.h>
#ifdef SA_ENABLE_NET
#include <fcntl.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <sys/time.h>
#ifdef SA_ENABLE_TLS
#include <openssl/ssl.h>
#include <openssl/err.h>
#include <openssl/x509v3.h>
#endif

#ifdef SA_ENABLE_NET
#ifndef NI_MAXHOST
#define NI_MAXHOST 1025
#endif
#ifndef NI_MAXSERV
#define NI_MAXSERV 32
#endif
#endif
#endif
#endif

#ifdef SA_ENABLE_GUI_GTK
#include <gtk/gtk.h>
#endif

/* TRY/CATCH 的跳转原语按目标运行时分两套：
 * - MinGW（__MINGW32__）下用 __builtin_setjmp：MinGW 的标准 setjmp 走 SEH 帧展开
 *   (_setjmpex/RtlUnwindEx)，遇到含不可归约控制流（GOTO 跳进循环、GOSUB）的函数在
 *   -O2 下会损坏展开表导致 access violation。__builtin_setjmp 用简单寄存器保存模型，
 *   不依赖 SEH，能稳过 -O2。第二参数固定 1 是 __builtin_longjmp 的硬性要求。
 * - 其余（MSVC ABI，含 clang --target=...-msvc）用标准 setjmp：__builtin_setjmp 在
 *   Windows x64 MSVC 下不可靠（缓冲区/SEH 假设不匹配，直接 access violation），而
 *   MSVC 的 setjmp 本就是 SEH-aware 的 _setjmpex，在自家工具链下正确且优化安全。 */
#if defined(__MINGW32__) && (defined(__GNUC__) || defined(__clang__))
typedef void* SaJmpBuf[5];
#define SA_SETJMP(buf) __builtin_setjmp(buf)
#define SA_LONGJMP(buf) __builtin_longjmp((buf), 1)
#else
#include <setjmp.h>
typedef jmp_buf SaJmpBuf;
#define SA_SETJMP(buf) setjmp(buf)
#define SA_LONGJMP(buf) longjmp((buf), 1)
#endif

typedef struct {
    char* data;
    size_t len;
    size_t cap;
} SaStringBuilder;

typedef enum {
    SA_SYM_CONST,
    SA_SYM_VAR,
    SA_SYM_OP,
    SA_SYM_FUNC
} SaSymbolKind;

typedef struct SaSymbolNode {
    SaSymbolKind kind;
    char* text;
    char op;
    struct SaSymbolNode* left;
    struct SaSymbolNode* right;
} SaSymbolNode;

typedef SaSymbolNode* SaSymbol;

typedef uint64_t SaHandle;

typedef struct {
    int err_code;
    const char* type;
    char* message;
    int line_number;
    const char* sub_name;
} SaError;

typedef struct {
    SaJmpBuf env;
} SaTryFrame;

static SaTryFrame sa_try_stack[64];
static int sa_try_top = 0;
static SaError sa_current_error = {0, "ERR_NONE", NULL, 0, NULL};

enum {
    SA_HANDLE_FILE = 1,
    SA_HANDLE_BUFFER = 2,
    SA_HANDLE_TCP_LISTENER = 3,
    SA_HANDLE_NET_STREAM = 4,
    SA_HANDLE_UDP_SOCKET = 5,
    SA_HANDLE_LIST = 6,
    SA_HANDLE_STR_LIST = 7,
    SA_HANDLE_MAP = 8,
    SA_HANDLE_STR_MAP = 9,
    SA_HANDLE_GUI_WINDOW = 10,
    SA_HANDLE_GUI_WIDGET = 11
};

static SaHandle sa_handle_make(unsigned int kind, uint32_t generation, size_t index) {
    return ((SaHandle)(kind & 0xffu) << 56) | ((SaHandle)generation << 16) | (SaHandle)(index + 1);
}

static int sa_handle_parse(SaHandle handle, unsigned int kind, size_t count, size_t* index, uint32_t* generation) {
    unsigned int actual_kind = (unsigned int)(handle >> 56);
    uint32_t raw_index = (uint32_t)(handle & 0xffffu);
    if (!handle || actual_kind != kind || raw_index == 0 || raw_index > count) return 0;
    if (index) *index = (size_t)(raw_index - 1);
    if (generation) *generation = (uint32_t)((handle >> 16) & 0xffffffffu);
    return 1;
}

static char* sa_strdup(const char* value);

static void* sa_try_push_env(void) {
    if (sa_try_top >= 64) {
        fputs("SonAlgebraic runtime: TRY stack overflow\n", stderr);
        exit(1);
    }
    return (void*)sa_try_stack[sa_try_top++].env;
}

static void sa_try_pop(void) {
    if (sa_try_top > 0) {
        sa_try_top--;
    }
}

static int sa_error_code(const char* type) {
    unsigned int hash = 2166136261u;
    const unsigned char* p = (const unsigned char*)type;
    while (*p) {
        hash ^= (unsigned int)(*p++);
        hash *= 16777619u;
    }
    return (int)(hash & 0x7fffffff);
}

static char* sa_strdup(const char* value) {
    const char* safe = value ? value : "";
    size_t len = strlen(safe);
    char* copy = (char*)malloc(len + 1);
    if (!copy) {
        fputs("SonAlgebraic runtime: out of memory\n", stderr);
        exit(1);
    }
    memcpy(copy, safe, len + 1);
    return copy;
}

#if defined(SA_ENABLE_BINARY) || defined(SA_ENABLE_NET)
#define SA_BUFFER_SLOT_COUNT 128
typedef struct {
    unsigned char* data;
    size_t len;
    uint32_t generation;
} SaBufferSlot;

static SaBufferSlot sa_buffer_slots[SA_BUFFER_SLOT_COUNT];
static char sa_binary_last_error[512] = "";
static int sa_binary_cleanup_registered = 0;

static void sa_binary_clear_error(void) { sa_binary_last_error[0] = '\0'; }
static void sa_binary_set_error(const char* message) { snprintf(sa_binary_last_error, sizeof(sa_binary_last_error), "%s", message ? message : "binary error"); }
static char* sa_binary_last_error_copy(void) { return sa_strdup(sa_binary_last_error); }

static SaBufferSlot* sa_binary_slot(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_BUFFER, SA_BUFFER_SLOT_COUNT, &index, &generation)) return NULL;
    SaBufferSlot* slot = &sa_buffer_slots[index];
    return slot->data && slot->generation == generation ? slot : NULL;
}

static void sa_binary_close_all(void) {
    for (size_t i = 0; i < SA_BUFFER_SLOT_COUNT; i++) {
        free(sa_buffer_slots[i].data);
        sa_buffer_slots[i].data = NULL;
        sa_buffer_slots[i].len = 0;
        sa_buffer_slots[i].generation++;
    }
}

static SaHandle sa_binary_take(unsigned char* data, size_t len) {
    for (size_t i = 0; i < SA_BUFFER_SLOT_COUNT; i++) {
        if (!sa_buffer_slots[i].data) {
            if (++sa_buffer_slots[i].generation == 0) sa_buffer_slots[i].generation = 1;
            sa_buffer_slots[i].data = data;
            sa_buffer_slots[i].len = len;
            if (!sa_binary_cleanup_registered) {
                atexit(sa_binary_close_all);
                sa_binary_cleanup_registered = 1;
            }
            return sa_handle_make(SA_HANDLE_BUFFER, sa_buffer_slots[i].generation, i);
        }
    }
    free(data);
    sa_binary_set_error("too many live binary buffers");
    return 0;
}

static SaHandle sa_binary_from_bytes(const unsigned char* data, size_t len) {
    unsigned char* copy = (unsigned char*)malloc(len ? len : 1);
    if (!copy) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    if (len) memcpy(copy, data, len);
    return sa_binary_take(copy, len);
}

static SaHandle sa_binary_new(long long length) {
    sa_binary_clear_error();
    if (length < 0 || (unsigned long long)length > (unsigned long long)SIZE_MAX) {
        sa_binary_set_error("buffer length is out of range");
        return 0;
    }
    size_t len = (size_t)length;
    unsigned char* data = (unsigned char*)calloc(len ? len : 1, 1);
    if (!data) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    return sa_binary_take(data, len);
}

static int sa_binary_close(SaHandle handle) {
    sa_binary_clear_error();
    SaBufferSlot* slot = sa_binary_slot(handle);
    if (!slot) { sa_binary_set_error("invalid or closed BUFFER handle"); return 0; }
    free(slot->data);
    slot->data = NULL;
    slot->len = 0;
    slot->generation++;
    return 1;
}

static long long sa_binary_length(SaHandle handle) {
    sa_binary_clear_error();
    SaBufferSlot* slot = sa_binary_slot(handle);
    if (!slot) { sa_binary_set_error("invalid or closed BUFFER handle"); return -1; }
    if (slot->len > 0x7fffffffffffffffULL) { sa_binary_set_error("buffer length exceeds LONG"); return -1; }
    return (long long)slot->len;
}

static int sa_binary_range(SaBufferSlot* slot, long long offset, long long count, size_t* start, size_t* length) {
    if (!slot || offset < 0 || count < 0) return 0;
    size_t off = (size_t)offset;
    size_t len = (size_t)count;
    if (off > slot->len || len > slot->len - off) return 0;
    if (start) *start = off;
    if (length) *length = len;
    return 1;
}

static SaHandle sa_binary_slice(SaHandle handle, long long offset, long long count) {
    sa_binary_clear_error();
    SaBufferSlot* slot = sa_binary_slot(handle);
    size_t start = 0, len = 0;
    if (!sa_binary_range(slot, offset, count, &start, &len)) { sa_binary_set_error("buffer slice is out of range"); return 0; }
    return sa_binary_from_bytes(slot->data + start, len);
}

static int sa_binary_copy(SaHandle target_handle, long long target_offset, SaHandle source_handle, long long source_offset, long long count) {
    sa_binary_clear_error();
    SaBufferSlot* target = sa_binary_slot(target_handle);
    SaBufferSlot* source = sa_binary_slot(source_handle);
    size_t dst = 0, src = 0, len = 0;
    if (!sa_binary_range(target, target_offset, count, &dst, &len) || !sa_binary_range(source, source_offset, count, &src, NULL)) {
        sa_binary_set_error("buffer copy is out of range");
        return 0;
    }
    memmove(target->data + dst, source->data + src, len);
    return 1;
}

static int sa_binary_hex_nibble(char ch) {
    if (ch >= '0' && ch <= '9') return ch - '0';
    if (ch >= 'a' && ch <= 'f') return ch - 'a' + 10;
    if (ch >= 'A' && ch <= 'F') return ch - 'A' + 10;
    return -1;
}

static SaHandle sa_binary_hex_decode(const char* value) {
    sa_binary_clear_error();
    const char* safe = value ? value : "";
    size_t text_len = strlen(safe);
    if (text_len % 2) { sa_binary_set_error("HEX text must have an even number of characters"); return 0; }
    size_t len = text_len / 2;
    unsigned char* data = (unsigned char*)malloc(len ? len : 1);
    if (!data) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    for (size_t i = 0; i < len; i++) {
        int high = sa_binary_hex_nibble(safe[i * 2]);
        int low = sa_binary_hex_nibble(safe[i * 2 + 1]);
        if (high < 0 || low < 0) { free(data); sa_binary_set_error("HEX text contains a non-hex character"); return 0; }
        data[i] = (unsigned char)((high << 4) | low);
    }
    return sa_binary_take(data, len);
}

static char* sa_binary_hex_encode(SaHandle handle) {
    sa_binary_clear_error();
    SaBufferSlot* slot = sa_binary_slot(handle);
    if (!slot) { sa_binary_set_error("invalid or closed BUFFER handle"); return sa_strdup(""); }
    if (slot->len > (SIZE_MAX - 1) / 2) { sa_binary_set_error("buffer is too large to encode"); return sa_strdup(""); }
    static const char hex[] = "0123456789ABCDEF";
    char* out = (char*)malloc(slot->len * 2 + 1);
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    for (size_t i = 0; i < slot->len; i++) {
        out[i * 2] = hex[slot->data[i] >> 4];
        out[i * 2 + 1] = hex[slot->data[i] & 15];
    }
    out[slot->len * 2] = '\0';
    return out;
}

static int sa_binary_pack(SaHandle handle, long long offset, long long value, int width, int big_endian) {
    sa_binary_clear_error();
    SaBufferSlot* slot = sa_binary_slot(handle);
    size_t start = 0;
    if (!sa_binary_range(slot, offset, width, &start, NULL)) { sa_binary_set_error("packet field is out of range"); return 0; }
    if (width == 2 && (value < 0 || value > 65535)) { sa_binary_set_error("U16 value is out of range"); return 0; }
    if (width == 4 && (value < 0 || (unsigned long long)value > 4294967295ULL)) { sa_binary_set_error("U32 value is out of range"); return 0; }
    uint64_t bits = (uint64_t)value;
    for (int i = 0; i < width; i++) {
        int shift_index = big_endian ? (width - 1 - i) : i;
        slot->data[start + (size_t)i] = (unsigned char)(bits >> (shift_index * 8));
    }
    return 1;
}

static long long sa_binary_unpack(SaHandle handle, long long offset, int width, int big_endian) {
    sa_binary_clear_error();
    SaBufferSlot* slot = sa_binary_slot(handle);
    size_t start = 0;
    if (!sa_binary_range(slot, offset, width, &start, NULL)) { sa_binary_set_error("packet field is out of range"); return 0; }
    uint64_t bits = 0;
    for (int i = 0; i < width; i++) {
        int shift_index = big_endian ? (width - 1 - i) : i;
        bits |= (uint64_t)slot->data[start + (size_t)i] << (shift_index * 8);
    }
    int64_t signed_bits = 0;
    memcpy(&signed_bits, &bits, sizeof(signed_bits));
    return (long long)signed_bits;
}

#define SA_BIN_PACK_FN(name, width, be) static int name(SaHandle h, long long o, long long v) { return sa_binary_pack(h, o, v, width, be); }
#define SA_BIN_UNPACK_FN(name, width, be) static long long name(SaHandle h, long long o) { return sa_binary_unpack(h, o, width, be); }
SA_BIN_PACK_FN(sa_binary_pack_u16_le, 2, 0)
SA_BIN_PACK_FN(sa_binary_pack_u16_be, 2, 1)
SA_BIN_PACK_FN(sa_binary_pack_u32_le, 4, 0)
SA_BIN_PACK_FN(sa_binary_pack_u32_be, 4, 1)
SA_BIN_PACK_FN(sa_binary_pack_u64_le, 8, 0)
SA_BIN_PACK_FN(sa_binary_pack_u64_be, 8, 1)
SA_BIN_UNPACK_FN(sa_binary_unpack_u16_le, 2, 0)
SA_BIN_UNPACK_FN(sa_binary_unpack_u16_be, 2, 1)
SA_BIN_UNPACK_FN(sa_binary_unpack_u32_le, 4, 0)
SA_BIN_UNPACK_FN(sa_binary_unpack_u32_be, 4, 1)
SA_BIN_UNPACK_FN(sa_binary_unpack_u64_le, 8, 0)
SA_BIN_UNPACK_FN(sa_binary_unpack_u64_be, 8, 1)

static long long sa_binary_checksum8(SaHandle handle, long long offset, long long count) {
    sa_binary_clear_error();
    SaBufferSlot* slot = sa_binary_slot(handle);
    size_t start = 0, len = 0;
    if (!sa_binary_range(slot, offset, count, &start, &len)) { sa_binary_set_error("checksum range is out of bounds"); return -1; }
    unsigned int sum = 0;
    for (size_t i = 0; i < len; i++) sum = (sum + slot->data[start + i]) & 0xffu;
    return (long long)sum;
}
#endif

#ifdef SA_ENABLE_LIST
/* 动态列表沿用 BUFFER 的槽位 + generation 句柄机制：kind 分 LIST(double 元素) 和
   STR_LIST(char* 元素)，静态类型层面就区分开，把元素类型错误留在编译期而不是运行期。 */
#define SA_LIST_SLOT_COUNT 128
typedef struct {
    double* nums;
    char** strs;
    size_t len;
    size_t cap;
    uint32_t generation;
} SaListSlot;

static SaListSlot sa_list_slots[SA_LIST_SLOT_COUNT];
static char sa_list_last_error[512] = "";
static int sa_list_cleanup_registered = 0;

static void sa_list_clear_error(void) { sa_list_last_error[0] = '\0'; }
static void sa_list_set_error(const char* message) { snprintf(sa_list_last_error, sizeof(sa_list_last_error), "%s", message ? message : "list error"); }
static char* sa_list_last_error_copy(void) { return sa_strdup(sa_list_last_error); }

static int sa_list_slot_live(const SaListSlot* slot) { return slot->nums != NULL || slot->strs != NULL; }

static void sa_list_slot_free(SaListSlot* slot) {
    if (slot->strs) {
        for (size_t i = 0; i < slot->len; i++) free(slot->strs[i]);
    }
    free(slot->nums);
    free(slot->strs);
    slot->nums = NULL;
    slot->strs = NULL;
    slot->len = 0;
    slot->cap = 0;
}

static void sa_list_close_all(void) {
    for (size_t i = 0; i < SA_LIST_SLOT_COUNT; i++) {
        sa_list_slot_free(&sa_list_slots[i]);
        sa_list_slots[i].generation++;
    }
}

static SaListSlot* sa_list_slot(SaHandle handle, unsigned int kind) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, kind, SA_LIST_SLOT_COUNT, &index, &generation)) return NULL;
    SaListSlot* slot = &sa_list_slots[index];
    return sa_list_slot_live(slot) && slot->generation == generation ? slot : NULL;
}

static SaHandle sa_list_alloc(unsigned int kind) {
    sa_list_clear_error();
    for (size_t i = 0; i < SA_LIST_SLOT_COUNT; i++) {
        SaListSlot* slot = &sa_list_slots[i];
        if (sa_list_slot_live(slot)) continue;
        if (++slot->generation == 0) slot->generation = 1;
        slot->len = 0;
        slot->cap = 8;
        /* cap 常驻非空指针，这样 live 判定可以复用 BUFFER 的“data 非 NULL”惯用法 */
        if (kind == SA_HANDLE_LIST) {
            slot->nums = (double*)malloc(slot->cap * sizeof(double));
            if (!slot->nums) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        } else {
            slot->strs = (char**)malloc(slot->cap * sizeof(char*));
            if (!slot->strs) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        }
        if (!sa_list_cleanup_registered) {
            atexit(sa_list_close_all);
            sa_list_cleanup_registered = 1;
        }
        return sa_handle_make(kind, slot->generation, i);
    }
    sa_list_set_error("too many live lists");
    return 0;
}

static SaHandle sa_list_new(void) { return sa_list_alloc(SA_HANDLE_LIST); }
static SaHandle sa_strlist_new(void) { return sa_list_alloc(SA_HANDLE_STR_LIST); }

static int sa_list_grow(SaListSlot* slot) {
    if (slot->len < slot->cap) return 1;
    size_t new_cap = slot->cap * 2;
    if (slot->nums) {
        double* grown = (double*)realloc(slot->nums, new_cap * sizeof(double));
        if (!grown) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        slot->nums = grown;
    } else {
        char** grown = (char**)realloc(slot->strs, new_cap * sizeof(char*));
        if (!grown) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        slot->strs = grown;
    }
    slot->cap = new_cap;
    return 1;
}

static int sa_list_index_ok(SaListSlot* slot, long long index) {
    return index >= 0 && (unsigned long long)index < (unsigned long long)slot->len;
}

static int sa_list_push(SaHandle handle, double value) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0; }
    sa_list_grow(slot);
    slot->nums[slot->len++] = value;
    return 1;
}

static double sa_list_pop(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0.0; }
    if (slot->len == 0) { sa_list_set_error("pop from empty LIST"); return 0.0; }
    return slot->nums[--slot->len];
}

static double sa_list_get(SaHandle handle, long long index) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0.0; }
    if (!sa_list_index_ok(slot, index)) { sa_list_set_error("LIST index is out of range"); return 0.0; }
    return slot->nums[index];
}

static int sa_list_set(SaHandle handle, long long index, double value) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0; }
    if (!sa_list_index_ok(slot, index)) { sa_list_set_error("LIST index is out of range"); return 0; }
    slot->nums[index] = value;
    return 1;
}

static int sa_list_insert(SaHandle handle, long long index, double value) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0; }
    if (index < 0 || (unsigned long long)index > (unsigned long long)slot->len) { sa_list_set_error("LIST insert index is out of range"); return 0; }
    sa_list_grow(slot);
    memmove(slot->nums + index + 1, slot->nums + index, (slot->len - (size_t)index) * sizeof(double));
    slot->nums[index] = value;
    slot->len++;
    return 1;
}

static int sa_list_remove(SaHandle handle, long long index) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0; }
    if (!sa_list_index_ok(slot, index)) { sa_list_set_error("LIST index is out of range"); return 0; }
    memmove(slot->nums + index, slot->nums + index + 1, (slot->len - (size_t)index - 1) * sizeof(double));
    slot->len--;
    return 1;
}

static long long sa_list_length(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return -1; }
    return (long long)slot->len;
}

static int sa_list_clear(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0; }
    slot->len = 0;
    return 1;
}

static int sa_list_close(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_LIST);
    if (!slot) { sa_list_set_error("invalid or closed LIST handle"); return 0; }
    sa_list_slot_free(slot);
    slot->generation++;
    return 1;
}

static int sa_strlist_push(SaHandle handle, const char* value) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return 0; }
    sa_list_grow(slot);
    slot->strs[slot->len++] = sa_strdup(value);
    return 1;
}

static char* sa_strlist_pop(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return sa_strdup(""); }
    if (slot->len == 0) { sa_list_set_error("pop from empty STR_LIST"); return sa_strdup(""); }
    return slot->strs[--slot->len];
}

static char* sa_strlist_get(SaHandle handle, long long index) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return sa_strdup(""); }
    if (!sa_list_index_ok(slot, index)) { sa_list_set_error("STR_LIST index is out of range"); return sa_strdup(""); }
    return sa_strdup(slot->strs[index]);
}

static int sa_strlist_set(SaHandle handle, long long index, const char* value) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return 0; }
    if (!sa_list_index_ok(slot, index)) { sa_list_set_error("STR_LIST index is out of range"); return 0; }
    free(slot->strs[index]);
    slot->strs[index] = sa_strdup(value);
    return 1;
}

static int sa_strlist_insert(SaHandle handle, long long index, const char* value) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return 0; }
    if (index < 0 || (unsigned long long)index > (unsigned long long)slot->len) { sa_list_set_error("STR_LIST insert index is out of range"); return 0; }
    sa_list_grow(slot);
    memmove(slot->strs + index + 1, slot->strs + index, (slot->len - (size_t)index) * sizeof(char*));
    slot->strs[index] = sa_strdup(value);
    slot->len++;
    return 1;
}

static int sa_strlist_remove(SaHandle handle, long long index) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return 0; }
    if (!sa_list_index_ok(slot, index)) { sa_list_set_error("STR_LIST index is out of range"); return 0; }
    free(slot->strs[index]);
    memmove(slot->strs + index, slot->strs + index + 1, (slot->len - (size_t)index - 1) * sizeof(char*));
    slot->len--;
    return 1;
}

static long long sa_strlist_length(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return -1; }
    return (long long)slot->len;
}

static int sa_strlist_clear(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return 0; }
    for (size_t i = 0; i < slot->len; i++) free(slot->strs[i]);
    slot->len = 0;
    return 1;
}

static int sa_strlist_close(SaHandle handle) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return 0; }
    sa_list_slot_free(slot);
    slot->generation++;
    return 1;
}

static char* sa_strlist_join(SaHandle handle, const char* separator) {
    sa_list_clear_error();
    SaListSlot* slot = sa_list_slot(handle, SA_HANDLE_STR_LIST);
    if (!slot) { sa_list_set_error("invalid or closed STR_LIST handle"); return sa_strdup(""); }
    const char* sep = separator ? separator : "";
    size_t sep_len = strlen(sep);
    size_t total = 1;
    for (size_t i = 0; i < slot->len; i++) {
        total += strlen(slot->strs[i]);
        if (i + 1 < slot->len) total += sep_len;
    }
    char* out = (char*)malloc(total);
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    char* cursor = out;
    for (size_t i = 0; i < slot->len; i++) {
        size_t item_len = strlen(slot->strs[i]);
        memcpy(cursor, slot->strs[i], item_len);
        cursor += item_len;
        if (i + 1 < slot->len) {
            memcpy(cursor, sep, sep_len);
            cursor += sep_len;
        }
    }
    *cursor = '\0';
    return out;
}
#endif

#ifdef SA_ENABLE_MAP
/* STRING key 的关联容器，链地址哈希 + 负载超 1 翻倍 rehash。kind 分 MAP(double 值)
   和 STR_MAP(char* 值)。KEYS 直接产出 STR_LIST 句柄，所以 map feature 总是连带启用 list。 */
#define SA_MAP_SLOT_COUNT 128
typedef struct SaMapEntry {
    char* key;
    double num;
    char* str;
    struct SaMapEntry* next;
} SaMapEntry;

typedef struct {
    SaMapEntry** buckets;
    size_t bucket_count;
    size_t len;
    int is_str;
    uint32_t generation;
} SaMapSlot;

static SaMapSlot sa_map_slots[SA_MAP_SLOT_COUNT];
static char sa_map_last_error[512] = "";
static int sa_map_cleanup_registered = 0;

static void sa_map_clear_error(void) { sa_map_last_error[0] = '\0'; }
static void sa_map_set_error(const char* message) { snprintf(sa_map_last_error, sizeof(sa_map_last_error), "%s", message ? message : "map error"); }
static char* sa_map_last_error_copy(void) { return sa_strdup(sa_map_last_error); }

static size_t sa_map_hash(const char* key) {
    unsigned int hash = 2166136261u;
    const unsigned char* p = (const unsigned char*)(key ? key : "");
    while (*p) {
        hash ^= (unsigned int)(*p++);
        hash *= 16777619u;
    }
    return (size_t)hash;
}

static void sa_map_slot_free(SaMapSlot* slot) {
    for (size_t i = 0; i < slot->bucket_count; i++) {
        SaMapEntry* entry = slot->buckets[i];
        while (entry) {
            SaMapEntry* next = entry->next;
            free(entry->key);
            free(entry->str);
            free(entry);
            entry = next;
        }
    }
    free(slot->buckets);
    slot->buckets = NULL;
    slot->bucket_count = 0;
    slot->len = 0;
}

static void sa_map_close_all(void) {
    for (size_t i = 0; i < SA_MAP_SLOT_COUNT; i++) {
        sa_map_slot_free(&sa_map_slots[i]);
        sa_map_slots[i].generation++;
    }
}

static SaMapSlot* sa_map_slot(SaHandle handle, unsigned int kind) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, kind, SA_MAP_SLOT_COUNT, &index, &generation)) return NULL;
    SaMapSlot* slot = &sa_map_slots[index];
    return slot->buckets && slot->generation == generation ? slot : NULL;
}

static SaHandle sa_map_alloc(unsigned int kind) {
    sa_map_clear_error();
    for (size_t i = 0; i < SA_MAP_SLOT_COUNT; i++) {
        SaMapSlot* slot = &sa_map_slots[i];
        if (slot->buckets) continue;
        if (++slot->generation == 0) slot->generation = 1;
        slot->bucket_count = 16;
        slot->len = 0;
        slot->is_str = kind == SA_HANDLE_STR_MAP;
        slot->buckets = (SaMapEntry**)calloc(slot->bucket_count, sizeof(SaMapEntry*));
        if (!slot->buckets) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        if (!sa_map_cleanup_registered) {
            atexit(sa_map_close_all);
            sa_map_cleanup_registered = 1;
        }
        return sa_handle_make(kind, slot->generation, i);
    }
    sa_map_set_error("too many live maps");
    return 0;
}

static SaHandle sa_map_new(void) { return sa_map_alloc(SA_HANDLE_MAP); }
static SaHandle sa_strmap_new(void) { return sa_map_alloc(SA_HANDLE_STR_MAP); }

static SaMapEntry* sa_map_find(SaMapSlot* slot, const char* key) {
    SaMapEntry* entry = slot->buckets[sa_map_hash(key) % slot->bucket_count];
    while (entry) {
        if (strcmp(entry->key, key ? key : "") == 0) return entry;
        entry = entry->next;
    }
    return NULL;
}

static void sa_map_rehash(SaMapSlot* slot) {
    if (slot->len <= slot->bucket_count) return;
    size_t new_count = slot->bucket_count * 2;
    SaMapEntry** grown = (SaMapEntry**)calloc(new_count, sizeof(SaMapEntry*));
    if (!grown) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    for (size_t i = 0; i < slot->bucket_count; i++) {
        SaMapEntry* entry = slot->buckets[i];
        while (entry) {
            SaMapEntry* next = entry->next;
            size_t target = sa_map_hash(entry->key) % new_count;
            entry->next = grown[target];
            grown[target] = entry;
            entry = next;
        }
    }
    free(slot->buckets);
    slot->buckets = grown;
    slot->bucket_count = new_count;
}

static int sa_map_put(SaHandle handle, unsigned int kind, const char* key, double num, const char* str) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, kind);
    if (!slot) { sa_map_set_error(kind == SA_HANDLE_MAP ? "invalid or closed MAP handle" : "invalid or closed STR_MAP handle"); return 0; }
    SaMapEntry* entry = sa_map_find(slot, key);
    if (entry) {
        entry->num = num;
        if (slot->is_str) {
            free(entry->str);
            entry->str = sa_strdup(str);
        }
        return 1;
    }
    entry = (SaMapEntry*)malloc(sizeof(SaMapEntry));
    if (!entry) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    entry->key = sa_strdup(key);
    entry->num = num;
    entry->str = slot->is_str ? sa_strdup(str) : NULL;
    size_t target = sa_map_hash(entry->key) % slot->bucket_count;
    entry->next = slot->buckets[target];
    slot->buckets[target] = entry;
    slot->len++;
    sa_map_rehash(slot);
    return 1;
}

static int sa_map_set(SaHandle handle, const char* key, double value) { return sa_map_put(handle, SA_HANDLE_MAP, key, value, NULL); }
static int sa_strmap_set(SaHandle handle, const char* key, const char* value) { return sa_map_put(handle, SA_HANDLE_STR_MAP, key, 0.0, value); }

static double sa_map_get(SaHandle handle, const char* key) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, SA_HANDLE_MAP);
    if (!slot) { sa_map_set_error("invalid or closed MAP handle"); return 0.0; }
    SaMapEntry* entry = sa_map_find(slot, key);
    if (!entry) { sa_map_set_error("MAP key not found"); return 0.0; }
    return entry->num;
}

static char* sa_strmap_get(SaHandle handle, const char* key) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, SA_HANDLE_STR_MAP);
    if (!slot) { sa_map_set_error("invalid or closed STR_MAP handle"); return sa_strdup(""); }
    SaMapEntry* entry = sa_map_find(slot, key);
    if (!entry) { sa_map_set_error("STR_MAP key not found"); return sa_strdup(""); }
    return sa_strdup(entry->str);
}

static int sa_map_has_impl(SaHandle handle, unsigned int kind, const char* key) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, kind);
    if (!slot) { sa_map_set_error(kind == SA_HANDLE_MAP ? "invalid or closed MAP handle" : "invalid or closed STR_MAP handle"); return 0; }
    return sa_map_find(slot, key) != NULL;
}

static int sa_map_has(SaHandle handle, const char* key) { return sa_map_has_impl(handle, SA_HANDLE_MAP, key); }
static int sa_strmap_has(SaHandle handle, const char* key) { return sa_map_has_impl(handle, SA_HANDLE_STR_MAP, key); }

static int sa_map_remove_impl(SaHandle handle, unsigned int kind, const char* key) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, kind);
    if (!slot) { sa_map_set_error(kind == SA_HANDLE_MAP ? "invalid or closed MAP handle" : "invalid or closed STR_MAP handle"); return 0; }
    SaMapEntry** cursor = &slot->buckets[sa_map_hash(key) % slot->bucket_count];
    while (*cursor) {
        SaMapEntry* entry = *cursor;
        if (strcmp(entry->key, key ? key : "") == 0) {
            *cursor = entry->next;
            free(entry->key);
            free(entry->str);
            free(entry);
            slot->len--;
            return 1;
        }
        cursor = &entry->next;
    }
    sa_map_set_error(kind == SA_HANDLE_MAP ? "MAP key not found" : "STR_MAP key not found");
    return 0;
}

static int sa_map_remove(SaHandle handle, const char* key) { return sa_map_remove_impl(handle, SA_HANDLE_MAP, key); }
static int sa_strmap_remove(SaHandle handle, const char* key) { return sa_map_remove_impl(handle, SA_HANDLE_STR_MAP, key); }

static long long sa_map_length_impl(SaHandle handle, unsigned int kind) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, kind);
    if (!slot) { sa_map_set_error(kind == SA_HANDLE_MAP ? "invalid or closed MAP handle" : "invalid or closed STR_MAP handle"); return -1; }
    return (long long)slot->len;
}

static long long sa_map_length(SaHandle handle) { return sa_map_length_impl(handle, SA_HANDLE_MAP); }
static long long sa_strmap_length(SaHandle handle) { return sa_map_length_impl(handle, SA_HANDLE_STR_MAP); }

static SaHandle sa_map_keys_impl(SaHandle handle, unsigned int kind) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, kind);
    if (!slot) { sa_map_set_error(kind == SA_HANDLE_MAP ? "invalid or closed MAP handle" : "invalid or closed STR_MAP handle"); return 0; }
    SaHandle keys = sa_strlist_new();
    if (!keys) { sa_map_set_error("cannot allocate STR_LIST for keys"); return 0; }
    for (size_t i = 0; i < slot->bucket_count; i++) {
        for (SaMapEntry* entry = slot->buckets[i]; entry; entry = entry->next) {
            sa_strlist_push(keys, entry->key);
        }
    }
    return keys;
}

static SaHandle sa_map_keys(SaHandle handle) { return sa_map_keys_impl(handle, SA_HANDLE_MAP); }
static SaHandle sa_strmap_keys(SaHandle handle) { return sa_map_keys_impl(handle, SA_HANDLE_STR_MAP); }

static int sa_map_clear_impl(SaHandle handle, unsigned int kind) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, kind);
    if (!slot) { sa_map_set_error(kind == SA_HANDLE_MAP ? "invalid or closed MAP handle" : "invalid or closed STR_MAP handle"); return 0; }
    size_t bucket_count = slot->bucket_count;
    for (size_t i = 0; i < bucket_count; i++) {
        SaMapEntry* entry = slot->buckets[i];
        while (entry) {
            SaMapEntry* next = entry->next;
            free(entry->key);
            free(entry->str);
            free(entry);
            entry = next;
        }
        slot->buckets[i] = NULL;
    }
    slot->len = 0;
    return 1;
}

static int sa_map_clear(SaHandle handle) { return sa_map_clear_impl(handle, SA_HANDLE_MAP); }
static int sa_strmap_clear(SaHandle handle) { return sa_map_clear_impl(handle, SA_HANDLE_STR_MAP); }

static int sa_map_close_impl(SaHandle handle, unsigned int kind) {
    sa_map_clear_error();
    SaMapSlot* slot = sa_map_slot(handle, kind);
    if (!slot) { sa_map_set_error(kind == SA_HANDLE_MAP ? "invalid or closed MAP handle" : "invalid or closed STR_MAP handle"); return 0; }
    sa_map_slot_free(slot);
    slot->generation++;
    return 1;
}

static int sa_map_close(SaHandle handle) { return sa_map_close_impl(handle, SA_HANDLE_MAP); }
static int sa_strmap_close(SaHandle handle) { return sa_map_close_impl(handle, SA_HANDLE_STR_MAP); }
#endif

#ifdef _WIN32
static wchar_t* sa_win_widen(const char* value) {
    const char* safe = value ? value : "";
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, safe, -1, NULL, 0);
    if (count <= 0) return NULL;
    wchar_t* out = (wchar_t*)malloc((size_t)count * sizeof(wchar_t));
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, safe, -1, out, count)) {
        free(out);
        return NULL;
    }
    return out;
}

static char* sa_win_narrow(const wchar_t* value) {
    const wchar_t* safe = value ? value : L"";
    int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, safe, -1, NULL, 0, NULL, NULL);
    if (count <= 0) return NULL;
    char* out = (char*)malloc((size_t)count);
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, safe, -1, out, count, NULL, NULL)) {
        free(out);
        return NULL;
    }
    return out;
}
#endif

static long long sa_str_length(const char* value) {
    return (long long)strlen(value ? value : "");
}

static char* sa_str_concat(const char* a, const char* b) {
    const char* sa = a ? a : "";
    const char* sb = b ? b : "";
    size_t la = strlen(sa);
    size_t lb = strlen(sb);
    char* out = (char*)malloc(la + lb + 1);
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    memcpy(out, sa, la);
    memcpy(out + la, sb, lb + 1);
    return out;
}

static char* sa_str_slice(const char* value, long long start, long long count) {
    const char* safe = value ? value : "";
    long long len = (long long)strlen(safe);
    if (start < 0) start = 0;
    if (start > len) start = len;
    if (count < 0) count = 0;
    if (start + count > len) count = len - start;
    char* out = (char*)malloc((size_t)count + 1);
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    memcpy(out, safe + start, (size_t)count);
    out[count] = '\0';
    return out;
}

static long long sa_str_find(const char* value, const char* needle) {
    const char* safe = value ? value : "";
    const char* sub = needle ? needle : "";
    const char* hit = strstr(safe, sub);
    return hit ? (long long)(hit - safe) : -1;
}

static char* sa_str_upper(const char* value) {
    char* out = sa_strdup(value);
    for (char* p = out; *p; ++p) {
        if (*p >= 'a' && *p <= 'z') *p = (char)(*p - 'a' + 'A');
    }
    return out;
}

static char* sa_str_lower(const char* value) {
    char* out = sa_strdup(value);
    for (char* p = out; *p; ++p) {
        if (*p >= 'A' && *p <= 'Z') *p = (char)(*p - 'A' + 'a');
    }
    return out;
}

static char* sa_str_replace(const char* value, const char* old_sub, const char* new_sub) {
    const char* safe = value ? value : "";
    const char* from = old_sub ? old_sub : "";
    const char* to = new_sub ? new_sub : "";
    size_t from_len = strlen(from);
    if (from_len == 0) return sa_strdup(safe);  /* 空匹配串：原样返回 */
    size_t to_len = strlen(to);
    /* 先统计出现次数，算出精确缓冲大小 */
    size_t count = 0;
    for (const char* p = safe; (p = strstr(p, from)) != NULL; p += from_len) count++;
    size_t out_len = strlen(safe) + count * (to_len > from_len ? to_len - from_len : 0)
                     - count * (from_len > to_len ? from_len - to_len : 0);
    char* out = (char*)malloc(out_len + 1);
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    char* dst = out;
    const char* src = safe;
    const char* hit;
    while ((hit = strstr(src, from)) != NULL) {
        size_t prefix = (size_t)(hit - src);
        memcpy(dst, src, prefix);
        dst += prefix;
        memcpy(dst, to, to_len);
        dst += to_len;
        src = hit + from_len;
    }
    strcpy(dst, src);
    return out;
}

static void sa_set_string(char** target, const char* value) {
    char* next = sa_strdup(value);
    free(*target);
    *target = next;
}

static void sa_set_error(SaError* target, const SaError* value) {
    free(target->message);
    *target = *value;
    target->message = sa_strdup(value->message ? value->message : "");
}

static void sa_error_clear(SaError* target) {
    free(target->message);
    target->message = NULL;
    target->err_code = 0;
    target->type = "ERR_NONE";
    target->line_number = 0;
    target->sub_name = NULL;
}

static void sa_raise_new(const char* type, const char* message, int line_number, const char* sub_name) {
    sa_error_clear(&sa_current_error);
    sa_current_error.err_code = sa_error_code(type);
    sa_current_error.type = type;
    sa_current_error.message = sa_strdup(message ? message : "");
    sa_current_error.line_number = line_number;
    sa_current_error.sub_name = sub_name;
}

/* 把待抛错误装入 sa_current_error，但不跳转。THROW 由 codegen 拆成
 * raise -> 清理当前 SUB 局部资源 -> dispatch 三步，保证 longjmp 前不泄漏局部。
 * error == &sa_current_error 时是重抛，跳过自拷贝避免 use-after-free。 */
static void sa_raise_error(const SaError* error) {
    if (error != &sa_current_error) {
        sa_set_error(&sa_current_error, error);
    }
}

static void sa_throw_dispatch(void) {
    if (sa_try_top <= 0) {
        fprintf(stderr, "Uncaught SonAlgebraic error %s at line %d: %s\n", sa_current_error.type, sa_current_error.line_number, sa_current_error.message);
        /* 与正常退出路径保持一致：退出前释放错误自身的 message，避免泄漏检测工具误报。 */
        sa_error_clear(&sa_current_error);
        exit(1);
    }
    SA_LONGJMP(sa_try_stack[sa_try_top - 1].env);
}

static void sa_throw_new(const char* type, const char* message, int line_number, const char* sub_name) {
    sa_raise_new(type, message, line_number, sub_name);
    sa_throw_dispatch();
}

static void sa_throw_error(const SaError* error) {
    sa_raise_error(error);
    sa_throw_dispatch();
}

static double sa_number(const char* value) {
    return strtod(value ? value : "0", NULL);
}

static char* sa_to_string_long(long long value) {
    char buffer[64];
    snprintf(buffer, sizeof(buffer), "%lld", value);
    return sa_strdup(buffer);
}

static char* sa_to_string_double(double value) {
    char buffer[128];
    snprintf(buffer, sizeof(buffer), "%.15g", value);
    return sa_strdup(buffer);
}

static char* sa_to_string_pointer(void* value) {
    char buffer[64];
    snprintf(buffer, sizeof(buffer), "%p", value);
    return sa_strdup(buffer);
}

static void sa_sb_init(SaStringBuilder* builder) {
    builder->cap = 64;
    builder->len = 0;
    builder->data = (char*)malloc(builder->cap);
    if (!builder->data) {
        fputs("SonAlgebraic runtime: out of memory\n", stderr);
        exit(1);
    }
    builder->data[0] = '\0';
}

static void sa_sb_append(SaStringBuilder* builder, const char* value) {
    const char* safe = value ? value : "";
    size_t add = strlen(safe);
    while (builder->len + add + 1 > builder->cap) {
        builder->cap *= 2;
        builder->data = (char*)realloc(builder->data, builder->cap);
        if (!builder->data) {
            fputs("SonAlgebraic runtime: out of memory\n", stderr);
            exit(1);
        }
    }
    memcpy(builder->data + builder->len, safe, add + 1);
    builder->len += add;
}

static char* sa_sb_take(SaStringBuilder* builder) {
    char* data = builder->data;
    builder->data = NULL;
    builder->len = 0;
    builder->cap = 0;
    return data;
}

static SaSymbol sa_symbol_new(SaSymbolKind kind, const char* text, char op, SaSymbol left, SaSymbol right) {
    SaSymbol node = (SaSymbol)malloc(sizeof(SaSymbolNode));
    if (!node) {
        fputs("SonAlgebraic runtime: out of memory\n", stderr);
        exit(1);
    }
    node->kind = kind;
    node->text = text ? sa_strdup(text) : NULL;
    node->op = op;
    node->left = left;
    node->right = right;
    return node;
}

static SaSymbol sa_symbol_const(const char* text) {
    return sa_symbol_new(SA_SYM_CONST, text, 0, NULL, NULL);
}

static SaSymbol sa_symbol_var(const char* name) {
    return sa_symbol_new(SA_SYM_VAR, name, 0, NULL, NULL);
}

static SaSymbol sa_symbol_func(const char* name, SaSymbol arg) {
    return sa_symbol_new(SA_SYM_FUNC, name, 0, arg, NULL);
}

static SaSymbol sa_symbol_op(char op, SaSymbol left, SaSymbol right) {
    return sa_symbol_new(SA_SYM_OP, NULL, op, left, right);
}

static SaSymbol sa_symbol_clone(SaSymbol s) {
    if (!s) return NULL;
    return sa_symbol_new(s->kind, s->text, s->op, sa_symbol_clone(s->left), sa_symbol_clone(s->right));
}

static void sa_symbol_free(SaSymbol symbol);
static int sa_symbol_is_const_value(SaSymbol s, double* out);

/* 数值求值：CONST 解析为数字，VAR 视为 0（应先 SUBST 消除自由变量） */
static double sa_symbol_eval(SaSymbol s) {
    if (!s) return 0.0;
    if (s->kind == SA_SYM_CONST) return strtod(s->text ? s->text : "0", NULL);
    if (s->kind == SA_SYM_VAR) return 0.0;
    if (s->kind == SA_SYM_FUNC) {
        double value = sa_symbol_eval(s->left);
        if (s->text && strcmp(s->text, "LOG") == 0) return log(value);
        if (s->text && strcmp(s->text, "EXP") == 0) return exp(value);
        if (s->text && strcmp(s->text, "SIN") == 0) return sin(value);
        if (s->text && strcmp(s->text, "COS") == 0) return cos(value);
        if (s->text && strcmp(s->text, "TAN") == 0) return tan(value);
        if (s->text && strcmp(s->text, "SQRT") == 0) return sqrt(value);
        return 0.0;
    }
    double l = sa_symbol_eval(s->left);
    double r = sa_symbol_eval(s->right);
    switch (s->op) {
        case '+': return l + r;
        case '-': return l - r;
        case '*': return l * r;
        case '/': return r != 0.0 ? l / r : 0.0;
        case '^': return pow(l, r);
        default: return 0.0;
    }
}

/* 把名为 var 的自由变量替换为常数 value */
static SaSymbol sa_symbol_subst(SaSymbol s, const char* var, double value) {
    if (!s) return NULL;
    if (s->kind == SA_SYM_VAR && s->text && strcmp(s->text, var) == 0) {
        char buf[64];
        snprintf(buf, sizeof(buf), "%g", value);
        return sa_symbol_const(buf);
    }
    if (s->kind == SA_SYM_OP) {
        return sa_symbol_op(s->op, sa_symbol_subst(s->left, var, value), sa_symbol_subst(s->right, var, value));
    }
    if (s->kind == SA_SYM_FUNC) {
        return sa_symbol_func(s->text, sa_symbol_subst(s->left, var, value));
    }
    return sa_symbol_clone(s);
}

/* 对 var 求符号导数，支持 + - * / 和常数指数幂。 */
static SaSymbol sa_symbol_deriv(SaSymbol s, const char* var) {
    if (!s) return sa_symbol_const("0");
    if (s->kind == SA_SYM_CONST) return sa_symbol_const("0");
    if (s->kind == SA_SYM_VAR) {
        return sa_symbol_const((s->text && strcmp(s->text, var) == 0) ? "1" : "0");
    }
    if (s->kind == SA_SYM_FUNC) {
        SaSymbol inner_deriv = sa_symbol_deriv(s->left, var);
        if (s->text && strcmp(s->text, "LOG") == 0) {
            return sa_symbol_op('/', inner_deriv, sa_symbol_clone(s->left));
        }
        if (s->text && strcmp(s->text, "EXP") == 0) {
            return sa_symbol_op('*', sa_symbol_func("EXP", sa_symbol_clone(s->left)), inner_deriv);
        }
        if (s->text && strcmp(s->text, "SIN") == 0) {
            return sa_symbol_op('*', sa_symbol_func("COS", sa_symbol_clone(s->left)), inner_deriv);
        }
        if (s->text && strcmp(s->text, "COS") == 0) {
            return sa_symbol_op('*', sa_symbol_const("-1"), sa_symbol_op('*', sa_symbol_func("SIN", sa_symbol_clone(s->left)), inner_deriv));
        }
        sa_symbol_free(inner_deriv);
        return sa_symbol_const("0");
    }
    /* OP 节点 */
    SaSymbol dl = sa_symbol_deriv(s->left, var);
    SaSymbol dr = sa_symbol_deriv(s->right, var);
    if (s->op == '+' || s->op == '-') {
        return sa_symbol_op(s->op, dl, dr);
    }
    if (s->op == '*') {
        /* (l*r)' = l'*r + l*r' */
        SaSymbol left_term = sa_symbol_op('*', dl, sa_symbol_clone(s->right));
        SaSymbol right_term = sa_symbol_op('*', sa_symbol_clone(s->left), dr);
        return sa_symbol_op('+', left_term, right_term);
    }
    if (s->op == '/') {
        /* (l/r)' = (l'*r - l*r') / (r*r) */
        SaSymbol num_left = sa_symbol_op('*', dl, sa_symbol_clone(s->right));
        SaSymbol num_right = sa_symbol_op('*', sa_symbol_clone(s->left), dr);
        SaSymbol numerator = sa_symbol_op('-', num_left, num_right);
        SaSymbol denom = sa_symbol_op('*', sa_symbol_clone(s->right), sa_symbol_clone(s->right));
        return sa_symbol_op('/', numerator, denom);
    }
    if (s->op == '^') {
        double exponent = 0.0;
        if (sa_symbol_is_const_value(s->right, &exponent)) {
            sa_symbol_free(dr);
            if (exponent == 0.0) {
                sa_symbol_free(dl);
                return sa_symbol_const("0");
            }
            if (exponent == 1.0) {
                return dl;
            }
            char coeff_buf[64];
            char exp_buf[64];
            snprintf(coeff_buf, sizeof(coeff_buf), "%g", exponent);
            snprintf(exp_buf, sizeof(exp_buf), "%g", exponent - 1.0);
            SaSymbol coeff = sa_symbol_const(coeff_buf);
            SaSymbol reduced_power = sa_symbol_op('^', sa_symbol_clone(s->left), sa_symbol_const(exp_buf));
            return sa_symbol_op('*', sa_symbol_op('*', coeff, reduced_power), dl);
        }
        SaSymbol term_left = sa_symbol_op('*', dr, sa_symbol_func("LOG", sa_symbol_clone(s->left)));
        SaSymbol quotient = sa_symbol_op('/', dl, sa_symbol_clone(s->left));
        SaSymbol term_right = sa_symbol_op('*', sa_symbol_clone(s->right), quotient);
        SaSymbol inner = sa_symbol_op('+', term_left, term_right);
        return sa_symbol_op('*', sa_symbol_clone(s), inner);
    }
    sa_symbol_free(dl);
    sa_symbol_free(dr);
    return sa_symbol_const("0");
}

static int sa_symbol_is_const_value(SaSymbol s, double* out) {
    if (s && s->kind == SA_SYM_CONST) {
        char* end = NULL;
        double v = strtod(s->text ? s->text : "0", &end);
        if (end && *end == '\0') { *out = v; return 1; }
    }
    return 0;
}

/* 代数化简：常量折叠 + 单位元/零元规则（x+0, x*1, x*0 等） */
static SaSymbol sa_symbol_simplify(SaSymbol s) {
    if (!s) return NULL;
    if (s->kind == SA_SYM_FUNC) {
        SaSymbol arg = sa_symbol_simplify(s->left);
        double value;
        if (sa_symbol_is_const_value(arg, &value)) {
            double res = 0.0;
            int fold = 1;
            if (s->text && strcmp(s->text, "LOG") == 0) res = log(value);
            else if (s->text && strcmp(s->text, "EXP") == 0) res = exp(value);
            else if (s->text && strcmp(s->text, "SIN") == 0) res = sin(value);
            else if (s->text && strcmp(s->text, "COS") == 0) res = cos(value);
            else if (s->text && strcmp(s->text, "TAN") == 0) res = tan(value);
            else if (s->text && strcmp(s->text, "SQRT") == 0) res = sqrt(value);
            else fold = 0;
            if (fold) {
                sa_symbol_free(arg);
                char buf[64];
                snprintf(buf, sizeof(buf), "%g", res);
                return sa_symbol_const(buf);
            }
        }
        return sa_symbol_func(s->text, arg);
    }
    if (s->kind != SA_SYM_OP) return sa_symbol_clone(s);
    SaSymbol l = sa_symbol_simplify(s->left);
    SaSymbol r = sa_symbol_simplify(s->right);
    double lv, rv;
    int lc = sa_symbol_is_const_value(l, &lv);
    int rc = sa_symbol_is_const_value(r, &rv);
    /* 两侧都是常量：直接折叠 */
    if (lc && rc) {
        double res = 0.0;
        switch (s->op) {
            case '+': res = lv + rv; break;
            case '-': res = lv - rv; break;
            case '*': res = lv * rv; break;
            case '/': res = rv != 0.0 ? lv / rv : 0.0; break;
            case '^': res = pow(lv, rv); break;
        }
        sa_symbol_free(l);
        sa_symbol_free(r);
        char buf[64];
        snprintf(buf, sizeof(buf), "%g", res);
        return sa_symbol_const(buf);
    }
    /* 单位元/零元 */
    if (s->op == '+') {
        if (lc && lv == 0.0) { sa_symbol_free(l); return r; }
        if (rc && rv == 0.0) { sa_symbol_free(r); return l; }
    }
    if (s->op == '-') {
        if (rc && rv == 0.0) { sa_symbol_free(r); return l; }
    }
    if (s->op == '*') {
        if ((lc && lv == 0.0) || (rc && rv == 0.0)) {
            sa_symbol_free(l); sa_symbol_free(r);
            return sa_symbol_const("0");
        }
        if (lc && lv == 1.0) { sa_symbol_free(l); return r; }
        if (rc && rv == 1.0) { sa_symbol_free(r); return l; }
    }
    if (s->op == '/') {
        if (rc && rv == 1.0) { sa_symbol_free(r); return l; }
    }
    if (s->op == '^') {
        if (rc && rv == 0.0) {
            sa_symbol_free(l); sa_symbol_free(r);
            return sa_symbol_const("1");
        }
        if (rc && rv == 1.0) { sa_symbol_free(r); return l; }
        if (lc && lv == 0.0) {
            sa_symbol_free(l); sa_symbol_free(r);
            return sa_symbol_const("0");
        }
        if (lc && lv == 1.0) {
            sa_symbol_free(l); sa_symbol_free(r);
            return sa_symbol_const("1");
        }
    }
    return sa_symbol_op(s->op, l, r);
}

static void sa_symbol_free(SaSymbol symbol) {
    if (!symbol) return;
    sa_symbol_free(symbol->left);
    sa_symbol_free(symbol->right);
    free(symbol->text);
    free(symbol);
}

static void sa_symbol_to_builder(SaStringBuilder* builder, SaSymbol symbol) {
    if (!symbol) {
        sa_sb_append(builder, "<null-symbol>");
        return;
    }
    if (symbol->kind == SA_SYM_CONST || symbol->kind == SA_SYM_VAR) {
        sa_sb_append(builder, symbol->text);
        return;
    }
    if (symbol->kind == SA_SYM_FUNC) {
        sa_sb_append(builder, symbol->text ? symbol->text : "<func>");
        sa_sb_append(builder, "(");
        sa_symbol_to_builder(builder, symbol->left);
        sa_sb_append(builder, ")");
        return;
    }
    const char* op = symbol->op == '^' ? " ** " : NULL;
    char op_buf[4] = {' ', symbol->op, ' ', '\0'};
    if (!op) op = op_buf;
    sa_sb_append(builder, "(");
    sa_symbol_to_builder(builder, symbol->left);
    sa_sb_append(builder, op);
    sa_symbol_to_builder(builder, symbol->right);
    sa_sb_append(builder, ")");
}

static char* sa_symbol_to_string(SaSymbol symbol) {
    SaStringBuilder builder;
    sa_sb_init(&builder);
    sa_symbol_to_builder(&builder, symbol);
    return sa_sb_take(&builder);
}

#ifdef SA_ENABLE_NET
#ifdef _WIN32
typedef SOCKET SaSocket;
typedef int SaSockLen;
#define SA_NET_INVALID_SOCKET INVALID_SOCKET
#define sa_net_close_socket closesocket
#else
typedef int SaSocket;
typedef socklen_t SaSockLen;
#define SA_NET_INVALID_SOCKET (-1)
#define sa_net_close_socket close
#endif

#define SA_NET_SLOT_COUNT 64
typedef struct {
    SaSocket socket;
    uint32_t generation;
    int active;
    void* tls_state;
} SaNetSocketSlot;

typedef struct {
    SaSocket socket;
    int family;
    uint32_t generation;
    int active;
} SaUdpSocketSlot;

static SaNetSocketSlot sa_net_stream_slots[SA_NET_SLOT_COUNT];
static SaNetSocketSlot sa_tcp_listener_slots[SA_NET_SLOT_COUNT];
static SaUdpSocketSlot sa_udp_socket_slots[SA_NET_SLOT_COUNT];
static char sa_net_last_error[512] = "";
static char* sa_net_last_headers = NULL;
static long long sa_net_last_code = 0;
static char sa_net_last_peer_host[256] = "";
static long long sa_net_last_peer_port = 0;
static int sa_net_runtime_initialized = 0;

#ifdef SA_ENABLE_TLS
#ifdef _WIN32
typedef struct {
    CredHandle credentials;
    CtxtHandle context;
    SecPkgContext_StreamSizes sizes;
    int credentials_valid;
    int context_valid;
    unsigned char* encrypted;
    size_t encrypted_len;
    size_t encrypted_cap;
    unsigned char* pending_plain;
    size_t pending_plain_len;
    size_t pending_plain_offset;
} SaTlsState;
#else
typedef struct {
    SSL_CTX* context;
    SSL* session;
} SaTlsState;
#endif
#endif

static void sa_net_tls_free_state(void* raw_state) {
#ifdef SA_ENABLE_TLS
    SaTlsState* state = (SaTlsState*)raw_state;
    if (!state) return;
#ifdef _WIN32
    if (state->context_valid) DeleteSecurityContext(&state->context);
    if (state->credentials_valid) FreeCredentialsHandle(&state->credentials);
    free(state->encrypted);
    free(state->pending_plain);
#else
    if (state->session) SSL_free(state->session);
    if (state->context) SSL_CTX_free(state->context);
#endif
    free(state);
#else
    (void)raw_state;
#endif
}

static void sa_net_close_all(void) {
    for (size_t i = 0; i < SA_NET_SLOT_COUNT; i++) {
        if (sa_net_stream_slots[i].active) {
            sa_net_tls_free_state(sa_net_stream_slots[i].tls_state);
            sa_net_close_socket(sa_net_stream_slots[i].socket);
        }
        if (sa_tcp_listener_slots[i].active) sa_net_close_socket(sa_tcp_listener_slots[i].socket);
        if (sa_udp_socket_slots[i].active && sa_udp_socket_slots[i].socket != SA_NET_INVALID_SOCKET) sa_net_close_socket(sa_udp_socket_slots[i].socket);
        sa_net_stream_slots[i].active = 0;
        sa_tcp_listener_slots[i].active = 0;
        sa_udp_socket_slots[i].active = 0;
    }
#ifdef _WIN32
    if (sa_net_runtime_initialized) WSACleanup();
#endif
    sa_net_runtime_initialized = 0;
}

static int sa_net_initialize(void) {
    if (sa_net_runtime_initialized) return 1;
#ifdef _WIN32
    WSADATA data;
    int result = WSAStartup(MAKEWORD(2, 2), &data);
    if (result != 0) {
        snprintf(sa_net_last_error, sizeof(sa_net_last_error), "WSAStartup failed (%d)", result);
        sa_net_last_code = result;
        return 0;
    }
#endif
#ifndef _WIN32
    signal(SIGPIPE, SIG_IGN);
#endif
    sa_net_runtime_initialized = 1;
    atexit(sa_net_close_all);
    return 1;
}

static void sa_net_clear_error(void) {
    sa_net_last_error[0] = '\0';
    sa_net_last_code = 0;
}

static void sa_net_clear_state(void) {
    sa_net_clear_error();
    free(sa_net_last_headers);
    sa_net_last_headers = NULL;
}

static void sa_net_set_error(const char* message) {
    snprintf(sa_net_last_error, sizeof(sa_net_last_error), "%s", message ? message : "network error");
}

static void sa_net_set_error_code(const char* operation, unsigned long code) {
    sa_net_last_code = (long long)code;
    snprintf(sa_net_last_error, sizeof(sa_net_last_error), "%s failed (%lu)", operation ? operation : "network", code);
}

static void sa_net_set_socket_error(const char* operation) {
#ifdef _WIN32
    sa_net_set_error_code(operation, (unsigned long)WSAGetLastError());
#else
    sa_net_last_code = (long long)errno;
    snprintf(sa_net_last_error, sizeof(sa_net_last_error), "%s: %s", operation ? operation : "network", strerror(errno));
#endif
}

static char* sa_net_last_error_copy(void) {
    return sa_strdup(sa_net_last_error);
}

static char* sa_net_last_headers_copy(void) {
    return sa_strdup(sa_net_last_headers ? sa_net_last_headers : "");
}

static long long sa_net_last_code_value(void) { return sa_net_last_code; }
static char* sa_net_last_peer_host_copy(void) { return sa_strdup(sa_net_last_peer_host); }
static long long sa_net_last_peer_port_value(void) { return sa_net_last_peer_port; }

static int sa_net_timeout_value(long long timeout_ms) {
    if (timeout_ms <= 0) return 30000;
    return timeout_ms > INT_MAX ? INT_MAX : (int)timeout_ms;
}

static int sa_net_set_timeouts(SaSocket socket_value, long long timeout_ms) {
    int timeout = sa_net_timeout_value(timeout_ms);
#ifdef _WIN32
    DWORD value = (DWORD)timeout;
    return setsockopt(socket_value, SOL_SOCKET, SO_RCVTIMEO, (const char*)&value, sizeof(value)) == 0
        && setsockopt(socket_value, SOL_SOCKET, SO_SNDTIMEO, (const char*)&value, sizeof(value)) == 0;
#else
    struct timeval value;
    value.tv_sec = timeout / 1000;
    value.tv_usec = (timeout % 1000) * 1000;
    return setsockopt(socket_value, SOL_SOCKET, SO_RCVTIMEO, &value, sizeof(value)) == 0
        && setsockopt(socket_value, SOL_SOCKET, SO_SNDTIMEO, &value, sizeof(value)) == 0;
#endif
}

static int sa_net_set_nonblocking(SaSocket socket_value, int enabled) {
#ifdef _WIN32
    u_long mode = enabled ? 1UL : 0UL;
    return ioctlsocket(socket_value, FIONBIO, &mode) == 0;
#else
    int flags = fcntl(socket_value, F_GETFL, 0);
    if (flags < 0) return 0;
    return fcntl(socket_value, F_SETFL, enabled ? (flags | O_NONBLOCK) : (flags & ~O_NONBLOCK)) == 0;
#endif
}

static int sa_net_connect_pending(void) {
#ifdef _WIN32
    int code = WSAGetLastError();
    return code == WSAEWOULDBLOCK || code == WSAEINPROGRESS || code == WSAEINVAL;
#else
    return errno == EINPROGRESS || errno == EWOULDBLOCK;
#endif
}

static int sa_net_wait_socket(SaSocket socket_value, int writable, long long timeout_ms) {
    fd_set set;
    FD_ZERO(&set);
    FD_SET(socket_value, &set);
    int timeout = sa_net_timeout_value(timeout_ms);
    struct timeval value;
    value.tv_sec = timeout / 1000;
    value.tv_usec = (timeout % 1000) * 1000;
#ifdef _WIN32
    int result = select(0, writable ? NULL : &set, writable ? &set : NULL, NULL, &value);
#else
    int result = select(socket_value + 1, writable ? NULL : &set, writable ? &set : NULL, NULL, &value);
#endif
    if (result == 0) {
        sa_net_last_code = 0;
        sa_net_set_error("network operation timed out");
        return 0;
    }
    if (result < 0) {
        sa_net_set_socket_error("select");
        return 0;
    }
    return 1;
}

static int sa_net_port_text(long long port, char* output, size_t output_size) {
    if (port < 0 || port > 65535) {
        sa_net_set_error("port must be between 0 and 65535");
        return 0;
    }
    snprintf(output, output_size, "%lld", port);
    return 1;
}

static void sa_net_store_peer(const struct sockaddr* address, SaSockLen address_len) {
    char host[NI_MAXHOST];
    char service[NI_MAXSERV];
    int result = getnameinfo(address, address_len, host, sizeof(host), service, sizeof(service), NI_NUMERICHOST | NI_NUMERICSERV);
    if (result == 0) {
        snprintf(sa_net_last_peer_host, sizeof(sa_net_last_peer_host), "%s", host);
        sa_net_last_peer_port = atoll(service);
    } else
    {
        sa_net_last_peer_host[0] = '\0';
        sa_net_last_peer_port = 0;
    }
}

static SaNetSocketSlot* sa_net_stream_slot(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_NET_STREAM, SA_NET_SLOT_COUNT, &index, &generation)) return NULL;
    SaNetSocketSlot* slot = &sa_net_stream_slots[index];
    return slot->active && slot->generation == generation ? slot : NULL;
}

static SaNetSocketSlot* sa_tcp_listener_slot(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_TCP_LISTENER, SA_NET_SLOT_COUNT, &index, &generation)) return NULL;
    SaNetSocketSlot* slot = &sa_tcp_listener_slots[index];
    return slot->active && slot->generation == generation ? slot : NULL;
}

static SaUdpSocketSlot* sa_udp_socket_slot(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_UDP_SOCKET, SA_NET_SLOT_COUNT, &index, &generation)) return NULL;
    SaUdpSocketSlot* slot = &sa_udp_socket_slots[index];
    return slot->active && slot->generation == generation ? slot : NULL;
}

static SaHandle sa_net_take_stream(SaSocket socket_value) {
    for (size_t i = 0; i < SA_NET_SLOT_COUNT; i++) {
        if (!sa_net_stream_slots[i].active) {
            if (++sa_net_stream_slots[i].generation == 0) sa_net_stream_slots[i].generation = 1;
            sa_net_stream_slots[i].socket = socket_value;
            sa_net_stream_slots[i].active = 1;
            sa_net_stream_slots[i].tls_state = NULL;
            return sa_handle_make(SA_HANDLE_NET_STREAM, sa_net_stream_slots[i].generation, i);
        }
    }
    sa_net_close_socket(socket_value);
    sa_net_set_error("too many open network streams");
    return 0;
}

static SaHandle sa_net_take_listener(SaSocket socket_value) {
    for (size_t i = 0; i < SA_NET_SLOT_COUNT; i++) {
        if (!sa_tcp_listener_slots[i].active) {
            if (++sa_tcp_listener_slots[i].generation == 0) sa_tcp_listener_slots[i].generation = 1;
            sa_tcp_listener_slots[i].socket = socket_value;
            sa_tcp_listener_slots[i].active = 1;
            return sa_handle_make(SA_HANDLE_TCP_LISTENER, sa_tcp_listener_slots[i].generation, i);
        }
    }
    sa_net_close_socket(socket_value);
    sa_net_set_error("too many open TCP listeners");
    return 0;
}

static long long sa_net_send_bytes(SaSocket socket_value, const unsigned char* data, size_t length);

static void sa_net_set_gai_error(const char* operation, int code) {
    sa_net_last_code = (long long)code;
#ifdef _WIN32
    snprintf(sa_net_last_error, sizeof(sa_net_last_error), "%s: %s", operation ? operation : "getaddrinfo", gai_strerrorA(code));
#else
    snprintf(sa_net_last_error, sizeof(sa_net_last_error), "%s: %s", operation ? operation : "getaddrinfo", gai_strerror(code));
#endif
}

static char* sa_net_dns(const char* host) {
    sa_net_clear_error();
    if (!sa_net_initialize()) return sa_strdup("");
    if (!host || !host[0]) { sa_net_set_error("DNS host must not be empty"); return sa_strdup(""); }
    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    struct addrinfo* result = NULL;
    int error = getaddrinfo(host, NULL, &hints, &result);
    if (error != 0) { sa_net_set_gai_error("DNS lookup", error); return sa_strdup(""); }
    SaStringBuilder builder;
    sa_sb_init(&builder);
    for (struct addrinfo* item = result; item; item = item->ai_next) {
        char numeric[NI_MAXHOST];
        if (getnameinfo(item->ai_addr, (SaSockLen)item->ai_addrlen, numeric, sizeof(numeric), NULL, 0, NI_NUMERICHOST) != 0) continue;
        if (builder.len) sa_sb_append(&builder, ",");
        sa_sb_append(&builder, numeric);
    }
    freeaddrinfo(result);
    return sa_sb_take(&builder);
}

static SaSocket sa_net_connect_socket(const char* host, long long port, long long timeout_ms, int socktype, int protocol, int* family_out) {
    char port_text[16];
    if (!host || !host[0]) { sa_net_set_error("host must not be empty"); return SA_NET_INVALID_SOCKET; }
    if (!sa_net_port_text(port, port_text, sizeof(port_text))) return SA_NET_INVALID_SOCKET;
    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = socktype;
    hints.ai_protocol = protocol;
    struct addrinfo* result = NULL;
    int error = getaddrinfo(host, port_text, &hints, &result);
    if (error != 0) { sa_net_set_gai_error("address lookup", error); return SA_NET_INVALID_SOCKET; }
    SaSocket connected = SA_NET_INVALID_SOCKET;
    for (struct addrinfo* item = result; item; item = item->ai_next) {
        SaSocket socket_value = (SaSocket)socket(item->ai_family, item->ai_socktype, item->ai_protocol);
        if (socket_value == SA_NET_INVALID_SOCKET) continue;
        if (!sa_net_set_nonblocking(socket_value, 1)) {
            sa_net_close_socket(socket_value);
            continue;
        }
        int result_code = connect(socket_value, item->ai_addr, (SaSockLen)item->ai_addrlen);
        if (result_code != 0 && (!sa_net_connect_pending() || !sa_net_wait_socket(socket_value, 1, timeout_ms))) {
            sa_net_close_socket(socket_value);
            continue;
        }
        if (result_code != 0) {
            int socket_error = 0;
            SaSockLen socket_error_size = (SaSockLen)sizeof(socket_error);
#ifdef _WIN32
            if (getsockopt(socket_value, SOL_SOCKET, SO_ERROR, (char*)&socket_error, &socket_error_size) != 0 || socket_error != 0) {
#else
            if (getsockopt(socket_value, SOL_SOCKET, SO_ERROR, &socket_error, &socket_error_size) != 0 || socket_error != 0) {
#endif
                if (socket_error) {
                    sa_net_last_code = socket_error;
                    snprintf(sa_net_last_error, sizeof(sa_net_last_error), "connect failed (%d)", socket_error);
                }
                sa_net_close_socket(socket_value);
                continue;
            }
        }
        if (!sa_net_set_nonblocking(socket_value, 0) || !sa_net_set_timeouts(socket_value, timeout_ms)) {
            sa_net_close_socket(socket_value);
            continue;
        }
        connected = socket_value;
        if (family_out) *family_out = item->ai_family;
        sa_net_store_peer(item->ai_addr, (SaSockLen)item->ai_addrlen);
        break;
    }
    freeaddrinfo(result);
    if (connected == SA_NET_INVALID_SOCKET && !sa_net_last_error[0]) sa_net_set_socket_error("connect");
    return connected;
}

#ifdef SA_ENABLE_TLS
#ifdef _WIN32
static int sa_net_tls_append_encrypted(SaTlsState* state, const unsigned char* data, size_t length) {
    if (length > SIZE_MAX - state->encrypted_len) return 0;
    size_t needed = state->encrypted_len + length;
    if (needed > state->encrypted_cap) {
        size_t next = state->encrypted_cap ? state->encrypted_cap : 32768;
        while (next < needed) {
            if (next > SIZE_MAX / 2) return 0;
            next *= 2;
        }
        unsigned char* grown = (unsigned char*)realloc(state->encrypted, next);
        if (!grown) return 0;
        state->encrypted = grown;
        state->encrypted_cap = next;
    }
    if (length) memcpy(state->encrypted + state->encrypted_len, data, length);
    state->encrypted_len += length;
    return 1;
}

static int sa_net_tls_recv_encrypted(SaSocket socket_value, SaTlsState* state) {
    unsigned char chunk[16384];
    int count = recv(socket_value, (char*)chunk, (int)sizeof(chunk), 0);
    if (count == 0) { sa_net_set_error("TLS peer closed during handshake or record receive"); return 0; }
    if (count < 0) { sa_net_set_socket_error("TLS recv"); return 0; }
    if (!sa_net_tls_append_encrypted(state, chunk, (size_t)count)) {
        sa_net_set_error("TLS encrypted buffer is too large");
        return 0;
    }
    return 1;
}

static int sa_net_tls_send_token(SaSocket socket_value, SecBuffer* output) {
    if (output->cbBuffer && output->pvBuffer) {
        long long sent = sa_net_send_bytes(socket_value, (const unsigned char*)output->pvBuffer, output->cbBuffer);
        FreeContextBuffer(output->pvBuffer);
        output->pvBuffer = NULL;
        output->cbBuffer = 0;
        return sent >= 0;
    }
    return 1;
}

static SaTlsState* sa_net_tls_handshake(SaSocket socket_value, const char* host) {
    SaTlsState* state = (SaTlsState*)calloc(1, sizeof(SaTlsState));
    if (!state) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    SCHANNEL_CRED credential;
    memset(&credential, 0, sizeof(credential));
    credential.dwVersion = SCHANNEL_CRED_VERSION;
    credential.dwFlags = SCH_CRED_AUTO_CRED_VALIDATION | SCH_CRED_NO_DEFAULT_CREDS | SCH_USE_STRONG_CRYPTO;
    TimeStamp expiry;
    SECURITY_STATUS status = AcquireCredentialsHandleW(NULL, UNISP_NAME_W, SECPKG_CRED_OUTBOUND, NULL, &credential, NULL, NULL, &state->credentials, &expiry);
    if (status != SEC_E_OK) {
        sa_net_set_error_code("Schannel AcquireCredentialsHandle", (unsigned long)status);
        sa_net_tls_free_state(state);
        return NULL;
    }
    state->credentials_valid = 1;
    wchar_t* host_w = sa_win_widen(host);
    if (!host_w) {
        sa_net_set_error("TLS host is not valid UTF-8");
        sa_net_tls_free_state(state);
        return NULL;
    }
    DWORD request_flags = ISC_REQ_SEQUENCE_DETECT | ISC_REQ_REPLAY_DETECT | ISC_REQ_CONFIDENTIALITY | ISC_REQ_ALLOCATE_MEMORY | ISC_REQ_STREAM;
    DWORD attributes = 0;
    SecBuffer output;
    SecBufferDesc output_desc;
    memset(&output, 0, sizeof(output));
    output.BufferType = SECBUFFER_TOKEN;
    output_desc.ulVersion = SECBUFFER_VERSION;
    output_desc.cBuffers = 1;
    output_desc.pBuffers = &output;
    status = InitializeSecurityContextW(&state->credentials, NULL, host_w, request_flags, 0, SECURITY_NATIVE_DREP, NULL, 0, &state->context, &output_desc, &attributes, &expiry);
    if (status != SEC_I_CONTINUE_NEEDED && status != SEC_E_OK) {
        free(host_w);
        sa_net_set_error_code("Schannel InitializeSecurityContext", (unsigned long)status);
        sa_net_tls_free_state(state);
        return NULL;
    }
    state->context_valid = 1;
    if (!sa_net_tls_send_token(socket_value, &output)) {
        free(host_w);
        sa_net_tls_free_state(state);
        return NULL;
    }
    while (status == SEC_I_CONTINUE_NEEDED || status == SEC_E_INCOMPLETE_MESSAGE) {
        if (status == SEC_E_INCOMPLETE_MESSAGE || state->encrypted_len == 0) {
            if (!sa_net_tls_recv_encrypted(socket_value, state)) {
                free(host_w);
                sa_net_tls_free_state(state);
                return NULL;
            }
        }
        SecBuffer input[2];
        SecBufferDesc input_desc;
        memset(input, 0, sizeof(input));
        input[0].BufferType = SECBUFFER_TOKEN;
        input[0].pvBuffer = state->encrypted;
        input[0].cbBuffer = (unsigned long)state->encrypted_len;
        input[1].BufferType = SECBUFFER_EMPTY;
        input_desc.ulVersion = SECBUFFER_VERSION;
        input_desc.cBuffers = 2;
        input_desc.pBuffers = input;
        memset(&output, 0, sizeof(output));
        output.BufferType = SECBUFFER_TOKEN;
        status = InitializeSecurityContextW(&state->credentials, &state->context, host_w, request_flags, 0, SECURITY_NATIVE_DREP, &input_desc, 0, &state->context, &output_desc, &attributes, &expiry);
        if (!sa_net_tls_send_token(socket_value, &output)) {
            free(host_w);
            sa_net_tls_free_state(state);
            return NULL;
        }
        if (status == SEC_E_INCOMPLETE_MESSAGE) continue;
        if (status != SEC_E_OK && status != SEC_I_CONTINUE_NEEDED) {
            free(host_w);
            sa_net_set_error_code("Schannel handshake", (unsigned long)status);
            sa_net_tls_free_state(state);
            return NULL;
        }
        if (input[1].BufferType == SECBUFFER_EXTRA && input[1].cbBuffer) {
            size_t extra = (size_t)input[1].cbBuffer;
            memmove(state->encrypted, state->encrypted + state->encrypted_len - extra, extra);
            state->encrypted_len = extra;
        } else
        {
            state->encrypted_len = 0;
        }
    }
    free(host_w);
    status = QueryContextAttributesW(&state->context, SECPKG_ATTR_STREAM_SIZES, &state->sizes);
    if (status != SEC_E_OK) {
        sa_net_set_error_code("Schannel QueryContextAttributes", (unsigned long)status);
        sa_net_tls_free_state(state);
        return NULL;
    }
    return state;
}

static long long sa_net_tls_send_bytes(SaNetSocketSlot* slot, const unsigned char* data, size_t length) {
    SaTlsState* state = (SaTlsState*)slot->tls_state;
    size_t sent = 0;
    while (sent < length) {
        size_t chunk = length - sent;
        if (chunk > state->sizes.cbMaximumMessage) chunk = state->sizes.cbMaximumMessage;
        size_t record_size = (size_t)state->sizes.cbHeader + chunk + (size_t)state->sizes.cbTrailer;
        unsigned char* record = (unsigned char*)malloc(record_size);
        if (!record) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        memcpy(record + state->sizes.cbHeader, data + sent, chunk);
        SecBuffer buffers[4];
        SecBufferDesc desc;
        memset(buffers, 0, sizeof(buffers));
        buffers[0].BufferType = SECBUFFER_STREAM_HEADER;
        buffers[0].pvBuffer = record;
        buffers[0].cbBuffer = state->sizes.cbHeader;
        buffers[1].BufferType = SECBUFFER_DATA;
        buffers[1].pvBuffer = record + state->sizes.cbHeader;
        buffers[1].cbBuffer = (unsigned long)chunk;
        buffers[2].BufferType = SECBUFFER_STREAM_TRAILER;
        buffers[2].pvBuffer = record + state->sizes.cbHeader + chunk;
        buffers[2].cbBuffer = state->sizes.cbTrailer;
        buffers[3].BufferType = SECBUFFER_EMPTY;
        desc.ulVersion = SECBUFFER_VERSION;
        desc.cBuffers = 4;
        desc.pBuffers = buffers;
        SECURITY_STATUS status = EncryptMessage(&state->context, 0, &desc, 0);
        if (status != SEC_E_OK) {
            free(record);
            sa_net_set_error_code("Schannel EncryptMessage", (unsigned long)status);
            return -1;
        }
        size_t encrypted_size = (size_t)buffers[0].cbBuffer + buffers[1].cbBuffer + buffers[2].cbBuffer;
        if (sa_net_send_bytes(slot->socket, record, encrypted_size) < 0) { free(record); return -1; }
        free(record);
        sent += chunk;
    }
    return (long long)sent;
}

static int sa_net_tls_recv_bytes(SaNetSocketSlot* slot, unsigned char* output, size_t limit) {
    SaTlsState* state = (SaTlsState*)slot->tls_state;
    if (state->pending_plain_len > state->pending_plain_offset) {
        size_t available = state->pending_plain_len - state->pending_plain_offset;
        size_t count = available < limit ? available : limit;
        memcpy(output, state->pending_plain + state->pending_plain_offset, count);
        state->pending_plain_offset += count;
        if (state->pending_plain_offset == state->pending_plain_len) {
            free(state->pending_plain);
            state->pending_plain = NULL;
            state->pending_plain_len = state->pending_plain_offset = 0;
        }
        return (int)count;
    }
    for (;;) {
        if (state->encrypted_len == 0 && !sa_net_tls_recv_encrypted(slot->socket, state)) return -1;
        SecBuffer buffers[4];
        SecBufferDesc desc;
        memset(buffers, 0, sizeof(buffers));
        buffers[0].BufferType = SECBUFFER_DATA;
        buffers[0].pvBuffer = state->encrypted;
        buffers[0].cbBuffer = (unsigned long)state->encrypted_len;
        for (int i = 1; i < 4; i++) buffers[i].BufferType = SECBUFFER_EMPTY;
        desc.ulVersion = SECBUFFER_VERSION;
        desc.cBuffers = 4;
        desc.pBuffers = buffers;
        SECURITY_STATUS status = DecryptMessage(&state->context, &desc, 0, NULL);
        if (status == SEC_E_INCOMPLETE_MESSAGE) {
            if (!sa_net_tls_recv_encrypted(slot->socket, state)) return -1;
            continue;
        }
        if (status == SEC_I_CONTEXT_EXPIRED) { state->encrypted_len = 0; return 0; }
        if (status == SEC_I_RENEGOTIATE) { sa_net_set_error("TLS renegotiation is not supported"); return -1; }
        if (status != SEC_E_OK) { sa_net_set_error_code("Schannel DecryptMessage", (unsigned long)status); return -1; }
        unsigned char* plain = NULL;
        size_t plain_len = 0;
        unsigned char* extra_ptr = NULL;
        size_t extra_len = 0;
        for (int i = 0; i < 4; i++) {
            if (buffers[i].BufferType == SECBUFFER_DATA) { plain = (unsigned char*)buffers[i].pvBuffer; plain_len = buffers[i].cbBuffer; }
            if (buffers[i].BufferType == SECBUFFER_EXTRA) { extra_ptr = (unsigned char*)buffers[i].pvBuffer; extra_len = buffers[i].cbBuffer; }
        }
        size_t count = plain_len < limit ? plain_len : limit;
        if (count) memcpy(output, plain, count);
        if (plain_len > count) {
            state->pending_plain = (unsigned char*)malloc(plain_len - count);
            if (!state->pending_plain) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
            memcpy(state->pending_plain, plain + count, plain_len - count);
            state->pending_plain_len = plain_len - count;
            state->pending_plain_offset = 0;
        }
        if (extra_len) memmove(state->encrypted, extra_ptr, extra_len);
        state->encrypted_len = extra_len;
        if (count) return (int)count;
    }
}
#else
static void sa_net_tls_set_openssl_error(const char* operation) {
    unsigned long code = ERR_get_error();
    sa_net_last_code = (long long)code;
    if (code) {
        char detail[256];
        ERR_error_string_n(code, detail, sizeof(detail));
        snprintf(sa_net_last_error, sizeof(sa_net_last_error), "%s: %s", operation ? operation : "OpenSSL", detail);
    } else
    {
        snprintf(sa_net_last_error, sizeof(sa_net_last_error), "%s failed", operation ? operation : "OpenSSL");
    }
}

static SaTlsState* sa_net_tls_handshake(SaSocket socket_value, const char* host) {
    OPENSSL_init_ssl(0, NULL);
    SaTlsState* state = (SaTlsState*)calloc(1, sizeof(SaTlsState));
    if (!state) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    state->context = SSL_CTX_new(TLS_client_method());
    if (!state->context) { sa_net_tls_set_openssl_error("SSL_CTX_new"); sa_net_tls_free_state(state); return NULL; }
    SSL_CTX_set_verify(state->context, SSL_VERIFY_PEER, NULL);
    if (SSL_CTX_set_default_verify_paths(state->context) != 1) { sa_net_tls_set_openssl_error("SSL_CTX_set_default_verify_paths"); sa_net_tls_free_state(state); return NULL; }
    state->session = SSL_new(state->context);
    if (!state->session) { sa_net_tls_set_openssl_error("SSL_new"); sa_net_tls_free_state(state); return NULL; }
    if (SSL_set_tlsext_host_name(state->session, host) != 1) { sa_net_tls_set_openssl_error("TLS SNI"); sa_net_tls_free_state(state); return NULL; }
    unsigned char ip[16];
    X509_VERIFY_PARAM* verify = SSL_get0_param(state->session);
    if (inet_pton(AF_INET, host, ip) == 1 || inet_pton(AF_INET6, host, ip) == 1) {
        if (X509_VERIFY_PARAM_set1_ip_asc(verify, host) != 1) { sa_net_tls_set_openssl_error("TLS IP verification"); sa_net_tls_free_state(state); return NULL; }
    } else if (SSL_set1_host(state->session, host) != 1) {
        sa_net_tls_set_openssl_error("TLS hostname verification"); sa_net_tls_free_state(state); return NULL;
    }
    if (SSL_set_fd(state->session, socket_value) != 1 || SSL_connect(state->session) != 1) {
        sa_net_tls_set_openssl_error("TLS handshake");
        sa_net_tls_free_state(state);
        return NULL;
    }
    return state;
}

static long long sa_net_tls_send_bytes(SaNetSocketSlot* slot, const unsigned char* data, size_t length) {
    SaTlsState* state = (SaTlsState*)slot->tls_state;
    size_t sent = 0;
    while (sent < length) {
        size_t remaining = length - sent;
        int chunk = remaining > INT_MAX ? INT_MAX : (int)remaining;
        int count = SSL_write(state->session, data + sent, chunk);
        if (count <= 0) { sa_net_tls_set_openssl_error("TLS write"); return -1; }
        sent += (size_t)count;
    }
    return (long long)sent;
}

static int sa_net_tls_recv_bytes(SaNetSocketSlot* slot, unsigned char* output, size_t limit) {
    SaTlsState* state = (SaTlsState*)slot->tls_state;
    int count = SSL_read(state->session, output, limit > INT_MAX ? INT_MAX : (int)limit);
    if (count > 0) return count;
    int error = SSL_get_error(state->session, count);
    if (error == SSL_ERROR_ZERO_RETURN) return 0;
    sa_net_tls_set_openssl_error("TLS read");
    return -1;
}
#endif

static void sa_net_tls_shutdown(SaNetSocketSlot* slot) {
    SaTlsState* state = (SaTlsState*)slot->tls_state;
    if (!state) return;
#ifdef _WIN32
    DWORD shutdown = SCHANNEL_SHUTDOWN;
    SecBuffer input;
    SecBufferDesc input_desc;
    memset(&input, 0, sizeof(input));
    input.BufferType = SECBUFFER_TOKEN;
    input.pvBuffer = &shutdown;
    input.cbBuffer = sizeof(shutdown);
    input_desc.ulVersion = SECBUFFER_VERSION;
    input_desc.cBuffers = 1;
    input_desc.pBuffers = &input;
    if (ApplyControlToken(&state->context, &input_desc) == SEC_E_OK) {
        SecBuffer output;
        SecBufferDesc output_desc;
        DWORD attributes = 0;
        TimeStamp expiry;
        memset(&output, 0, sizeof(output));
        output.BufferType = SECBUFFER_TOKEN;
        output_desc.ulVersion = SECBUFFER_VERSION;
        output_desc.cBuffers = 1;
        output_desc.pBuffers = &output;
        SECURITY_STATUS status = InitializeSecurityContextW(
            &state->credentials, &state->context, NULL,
            ISC_REQ_SEQUENCE_DETECT | ISC_REQ_REPLAY_DETECT | ISC_REQ_CONFIDENTIALITY | ISC_REQ_ALLOCATE_MEMORY | ISC_REQ_STREAM,
            0, SECURITY_NATIVE_DREP, NULL, 0, &state->context, &output_desc, &attributes, &expiry
        );
        if (status == SEC_E_OK || status == SEC_I_CONTEXT_EXPIRED) sa_net_tls_send_token(slot->socket, &output);
        else if (output.pvBuffer) FreeContextBuffer(output.pvBuffer);
    }
#else
    SSL_shutdown(state->session);
#endif
}

static SaHandle sa_net_tls_connect(const char* host, long long port, long long timeout_ms) {
    sa_net_clear_error();
    if (!sa_net_initialize()) return 0;
    SaSocket socket_value = sa_net_connect_socket(host, port, timeout_ms, SOCK_STREAM, IPPROTO_TCP, NULL);
    if (socket_value == SA_NET_INVALID_SOCKET) return 0;
    SaTlsState* state = sa_net_tls_handshake(socket_value, host);
    if (!state) { sa_net_close_socket(socket_value); return 0; }
    SaHandle handle = sa_net_take_stream(socket_value);
    SaNetSocketSlot* slot = sa_net_stream_slot(handle);
    if (!slot) { sa_net_tls_free_state(state); return 0; }
    slot->tls_state = state;
    return handle;
}
#endif

static SaHandle sa_net_tcp_connect(const char* host, long long port, long long timeout_ms) {
    sa_net_clear_error();
    if (!sa_net_initialize()) return 0;
    SaSocket socket_value = sa_net_connect_socket(host, port, timeout_ms, SOCK_STREAM, IPPROTO_TCP, NULL);
    return socket_value == SA_NET_INVALID_SOCKET ? 0 : sa_net_take_stream(socket_value);
}

static SaHandle sa_net_tcp_listen(const char* bind_host, long long port, long long backlog) {
    sa_net_clear_error();
    if (!sa_net_initialize()) return 0;
    char port_text[16];
    if (!sa_net_port_text(port, port_text, sizeof(port_text))) return 0;
    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    hints.ai_flags = AI_PASSIVE;
    struct addrinfo* result = NULL;
    const char* node = bind_host && bind_host[0] ? bind_host : NULL;
    int error = getaddrinfo(node, port_text, &hints, &result);
    if (error != 0) { sa_net_set_gai_error("listen address lookup", error); return 0; }
    SaSocket listener = SA_NET_INVALID_SOCKET;
    for (struct addrinfo* item = result; item; item = item->ai_next) {
        SaSocket socket_value = (SaSocket)socket(item->ai_family, item->ai_socktype, item->ai_protocol);
        if (socket_value == SA_NET_INVALID_SOCKET) continue;
        int reuse = 1;
#ifdef _WIN32
        setsockopt(socket_value, SOL_SOCKET, SO_REUSEADDR, (const char*)&reuse, sizeof(reuse));
#else
        setsockopt(socket_value, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
#endif
        int queue = backlog <= 0 ? 16 : (backlog > INT_MAX ? INT_MAX : (int)backlog);
        if (bind(socket_value, item->ai_addr, (SaSockLen)item->ai_addrlen) == 0 && listen(socket_value, queue) == 0) {
            listener = socket_value;
            break;
        }
        sa_net_close_socket(socket_value);
    }
    freeaddrinfo(result);
    if (listener == SA_NET_INVALID_SOCKET) { sa_net_set_socket_error("listen"); return 0; }
    return sa_net_take_listener(listener);
}

static SaHandle sa_net_tcp_accept(SaHandle listener_handle, long long timeout_ms) {
    sa_net_clear_error();
    SaNetSocketSlot* listener = sa_tcp_listener_slot(listener_handle);
    if (!listener) { sa_net_set_error("invalid or closed TCP_LISTENER handle"); return 0; }
    if (!sa_net_wait_socket(listener->socket, 0, timeout_ms)) return 0;
    struct sockaddr_storage peer;
    SaSockLen peer_len = (SaSockLen)sizeof(peer);
    SaSocket socket_value = accept(listener->socket, (struct sockaddr*)&peer, &peer_len);
    if (socket_value == SA_NET_INVALID_SOCKET) { sa_net_set_socket_error("accept"); return 0; }
    if (!sa_net_set_timeouts(socket_value, timeout_ms)) {
        sa_net_close_socket(socket_value);
        sa_net_set_socket_error("set stream timeout");
        return 0;
    }
    sa_net_store_peer((struct sockaddr*)&peer, peer_len);
    return sa_net_take_stream(socket_value);
}

static int sa_net_tcp_listener_close(SaHandle handle) {
    sa_net_clear_error();
    SaNetSocketSlot* slot = sa_tcp_listener_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed TCP_LISTENER handle"); return 0; }
    sa_net_close_socket(slot->socket);
    slot->active = 0;
    slot->generation++;
    return 1;
}

static long long sa_net_send_bytes(SaSocket socket_value, const unsigned char* data, size_t length) {
    size_t sent = 0;
    while (sent < length) {
        size_t remaining = length - sent;
        int chunk = remaining > INT_MAX ? INT_MAX : (int)remaining;
#ifdef _WIN32
        int count = send(socket_value, (const char*)data + sent, chunk, 0);
#else
        int flags = 0;
#ifdef MSG_NOSIGNAL
        flags = MSG_NOSIGNAL;
#endif
        int count = (int)send(socket_value, data + sent, (size_t)chunk, flags);
#endif
        if (count <= 0) { sa_net_set_socket_error("send"); return -1; }
        sent += (size_t)count;
    }
    return (long long)sent;
}

static long long sa_net_stream_send(SaHandle handle, const char* text) {
    sa_net_clear_error();
    SaNetSocketSlot* slot = sa_net_stream_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed NET_STREAM handle"); return -1; }
    const char* safe = text ? text : "";
#ifdef SA_ENABLE_TLS
    if (slot->tls_state) return sa_net_tls_send_bytes(slot, (const unsigned char*)safe, strlen(safe));
#endif
    return sa_net_send_bytes(slot->socket, (const unsigned char*)safe, strlen(safe));
}

static long long sa_net_stream_send_buffer(SaHandle handle, SaHandle buffer_handle, long long offset, long long count) {
    sa_net_clear_error();
    SaNetSocketSlot* slot = sa_net_stream_slot(handle);
    SaBufferSlot* buffer = sa_binary_slot(buffer_handle);
    size_t start = 0, length = 0;
    if (!slot) { sa_net_set_error("invalid or closed NET_STREAM handle"); return -1; }
    if (!sa_binary_range(buffer, offset, count, &start, &length)) { sa_net_set_error("buffer send range is out of bounds"); return -1; }
#ifdef SA_ENABLE_TLS
    if (slot->tls_state) return sa_net_tls_send_bytes(slot, buffer->data + start, length);
#endif
    return sa_net_send_bytes(slot->socket, buffer->data + start, length);
}

static int sa_net_recv_limit(long long max_bytes, size_t* length) {
    if (max_bytes <= 0 || (unsigned long long)max_bytes > (unsigned long long)INT_MAX) {
        sa_net_set_error("receive size must be between 1 and INT_MAX");
        return 0;
    }
    *length = (size_t)max_bytes;
    return 1;
}

static char* sa_net_stream_recv(SaHandle handle, long long max_bytes) {
    sa_net_clear_error();
    SaNetSocketSlot* slot = sa_net_stream_slot(handle);
    size_t limit = 0;
    if (!slot) { sa_net_set_error("invalid or closed NET_STREAM handle"); return sa_strdup(""); }
    if (!sa_net_recv_limit(max_bytes, &limit)) return sa_strdup("");
    char* output = (char*)malloc(limit + 1);
    if (!output) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    int count;
#ifdef SA_ENABLE_TLS
    if (slot->tls_state) count = sa_net_tls_recv_bytes(slot, (unsigned char*)output, limit);
    else
#endif
    count = recv(slot->socket, output, (int)limit, 0);
    if (count < 0) { free(output); sa_net_set_socket_error("recv"); return sa_strdup(""); }
    output[count] = '\0';
    return output;
}

static SaHandle sa_net_stream_recv_buffer(SaHandle handle, long long max_bytes) {
    sa_net_clear_error();
    SaNetSocketSlot* slot = sa_net_stream_slot(handle);
    size_t limit = 0;
    if (!slot) { sa_net_set_error("invalid or closed NET_STREAM handle"); return 0; }
    if (!sa_net_recv_limit(max_bytes, &limit)) return 0;
    unsigned char* output = (unsigned char*)malloc(limit);
    if (!output) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    int count;
#ifdef SA_ENABLE_TLS
    if (slot->tls_state) count = sa_net_tls_recv_bytes(slot, output, limit);
    else
#endif
    count = recv(slot->socket, (char*)output, (int)limit, 0);
    if (count < 0) { free(output); sa_net_set_socket_error("recv"); return 0; }
    if (count == 0) { free(output); output = (unsigned char*)malloc(1); if (!output) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); } }
    return sa_binary_take(output, (size_t)count);
}

static int sa_net_stream_close(SaHandle handle) {
    sa_net_clear_error();
    SaNetSocketSlot* slot = sa_net_stream_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed NET_STREAM handle"); return 0; }
#ifdef SA_ENABLE_TLS
    if (slot->tls_state) sa_net_tls_shutdown(slot);
#endif
    sa_net_tls_free_state(slot->tls_state);
    slot->tls_state = NULL;
    sa_net_close_socket(slot->socket);
    slot->active = 0;
    slot->generation++;
    return 1;
}

static long long sa_net_socket_local_port(SaSocket socket_value) {
    struct sockaddr_storage address;
    SaSockLen address_len = (SaSockLen)sizeof(address);
    if (getsockname(socket_value, (struct sockaddr*)&address, &address_len) != 0) { sa_net_set_socket_error("getsockname"); return -1; }
    char service[NI_MAXSERV];
    int result = getnameinfo((struct sockaddr*)&address, address_len, NULL, 0, service, sizeof(service), NI_NUMERICSERV);
    if (result != 0) { sa_net_set_gai_error("local port", result); return -1; }
    return atoll(service);
}

static long long sa_net_tcp_listener_local_port(SaHandle handle) {
    sa_net_clear_error();
    SaNetSocketSlot* slot = sa_tcp_listener_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed TCP_LISTENER handle"); return -1; }
    return sa_net_socket_local_port(slot->socket);
}

static SaHandle sa_net_udp_open(void) {
    sa_net_clear_error();
    if (!sa_net_initialize()) return 0;
    for (size_t i = 0; i < SA_NET_SLOT_COUNT; i++) {
        if (!sa_udp_socket_slots[i].active) {
            if (++sa_udp_socket_slots[i].generation == 0) sa_udp_socket_slots[i].generation = 1;
            sa_udp_socket_slots[i].socket = SA_NET_INVALID_SOCKET;
            sa_udp_socket_slots[i].family = AF_UNSPEC;
            sa_udp_socket_slots[i].active = 1;
            return sa_handle_make(SA_HANDLE_UDP_SOCKET, sa_udp_socket_slots[i].generation, i);
        }
    }
    sa_net_set_error("too many open UDP sockets");
    return 0;
}

static int sa_net_udp_prepare(SaUdpSocketSlot* slot, int family) {
    if (slot->socket != SA_NET_INVALID_SOCKET) return slot->family == family;
    SaSocket socket_value = (SaSocket)socket(family, SOCK_DGRAM, IPPROTO_UDP);
    if (socket_value == SA_NET_INVALID_SOCKET) { sa_net_set_socket_error("UDP socket"); return 0; }
    if (!sa_net_set_timeouts(socket_value, 30000)) {
        sa_net_close_socket(socket_value);
        sa_net_set_socket_error("set UDP timeout");
        return 0;
    }
    slot->socket = socket_value;
    slot->family = family;
    return 1;
}

static int sa_net_udp_bind(SaHandle handle, const char* bind_host, long long port) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed UDP_SOCKET handle"); return 0; }
    if (slot->socket != SA_NET_INVALID_SOCKET) { sa_net_set_error("UDP socket is already initialized"); return 0; }
    char port_text[16];
    if (!sa_net_port_text(port, port_text, sizeof(port_text))) return 0;
    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_DGRAM;
    hints.ai_protocol = IPPROTO_UDP;
    hints.ai_flags = AI_PASSIVE;
    struct addrinfo* result = NULL;
    const char* node = bind_host && bind_host[0] ? bind_host : NULL;
    int error = getaddrinfo(node, port_text, &hints, &result);
    if (error != 0) { sa_net_set_gai_error("UDP bind address lookup", error); return 0; }
    int bound = 0;
    for (struct addrinfo* item = result; item; item = item->ai_next) {
        if (!sa_net_udp_prepare(slot, item->ai_family)) continue;
        if (bind(slot->socket, item->ai_addr, (SaSockLen)item->ai_addrlen) == 0) { bound = 1; break; }
        sa_net_close_socket(slot->socket);
        slot->socket = SA_NET_INVALID_SOCKET;
        slot->family = AF_UNSPEC;
    }
    freeaddrinfo(result);
    if (!bound) sa_net_set_socket_error("UDP bind");
    return bound;
}

static int sa_net_udp_connect(SaHandle handle, const char* host, long long port) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed UDP_SOCKET handle"); return 0; }
    if (slot->socket != SA_NET_INVALID_SOCKET) { sa_net_set_error("UDP socket is already initialized"); return 0; }
    int family = AF_UNSPEC;
    SaSocket socket_value = sa_net_connect_socket(host, port, 30000, SOCK_DGRAM, IPPROTO_UDP, &family);
    if (socket_value == SA_NET_INVALID_SOCKET) return 0;
    slot->socket = socket_value;
    slot->family = family;
    return 1;
}

static long long sa_net_udp_send_bytes_to(SaUdpSocketSlot* slot, const char* host, long long port, const unsigned char* data, size_t length) {
    if (length > INT_MAX) { sa_net_set_error("UDP datagram exceeds INT_MAX bytes"); return -1; }
    char port_text[16];
    if (!host || !host[0]) { sa_net_set_error("UDP destination host must not be empty"); return -1; }
    if (!sa_net_port_text(port, port_text, sizeof(port_text))) return -1;
    struct addrinfo hints;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = slot->socket == SA_NET_INVALID_SOCKET ? AF_UNSPEC : slot->family;
    hints.ai_socktype = SOCK_DGRAM;
    hints.ai_protocol = IPPROTO_UDP;
    struct addrinfo* result = NULL;
    int error = getaddrinfo(host, port_text, &hints, &result);
    if (error != 0) { sa_net_set_gai_error("UDP destination lookup", error); return -1; }
    long long sent = -1;
    for (struct addrinfo* item = result; item; item = item->ai_next) {
        if (!sa_net_udp_prepare(slot, item->ai_family)) continue;
#ifdef _WIN32
        int count = sendto(slot->socket, (const char*)data, (int)length, 0, item->ai_addr, (SaSockLen)item->ai_addrlen);
#else
        int count = (int)sendto(slot->socket, data, length, 0, item->ai_addr, (SaSockLen)item->ai_addrlen);
#endif
        if (count >= 0) { sent = count; break; }
    }
    freeaddrinfo(result);
    if (sent < 0) sa_net_set_socket_error("UDP sendto");
    return sent;
}

static long long sa_net_udp_send(SaHandle handle, const char* text) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    if (!slot || slot->socket == SA_NET_INVALID_SOCKET) { sa_net_set_error("UDP socket is not connected"); return -1; }
    const char* safe = text ? text : "";
    size_t length = strlen(safe);
    if (length > INT_MAX) { sa_net_set_error("UDP datagram exceeds INT_MAX bytes"); return -1; }
    int count = send(slot->socket, safe, (int)length, 0);
    if (count < 0) { sa_net_set_socket_error("UDP send"); return -1; }
    return count;
}

static long long sa_net_udp_send_to(SaHandle handle, const char* host, long long port, const char* text) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed UDP_SOCKET handle"); return -1; }
    const char* safe = text ? text : "";
    return sa_net_udp_send_bytes_to(slot, host, port, (const unsigned char*)safe, strlen(safe));
}

static long long sa_net_udp_send_buffer(SaHandle handle, SaHandle buffer_handle, long long offset, long long count) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    SaBufferSlot* buffer = sa_binary_slot(buffer_handle);
    size_t start = 0, length = 0;
    if (!slot || slot->socket == SA_NET_INVALID_SOCKET) { sa_net_set_error("UDP socket is not connected"); return -1; }
    if (!sa_binary_range(buffer, offset, count, &start, &length)) { sa_net_set_error("buffer send range is out of bounds"); return -1; }
    if (length > INT_MAX) { sa_net_set_error("UDP datagram exceeds INT_MAX bytes"); return -1; }
    int sent = send(slot->socket, (const char*)buffer->data + start, (int)length, 0);
    if (sent < 0) { sa_net_set_socket_error("UDP send"); return -1; }
    return sent;
}

static long long sa_net_udp_send_buffer_to(SaHandle handle, const char* host, long long port, SaHandle buffer_handle, long long offset, long long count) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    SaBufferSlot* buffer = sa_binary_slot(buffer_handle);
    size_t start = 0, length = 0;
    if (!slot) { sa_net_set_error("invalid or closed UDP_SOCKET handle"); return -1; }
    if (!sa_binary_range(buffer, offset, count, &start, &length)) { sa_net_set_error("buffer send range is out of bounds"); return -1; }
    return sa_net_udp_send_bytes_to(slot, host, port, buffer->data + start, length);
}

static int sa_net_udp_recv_bytes(SaUdpSocketSlot* slot, unsigned char* output, size_t limit) {
    struct sockaddr_storage peer;
    SaSockLen peer_len = (SaSockLen)sizeof(peer);
    int count = recvfrom(slot->socket, (char*)output, (int)limit, 0, (struct sockaddr*)&peer, &peer_len);
    if (count < 0) { sa_net_set_socket_error("UDP recvfrom"); return -1; }
    sa_net_store_peer((struct sockaddr*)&peer, peer_len);
    return count;
}

static char* sa_net_udp_recv(SaHandle handle, long long max_bytes) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    size_t limit = 0;
    if (!slot || slot->socket == SA_NET_INVALID_SOCKET) { sa_net_set_error("UDP socket is not bound or connected"); return sa_strdup(""); }
    if (!sa_net_recv_limit(max_bytes, &limit)) return sa_strdup("");
    char* output = (char*)malloc(limit + 1);
    if (!output) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    int count = sa_net_udp_recv_bytes(slot, (unsigned char*)output, limit);
    if (count < 0) { free(output); return sa_strdup(""); }
    output[count] = '\0';
    return output;
}

static SaHandle sa_net_udp_recv_buffer(SaHandle handle, long long max_bytes) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    size_t limit = 0;
    if (!slot || slot->socket == SA_NET_INVALID_SOCKET) { sa_net_set_error("UDP socket is not bound or connected"); return 0; }
    if (!sa_net_recv_limit(max_bytes, &limit)) return 0;
    unsigned char* output = (unsigned char*)malloc(limit);
    if (!output) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    int count = sa_net_udp_recv_bytes(slot, output, limit);
    if (count < 0) { free(output); return 0; }
    if (count == 0) { free(output); output = (unsigned char*)malloc(1); if (!output) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); } }
    return sa_binary_take(output, (size_t)count);
}

static int sa_net_udp_close(SaHandle handle) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    if (!slot) { sa_net_set_error("invalid or closed UDP_SOCKET handle"); return 0; }
    if (slot->socket != SA_NET_INVALID_SOCKET) sa_net_close_socket(slot->socket);
    slot->socket = SA_NET_INVALID_SOCKET;
    slot->family = AF_UNSPEC;
    slot->active = 0;
    slot->generation++;
    return 1;
}

static long long sa_net_udp_local_port(SaHandle handle) {
    sa_net_clear_error();
    SaUdpSocketSlot* slot = sa_udp_socket_slot(handle);
    if (!slot || slot->socket == SA_NET_INVALID_SOCKET) { sa_net_set_error("UDP socket is not bound or connected"); return -1; }
    return sa_net_socket_local_port(slot->socket);
}

static int sa_net_parse_http_url(const char* url, int* secure, char* host, size_t host_size, char* port, size_t port_size, char* path, size_t path_size) {
    const char* rest = NULL;
    if (url && strncmp(url, "http://", 7) == 0) {
        *secure = 0;
        rest = url + 7;
    } else if (url && strncmp(url, "https://", 8) == 0) {
        *secure = 1;
        rest = url + 8;
    } else
    {
        return 0;
    }
    const char* suffix = strpbrk(rest, "/?#");
    const char* authority_end = suffix ? suffix : rest + strlen(rest);
    if (authority_end == rest) return 0;
    const char* port_value = NULL;
    if (*rest == '[') {
        const char* closing = strchr(rest, ']');
        if (!closing || closing >= authority_end) return 0;
        size_t address_len = (size_t)(closing - rest - 1);
        if (address_len == 0 || address_len >= host_size) return 0;
        memcpy(host, rest + 1, address_len);
        host[address_len] = '\0';
        if (closing + 1 < authority_end) {
            if (closing[1] != ':') return 0;
            port_value = closing + 2;
        }
    } else
    {
        const char* colon = NULL;
        for (const char* p = rest; p < authority_end; p++) if (*p == ':') colon = p;
        const char* host_end = colon ? colon : authority_end;
        size_t address_len = (size_t)(host_end - rest);
        if (address_len == 0 || address_len >= host_size) return 0;
        memcpy(host, rest, address_len);
        host[address_len] = '\0';
        if (colon) port_value = colon + 1;
    }
    if (port_value) {
        size_t port_len = (size_t)(authority_end - port_value);
        if (port_len == 0 || port_len >= port_size) return 0;
        memcpy(port, port_value, port_len);
        port[port_len] = '\0';
    } else
    {
        const char* default_port = *secure ? "443" : "80";
        if (strlen(default_port) >= port_size) return 0;
        strcpy(port, default_port);
    }
    const char* fragment = suffix ? strchr(suffix, '#') : NULL;
    size_t suffix_len = suffix ? (size_t)((fragment ? fragment : rest + strlen(rest)) - suffix) : 0;
    size_t prefix_len = suffix && *suffix == '?' ? 1 : 0;
    if (prefix_len + suffix_len + 1 >= path_size) return 0;
    if (!suffix || *suffix == '#') {
        strcpy(path, "/");
    } else
    {
        if (prefix_len) path[0] = '/';
        memcpy(path + prefix_len, suffix, suffix_len);
        path[prefix_len + suffix_len] = '\0';
    }
    return host[0] != '\0';
}

static char* sa_net_urlencode(const char* value) {
    const unsigned char* input = (const unsigned char*)(value ? value : "");
    size_t len = strlen((const char*)input);
    char* out = (char*)malloc(len * 3 + 1);
    if (!out) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    static const char hex[] = "0123456789ABCDEF";
    char* p = out;
    for (size_t i = 0; i < len; i++) {
        unsigned char ch = input[i];
        if ((ch >= 'A' && ch <= 'Z') || (ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '-' || ch == '_' || ch == '.' || ch == '~') {
            *p++ = (char)ch;
        } else
        {
            *p++ = '%';
            *p++ = hex[ch >> 4];
            *p++ = hex[ch & 15];
        }
    }
    *p = '\0';
    return out;
}

static int sa_net_append(char** data, size_t* len, size_t* cap, const char* chunk, size_t chunk_len) {
    if (chunk_len > SIZE_MAX - *len - 1) return 0;
    if (*len + chunk_len + 1 > *cap) {
        size_t next = *cap ? *cap : 8192;
        while (*len + chunk_len + 1 > next) {
            if (next > SIZE_MAX / 2) return 0;
            next *= 2;
        }
        char* grown = (char*)realloc(*data, next);
        if (!grown) {
            free(*data);
            *data = NULL;
            *len = *cap = 0;
            return 0;
        }
        *data = grown;
        *cap = next;
    }
    memcpy(*data + *len, chunk, chunk_len);
    *len += chunk_len;
    (*data)[*len] = '\0';
    return 1;
}

static int sa_net_headers_have_content_type(const char* headers) {
    if (!headers) return 0;
    const char* p = headers;
    const char needle[] = "content-type:";
    while (*p) {
        size_t i = 0;
        while (needle[i] && p[i]) {
            char a = p[i];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (a != needle[i]) break;
            i++;
        }
        if (!needle[i]) return 1;
        p++;
    }
    return 0;
}

#ifdef _WIN32
static char* sa_net_http_fetch(const char* method, const char* url, const char* body, const char* headers, long long* status_out, long long timeout_ms) {
    sa_net_clear_state();
    if (status_out) *status_out = 0;
    if (!sa_net_initialize()) return sa_strdup("");
    wchar_t* url_w = sa_win_widen(url);
    wchar_t* method_w = sa_win_widen(method && method[0] ? method : "GET");
    wchar_t* headers_w = sa_win_widen(headers ? headers : "");
    if (!url_w || !method_w || !headers_w) {
        free(url_w); free(method_w); free(headers_w);
        sa_net_set_error("invalid UTF-8 in request");
        return sa_strdup("");
    }

    URL_COMPONENTS parts;
    memset(&parts, 0, sizeof(parts));
    parts.dwStructSize = sizeof(parts);
    parts.dwSchemeLength = (DWORD)-1;
    parts.dwHostNameLength = (DWORD)-1;
    parts.dwUrlPathLength = (DWORD)-1;
    parts.dwExtraInfoLength = (DWORD)-1;
    if (!WinHttpCrackUrl(url_w, 0, 0, &parts) || (parts.nScheme != INTERNET_SCHEME_HTTP && parts.nScheme != INTERNET_SCHEME_HTTPS)) {
        free(url_w); free(method_w); free(headers_w);
        sa_net_set_error_code("WinHttpCrackUrl", GetLastError());
        return sa_strdup("");
    }
    wchar_t* host = (wchar_t*)malloc((parts.dwHostNameLength + 1) * sizeof(wchar_t));
    wchar_t* path = (wchar_t*)malloc((parts.dwUrlPathLength + parts.dwExtraInfoLength + 1) * sizeof(wchar_t));
    if (!host || !path) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    memcpy(host, parts.lpszHostName, parts.dwHostNameLength * sizeof(wchar_t));
    host[parts.dwHostNameLength] = L'\0';
    memcpy(path, parts.lpszUrlPath, parts.dwUrlPathLength * sizeof(wchar_t));
    if (parts.dwExtraInfoLength) memcpy(path + parts.dwUrlPathLength, parts.lpszExtraInfo, parts.dwExtraInfoLength * sizeof(wchar_t));
    path[parts.dwUrlPathLength + parts.dwExtraInfoLength] = L'\0';

    HINTERNET session = WinHttpOpen(L"SonAlgebraic/0.1", WINHTTP_ACCESS_TYPE_DEFAULT_PROXY, WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (session) {
        int timeout = timeout_ms <= 0 ? 30000 : (timeout_ms > 2147483647LL ? 2147483647 : (int)timeout_ms);
        WinHttpSetTimeouts(session, timeout, timeout, timeout, timeout);
    }
    HINTERNET connect = session ? WinHttpConnect(session, host, parts.nPort, 0) : NULL;
    DWORD flags = parts.nScheme == INTERNET_SCHEME_HTTPS ? WINHTTP_FLAG_SECURE : 0;
    HINTERNET request = connect ? WinHttpOpenRequest(connect, method_w, path[0] ? path : L"/", NULL, WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES, flags) : NULL;
    free(url_w); free(method_w); free(host); free(path);
    if (!request) {
        sa_net_set_error_code("WinHTTP request setup", GetLastError());
        if (connect) WinHttpCloseHandle(connect);
        if (session) WinHttpCloseHandle(session);
        free(headers_w);
        return sa_strdup("");
    }

    if (headers_w[0]) {
        WinHttpAddRequestHeaders(request, headers_w, (DWORD)-1, WINHTTP_ADDREQ_FLAG_ADD);
    }
    free(headers_w);
    const char* body_safe = body ? body : "";
    size_t body_size = strlen(body_safe);
    if (body_size > 0xffffffffu) {
        WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
        sa_net_set_error("request body is too large for WinHTTP");
        return sa_strdup("");
    }
    DWORD body_len = (DWORD)body_size;
    BOOL ok = WinHttpSendRequest(request, WINHTTP_NO_ADDITIONAL_HEADERS, 0, body_len ? (LPVOID)body_safe : WINHTTP_NO_REQUEST_DATA, body_len, body_len, 0)
        && WinHttpReceiveResponse(request, NULL);
    if (!ok) {
        sa_net_set_error_code("WinHTTP request", GetLastError());
        WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
        return sa_strdup("");
    }

    DWORD status = 0;
    DWORD status_size = sizeof(status);
    WinHttpQueryHeaders(request, WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER, WINHTTP_HEADER_NAME_BY_INDEX, &status, &status_size, WINHTTP_NO_HEADER_INDEX);
    if (status_out) *status_out = (long long)status;

    DWORD header_bytes = 0;
    WinHttpQueryHeaders(request, WINHTTP_QUERY_RAW_HEADERS_CRLF, WINHTTP_HEADER_NAME_BY_INDEX, NULL, &header_bytes, WINHTTP_NO_HEADER_INDEX);
    if (GetLastError() == ERROR_INSUFFICIENT_BUFFER && header_bytes > sizeof(wchar_t)) {
        wchar_t* raw_headers = (wchar_t*)malloc((size_t)header_bytes);
        if (!raw_headers) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        if (WinHttpQueryHeaders(request, WINHTTP_QUERY_RAW_HEADERS_CRLF, WINHTTP_HEADER_NAME_BY_INDEX, raw_headers, &header_bytes, WINHTTP_NO_HEADER_INDEX)) {
            sa_net_last_headers = sa_win_narrow(raw_headers);
        }
        free(raw_headers);
    }

    char* response = NULL;
    size_t response_len = 0;
    size_t response_cap = 0;
    for (;;) {
        DWORD available = 0;
        if (!WinHttpQueryDataAvailable(request, &available)) {
            free(response);
            sa_net_set_error_code("WinHttpQueryDataAvailable", GetLastError());
            WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
            return sa_strdup("");
        }
        if (available == 0) break;
        char* buffer = (char*)malloc((size_t)available);
        if (!buffer) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        DWORD read = 0;
        if (!WinHttpReadData(request, buffer, available, &read)) {
            free(buffer);
            free(response);
            WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
            sa_net_set_error_code("WinHttpReadData", GetLastError());
            return sa_strdup("");
        }
        if (read && !sa_net_append(&response, &response_len, &response_cap, buffer, (size_t)read)) {
            free(buffer);
            WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
            return sa_strdup("");
        }
        free(buffer);
    }
    WinHttpCloseHandle(request); WinHttpCloseHandle(connect); WinHttpCloseHandle(session);
    if (!response) {
        return sa_strdup("");
    }
    return response;
}
#else
static char* sa_net_http_fetch(const char* method, const char* url, const char* body, const char* headers, long long* status_out, long long timeout_ms) {
    sa_net_clear_state();
    if (status_out) *status_out = 0;
    if (!sa_net_initialize()) return sa_strdup("");
    int secure = 0;
    char host[256];
    char port[16];
    char path[1024];
    if (!sa_net_parse_http_url(url, &secure, host, sizeof(host), port, sizeof(port), path, sizeof(path))) {
        sa_net_set_error("invalid HTTP URL");
        return sa_strdup("");
    }
#ifndef SA_ENABLE_TLS
    if (secure) {
        sa_net_set_error("HTTPS requires the OpenSSL TLS backend");
        return sa_strdup("");
    }
#endif

    long long parsed_port = atoll(port);
    SaSocket sock = sa_net_connect_socket(host, parsed_port, timeout_ms, SOCK_STREAM, IPPROTO_TCP, NULL);
    if (sock == SA_NET_INVALID_SOCKET) return sa_strdup("");

#ifdef SA_ENABLE_TLS
    SaTlsState* tls_state = secure ? sa_net_tls_handshake(sock, host) : NULL;
    if (secure && !tls_state) {
        sa_net_close_socket(sock);
        return sa_strdup("");
    }
    SaNetSocketSlot transport;
    memset(&transport, 0, sizeof(transport));
    transport.socket = sock;
    transport.tls_state = tls_state;
#endif

    const char* method_safe = method && method[0] ? method : "GET";
    const char* body_safe = body ? body : "";
    const char* headers_safe = headers ? headers : "";
    size_t body_len = strlen(body_safe);
    size_t headers_len = strlen(headers_safe);
    const char* header_end = "";
    if (headers_len && !(headers_len >= 2 && headers_safe[headers_len - 2] == '\r' && headers_safe[headers_len - 1] == '\n')) {
        header_end = "\r\n";
    }
    char host_header[320];
    int default_port = (secure && strcmp(port, "443") == 0) || (!secure && strcmp(port, "80") == 0);
    if (strchr(host, ':')) snprintf(host_header, sizeof(host_header), default_port ? "[%s]" : "[%s]:%s", host, port);
    else snprintf(host_header, sizeof(host_header), default_port ? "%s" : "%s:%s", host, port);
    size_t request_len = strlen(method_safe) + strlen(path) + strlen(host_header) + strlen(headers_safe) + body_len + 256;
    char* request = (char*)malloc(request_len);
    if (!request) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    snprintf(request, request_len,
        "%s %s HTTP/1.0\r\nHost: %s\r\nUser-Agent: SonAlgebraic/0.1\r\nConnection: close\r\n%s%s%sContent-Length: %zu\r\n\r\n%s",
        method_safe, path, host_header, headers_safe,
        header_end,
        (body_len && !sa_net_headers_have_content_type(headers_safe)) ? "Content-Type: text/plain\r\n" : "",
        body_len, body_safe);
    size_t total = strlen(request);
#ifdef SA_ENABLE_TLS
    long long sent = secure
        ? sa_net_tls_send_bytes(&transport, (const unsigned char*)request, total)
        : sa_net_send_bytes(sock, (const unsigned char*)request, total);
#else
    long long sent = sa_net_send_bytes(sock, (const unsigned char*)request, total);
#endif
    free(request);
    if (sent < 0) {
#ifdef SA_ENABLE_TLS
        sa_net_tls_free_state(tls_state);
#endif
        sa_net_close_socket(sock);
        return sa_strdup("");
    }

    char* response = NULL;
    size_t response_len = 0;
    size_t response_cap = 0;
    char buffer[4096];
    for (;;) {
        int got;
#ifdef SA_ENABLE_TLS
        if (secure) got = sa_net_tls_recv_bytes(&transport, (unsigned char*)buffer, sizeof(buffer));
        else
#endif
        got = recv(sock, buffer, (int)sizeof(buffer), 0);
        if (got == 0) break;
        if (got < 0) {
            free(response);
#ifdef SA_ENABLE_TLS
            sa_net_tls_free_state(tls_state);
#endif
            sa_net_close_socket(sock);
            if (!sa_net_last_error[0]) sa_net_set_socket_error("HTTP receive");
            return sa_strdup("");
        }
        if (!sa_net_append(&response, &response_len, &response_cap, buffer, (size_t)got)) {
            free(response);
#ifdef SA_ENABLE_TLS
            sa_net_tls_free_state(tls_state);
#endif
            sa_net_close_socket(sock);
            sa_net_set_error("HTTP response is too large");
            return sa_strdup("");
        }
    }
#ifdef SA_ENABLE_TLS
    sa_net_tls_free_state(tls_state);
#endif
    sa_net_close_socket(sock);
    if (!response) return sa_strdup("");

    long long status = 0;
    if (strncmp(response, "HTTP/", 5) == 0) {
        char* first_space = strchr(response, ' ');
        if (first_space) status = atoll(first_space + 1);
    }
    if (status_out) *status_out = status;

    char* body_start = strstr(response, "\r\n\r\n");
    size_t header_len = 0;
    if (body_start) {
        header_len = (size_t)(body_start - response) + 4;
    } else
    {
        body_start = strstr(response, "\n\n");
        header_len = body_start ? (size_t)(body_start - response) + 2 : response_len;
    }
    sa_net_last_headers = (char*)malloc(header_len + 1);
    if (!sa_net_last_headers) { free(response); fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    memcpy(sa_net_last_headers, response, header_len);
    sa_net_last_headers[header_len] = '\0';
    size_t response_body_len = response_len >= header_len ? response_len - header_len : 0;
    char* response_body = (char*)malloc(response_body_len + 1);
    if (!response_body) {
        free(response);
        fputs("SonAlgebraic runtime: out of memory\n", stderr);
        exit(1);
    }
    memcpy(response_body, response + header_len, response_body_len);
    response_body[response_body_len] = '\0';
    free(response);
    return response_body;
}
#endif

static char* sa_net_http_get(const char* url) {
    return sa_net_http_fetch("GET", url, "", "", NULL, 30000);
}

static long long sa_net_http_status(const char* url) {
    long long status = 0;
    char* body = sa_net_http_fetch("GET", url, "", "", &status, 30000);
    free(body);
    return status;
}

static char* sa_net_http_post(const char* url, const char* body, const char* content_type) {
    size_t headers_len = strlen(content_type ? content_type : "") + 32;
    char* headers = (char*)malloc(headers_len);
    if (!headers) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    snprintf(headers, headers_len, "Content-Type: %s\r\n", content_type && content_type[0] ? content_type : "text/plain");
    char* result = sa_net_http_fetch("POST", url, body ? body : "", headers, NULL, 30000);
    free(headers);
    return result;
}

static char* sa_net_http_request(const char* method, const char* url, const char* body, const char* headers) {
    return sa_net_http_fetch(method, url, body, headers, NULL, 30000);
}

static long long sa_net_http_request_status(const char* method, const char* url, const char* body, const char* headers) {
    long long status = 0;
    char* result = sa_net_http_fetch(method, url, body, headers, &status, 30000);
    free(result);
    return status;
}

static char* sa_net_http_request_timeout(const char* method, const char* url, const char* body, const char* headers, long long timeout_ms) {
    return sa_net_http_fetch(method, url, body, headers, NULL, timeout_ms);
}

static long long sa_net_http_request_status_timeout(const char* method, const char* url, const char* body, const char* headers, long long timeout_ms) {
    long long status = 0;
    char* result = sa_net_http_fetch(method, url, body, headers, &status, timeout_ms);
    free(result);
    return status;
}
#endif

#ifdef SA_ENABLE_FILE
#define SA_FILE_SLOT_COUNT 64
typedef struct {
    FILE* stream;
    uint32_t generation;
} SaFileSlot;

static SaFileSlot sa_file_slots[SA_FILE_SLOT_COUNT];
static char sa_file_last_error[512] = "";
static int sa_file_cleanup_registered = 0;

static void sa_file_set_error(const char* message) {
    snprintf(sa_file_last_error, sizeof(sa_file_last_error), "%s", message ? message : "file error");
}

static void sa_file_set_errno(const char* operation) {
    snprintf(sa_file_last_error, sizeof(sa_file_last_error), "%s: %s", operation ? operation : "file", strerror(errno));
}

static void sa_file_clear_error(void) {
    sa_file_last_error[0] = '\0';
}

static char* sa_file_last_error_copy(void) {
    return sa_strdup(sa_file_last_error);
}

static FILE* sa_file_fopen(const char* path, const char* mode) {
#ifdef _WIN32
    wchar_t* path_w = sa_win_widen(path);
    wchar_t* mode_w = sa_win_widen(mode);
    if (!path_w || !mode_w) {
        free(path_w); free(mode_w);
        sa_file_set_error("invalid UTF-8 path or mode");
        return NULL;
    }
    FILE* stream = NULL;
    errno_t result = _wfopen_s(&stream, path_w, mode_w);
    free(path_w); free(mode_w);
    if (result != 0) errno = (int)result;
    return stream;
#else
    return fopen(path ? path : "", mode);
#endif
}

static const char* sa_file_mode(const char* mode) {
    const char* value = mode ? mode : "";
    if (_stricmp(value, "READ") == 0) return "rb";
    if (_stricmp(value, "WRITE") == 0) return "wb";
    if (_stricmp(value, "APPEND") == 0) return "ab";
    if (_stricmp(value, "UPDATE") == 0) return "r+b";
    if (_stricmp(value, "CREATE") == 0) return "w+b";
    return NULL;
}

#ifndef _WIN32
static int sa_stricmp_ascii(const char* left, const char* right) {
    while (*left && *right) {
        char a = *left++;
        char b = *right++;
        if (a >= 'a' && a <= 'z') a = (char)(a - 'a' + 'A');
        if (b >= 'a' && b <= 'z') b = (char)(b - 'a' + 'A');
        if (a != b) return (unsigned char)a - (unsigned char)b;
    }
    return (unsigned char)*left - (unsigned char)*right;
}
#define _stricmp sa_stricmp_ascii
#endif

static SaFileSlot* sa_file_slot(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_FILE, SA_FILE_SLOT_COUNT, &index, &generation)) return NULL;
    SaFileSlot* slot = &sa_file_slots[index];
    return slot->stream && slot->generation == generation ? slot : NULL;
}

static void sa_file_close_all(void) {
    for (size_t i = 0; i < SA_FILE_SLOT_COUNT; i++) {
        if (sa_file_slots[i].stream) {
            fclose(sa_file_slots[i].stream);
            sa_file_slots[i].stream = NULL;
            sa_file_slots[i].generation++;
        }
    }
}

static SaHandle sa_file_open(const char* path, const char* mode) {
    sa_file_clear_error();
    const char* c_mode = sa_file_mode(mode);
    if (!c_mode) { sa_file_set_error("mode must be READ, WRITE, APPEND, UPDATE, or CREATE"); return 0; }
    FILE* stream = sa_file_fopen(path, c_mode);
    if (!stream) { sa_file_set_errno("open"); return 0; }
    for (size_t i = 0; i < SA_FILE_SLOT_COUNT; i++) {
        if (!sa_file_slots[i].stream) {
            if (++sa_file_slots[i].generation == 0) sa_file_slots[i].generation = 1;
            sa_file_slots[i].stream = stream;
            if (!sa_file_cleanup_registered) {
                atexit(sa_file_close_all);
                sa_file_cleanup_registered = 1;
            }
            return sa_handle_make(SA_HANDLE_FILE, sa_file_slots[i].generation, i);
        }
    }
    fclose(stream);
    sa_file_set_error("too many open files in SonAlgebraic runtime");
    return 0;
}

static char* sa_file_read(SaHandle handle, long long count) {
    sa_file_clear_error();
    SaFileSlot* slot = sa_file_slot(handle);
    if (!slot) { sa_file_set_error("invalid or closed FILE handle"); return sa_strdup(""); }
    if (count < 0 || (unsigned long long)count > (unsigned long long)(SIZE_MAX - 1)) {
        sa_file_set_error("invalid read size");
        return sa_strdup("");
    }
    char* data = (char*)malloc((size_t)count + 1);
    if (!data) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    size_t got = fread(data, 1, (size_t)count, slot->stream);
    if (ferror(slot->stream)) {
        free(data);
        clearerr(slot->stream);
        sa_file_set_errno("read");
        return sa_strdup("");
    }
    if (memchr(data, '\0', got)) {
        free(data);
        sa_file_set_error("binary NUL data cannot be represented as STRING");
        return sa_strdup("");
    }
    data[got] = '\0';
    return data;
}

static long long sa_file_write(SaHandle handle, const char* text) {
    sa_file_clear_error();
    SaFileSlot* slot = sa_file_slot(handle);
    if (!slot) { sa_file_set_error("invalid or closed FILE handle"); return -1; }
    const char* safe = text ? text : "";
    size_t len = strlen(safe);
    size_t written = fwrite(safe, 1, len, slot->stream);
    if (written != len || fflush(slot->stream) != 0) { sa_file_set_errno("write"); return -1; }
    return (long long)written;
}

static int sa_file_seek(SaHandle handle, long long offset, const char* origin) {
    sa_file_clear_error();
    SaFileSlot* slot = sa_file_slot(handle);
    if (!slot) { sa_file_set_error("invalid or closed FILE handle"); return 0; }
    int whence;
    if (_stricmp(origin ? origin : "", "START") == 0) whence = SEEK_SET;
    else if (_stricmp(origin ? origin : "", "CURRENT") == 0) whence = SEEK_CUR;
    else if (_stricmp(origin ? origin : "", "END") == 0) whence = SEEK_END;
    else
    { sa_file_set_error("origin must be START, CURRENT, or END"); return 0; }
#ifdef _WIN32
    if (_fseeki64(slot->stream, offset, whence) != 0) { sa_file_set_errno("seek"); return 0; }
#else
    if (fseek(slot->stream, (long)offset, whence) != 0) { sa_file_set_errno("seek"); return 0; }
#endif
    return 1;
}

static long long sa_file_tell(SaHandle handle) {
    sa_file_clear_error();
    SaFileSlot* slot = sa_file_slot(handle);
    if (!slot) { sa_file_set_error("invalid or closed FILE handle"); return -1; }
#ifdef _WIN32
    __int64 position = _ftelli64(slot->stream);
#else
    long position = ftell(slot->stream);
#endif
    if (position < 0) { sa_file_set_errno("tell"); return -1; }
    return (long long)position;
}

static long long sa_file_size(SaHandle handle) {
    long long current = sa_file_tell(handle);
    if (current < 0) return -1;
    if (!sa_file_seek(handle, 0, "END")) return -1;
    long long size = sa_file_tell(handle);
    if (!sa_file_seek(handle, current, "START")) return -1;
    return size;
}

static int sa_file_close(SaHandle handle) {
    sa_file_clear_error();
    SaFileSlot* slot = sa_file_slot(handle);
    if (!slot) { sa_file_set_error("invalid or already closed FILE handle"); return 0; }
    int result = fclose(slot->stream);
    slot->stream = NULL;
    slot->generation++;
    if (result != 0) { sa_file_set_errno("close"); return 0; }
    return 1;
}

static char* sa_file_read_text(const char* path) {
    SaHandle handle = sa_file_open(path, "READ");
    if (!handle) return sa_strdup("");
    long long size = sa_file_size(handle);
    if (size < 0) {
        char saved_error[sizeof(sa_file_last_error)];
        snprintf(saved_error, sizeof(saved_error), "%s", sa_file_last_error);
        sa_file_close(handle);
        snprintf(sa_file_last_error, sizeof(sa_file_last_error), "%s", saved_error);
        return sa_strdup("");
    }
    char* result = sa_file_read(handle, size);
    char saved_error[sizeof(sa_file_last_error)];
    snprintf(saved_error, sizeof(saved_error), "%s", sa_file_last_error);
    sa_file_close(handle);
    snprintf(sa_file_last_error, sizeof(sa_file_last_error), "%s", saved_error);
    return result;
}

static int sa_file_write_text_mode(const char* path, const char* text, const char* mode) {
    SaHandle handle = sa_file_open(path, mode);
    if (!handle) return 0;
    long long written = sa_file_write(handle, text);
    char saved_error[sizeof(sa_file_last_error)];
    snprintf(saved_error, sizeof(saved_error), "%s", sa_file_last_error);
    int closed = sa_file_close(handle);
    if (written < 0) snprintf(sa_file_last_error, sizeof(sa_file_last_error), "%s", saved_error);
    return written >= 0 && closed;
}

static int sa_file_write_text(const char* path, const char* text) { return sa_file_write_text_mode(path, text, "WRITE"); }
static int sa_file_append_text(const char* path, const char* text) { return sa_file_write_text_mode(path, text, "APPEND"); }

static int sa_file_stat(const char* path, int expected) {
#ifdef _WIN32
    wchar_t* path_w = sa_win_widen(path);
    if (!path_w) { sa_file_set_error("invalid UTF-8 path"); return 0; }
    struct _stat64 info;
    int result = _wstat64(path_w, &info);
    free(path_w);
    if (result != 0) return 0;
    int is_dir = (info.st_mode & _S_IFDIR) != 0;
#else
    struct stat info;
    if (stat(path ? path : "", &info) != 0) return 0;
    int is_dir = S_ISDIR(info.st_mode);
#endif
    if (expected == 0) return 1;
    return expected == 1 ? !is_dir : is_dir;
}

static int sa_file_exists(const char* path) { sa_file_clear_error(); return sa_file_stat(path, 0); }
static int sa_file_is_file(const char* path) { sa_file_clear_error(); return sa_file_stat(path, 1); }
static int sa_file_is_dir(const char* path) { sa_file_clear_error(); return sa_file_stat(path, 2); }

static int sa_file_delete(const char* path) {
    sa_file_clear_error();
#ifdef _WIN32
    wchar_t* path_w = sa_win_widen(path);
    if (!path_w) { sa_file_set_error("invalid UTF-8 path"); return 0; }
    DWORD attributes = GetFileAttributesW(path_w);
    BOOL ok = attributes != INVALID_FILE_ATTRIBUTES && ((attributes & FILE_ATTRIBUTE_DIRECTORY) ? RemoveDirectoryW(path_w) : DeleteFileW(path_w));
    free(path_w);
    if (!ok) { sa_file_set_error("delete failed"); return 0; }
#else
    if (remove(path ? path : "") != 0) { sa_file_set_errno("delete"); return 0; }
#endif
    return 1;
}

static int sa_file_mkdir(const char* path) {
    sa_file_clear_error();
#ifdef _WIN32
    wchar_t* path_w = sa_win_widen(path);
    if (!path_w) { sa_file_set_error("invalid UTF-8 path"); return 0; }
    BOOL ok = CreateDirectoryW(path_w, NULL);
    DWORD error = ok ? ERROR_SUCCESS : GetLastError();
    free(path_w);
    if (!ok && error != ERROR_ALREADY_EXISTS) { sa_file_set_error("mkdir failed"); return 0; }
#else
    if (mkdir(path ? path : "", 0777) != 0 && errno != EEXIST) { sa_file_set_errno("mkdir"); return 0; }
#endif
    if (!sa_file_is_dir(path)) {
        sa_file_set_error("path exists but is not a directory");
        return 0;
    }
    return 1;
}

static char* sa_file_cwd(void) {
    sa_file_clear_error();
#ifdef _WIN32
    DWORD count = GetCurrentDirectoryW(0, NULL);
    if (!count) { sa_file_set_error("GetCurrentDirectoryW failed"); return sa_strdup(""); }
    wchar_t* buffer = (wchar_t*)malloc((size_t)count * sizeof(wchar_t));
    if (!buffer) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    if (!GetCurrentDirectoryW(count, buffer)) { free(buffer); sa_file_set_error("GetCurrentDirectoryW failed"); return sa_strdup(""); }
    char* result = sa_win_narrow(buffer);
    free(buffer);
    if (!result) { sa_file_set_error("current directory is not valid UTF-8"); return sa_strdup(""); }
    return result;
#else
    size_t size = 256;
    for (;;) {
        char* buffer = (char*)malloc(size);
        if (!buffer) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
        if (getcwd(buffer, size)) return buffer;
        free(buffer);
        if (errno != ERANGE || size > SIZE_MAX / 2) { sa_file_set_errno("getcwd"); return sa_strdup(""); }
        size *= 2;
    }
#endif
}

static char* sa_file_absolute(const char* path) {
    sa_file_clear_error();
#ifdef _WIN32
    wchar_t* path_w = sa_win_widen(path);
    if (!path_w) { sa_file_set_error("invalid UTF-8 path"); return sa_strdup(""); }
    DWORD count = GetFullPathNameW(path_w, 0, NULL, NULL);
    if (!count) { free(path_w); sa_file_set_error("GetFullPathNameW failed"); return sa_strdup(""); }
    wchar_t* buffer = (wchar_t*)malloc((size_t)count * sizeof(wchar_t));
    if (!buffer) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    if (!GetFullPathNameW(path_w, count, buffer, NULL)) { free(path_w); free(buffer); sa_file_set_error("GetFullPathNameW failed"); return sa_strdup(""); }
    free(path_w);
    char* result = sa_win_narrow(buffer);
    free(buffer);
    if (!result) { sa_file_set_error("absolute path conversion failed"); return sa_strdup(""); }
    return result;
#else
    if (path && path[0] == '/') return sa_strdup(path);
    char* cwd = sa_file_cwd();
    if (!cwd[0]) return cwd;
    const char* relative = path ? path : "";
    size_t len = strlen(cwd) + strlen(relative) + 2;
    char* result = (char*)malloc(len);
    if (!result) { free(cwd); fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    snprintf(result, len, "%s/%s", cwd, relative);
    free(cwd);
    return result;
#endif
}
#endif

#ifdef SA_ENABLE_DESKTOP
static char sa_desktop_last_error[512] = "";
static void sa_desktop_set_error(const char* message) { snprintf(sa_desktop_last_error, sizeof(sa_desktop_last_error), "%s", message ? message : "desktop error"); }
static void sa_desktop_clear_error(void) { sa_desktop_last_error[0] = '\0'; }
static char* sa_desktop_last_error_copy(void) { return sa_strdup(sa_desktop_last_error); }

static int sa_desktop_message(const char* title, const char* text) {
    sa_desktop_clear_error();
#ifdef _WIN32
    wchar_t* title_w = sa_win_widen(title);
    wchar_t* text_w = sa_win_widen(text);
    if (!title_w || !text_w) { free(title_w); free(text_w); sa_desktop_set_error("invalid UTF-8 text"); return 0; }
    int result = MessageBoxW(NULL, text_w, title_w, MB_OK | MB_ICONINFORMATION | MB_SETFOREGROUND);
    free(title_w); free(text_w);
    if (!result) { sa_desktop_set_error("MessageBoxW failed"); return 0; }
    return 1;
#else
    fprintf(stderr, "%s: %s\n", title ? title : "SonAlgebraic", text ? text : "");
    sa_desktop_set_error("native message boxes are not available on this platform");
    return 0;
#endif
}

static int sa_desktop_open(const char* target) {
    sa_desktop_clear_error();
#ifdef _WIN32
    wchar_t* target_w = sa_win_widen(target);
    if (!target_w) { sa_desktop_set_error("invalid UTF-8 target"); return 0; }
    HINSTANCE result = ShellExecuteW(NULL, L"open", target_w, NULL, NULL, SW_SHOWNORMAL);
    free(target_w);
    if ((INT_PTR)result <= 32) { sa_desktop_set_error("ShellExecuteW rejected the target"); return 0; }
    return 1;
#else
    (void)target;
    sa_desktop_set_error("desktop OPEN is not available on this platform");
    return 0;
#endif
}

static int sa_desktop_clipboard_set(const char* text) {
    sa_desktop_clear_error();
#ifdef _WIN32
    wchar_t* text_w = sa_win_widen(text);
    if (!text_w) { sa_desktop_set_error("invalid UTF-8 clipboard text"); return 0; }
    if (!OpenClipboard(NULL)) { free(text_w); sa_desktop_set_error("OpenClipboard failed"); return 0; }
    size_t bytes = (wcslen(text_w) + 1) * sizeof(wchar_t);
    HGLOBAL memory = GlobalAlloc(GMEM_MOVEABLE, bytes);
    if (!memory) { CloseClipboard(); free(text_w); sa_desktop_set_error("GlobalAlloc failed"); return 0; }
    void* target = GlobalLock(memory);
    if (!target) { GlobalFree(memory); CloseClipboard(); free(text_w); sa_desktop_set_error("GlobalLock failed"); return 0; }
    memcpy(target, text_w, bytes);
    GlobalUnlock(memory);
    free(text_w);
    EmptyClipboard();
    if (!SetClipboardData(CF_UNICODETEXT, memory)) { GlobalFree(memory); CloseClipboard(); sa_desktop_set_error("SetClipboardData failed"); return 0; }
    CloseClipboard();
    return 1;
#else
    (void)text;
    sa_desktop_set_error("clipboard is not available on this platform");
    return 0;
#endif
}

static char* sa_desktop_clipboard_get(void) {
    sa_desktop_clear_error();
#ifdef _WIN32
    if (!OpenClipboard(NULL)) { sa_desktop_set_error("OpenClipboard failed"); return sa_strdup(""); }
    HANDLE memory = GetClipboardData(CF_UNICODETEXT);
    if (!memory) { CloseClipboard(); sa_desktop_set_error("clipboard does not contain Unicode text"); return sa_strdup(""); }
    const wchar_t* text_w = (const wchar_t*)GlobalLock(memory);
    if (!text_w) { CloseClipboard(); sa_desktop_set_error("GlobalLock failed"); return sa_strdup(""); }
    char* result = sa_win_narrow(text_w);
    GlobalUnlock(memory);
    CloseClipboard();
    if (!result) { sa_desktop_set_error("clipboard text conversion failed"); return sa_strdup(""); }
    return result;
#else
    sa_desktop_set_error("clipboard is not available on this platform");
    return sa_strdup("");
#endif
}
#endif

#ifdef SA_ENABLE_GUI
/* 轮询式窗口 GUI：SA 没有函数指针，所以不走回调注册，而是 Win32 原生的
   control id 路线——按钮点击进事件队列，WAIT_EVENT 阻塞取 id，SA 侧用
   WHILE + IF 分发。窗口/控件句柄沿用槽位 + generation 机制。 */
static char sa_gui_last_error[512] = "";
static void sa_gui_clear_error(void) { sa_gui_last_error[0] = '\0'; }
static void sa_gui_set_error(const char* message) { snprintf(sa_gui_last_error, sizeof(sa_gui_last_error), "%s", message ? message : "gui error"); }
static char* sa_gui_last_error_copy(void) { return sa_strdup(sa_gui_last_error); }

#if defined(_WIN32) || defined(SA_ENABLE_GUI_GTK)
#define SA_GUI_WINDOW_COUNT 16
#define SA_GUI_WIDGET_COUNT 128
#define SA_GUI_EVENT_QUEUE 64

static long long sa_gui_events[SA_GUI_EVENT_QUEUE];
static int sa_gui_event_head = 0;
static int sa_gui_event_count = 0;
static int sa_gui_live_windows = 0;

static void sa_gui_push_event(long long id) {
    if (sa_gui_event_count >= SA_GUI_EVENT_QUEUE) return;
    sa_gui_events[(sa_gui_event_head + sa_gui_event_count) % SA_GUI_EVENT_QUEUE] = id;
    sa_gui_event_count++;
}

static long long sa_gui_pop_event(void) {
    long long id = sa_gui_events[sa_gui_event_head];
    sa_gui_event_head = (sa_gui_event_head + 1) % SA_GUI_EVENT_QUEUE;
    sa_gui_event_count--;
    return id;
}
#endif

#ifdef _WIN32
typedef struct {
    HWND hwnd;
    uint32_t generation;
} SaGuiWindowSlot;

typedef struct {
    HWND hwnd;
    uint32_t generation;
} SaGuiWidgetSlot;

static SaGuiWindowSlot sa_gui_windows[SA_GUI_WINDOW_COUNT];
static SaGuiWidgetSlot sa_gui_widgets[SA_GUI_WIDGET_COUNT];
static int sa_gui_class_registered = 0;

static LRESULT CALLBACK sa_gui_wndproc(HWND hwnd, UINT msg, WPARAM wparam, LPARAM lparam) {
    switch (msg) {
    case WM_COMMAND:
        /* 只把按钮点击当事件；EDIT 的 EN_* 通知忽略，文本用 GET_TEXT 拉取 */
        if (HIWORD(wparam) == BN_CLICKED && LOWORD(wparam) != 0) {
            sa_gui_push_event((long long)LOWORD(wparam));
        }
        return 0;
    case WM_CLOSE:
        DestroyWindow(hwnd);
        return 0;
    case WM_DESTROY:
        /* 点 X 关闭也走这里：清掉窗口槽位和随之销毁的子控件槽位，
           否则句柄仍判定为 live，后续 SET_TEXT 会打到已销毁的 HWND 上 */
        for (size_t i = 0; i < SA_GUI_WINDOW_COUNT; i++) {
            if (sa_gui_windows[i].hwnd == hwnd) {
                sa_gui_windows[i].hwnd = NULL;
                sa_gui_windows[i].generation++;
            }
        }
        for (size_t i = 0; i < SA_GUI_WIDGET_COUNT; i++) {
            if (sa_gui_widgets[i].hwnd && !IsWindow(sa_gui_widgets[i].hwnd)) {
                sa_gui_widgets[i].hwnd = NULL;
                sa_gui_widgets[i].generation++;
            }
        }
        if (sa_gui_live_windows > 0 && --sa_gui_live_windows == 0) {
            sa_gui_push_event(0);
        }
        return 0;
    default:
        return DefWindowProcW(hwnd, msg, wparam, lparam);
    }
}

static void sa_gui_apply_font(HWND hwnd) {
    /* 不发 WM_SETFONT 的话控件用远古 System 粗体字，观感直接回到 Win3.1 */
    SendMessageW(hwnd, WM_SETFONT, (WPARAM)GetStockObject(DEFAULT_GUI_FONT), TRUE);
}

static int sa_gui_ensure_class(void) {
    if (sa_gui_class_registered) return 1;
    WNDCLASSW wc;
    memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = sa_gui_wndproc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_ARROW);
    wc.hbrBackground = (HBRUSH)(COLOR_BTNFACE + 1);
    wc.lpszClassName = L"SonAlgebraicWindow";
    if (!RegisterClassW(&wc)) { sa_gui_set_error("RegisterClassW failed"); return 0; }
    sa_gui_class_registered = 1;
    return 1;
}

static HWND sa_gui_window_hwnd(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_GUI_WINDOW, SA_GUI_WINDOW_COUNT, &index, &generation)) return NULL;
    SaGuiWindowSlot* slot = &sa_gui_windows[index];
    return slot->hwnd && slot->generation == generation ? slot->hwnd : NULL;
}

static HWND sa_gui_widget_hwnd(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_GUI_WIDGET, SA_GUI_WIDGET_COUNT, &index, &generation)) return NULL;
    SaGuiWidgetSlot* slot = &sa_gui_widgets[index];
    return slot->hwnd && slot->generation == generation ? slot->hwnd : NULL;
}
#endif

#ifdef SA_ENABLE_GUI_GTK
/* GTK3 后端：与 Win32 版共用事件队列和轮询语义。绝对坐标布局用 GtkFixed，
   clicked 信号在 C 层把 control id 塞进队列，SA 层 API 完全一致。 */
typedef struct {
    GtkWidget* window;
    GtkWidget* fixed;
    uint32_t generation;
} SaGuiWindowSlot;

typedef struct {
    GtkWidget* widget;
    uint32_t generation;
} SaGuiWidgetSlot;

static SaGuiWindowSlot sa_gui_windows[SA_GUI_WINDOW_COUNT];
static SaGuiWidgetSlot sa_gui_widgets[SA_GUI_WIDGET_COUNT];
static int sa_gui_gtk_ready = 0;

static int sa_gui_ensure_gtk(void) {
    if (sa_gui_gtk_ready) return 1;
    if (!gtk_init_check(NULL, NULL)) { sa_gui_set_error("gtk_init failed (is a display available?)"); return 0; }
    sa_gui_gtk_ready = 1;
    return 1;
}

static void sa_gui_on_button_clicked(GtkWidget* source, gpointer user_data) {
    (void)source;
    sa_gui_push_event((long long)(intptr_t)user_data);
}

static void sa_gui_on_widget_destroy(GtkWidget* source, gpointer user_data) {
    (void)source;
    SaGuiWidgetSlot* slot = (SaGuiWidgetSlot*)user_data;
    slot->widget = NULL;
    slot->generation++;
}

static void sa_gui_on_window_destroy(GtkWidget* source, gpointer user_data) {
    (void)source;
    SaGuiWindowSlot* slot = (SaGuiWindowSlot*)user_data;
    slot->window = NULL;
    slot->fixed = NULL;
    slot->generation++;
    if (sa_gui_live_windows > 0 && --sa_gui_live_windows == 0) {
        sa_gui_push_event(0);
    }
}

static SaGuiWindowSlot* sa_gui_window_slot(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_GUI_WINDOW, SA_GUI_WINDOW_COUNT, &index, &generation)) return NULL;
    SaGuiWindowSlot* slot = &sa_gui_windows[index];
    return slot->window && slot->generation == generation ? slot : NULL;
}

static GtkWidget* sa_gui_widget_ptr(SaHandle handle) {
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(handle, SA_HANDLE_GUI_WIDGET, SA_GUI_WIDGET_COUNT, &index, &generation)) return NULL;
    SaGuiWidgetSlot* slot = &sa_gui_widgets[index];
    return slot->widget && slot->generation == generation ? slot->widget : NULL;
}

static SaHandle sa_gui_register_widget(SaGuiWindowSlot* owner, GtkWidget* widget, long long x, long long y, long long width, long long height) {
    for (size_t i = 0; i < SA_GUI_WIDGET_COUNT; i++) {
        SaGuiWidgetSlot* slot = &sa_gui_widgets[i];
        if (slot->widget) continue;
        gtk_fixed_put(GTK_FIXED(owner->fixed), widget, (gint)x, (gint)y);
        gtk_widget_set_size_request(widget, (gint)width, (gint)height);
        gtk_widget_show(widget);
        if (++slot->generation == 0) slot->generation = 1;
        slot->widget = widget;
        g_signal_connect(widget, "destroy", G_CALLBACK(sa_gui_on_widget_destroy), slot);
        return sa_handle_make(SA_HANDLE_GUI_WIDGET, slot->generation, i);
    }
    gtk_widget_destroy(widget);
    sa_gui_set_error("too many live widgets");
    return 0;
}
#endif

static SaHandle sa_gui_window(const char* title, long long width, long long height) {
    sa_gui_clear_error();
#ifdef _WIN32
    if (!sa_gui_ensure_class()) return 0;
    wchar_t* title_w = sa_win_widen(title);
    if (!title_w) { sa_gui_set_error("invalid UTF-8 window title"); return 0; }
    for (size_t i = 0; i < SA_GUI_WINDOW_COUNT; i++) {
        SaGuiWindowSlot* slot = &sa_gui_windows[i];
        if (slot->hwnd) continue;
        RECT frame = {0, 0, (LONG)width, (LONG)height};
        AdjustWindowRect(&frame, WS_OVERLAPPEDWINDOW & ~(WS_THICKFRAME | WS_MAXIMIZEBOX), FALSE);
        HWND hwnd = CreateWindowW(L"SonAlgebraicWindow", title_w,
            WS_OVERLAPPEDWINDOW & ~(WS_THICKFRAME | WS_MAXIMIZEBOX),
            CW_USEDEFAULT, CW_USEDEFAULT, frame.right - frame.left, frame.bottom - frame.top,
            NULL, NULL, GetModuleHandleW(NULL), NULL);
        free(title_w);
        if (!hwnd) { sa_gui_set_error("CreateWindowW failed"); return 0; }
        if (++slot->generation == 0) slot->generation = 1;
        slot->hwnd = hwnd;
        sa_gui_live_windows++;
        ShowWindow(hwnd, SW_SHOW);
        UpdateWindow(hwnd);
        return sa_handle_make(SA_HANDLE_GUI_WINDOW, slot->generation, i);
    }
    free(title_w);
    sa_gui_set_error("too many live windows");
    return 0;
#elif defined(SA_ENABLE_GUI_GTK)
    if (!sa_gui_ensure_gtk()) return 0;
    for (size_t i = 0; i < SA_GUI_WINDOW_COUNT; i++) {
        SaGuiWindowSlot* slot = &sa_gui_windows[i];
        if (slot->window) continue;
        GtkWidget* created = gtk_window_new(GTK_WINDOW_TOPLEVEL);
        gtk_window_set_title(GTK_WINDOW(created), title ? title : "");
        gtk_window_set_default_size(GTK_WINDOW(created), (gint)width, (gint)height);
        gtk_window_set_resizable(GTK_WINDOW(created), FALSE);
        GtkWidget* fixed = gtk_fixed_new();
        gtk_container_add(GTK_CONTAINER(created), fixed);
        if (++slot->generation == 0) slot->generation = 1;
        slot->window = created;
        slot->fixed = fixed;
        g_signal_connect(created, "destroy", G_CALLBACK(sa_gui_on_window_destroy), slot);
        sa_gui_live_windows++;
        gtk_widget_show_all(created);
        return sa_handle_make(SA_HANDLE_GUI_WINDOW, slot->generation, i);
    }
    sa_gui_set_error("too many live windows");
    return 0;
#else
    (void)title; (void)width; (void)height;
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return 0;
#endif
}

#ifdef _WIN32
static SaHandle sa_gui_create_widget(SaHandle window, const wchar_t* wclass, DWORD style, long long control_id, const char* text, long long x, long long y, long long width, long long height) {
    HWND parent = sa_gui_window_hwnd(window);
    if (!parent) { sa_gui_set_error("invalid or closed WINDOW handle"); return 0; }
    wchar_t* text_w = sa_win_widen(text);
    if (!text_w) { sa_gui_set_error("invalid UTF-8 widget text"); return 0; }
    for (size_t i = 0; i < SA_GUI_WIDGET_COUNT; i++) {
        SaGuiWidgetSlot* slot = &sa_gui_widgets[i];
        if (slot->hwnd) continue;
        HWND hwnd = CreateWindowW(wclass, text_w, WS_CHILD | WS_VISIBLE | style,
            (int)x, (int)y, (int)width, (int)height,
            parent, (HMENU)(INT_PTR)control_id, GetModuleHandleW(NULL), NULL);
        free(text_w);
        if (!hwnd) { sa_gui_set_error("CreateWindowW failed for widget"); return 0; }
        sa_gui_apply_font(hwnd);
        if (++slot->generation == 0) slot->generation = 1;
        slot->hwnd = hwnd;
        return sa_handle_make(SA_HANDLE_GUI_WIDGET, slot->generation, i);
    }
    sa_gui_set_error("too many live widgets");
    free(text_w);
    return 0;
}
#endif

static SaHandle sa_gui_button(SaHandle window, long long control_id, const char* text, long long x, long long y, long long width, long long height) {
    sa_gui_clear_error();
#ifdef _WIN32
    if (control_id <= 0 || control_id > 65535) { sa_gui_set_error("BUTTON control id must be in 1..65535"); return 0; }
    return sa_gui_create_widget(window, L"BUTTON", BS_PUSHBUTTON, control_id, text, x, y, width, height);
#elif defined(SA_ENABLE_GUI_GTK)
    if (control_id <= 0 || control_id > 65535) { sa_gui_set_error("BUTTON control id must be in 1..65535"); return 0; }
    SaGuiWindowSlot* owner = sa_gui_window_slot(window);
    if (!owner) { sa_gui_set_error("invalid or closed WINDOW handle"); return 0; }
    GtkWidget* created = gtk_button_new_with_label(text ? text : "");
    g_signal_connect(created, "clicked", G_CALLBACK(sa_gui_on_button_clicked), (gpointer)(intptr_t)control_id);
    return sa_gui_register_widget(owner, created, x, y, width, height);
#else
    (void)window; (void)control_id; (void)text; (void)x; (void)y; (void)width; (void)height;
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return 0;
#endif
}

static SaHandle sa_gui_label(SaHandle window, const char* text, long long x, long long y, long long width, long long height) {
    sa_gui_clear_error();
#ifdef _WIN32
    return sa_gui_create_widget(window, L"STATIC", 0, 0, text, x, y, width, height);
#elif defined(SA_ENABLE_GUI_GTK)
    SaGuiWindowSlot* owner = sa_gui_window_slot(window);
    if (!owner) { sa_gui_set_error("invalid or closed WINDOW handle"); return 0; }
    GtkWidget* created = gtk_label_new(text ? text : "");
    /* GtkLabel 默认居中，Win32 STATIC 是左上对齐，行为对齐后 SA 程序跨平台观感一致 */
    gtk_widget_set_halign(created, GTK_ALIGN_START);
    gtk_widget_set_valign(created, GTK_ALIGN_START);
    return sa_gui_register_widget(owner, created, x, y, width, height);
#else
    (void)window; (void)text; (void)x; (void)y; (void)width; (void)height;
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return 0;
#endif
}

static SaHandle sa_gui_textbox(SaHandle window, long long x, long long y, long long width, long long height) {
    sa_gui_clear_error();
#ifdef _WIN32
    return sa_gui_create_widget(window, L"EDIT", WS_BORDER | ES_AUTOHSCROLL, 0, "", x, y, width, height);
#elif defined(SA_ENABLE_GUI_GTK)
    SaGuiWindowSlot* owner = sa_gui_window_slot(window);
    if (!owner) { sa_gui_set_error("invalid or closed WINDOW handle"); return 0; }
    GtkWidget* created = gtk_entry_new();
    return sa_gui_register_widget(owner, created, x, y, width, height);
#else
    (void)window; (void)x; (void)y; (void)width; (void)height;
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return 0;
#endif
}

static int sa_gui_set_text(SaHandle widget, const char* text) {
    sa_gui_clear_error();
#ifdef _WIN32
    HWND hwnd = sa_gui_widget_hwnd(widget);
    if (!hwnd) { sa_gui_set_error("invalid or closed WIDGET handle"); return 0; }
    wchar_t* text_w = sa_win_widen(text);
    if (!text_w) { sa_gui_set_error("invalid UTF-8 text"); return 0; }
    int ok = SetWindowTextW(hwnd, text_w);
    free(text_w);
    if (!ok) { sa_gui_set_error("SetWindowTextW failed"); return 0; }
    return 1;
#elif defined(SA_ENABLE_GUI_GTK)
    GtkWidget* target = sa_gui_widget_ptr(widget);
    if (!target) { sa_gui_set_error("invalid or closed WIDGET handle"); return 0; }
    if (GTK_IS_ENTRY(target)) gtk_entry_set_text(GTK_ENTRY(target), text ? text : "");
    else if (GTK_IS_LABEL(target)) gtk_label_set_text(GTK_LABEL(target), text ? text : "");
    else if (GTK_IS_BUTTON(target)) gtk_button_set_label(GTK_BUTTON(target), text ? text : "");
    else { sa_gui_set_error("unsupported widget type"); return 0; }
    return 1;
#else
    (void)widget; (void)text;
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return 0;
#endif
}

static char* sa_gui_get_text(SaHandle widget) {
    sa_gui_clear_error();
#ifdef _WIN32
    HWND hwnd = sa_gui_widget_hwnd(widget);
    if (!hwnd) { sa_gui_set_error("invalid or closed WIDGET handle"); return sa_strdup(""); }
    int length = GetWindowTextLengthW(hwnd);
    if (length <= 0) return sa_strdup("");
    wchar_t* text_w = (wchar_t*)malloc(((size_t)length + 1) * sizeof(wchar_t));
    if (!text_w) { fputs("SonAlgebraic runtime: out of memory\n", stderr); exit(1); }
    GetWindowTextW(hwnd, text_w, length + 1);
    char* result = sa_win_narrow(text_w);
    free(text_w);
    if (!result) { sa_gui_set_error("widget text conversion failed"); return sa_strdup(""); }
    return result;
#elif defined(SA_ENABLE_GUI_GTK)
    GtkWidget* target = sa_gui_widget_ptr(widget);
    if (!target) { sa_gui_set_error("invalid or closed WIDGET handle"); return sa_strdup(""); }
    const char* text = NULL;
    if (GTK_IS_ENTRY(target)) text = gtk_entry_get_text(GTK_ENTRY(target));
    else if (GTK_IS_LABEL(target)) text = gtk_label_get_text(GTK_LABEL(target));
    else if (GTK_IS_BUTTON(target)) text = gtk_button_get_label(GTK_BUTTON(target));
    else { sa_gui_set_error("unsupported widget type"); return sa_strdup(""); }
    return sa_strdup(text);
#else
    (void)widget;
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return sa_strdup("");
#endif
}

static long long sa_gui_wait_event(void) {
    sa_gui_clear_error();
#ifdef _WIN32
    MSG msg;
    for (;;) {
        if (sa_gui_event_count > 0) return sa_gui_pop_event();
        if (sa_gui_live_windows == 0) return 0;
        BOOL result = GetMessageW(&msg, NULL, 0, 0);
        if (result <= 0) return 0;
        /* 让 TEXTBOX 里 Tab 键在控件间移动焦点 */
        HWND active = GetActiveWindow();
        if (active && IsDialogMessageW(active, &msg)) continue;
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }
#elif defined(SA_ENABLE_GUI_GTK)
    if (!sa_gui_gtk_ready) return 0;
    for (;;) {
        if (sa_gui_event_count > 0) return sa_gui_pop_event();
        if (sa_gui_live_windows == 0) return 0;
        gtk_main_iteration();
    }
#else
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return 0;
#endif
}

static int sa_gui_close(SaHandle window) {
    sa_gui_clear_error();
#ifdef _WIN32
    size_t index = 0;
    uint32_t generation = 0;
    if (!sa_handle_parse(window, SA_HANDLE_GUI_WINDOW, SA_GUI_WINDOW_COUNT, &index, &generation)) { sa_gui_set_error("invalid or closed WINDOW handle"); return 0; }
    SaGuiWindowSlot* slot = &sa_gui_windows[index];
    if (!slot->hwnd || slot->generation != generation) { sa_gui_set_error("invalid or closed WINDOW handle"); return 0; }
    DestroyWindow(slot->hwnd);
    slot->hwnd = NULL;
    slot->generation++;
    return 1;
#elif defined(SA_ENABLE_GUI_GTK)
    SaGuiWindowSlot* slot = sa_gui_window_slot(window);
    if (!slot) { sa_gui_set_error("invalid or closed WINDOW handle"); return 0; }
    /* destroy 信号回调负责清槽位、减窗口计数 */
    gtk_widget_destroy(slot->window);
    return 1;
#else
    (void)window;
    sa_gui_set_error("SYS.GUI is only available on Windows");
    return 0;
#endif
}
#endif

static void sa_print_string(const char* value) {
    printf("%s\n", value ? value : "");
}

static void sa_print_long(long long value) {
    printf("%lld\n", value);
}

static void sa_print_double(double value) {
    printf("%.15g\n", value);
}

static void sa_read_line(char* buffer, size_t size) {
    if (!fgets(buffer, (int)size, stdin)) {
        buffer[0] = '\0';
        return;
    }
    size_t len = strlen(buffer);
    while (len > 0 && (buffer[len - 1] == '\n' || buffer[len - 1] == '\r')) {
        buffer[--len] = '\0';
    }
}

static void sa_cls(void) {
#ifdef _WIN32
    system("cls");
#else
    system("clear");
#endif
}

static void sa_setup_console(void) {
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
#endif
}
'''


RUNTIME_HEADER = r'''
#ifndef SONALGEBRAIC_SA_RUNTIME_H
#define SONALGEBRAIC_SA_RUNTIME_H

#ifndef _WIN32
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200112L
#endif
#endif

#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <string.h>
#include <stdint.h>
#include <errno.h>
#include <limits.h>
#include <sys/stat.h>

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifdef SA_ENABLE_NET
#include <winsock2.h>
#include <ws2tcpip.h>
#endif
#include <windows.h>
#ifdef SA_ENABLE_TLS
#ifndef SECURITY_WIN32
#define SECURITY_WIN32
#endif
#include <security.h>
#include <schannel.h>
#endif
#ifdef SA_ENABLE_DESKTOP
#include <shellapi.h>
#endif
#ifdef SA_ENABLE_NET
#include <winhttp.h>
#endif
#if defined(SA_ENABLE_NET) && defined(_MSC_VER)
#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "ws2_32.lib")
#endif
#if defined(SA_ENABLE_TLS) && defined(_MSC_VER)
#pragma comment(lib, "secur32.lib")
#endif
#if defined(SA_ENABLE_DESKTOP) && defined(_MSC_VER)
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "shell32.lib")
#endif
#else
#include <unistd.h>
#ifdef SA_ENABLE_NET
#include <fcntl.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>
#include <sys/time.h>
#ifdef SA_ENABLE_TLS
#include <openssl/ssl.h>
#include <openssl/err.h>
#include <openssl/x509v3.h>
#endif
#endif
#endif

/* 见 RUNTIME 中的说明：分离编译的模块也必须用同一套跳转宏，否则跨翻译单元的
 * SaTryFrame 布局不一致。 */
#if defined(__MINGW32__) && (defined(__GNUC__) || defined(__clang__))
typedef void* SaJmpBuf[5];
#define SA_SETJMP(buf) __builtin_setjmp(buf)
#define SA_LONGJMP(buf) __builtin_longjmp((buf), 1)
#else
#include <setjmp.h>
typedef jmp_buf SaJmpBuf;
#define SA_SETJMP(buf) setjmp(buf)
#define SA_LONGJMP(buf) longjmp((buf), 1)
#endif

typedef struct {
    char* data;
    size_t len;
    size_t cap;
} SaStringBuilder;

typedef enum {
    SA_SYM_CONST,
    SA_SYM_VAR,
    SA_SYM_OP,
    SA_SYM_FUNC
} SaSymbolKind;

typedef struct SaSymbolNode {
    SaSymbolKind kind;
    char* text;
    char op;
    struct SaSymbolNode* left;
    struct SaSymbolNode* right;
} SaSymbolNode;

typedef SaSymbolNode* SaSymbol;

typedef uint64_t SaHandle;

typedef struct {
    int err_code;
    const char* type;
    char* message;
    int line_number;
    const char* sub_name;
} SaError;

typedef struct {
    SaJmpBuf env;
} SaTryFrame;

extern SaTryFrame sa_try_stack[64];
extern int sa_try_top;
extern SaError sa_current_error;

void* sa_try_push_env(void);
void sa_try_pop(void);
char* sa_strdup(const char* value);
long long sa_str_length(const char* value);
char* sa_str_concat(const char* a, const char* b);
char* sa_str_slice(const char* value, long long start, long long count);
long long sa_str_find(const char* value, const char* needle);
char* sa_str_upper(const char* value);
char* sa_str_lower(const char* value);
char* sa_str_replace(const char* value, const char* old_sub, const char* new_sub);
void sa_set_string(char** target, const char* value);
void sa_set_error(SaError* target, const SaError* value);
void sa_error_clear(SaError* target);
void sa_throw_new(const char* type, const char* message, int line_number, const char* sub_name);
void sa_throw_error(const SaError* error);
void sa_raise_new(const char* type, const char* message, int line_number, const char* sub_name);
void sa_raise_error(const SaError* error);
void sa_throw_dispatch(void);
double sa_number(const char* value);
char* sa_to_string_long(long long value);
char* sa_to_string_double(double value);
char* sa_to_string_pointer(void* value);
void sa_sb_init(SaStringBuilder* builder);
void sa_sb_append(SaStringBuilder* builder, const char* value);
char* sa_sb_take(SaStringBuilder* builder);
SaSymbol sa_symbol_const(const char* text);
SaSymbol sa_symbol_var(const char* name);
SaSymbol sa_symbol_func(const char* name, SaSymbol arg);
SaSymbol sa_symbol_op(char op, SaSymbol left, SaSymbol right);
SaSymbol sa_symbol_clone(SaSymbol s);
double sa_symbol_eval(SaSymbol s);
SaSymbol sa_symbol_subst(SaSymbol s, const char* var, double value);
SaSymbol sa_symbol_deriv(SaSymbol s, const char* var);
SaSymbol sa_symbol_simplify(SaSymbol s);
void sa_symbol_free(SaSymbol symbol);
char* sa_symbol_to_string(SaSymbol symbol);
char* sa_net_http_get(const char* url);
long long sa_net_http_status(const char* url);
char* sa_net_http_post(const char* url, const char* body, const char* content_type);
char* sa_net_http_request(const char* method, const char* url, const char* body, const char* headers);
long long sa_net_http_request_status(const char* method, const char* url, const char* body, const char* headers);
char* sa_net_http_request_timeout(const char* method, const char* url, const char* body, const char* headers, long long timeout_ms);
long long sa_net_http_request_status_timeout(const char* method, const char* url, const char* body, const char* headers, long long timeout_ms);
char* sa_net_last_headers_copy(void);
char* sa_net_last_error_copy(void);
long long sa_net_last_code_value(void);
char* sa_net_last_peer_host_copy(void);
long long sa_net_last_peer_port_value(void);
char* sa_net_urlencode(const char* value);
char* sa_net_dns(const char* host);
SaHandle sa_net_tcp_connect(const char* host, long long port, long long timeout_ms);
SaHandle sa_net_tls_connect(const char* host, long long port, long long timeout_ms);
SaHandle sa_net_tcp_listen(const char* bind_host, long long port, long long backlog);
SaHandle sa_net_tcp_accept(SaHandle listener, long long timeout_ms);
int sa_net_tcp_listener_close(SaHandle listener);
long long sa_net_tcp_listener_local_port(SaHandle listener);
long long sa_net_stream_send(SaHandle stream, const char* text);
char* sa_net_stream_recv(SaHandle stream, long long max_bytes);
long long sa_net_stream_send_buffer(SaHandle stream, SaHandle buffer, long long offset, long long count);
SaHandle sa_net_stream_recv_buffer(SaHandle stream, long long max_bytes);
int sa_net_stream_close(SaHandle stream);
SaHandle sa_net_udp_open(void);
int sa_net_udp_bind(SaHandle socket_handle, const char* bind_host, long long port);
int sa_net_udp_connect(SaHandle socket_handle, const char* host, long long port);
long long sa_net_udp_send(SaHandle socket_handle, const char* text);
long long sa_net_udp_send_to(SaHandle socket_handle, const char* host, long long port, const char* text);
char* sa_net_udp_recv(SaHandle socket_handle, long long max_bytes);
long long sa_net_udp_send_buffer(SaHandle socket_handle, SaHandle buffer, long long offset, long long count);
long long sa_net_udp_send_buffer_to(SaHandle socket_handle, const char* host, long long port, SaHandle buffer, long long offset, long long count);
SaHandle sa_net_udp_recv_buffer(SaHandle socket_handle, long long max_bytes);
int sa_net_udp_close(SaHandle socket_handle);
long long sa_net_udp_local_port(SaHandle socket_handle);
SaHandle sa_binary_new(long long length);
int sa_binary_close(SaHandle handle);
long long sa_binary_length(SaHandle handle);
SaHandle sa_binary_slice(SaHandle handle, long long offset, long long count);
int sa_binary_copy(SaHandle target, long long target_offset, SaHandle source, long long source_offset, long long count);
SaHandle sa_binary_hex_decode(const char* value);
char* sa_binary_hex_encode(SaHandle handle);
int sa_binary_pack_u16_le(SaHandle handle, long long offset, long long value);
int sa_binary_pack_u16_be(SaHandle handle, long long offset, long long value);
int sa_binary_pack_u32_le(SaHandle handle, long long offset, long long value);
int sa_binary_pack_u32_be(SaHandle handle, long long offset, long long value);
int sa_binary_pack_u64_le(SaHandle handle, long long offset, long long value);
int sa_binary_pack_u64_be(SaHandle handle, long long offset, long long value);
long long sa_binary_unpack_u16_le(SaHandle handle, long long offset);
long long sa_binary_unpack_u16_be(SaHandle handle, long long offset);
long long sa_binary_unpack_u32_le(SaHandle handle, long long offset);
long long sa_binary_unpack_u32_be(SaHandle handle, long long offset);
long long sa_binary_unpack_u64_le(SaHandle handle, long long offset);
long long sa_binary_unpack_u64_be(SaHandle handle, long long offset);
long long sa_binary_checksum8(SaHandle handle, long long offset, long long count);
char* sa_binary_last_error_copy(void);
SaHandle sa_file_open(const char* path, const char* mode);
char* sa_file_read(SaHandle handle, long long count);
long long sa_file_write(SaHandle handle, const char* text);
int sa_file_seek(SaHandle handle, long long offset, const char* origin);
long long sa_file_tell(SaHandle handle);
long long sa_file_size(SaHandle handle);
int sa_file_close(SaHandle handle);
char* sa_file_read_text(const char* path);
int sa_file_write_text(const char* path, const char* text);
int sa_file_append_text(const char* path, const char* text);
int sa_file_exists(const char* path);
int sa_file_is_file(const char* path);
int sa_file_is_dir(const char* path);
int sa_file_delete(const char* path);
int sa_file_mkdir(const char* path);
char* sa_file_cwd(void);
char* sa_file_absolute(const char* path);
char* sa_file_last_error_copy(void);
int sa_desktop_message(const char* title, const char* text);
int sa_desktop_open(const char* target);
int sa_desktop_clipboard_set(const char* text);
char* sa_desktop_clipboard_get(void);
char* sa_desktop_last_error_copy(void);
void sa_print_string(const char* value);
void sa_print_long(long long value);
void sa_print_double(double value);
void sa_read_line(char* buffer, size_t size);
void sa_cls(void);
void sa_setup_console(void);

#endif
'''


RUNTIME_SOURCE = RUNTIME.replace("static ", "")
RUNTIME_SOURCE = RUNTIME_SOURCE[RUNTIME_SOURCE.index("SaTryFrame sa_try_stack") :]
