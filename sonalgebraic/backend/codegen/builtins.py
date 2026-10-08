"""内置模块调用映射：SYMBOL 代数、STRING、NET、FILE、DESKTOP、BINARY、LIST、MAP、GUI。"""
from __future__ import annotations

from ...analysis.builtin_effects import ArgumentEffect, builtin_effect
from ...analysis.resource_traits import handle_close_owned_function, handle_drop_prefix
from ...analysis.typesys import c_type, is_managed_resource, is_string, resolve_managed_container_function
from ...core import ast
from ...core.errors import SonCompileError
from ...core.names import split_module_member
from .base import c_string, CGenBase


# 这些成员的参数只有句柄、下标和 key，runtime 函数名就是 `<前缀>_<成员名小写>`。
_CONTAINER_PLAIN_MEMBERS = {"LENGTH", "CLEAR", "CLOSE", "REMOVE", "HAS"}

# 元素要进容器的成员：最后一个参数走 void*，得先落到临时量再取址。复制形态和移动形态
# 共用同一套发射，差别只在 runtime 那头——copy 读它，move 读完再把它清空。
_CONTAINER_ELEMENT_IN_MEMBERS = {"PUSH", "PUSH_MOVE", "INSERT", "INSERT_MOVE", "SET", "SET_MOVE"}

# 元素要出容器的成员：runtime 把值写进调用方给的槽位，返回的是成功标志而不是元素本身。
_CONTAINER_ELEMENT_OUT_MEMBERS = {"GET", "TAKE"}


class BuiltinsMixin(CGenBase):
    """SYS.* 内置模块函数 -> 运行时调用的映射表。每个函数返回 None 表示「不归我管」，由 call_expr 逐个试。"""

    def managed_first_arg_call(self, expr: ast.CallExpr, call: str, args: list[str]) -> str | None:
        """首参是托管资源的内置调用。CLOSE 换成清空槽位的版本，免得变量留着已失效的 token；
        其余调用也要先落到临时量——句柄可能在同一语句的后续求值里被改写，内联会把求值顺序算错。"""
        if not expr.args:
            return None
        kind = self.type_of(expr.args[0])
        if not is_managed_resource(kind):
            return None
        effect = builtin_effect(expr.name, self.checked.uses)
        if effect is not None and effect.argument(0) is ArgumentEffect.CONSUME:
            call = f"{handle_close_owned_function(kind)}(&{args[0]})"
        temp = self.next_temp()
        self.add_prelude(f"long long {temp} = {call};")
        return temp

    def managed_container_call(self, expr: ast.CallExpr) -> str | None:
        """托管容器的成员调用。

        必须排在旧 SYS.LIST／SYS.MAP 发射之前：两条路的成员名大量重名，落到旧表就会对着
        托管容器的句柄发 `sa_list_push`——那是另一张槽位表，kind 校验拒得掉，但错误只在
        运行时现形。
        """
        signature = resolve_managed_container_function(expr, self.checked.uses, self.symbols, self.checked.entities)
        if signature is None:
            return None
        member = (split_module_member(expr.name) or ("", ""))[1].upper()
        args = [self.expr(arg) for arg in expr.args]
        # 前缀就是析构函数那套名字的来源（sa_vec / sa_dict），容器的全部 API 共用它
        prefix = handle_drop_prefix(self.type_of(expr.args[0]))
        if member in _CONTAINER_ELEMENT_OUT_MEMBERS:
            return self.container_element_out(prefix, member, signature[1], args)
        if member == "BORROW":
            return self.container_element_borrow(prefix, signature[1], args)
        if member in _CONTAINER_ELEMENT_IN_MEMBERS:
            # runtime 收 void*，所以元素得先落到一块能取址的内存——表达式本身不一定有地址
            # （字面量尤其没有）。临时量的类型直接取这个参数位的类型，ops 表的 size 就是按
            # 它算的。不加 const：移动形态要靠这块内存回写清空。
            temp = self.next_temp()
            self.add_prelude(f"{c_type(signature[0][-1])} {temp} = {args[-1]};")
            args = [*args[:-1], f"&{temp}"]
        elif member not in _CONTAINER_PLAIN_MEMBERS:
            # 不能静默交回旧表：那会生成一个函数名对得上、槽位表却错的调用。
            raise SonCompileError(f"{expr.name} 作用在托管容器上的代码生成还没接入", expr.line_no)
        call = f"{prefix}_{member.lower()}({', '.join(args)})"
        return self.managed_first_arg_call(expr, call, args) or call

    def container_element_out(self, prefix: str, member: str, element: ast.TypeSpec, args: list[str]) -> str:
        """GET／TAKE：元素落到临时量，所有权归调用方。

        两者的发射完全同构——GET 让 ops 的 copy 产出一份副本，TAKE 把容器那格搬走再清零，
        调用方拿到的都是一个得自己负责释放的拥有值，所以一律登记清理。登记用的是声明局部
        变量那套释放行，`adopt_temp_cleanup` 才认得出来：`DIM s AS STRING = L.GET(...)`
        能直接接管这一份，不必再 strdup 一次。
        """
        temp = self.next_temp()
        ctype = c_type(element)
        init = self.zero_init_value(element)
        if init.startswith("{"):
            init = f"({ctype}){init}"
        self.add_prelude(f"{ctype} {temp} = {init};")
        call = f"{prefix}_{member.lower()}({', '.join([*args, f'&{temp}'])})"
        if is_string(element):
            # 下标越界时 runtime 完全不碰 out，而 STRING 的零值是 NULL——交给 printf 是 UB。
            # 补一份空串和旧 sa_strlist_get 越界时的行为一致，错因去 LAST_ERROR 查。
            self.add_prelude(f'if (!{call}) {{ {temp} = sa_strdup(""); }}')
        else:
            # 其余元素类型的零值本身就是合理的失败结果：数值是 0，SYMBOL／callable 是 NULL，
            # ERROR 是 ERR_NONE，嵌套容器是空句柄——释放它们全是空操作。
            self.add_prelude(f"{call};")
        for line in self.local_resource_cleanup_lines([(temp, element)], 0):
            self.add_cleanup(line)
        return temp

    def container_element_borrow(self, prefix: str, element: ast.TypeSpec, args: list[str]) -> str:
        """BORROW：容器里那一格的视图，**不登记清理**——所有权一直在容器手里。

        runtime 给的是槽位内部的地址，所以解引用读到的就是容器那份本体；语义层只让它落到
        `f=` 的右侧，ownership 在借用存续期间冻结容器，realloc 搬走这块地址的路都被堵住。
        越界／key 不存在时 runtime 返回 NULL，这里退回零值——STRING 用静态空串而不是
        `sa_strdup("")`：借用不分配，这个值也没有谁会来释放它。失败原因照旧查 LAST_ERROR。
        """
        ctype = c_type(element)
        slot = self.next_temp()
        temp = self.next_temp()
        self.add_prelude(f"void* {slot} = {prefix}_borrow({', '.join(args)});")
        empty = '(char*)""' if is_string(element) else self.zero_init_value(element)
        if empty.startswith("{"):
            empty = f"({ctype}){empty}"
        self.add_prelude(f"{ctype} {temp} = {slot} ? *({ctype}*){slot} : {empty};")
        return temp

    def symbol_algebra_call(self, expr: ast.CallExpr) -> str | None:
        """SYMBOL 代数内置函数 -> runtime 调用。"""
        name = expr.name.upper()
        if name not in {"DERIV", "SIMPLIFY", "SUBST", "EVAL"}:
            return None
        sym = self.expr(expr.args[0])
        if name == "EVAL":
            return f"sa_symbol_eval({sym})"
        # 返回新建的 SaSymbol，登记 free
        temp = self.next_temp()
        if name == "SIMPLIFY":
            call = f"sa_symbol_simplify({sym})"
        elif name == "DERIV":
            var = c_string(expr.args[1].value)
            call = f"sa_symbol_deriv({sym}, {var})"
        else:  # SUBST
            var = c_string(expr.args[1].value)
            value = self.expr(expr.args[2])
            call = f"sa_symbol_subst({sym}, {var}, {value})"
        self.add_prelude(f"SaSymbol {temp} = {call};")
        self.add_cleanup(f"sa_symbol_free({temp});")
        return temp

    def string_function_call(self, expr: ast.CallExpr) -> str | None:
        """SYS.STRING 内置函数 -> runtime 调用。返回 None 表示不是字符串函数。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.STRING":
            return None
        member = member.upper()
        args = [self.expr(arg) for arg in expr.args]
        # 返回 char* 的函数：包临时变量并登记 free
        heap_returning = {
            "CONCAT": "sa_str_concat",
            "SLICE": "sa_str_slice",
            "UPPER": "sa_str_upper",
            "LOWER": "sa_str_lower",
            "REPLACE": "sa_str_replace",
        }
        if member in heap_returning:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap_returning[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        # 返回 long long 的函数直接内联
        if member == "LENGTH":
            return f"sa_str_length({args[0]})"
        if member == "FIND":
            return f"sa_str_find({args[0]}, {args[1]})"
        return None

    def net_function_call(self, expr: ast.CallExpr) -> str | None:
        """SYS.NET 内置函数 -> runtime 调用。当前支持阻塞 HTTP GET/STATUS。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.NET":
            return None
        member = member.upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "GET":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_get({args[0]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "STATUS":
            return f"sa_net_http_status({args[0]})"
        if member == "POST":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_post({args[0]}, {args[1]}, {args[2]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "REQUEST":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_request({args[0]}, {args[1]}, {args[2]}, {args[3]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "REQUEST_STATUS":
            return f"sa_net_http_request_status({args[0]}, {args[1]}, {args[2]}, {args[3]})"
        if member == "REQUEST_TIMEOUT":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_http_request_timeout({args[0]}, {args[1]}, {args[2]}, {args[3]}, {args[4]});")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "REQUEST_STATUS_TIMEOUT":
            return f"sa_net_http_request_status_timeout({args[0]}, {args[1]}, {args[2]}, {args[3]}, {args[4]})"
        if member == "LAST_HEADERS":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_last_headers_copy();")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "LAST_ERROR":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_last_error_copy();")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "LAST_CODE":
            return "sa_net_last_code_value()"
        if member == "LAST_PEER_PORT":
            return "sa_net_last_peer_port_value()"
        if member in {"LAST_PEER_HOST", "DNS"}:
            temp = self.next_temp()
            call = "sa_net_last_peer_host_copy()" if member == "LAST_PEER_HOST" else f"sa_net_dns({args[0]})"
            self.add_prelude(f"char* {temp} = {call};")
            self.add_cleanup(f"free({temp});")
            return temp
        if member == "URLENCODE":
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = sa_net_urlencode({args[0]});")
            self.add_cleanup(f"free({temp});")
            return temp
        direct = {
            "TCP_CONNECT": "sa_net_tcp_connect",
            "TLS_CONNECT": "sa_net_tls_connect",
            "TCP_LISTEN": "sa_net_tcp_listen",
            "TCP_ACCEPT": "sa_net_tcp_accept",
            "TCP_LISTENER_CLOSE": "sa_net_tcp_listener_close",
            "STREAM_SEND": "sa_net_stream_send",
            "STREAM_SEND_BUFFER": "sa_net_stream_send_buffer",
            "STREAM_RECV_BUFFER": "sa_net_stream_recv_buffer",
            "STREAM_CLOSE": "sa_net_stream_close",
            "UDP_OPEN": "sa_net_udp_open",
            "UDP_BIND": "sa_net_udp_bind",
            "UDP_CONNECT": "sa_net_udp_connect",
            "UDP_SEND": "sa_net_udp_send",
            "UDP_SEND_TO": "sa_net_udp_send_to",
            "UDP_SEND_BUFFER": "sa_net_udp_send_buffer",
            "UDP_SEND_BUFFER_TO": "sa_net_udp_send_buffer_to",
            "UDP_RECV_BUFFER": "sa_net_udp_recv_buffer",
            "UDP_CLOSE": "sa_net_udp_close",
            "LOCAL_PORT": "sa_net_tcp_listener_local_port",
            "UDP_LOCAL_PORT": "sa_net_udp_local_port",
            # 异步 I/O：返回 PROMISE 句柄（SaHandle）。生命周期由 AWAIT/SYNC 尾部的
            # sa_promise_release 接管，故这里不登记 cleanup——和同步版一样直接拼调用。
            "ACCEPT_ASYNC": "sa_net_accept_promise",
            "RECV_ASYNC": "sa_net_recv_promise",
            "SEND_ASYNC": "sa_net_send_promise",
            "CONNECT_ASYNC": "sa_net_connect_promise",
        }
        if member in direct:
            call = f"{direct[member]}({', '.join(args)})"
            return self.managed_first_arg_call(expr, call, args) or call
        if member in {"STREAM_RECV", "UDP_RECV"}:
            temp = self.next_temp()
            fn = "sa_net_stream_recv" if member == "STREAM_RECV" else "sa_net_udp_recv"
            self.add_prelude(f"char* {temp} = {fn}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def file_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.FILE":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"READ", "WRITE", "SEEK", "TELL", "SIZE", "CLOSE"} and isinstance(expr.args[0], ast.NullLiteral):
            args[0] = "0"
        direct = {
            "OPEN": "sa_file_open",
            "WRITE": "sa_file_write",
            "SEEK": "sa_file_seek",
            "TELL": "sa_file_tell",
            "SIZE": "sa_file_size",
            "CLOSE": "sa_file_close",
            "WRITE_TEXT": "sa_file_write_text",
            "APPEND_TEXT": "sa_file_append_text",
            "EXISTS": "sa_file_exists",
            "IS_FILE": "sa_file_is_file",
            "IS_DIR": "sa_file_is_dir",
            "DELETE": "sa_file_delete",
            "MKDIR": "sa_file_mkdir",
        }
        if member in direct:
            call = f"{direct[member]}({', '.join(args)})"
            return self.managed_first_arg_call(expr, call, args) or call
        heap = {
            "READ": "sa_file_read",
            "READ_TEXT": "sa_file_read_text",
            "CWD": "sa_file_cwd",
            "ABSOLUTE": "sa_file_absolute",
            "LAST_ERROR": "sa_file_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def desktop_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.DESKTOP":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "MESSAGE": "sa_desktop_message",
            "OPEN": "sa_desktop_open",
            "CLIPBOARD_SET": "sa_desktop_clipboard_set",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "CLIPBOARD_GET": "sa_desktop_clipboard_get",
            "LAST_ERROR": "sa_desktop_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def binary_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.BINARY":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "NEW": "sa_binary_new",
            "CLOSE": "sa_binary_close",
            "LENGTH": "sa_binary_length",
            "SLICE": "sa_binary_slice",
            "COPY": "sa_binary_copy",
            "HEX_DECODE": "sa_binary_hex_decode",
            "PACK_U16_LE": "sa_binary_pack_u16_le",
            "PACK_U16_BE": "sa_binary_pack_u16_be",
            "PACK_U32_LE": "sa_binary_pack_u32_le",
            "PACK_U32_BE": "sa_binary_pack_u32_be",
            "PACK_U64_LE": "sa_binary_pack_u64_le",
            "PACK_U64_BE": "sa_binary_pack_u64_be",
            "UNPACK_U16_LE": "sa_binary_unpack_u16_le",
            "UNPACK_U16_BE": "sa_binary_unpack_u16_be",
            "UNPACK_U32_LE": "sa_binary_unpack_u32_le",
            "UNPACK_U32_BE": "sa_binary_unpack_u32_be",
            "UNPACK_U64_LE": "sa_binary_unpack_u64_le",
            "UNPACK_U64_BE": "sa_binary_unpack_u64_be",
            "CHECKSUM8": "sa_binary_checksum8",
        }
        if member in direct:
            call = f"{direct[member]}({', '.join(args)})"
            return self.managed_first_arg_call(expr, call, args) or call
        heap = {
            "HEX_ENCODE": "sa_binary_hex_encode",
            "LAST_ERROR": "sa_binary_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def list_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.LIST":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "NEW": "sa_list_new",
            "PUSH": "sa_list_push",
            "POP": "sa_list_pop",
            "GET": "sa_list_get",
            "SET": "sa_list_set",
            "INSERT": "sa_list_insert",
            "REMOVE": "sa_list_remove",
            "LENGTH": "sa_list_length",
            "CLEAR": "sa_list_clear",
            "CLOSE": "sa_list_close",
            "NEW_STR": "sa_strlist_new",
            "PUSH_STR": "sa_strlist_push",
            "SET_STR": "sa_strlist_set",
            "INSERT_STR": "sa_strlist_insert",
            "REMOVE_STR": "sa_strlist_remove",
            "LENGTH_STR": "sa_strlist_length",
            "CLEAR_STR": "sa_strlist_clear",
            "CLOSE_STR": "sa_strlist_close",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "POP_STR": "sa_strlist_pop",
            "GET_STR": "sa_strlist_get",
            "JOIN_STR": "sa_strlist_join",
            "LAST_ERROR": "sa_list_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def map_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.MAP":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "NEW": "sa_map_new",
            "SET": "sa_map_set",
            "GET": "sa_map_get",
            "HAS": "sa_map_has",
            "REMOVE": "sa_map_remove",
            "LENGTH": "sa_map_length",
            "KEYS": "sa_map_keys",
            "CLEAR": "sa_map_clear",
            "CLOSE": "sa_map_close",
            "NEW_STR": "sa_strmap_new",
            "SET_STR": "sa_strmap_set",
            "HAS_STR": "sa_strmap_has",
            "REMOVE_STR": "sa_strmap_remove",
            "LENGTH_STR": "sa_strmap_length",
            "KEYS_STR": "sa_strmap_keys",
            "CLEAR_STR": "sa_strmap_clear",
            "CLOSE_STR": "sa_strmap_close",
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
        heap = {
            "GET_STR": "sa_strmap_get",
            "LAST_ERROR": "sa_map_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None

    def gui_function_call(self, expr: ast.CallExpr) -> str | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.GUI":
            return None
        member = split[1].upper()
        args = ["0" if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        direct = {
            "WINDOW": "sa_gui_window",
            "BUTTON": "sa_gui_button",
            "LABEL": "sa_gui_label",
            "TEXTBOX": "sa_gui_textbox",
            "SET_TEXT": "sa_gui_set_text",
            "WAIT_EVENT": "sa_gui_wait_event",
            "CLOSE": "sa_gui_close",
            "ON_CLICK": "sa_gui_on_click",
            "RUN": "sa_gui_run",
        }
        if member in direct:
            call = f"{direct[member]}({', '.join(args)})"
            return self.managed_first_arg_call(expr, call, args) or call
        heap = {
            "GET_TEXT": "sa_gui_get_text",
            "LAST_ERROR": "sa_gui_last_error_copy",
        }
        if member in heap:
            temp = self.next_temp()
            self.add_prelude(f"char* {temp} = {heap[member]}({', '.join(args)});")
            self.add_cleanup(f"free({temp});")
            return temp
        return None
