"""内置模块调用映射：SYMBOL 代数、STRING、NET、FILE、DESKTOP、BINARY、LIST、MAP、GUI。"""
from __future__ import annotations

from ...core import ast
from ...core.names import split_module_member
from .base import c_string, CGenBase


class BuiltinsMixin(CGenBase):
    """SYS.* 内置模块函数 -> 运行时调用的映射表。每个函数返回 None 表示「不归我管」，由 call_expr 逐个试。"""

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
            return f"{direct[member]}({', '.join(args)})"
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
            return f"{direct[member]}({', '.join(args)})"
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
            return f"{direct[member]}({', '.join(args)})"
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
        }
        if member in direct:
            return f"{direct[member]}({', '.join(args)})"
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
