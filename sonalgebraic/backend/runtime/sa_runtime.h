
#ifndef SONALGEBRAIC_SA_RUNTIME_H
#define SONALGEBRAIC_SA_RUNTIME_H
/* >>> SA_RUNTIME_PRELUDE_BEGIN（单文件 RUNTIME 与本头共用，勿删此标记） */

#ifndef _WIN32
#ifndef _POSIX_C_SOURCE
#define _POSIX_C_SOURCE 200112L
#endif
/* 32 位 POSIX 上 off_t 默认还是 32 位，fseeko/ftello 会在 2GB 处翻车；
 * 这个宏必须在任何系统头之前定义才生效。 */
#ifndef _FILE_OFFSET_BITS
#define _FILE_OFFSET_BITS 64
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
/* winsock2.h 必须在 windows.h 之前，且异步 I/O 也要它（WSAPoll、非阻塞 socket）。
 * 首期 async 必伴 SYS.NET，NET 一定共现，但放宽到 NET||ASYNC 让头依赖自洽、不靠巧合。 */
#if defined(SA_ENABLE_NET) || defined(SA_ENABLE_ASYNC)
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
/* 异步 I/O 与 NET 共用这批 socket/poll 头。放宽到 NET||ASYNC 让头依赖自洽——
 * 首期 async 必伴 SYS.NET 只是巧合共现，不该拿它当头可见性的保证。 */
#if defined(SA_ENABLE_NET) || defined(SA_ENABLE_ASYNC)
#include <fcntl.h>
#include <poll.h>
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
/* <<< SA_RUNTIME_PRELUDE_END */

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
SaHandle sa_list_new(void);
int sa_list_push(SaHandle handle, double value);
double sa_list_pop(SaHandle handle);
double sa_list_get(SaHandle handle, long long index);
int sa_list_set(SaHandle handle, long long index, double value);
int sa_list_insert(SaHandle handle, long long index, double value);
int sa_list_remove(SaHandle handle, long long index);
long long sa_list_length(SaHandle handle);
int sa_list_clear(SaHandle handle);
int sa_list_close(SaHandle handle);
char* sa_list_last_error_copy(void);
SaHandle sa_strlist_new(void);
int sa_strlist_push(SaHandle handle, const char* value);
char* sa_strlist_pop(SaHandle handle);
char* sa_strlist_get(SaHandle handle, long long index);
int sa_strlist_set(SaHandle handle, long long index, const char* value);
int sa_strlist_insert(SaHandle handle, long long index, const char* value);
int sa_strlist_remove(SaHandle handle, long long index);
long long sa_strlist_length(SaHandle handle);
int sa_strlist_clear(SaHandle handle);
int sa_strlist_close(SaHandle handle);
char* sa_strlist_join(SaHandle handle, const char* separator);
SaHandle sa_map_new(void);
int sa_map_set(SaHandle handle, const char* key, double value);
double sa_map_get(SaHandle handle, const char* key);
int sa_map_has(SaHandle handle, const char* key);
int sa_map_remove(SaHandle handle, const char* key);
long long sa_map_length(SaHandle handle);
SaHandle sa_map_keys(SaHandle handle);
int sa_map_clear(SaHandle handle);
int sa_map_close(SaHandle handle);
char* sa_map_last_error_copy(void);
SaHandle sa_strmap_new(void);
int sa_strmap_set(SaHandle handle, const char* key, const char* value);
char* sa_strmap_get(SaHandle handle, const char* key);
int sa_strmap_has(SaHandle handle, const char* key);
int sa_strmap_remove(SaHandle handle, const char* key);
long long sa_strmap_length(SaHandle handle);
SaHandle sa_strmap_keys(SaHandle handle);
int sa_strmap_clear(SaHandle handle);
int sa_strmap_close(SaHandle handle);
SaHandle sa_gui_window(const char* title, long long width, long long height);
SaHandle sa_gui_button(SaHandle window, long long control_id, const char* text, long long x, long long y, long long width, long long height);
SaHandle sa_gui_label(SaHandle window, const char* text, long long x, long long y, long long width, long long height);
SaHandle sa_gui_textbox(SaHandle window, long long x, long long y, long long width, long long height);
int sa_gui_set_text(SaHandle widget, const char* text);
char* sa_gui_get_text(SaHandle widget);
long long sa_gui_wait_event(void);
int sa_gui_close(SaHandle window);
char* sa_gui_last_error_copy(void);
void sa_print_string(const char* value);
void sa_print_long(long long value);
void sa_print_double(double value);
void sa_read_line(char* buffer, size_t size);
void sa_cls(void);
void sa_setup_console(void);

#ifdef SA_ENABLE_ASYNC
/* 分离编译时用户模块的协程帧把 SaCoroBase 按值内嵌作首成员、还要读 f->base.state 等，
 * 所以共享头必须给出完整定义（不能是不透明前置声明）。SA_COROBASE_DEFINED 守卫保证它
 * 与 RUNTIME_IMPL 里的同一份定义在 sa_runtime.c（同时含本头与去 static 的实现切片）中
 * 只落一次，不会重定义。 */
#ifndef SA_COROBASE_DEFINED
#define SA_COROBASE_DEFINED
typedef struct SaCoroBase {
    int state;
    void (*resume)(struct SaCoroBase*);
    void (*cleanup)(struct SaCoroBase*);
    SaHandle self;
    SaHandle awaited;
} SaCoroBase;
#endif
SaHandle sa_promise_alloc(SaCoroBase* coro);
void sa_coro_schedule(SaHandle promise);
void sa_coro_await(SaCoroBase* base, SaHandle awaited);
void sa_event_loop_run_until(SaHandle target);
void sa_promise_release(SaHandle promise);
void sa_promise_reject(SaHandle promise, const char* message);
void sa_promise_fulfill_long(SaHandle promise, long long value);
void sa_promise_fulfill_double(SaHandle promise, double value);
void sa_promise_fulfill_handle(SaHandle promise, SaHandle value);
void sa_promise_fulfill_str(SaHandle promise, char* value);
void sa_promise_fulfill_void(SaHandle promise);
long long sa_promise_take_long(SaHandle promise);
double sa_promise_take_double(SaHandle promise);
SaHandle sa_promise_take_handle(SaHandle promise);
char* sa_promise_take_str(SaHandle promise);
void sa_promise_take_void(SaHandle promise);
#ifdef SA_ENABLE_NET
SaHandle sa_net_accept_promise(SaHandle listener_handle);
SaHandle sa_net_recv_promise(SaHandle stream_handle, long long max_bytes);
SaHandle sa_net_send_promise(SaHandle stream_handle, const char* text);
SaHandle sa_net_connect_promise(const char* host, long long port);
#endif
#endif

#endif
