from __future__ import annotations

from ...analysis.typesys import is_error, is_null, is_symbol
from ...core import ast
from ...core.names import split_module_member
from .base import LLVMValue, NativeGenBase


class BuiltinsMixin(NativeGenBase):
    """SYS.* 内建模块的函数调用发射（NET/FILE/LIST/MAP/GUI/BINARY 等）。"""

    def is_math_function(self, name: str, function_name: str) -> bool:
        split = split_module_member(name)
        return bool(split and self.checked.uses.get(split[0]) == "SYS.MATH" and split[1].upper() == function_name.upper())

    def builtin_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        """全局内置 NUMBER/STRING、SYS.MATH.POW、SYS.STRING/SYS.NET 函数 -> 运行时调用。"""
        name = expr.name.upper()
        if name == "NUMBER":
            self.use_runtime("sa_number")
            arg = self.expr(expr.args[0])
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @sa_number(ptr {arg.value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if name == "STRING":
            return self.string_conversion(expr)
        if self.is_math_function(expr.name, "POW"):
            if is_symbol(self.type_of_expr(expr)):
                return self.symbol_expr(expr)
            self.use_runtime("pow")
            a = self.cast_to_double(self.expr(expr.args[0]))
            b = self.cast_to_double(self.expr(expr.args[1]))
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @pow(double {a.value}, double {b.value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        algebra = self.symbol_algebra_call(expr)
        if algebra is not None:
            return algebra
        string_call = self.string_function_call(expr)
        if string_call is not None:
            return string_call
        net_call = self.net_function_call(expr)
        if net_call is not None:
            return net_call
        file_call = self.file_function_call(expr)
        if file_call is not None:
            return file_call
        desktop_call = self.desktop_function_call(expr)
        if desktop_call is not None:
            return desktop_call
        binary_call = self.binary_function_call(expr)
        if binary_call is not None:
            return binary_call
        list_call = self.list_function_call(expr)
        if list_call is not None:
            return list_call
        map_call = self.map_function_call(expr)
        if map_call is not None:
            return map_call
        return self.gui_function_call(expr)

    def symbol_algebra_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        name = expr.name.upper()
        if name not in {"DERIV", "SIMPLIFY", "SUBST", "EVAL"}:
            return None
        sym = self.expr(expr.args[0])
        if name == "EVAL":
            self.use_runtime("sa_symbol_eval")
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @sa_symbol_eval(ptr {sym.value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if name == "SIMPLIFY":
            self.use_runtime("sa_symbol_simplify")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_symbol_simplify(ptr {sym.value})")
        elif name == "DERIV":
            self.use_runtime("sa_symbol_deriv")
            temp = self.next_temp()
            var = expr.args[1]
            assert isinstance(var, ast.StringLiteral)
            self.emit(f"  {temp} = call ptr @sa_symbol_deriv(ptr {sym.value}, ptr {self.string_ptr(var.value)})")
        else:
            self.use_runtime("sa_symbol_subst")
            temp = self.next_temp()
            var = expr.args[1]
            assert isinstance(var, ast.StringLiteral)
            value = self.cast_to_double(self.expr(expr.args[2]))
            self.emit(f"  {temp} = call ptr @sa_symbol_subst(ptr {sym.value}, ptr {self.string_ptr(var.value)}, double {value.value})")
        self.use_runtime("sa_symbol_free")
        self.add_temp_cleanup(f"  call void @sa_symbol_free(ptr {temp})")
        return LLVMValue("ptr", temp, ast.TypeSpec("SYMBOL"))

    def string_conversion(self, expr: ast.CallExpr) -> LLVMValue:
        """STRING(x)：按参数类型转字符串。堆返回的登记临时清理。"""
        value = self.expr(expr.args[0])
        if value.type_name == "ptr" and self.is_string_type(value.type_spec):
            return value  # 已经是字符串，直接透传
        if value.type_name == "ptr" and is_error(value.type_spec or ast.TypeSpec("VOID")):
            return LLVMValue("ptr", self.error_message_ptr(value.value), ast.TypeSpec("STRING"))
        temp = self.next_temp()
        if value.type_name == "ptr" and is_symbol(value.type_spec or ast.TypeSpec("VOID")):
            self.use_runtime("sa_symbol_to_string")
            self.emit(f"  {temp} = call ptr @sa_symbol_to_string(ptr {value.value})")
        elif value.type_name == "ptr":
            self.use_runtime("sa_to_string_pointer")
            self.emit(f"  {temp} = call ptr @sa_to_string_pointer(ptr {value.value})")
        elif value.type_name == "i1":
            wide = self.cast_to_i64(value)
            self.use_runtime("sa_to_string_long")
            self.emit(f"  {temp} = call ptr @sa_to_string_long(i64 {wide.value})")
        elif value.type_name == "i64":
            self.use_runtime("sa_to_string_long")
            self.emit(f"  {temp} = call ptr @sa_to_string_long(i64 {value.value})")
        else:  # double
            self.use_runtime("sa_to_string_double")
            self.emit(f"  {temp} = call ptr @sa_to_string_double(double {value.value})")
        self.use_runtime("free")
        self.add_temp_cleanup(f"  call void @free(ptr {temp})")
        return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))

    def string_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        """SYS.STRING 内置函数 -> 运行时调用。返回 None 表示不是字符串函数。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.STRING":
            return None
        member = member.upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        heap_returning = {
            "CONCAT": ("sa_str_concat", "ptr {0}, ptr {1}"),
            "SLICE": ("sa_str_slice", "ptr {0}, i64 {1}, i64 {2}"),
            "UPPER": ("sa_str_upper", "ptr {0}"),
            "LOWER": ("sa_str_lower", "ptr {0}"),
            "REPLACE": ("sa_str_replace", "ptr {0}, ptr {1}, ptr {2}"),
        }
        if member in heap_returning:
            fn, fmt = heap_returning[member]
            self.use_runtime(fn)
            call_args = fmt.format(*(self.coerce_str_arg(a) for a in args))
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "LENGTH":
            self.use_runtime("sa_str_length")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_str_length(ptr {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "FIND":
            self.use_runtime("sa_str_find")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_str_find(ptr {args[0].value}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        return None

    def net_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        """SYS.NET 内置函数 -> runtime 调用。当前支持阻塞 HTTP GET/STATUS。"""
        split = split_module_member(expr.name)
        if split is None:
            return None
        alias, member = split
        if self.checked.uses.get(alias) != "SYS.NET":
            return None
        member = member.upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "GET":
            self.use_runtime("sa_net_http_get")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_http_get(ptr {args[0].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "STATUS":
            self.use_runtime("sa_net_http_status")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_http_status(ptr {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "POST":
            self.use_runtime("sa_net_http_post")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_http_post(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "REQUEST":
            self.use_runtime("sa_net_http_request")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_http_request(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "REQUEST_STATUS":
            self.use_runtime("sa_net_http_request_status")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_http_request_status(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "REQUEST_TIMEOUT":
            self.use_runtime("sa_net_http_request_timeout")
            temp = self.next_temp()
            timeout = self.cast_to_i64(args[4])
            self.emit(f"  {temp} = call ptr @sa_net_http_request_timeout(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value}, i64 {timeout.value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "REQUEST_STATUS_TIMEOUT":
            self.use_runtime("sa_net_http_request_status_timeout")
            temp = self.next_temp()
            timeout = self.cast_to_i64(args[4])
            self.emit(f"  {temp} = call i64 @sa_net_http_request_status_timeout(ptr {args[0].value}, ptr {args[1].value}, ptr {args[2].value}, ptr {args[3].value}, i64 {timeout.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"LAST_HEADERS", "LAST_ERROR"}:
            fn = "sa_net_last_headers_copy" if member == "LAST_HEADERS" else "sa_net_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"LAST_PEER_HOST", "DNS"}:
            fn = "sa_net_last_peer_host_copy" if member == "LAST_PEER_HOST" else "sa_net_dns"
            self.use_runtime(fn)
            temp = self.next_temp()
            call_args = "" if member == "LAST_PEER_HOST" else f"ptr {args[0].value}"
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"LAST_CODE", "LAST_PEER_PORT"}:
            fn = "sa_net_last_code_value" if member == "LAST_CODE" else "sa_net_last_peer_port_value"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}()")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "URLENCODE":
            self.use_runtime("sa_net_urlencode")
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @sa_net_urlencode(ptr {args[0].value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"TCP_CONNECT", "TLS_CONNECT", "TCP_LISTEN"}:
            fn = {
                "TCP_CONNECT": "sa_net_tcp_connect",
                "TLS_CONNECT": "sa_net_tls_connect",
                "TCP_LISTEN": "sa_net_tcp_listen",
            }[member]
            self.use_runtime(fn)
            second = self.cast_to_i64(args[1])
            third = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(ptr {args[0].value}, i64 {second.value}, i64 {third.value})")
            kind = "NET_STREAM" if member != "TCP_LISTEN" else "TCP_LISTENER"
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", kind))
        if member == "TCP_ACCEPT":
            self.use_runtime("sa_net_tcp_accept")
            timeout = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_tcp_accept(i64 {args[0].value}, i64 {timeout.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "NET_STREAM"))
        if member in {"TCP_LISTENER_CLOSE", "STREAM_CLOSE", "UDP_CLOSE"}:
            fn = {
                "TCP_LISTENER_CLOSE": "sa_net_tcp_listener_close",
                "STREAM_CLOSE": "sa_net_stream_close",
                "UDP_CLOSE": "sa_net_udp_close",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value})")
            return self.i32_status(raw)
        if member in {"STREAM_SEND", "UDP_SEND"}:
            fn = "sa_net_stream_send" if member == "STREAM_SEND" else "sa_net_udp_send"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"STREAM_RECV", "UDP_RECV"}:
            fn = "sa_net_stream_recv" if member == "STREAM_RECV" else "sa_net_udp_recv"
            self.use_runtime(fn)
            limit = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, i64 {limit.value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member in {"STREAM_SEND_BUFFER", "UDP_SEND_BUFFER"}:
            fn = "sa_net_stream_send_buffer" if member == "STREAM_SEND_BUFFER" else "sa_net_udp_send_buffer"
            self.use_runtime(fn)
            offset = self.cast_to_i64(args[2])
            count = self.cast_to_i64(args[3])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, i64 {args[1].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"STREAM_RECV_BUFFER", "UDP_RECV_BUFFER"}:
            fn = "sa_net_stream_recv_buffer" if member == "STREAM_RECV_BUFFER" else "sa_net_udp_recv_buffer"
            self.use_runtime(fn)
            limit = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, i64 {limit.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member == "UDP_OPEN":
            self.use_runtime("sa_net_udp_open")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_udp_open()")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "UDP_SOCKET"))
        if member in {"UDP_BIND", "UDP_CONNECT"}:
            fn = "sa_net_udp_bind" if member == "UDP_BIND" else "sa_net_udp_connect"
            self.use_runtime(fn)
            port = self.cast_to_i64(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, ptr {args[1].value}, i64 {port.value})")
            return self.i32_status(raw)
        if member == "UDP_SEND_TO":
            self.use_runtime("sa_net_udp_send_to")
            port = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_udp_send_to(i64 {args[0].value}, ptr {args[1].value}, i64 {port.value}, ptr {args[3].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "UDP_SEND_BUFFER_TO":
            self.use_runtime("sa_net_udp_send_buffer_to")
            port = self.cast_to_i64(args[2])
            offset = self.cast_to_i64(args[4])
            count = self.cast_to_i64(args[5])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_net_udp_send_buffer_to(i64 {args[0].value}, ptr {args[1].value}, i64 {port.value}, i64 {args[3].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"LOCAL_PORT", "UDP_LOCAL_PORT"}:
            fn = "sa_net_tcp_listener_local_port" if member == "LOCAL_PORT" else "sa_net_udp_local_port"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        return None

    def file_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.FILE":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        handle = "0" if args and is_null(args[0].type_spec or ast.TypeSpec("VOID")) else (args[0].value if args else "0")
        if member == "OPEN":
            self.use_runtime("sa_file_open")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_file_open(ptr {args[0].value}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "FILE"))
        if member == "READ":
            self.use_runtime("sa_file_read")
            temp = self.next_temp()
            count = self.cast_to_i64(args[1])
            self.emit(f"  {temp} = call ptr @sa_file_read(i64 {handle}, i64 {count.value})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        if member == "WRITE":
            self.use_runtime("sa_file_write")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_file_write(i64 {handle}, ptr {args[1].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "SEEK":
            self.use_runtime("sa_file_seek")
            raw = self.next_temp()
            offset = self.cast_to_i64(args[1])
            self.emit(f"  {raw} = call i32 @sa_file_seek(i64 {handle}, i64 {offset.value}, ptr {args[2].value})")
            return self.i32_status(raw)
        if member in {"TELL", "SIZE"}:
            fn = "sa_file_tell" if member == "TELL" else "sa_file_size"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {handle})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "CLOSE":
            self.use_runtime("sa_file_close")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_file_close(i64 {handle})")
            return self.i32_status(raw)
        if member in {"WRITE_TEXT", "APPEND_TEXT"}:
            fn = "sa_file_write_text" if member == "WRITE_TEXT" else "sa_file_append_text"
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(ptr {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member in {"EXISTS", "IS_FILE", "IS_DIR", "DELETE", "MKDIR"}:
            fn = {
                "EXISTS": "sa_file_exists", "IS_FILE": "sa_file_is_file", "IS_DIR": "sa_file_is_dir",
                "DELETE": "sa_file_delete", "MKDIR": "sa_file_mkdir",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(ptr {args[0].value})")
            return self.i32_status(raw)
        if member in {"READ_TEXT", "CWD", "ABSOLUTE", "LAST_ERROR"}:
            fn = {
                "READ_TEXT": "sa_file_read_text", "CWD": "sa_file_cwd",
                "ABSOLUTE": "sa_file_absolute", "LAST_ERROR": "sa_file_last_error_copy",
            }[member]
            self.use_runtime(fn)
            temp = self.next_temp()
            call_args = "" if not args else f"ptr {args[0].value}"
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def desktop_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.DESKTOP":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"MESSAGE", "OPEN", "CLIPBOARD_SET"}:
            fn = {"MESSAGE": "sa_desktop_message", "OPEN": "sa_desktop_open", "CLIPBOARD_SET": "sa_desktop_clipboard_set"}[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            call_args = ", ".join(f"ptr {arg.value}" for arg in args)
            self.emit(f"  {raw} = call i32 @{fn}({call_args})")
            return self.i32_status(raw)
        if member in {"CLIPBOARD_GET", "LAST_ERROR"}:
            fn = "sa_desktop_clipboard_get" if member == "CLIPBOARD_GET" else "sa_desktop_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def binary_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.BINARY":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "NEW":
            self.use_runtime("sa_binary_new")
            length = self.cast_to_i64(args[0])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_new(i64 {length.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member == "CLOSE":
            self.use_runtime("sa_binary_close")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_binary_close(i64 {args[0].value})")
            return self.i32_status(raw)
        if member == "LENGTH":
            self.use_runtime("sa_binary_length")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_length(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "SLICE":
            self.use_runtime("sa_binary_slice")
            offset = self.cast_to_i64(args[1])
            count = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_slice(i64 {args[0].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member == "COPY":
            self.use_runtime("sa_binary_copy")
            target_offset = self.cast_to_i64(args[1])
            source_offset = self.cast_to_i64(args[3])
            count = self.cast_to_i64(args[4])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_binary_copy(i64 {args[0].value}, i64 {target_offset.value}, i64 {args[2].value}, i64 {source_offset.value}, i64 {count.value})")
            return self.i32_status(raw)
        if member == "HEX_DECODE":
            self.use_runtime("sa_binary_hex_decode")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_hex_decode(ptr {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "BUFFER"))
        if member in {"HEX_ENCODE", "LAST_ERROR"}:
            fn = "sa_binary_hex_encode" if member == "HEX_ENCODE" else "sa_binary_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            call_args = f"i64 {args[0].value}" if args else ""
            self.emit(f"  {temp} = call ptr @{fn}({call_args})")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        pack_functions = {
            "PACK_U16_LE": "sa_binary_pack_u16_le", "PACK_U16_BE": "sa_binary_pack_u16_be",
            "PACK_U32_LE": "sa_binary_pack_u32_le", "PACK_U32_BE": "sa_binary_pack_u32_be",
            "PACK_U64_LE": "sa_binary_pack_u64_le", "PACK_U64_BE": "sa_binary_pack_u64_be",
        }
        if member in pack_functions:
            fn = pack_functions[member]
            self.use_runtime(fn)
            offset = self.cast_to_i64(args[1])
            value = self.cast_to_i64(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {offset.value}, i64 {value.value})")
            return self.i32_status(raw)
        unpack_functions = {
            "UNPACK_U16_LE": "sa_binary_unpack_u16_le", "UNPACK_U16_BE": "sa_binary_unpack_u16_be",
            "UNPACK_U32_LE": "sa_binary_unpack_u32_le", "UNPACK_U32_BE": "sa_binary_unpack_u32_be",
            "UNPACK_U64_LE": "sa_binary_unpack_u64_le", "UNPACK_U64_BE": "sa_binary_unpack_u64_be",
        }
        if member in unpack_functions:
            fn = unpack_functions[member]
            self.use_runtime(fn)
            offset = self.cast_to_i64(args[1])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value}, i64 {offset.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "CHECKSUM8":
            self.use_runtime("sa_binary_checksum8")
            offset = self.cast_to_i64(args[1])
            count = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_binary_checksum8(i64 {args[0].value}, i64 {offset.value}, i64 {count.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        return None

    def i32_status(self, value: str) -> LLVMValue:
        temp = self.next_temp()
        self.emit(f"  {temp} = icmp ne i32 {value}, 0")
        return LLVMValue("i1", temp, ast.TypeSpec("BOOL"))

    def list_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.LIST":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"NEW", "NEW_STR"}:
            fn = "sa_list_new" if member == "NEW" else "sa_strlist_new"
            kind = "LIST" if member == "NEW" else "STR_LIST"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}()")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", kind))
        if member == "PUSH":
            self.use_runtime("sa_list_push")
            value = self.cast_to_double(args[1])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_list_push(i64 {args[0].value}, double {value.value})")
            return self.i32_status(raw)
        if member in {"POP", "GET"}:
            fn = "sa_list_pop" if member == "POP" else "sa_list_get"
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET":
                index = self.cast_to_i64(args[1])
                self.emit(f"  {temp} = call double @{fn}(i64 {args[0].value}, i64 {index.value})")
            else:
                self.emit(f"  {temp} = call double @{fn}(i64 {args[0].value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if member in {"SET", "INSERT"}:
            fn = "sa_list_set" if member == "SET" else "sa_list_insert"
            self.use_runtime(fn)
            index = self.cast_to_i64(args[1])
            value = self.cast_to_double(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {index.value}, double {value.value})")
            return self.i32_status(raw)
        if member in {"REMOVE", "REMOVE_STR"}:
            fn = "sa_list_remove" if member == "REMOVE" else "sa_strlist_remove"
            self.use_runtime(fn)
            index = self.cast_to_i64(args[1])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {index.value})")
            return self.i32_status(raw)
        if member in {"LENGTH", "LENGTH_STR"}:
            fn = "sa_list_length" if member == "LENGTH" else "sa_strlist_length"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"CLEAR", "CLOSE", "CLEAR_STR", "CLOSE_STR"}:
            fn = {
                "CLEAR": "sa_list_clear",
                "CLOSE": "sa_list_close",
                "CLEAR_STR": "sa_strlist_clear",
                "CLOSE_STR": "sa_strlist_close",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value})")
            return self.i32_status(raw)
        if member == "PUSH_STR":
            self.use_runtime("sa_strlist_push")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_strlist_push(i64 {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member in {"SET_STR", "INSERT_STR"}:
            fn = "sa_strlist_set" if member == "SET_STR" else "sa_strlist_insert"
            self.use_runtime(fn)
            index = self.cast_to_i64(args[1])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, i64 {index.value}, ptr {args[2].value})")
            return self.i32_status(raw)
        if member in {"POP_STR", "GET_STR", "JOIN_STR", "LAST_ERROR"}:
            fn = {
                "POP_STR": "sa_strlist_pop",
                "GET_STR": "sa_strlist_get",
                "JOIN_STR": "sa_strlist_join",
                "LAST_ERROR": "sa_list_last_error_copy",
            }[member]
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET_STR":
                index = self.cast_to_i64(args[1])
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, i64 {index.value})")
            elif member == "JOIN_STR":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            elif member == "POP_STR":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value})")
            else:
                self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def map_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.MAP":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member in {"NEW", "NEW_STR"}:
            fn = "sa_map_new" if member == "NEW" else "sa_strmap_new"
            kind = "MAP" if member == "NEW" else "STR_MAP"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}()")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", kind))
        if member == "SET":
            self.use_runtime("sa_map_set")
            value = self.cast_to_double(args[2])
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_map_set(i64 {args[0].value}, ptr {args[1].value}, double {value.value})")
            return self.i32_status(raw)
        if member == "SET_STR":
            self.use_runtime("sa_strmap_set")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_strmap_set(i64 {args[0].value}, ptr {args[1].value}, ptr {args[2].value})")
            return self.i32_status(raw)
        if member == "GET":
            self.use_runtime("sa_map_get")
            temp = self.next_temp()
            self.emit(f"  {temp} = call double @sa_map_get(i64 {args[0].value}, ptr {args[1].value})")
            return LLVMValue("double", temp, ast.TypeSpec("NUM", "DOUBLE"))
        if member in {"HAS", "HAS_STR", "REMOVE", "REMOVE_STR"}:
            fn = {
                "HAS": "sa_map_has",
                "HAS_STR": "sa_strmap_has",
                "REMOVE": "sa_map_remove",
                "REMOVE_STR": "sa_strmap_remove",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member in {"LENGTH", "LENGTH_STR"}:
            fn = "sa_map_length" if member == "LENGTH" else "sa_strmap_length"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member in {"KEYS", "KEYS_STR"}:
            fn = "sa_map_keys" if member == "KEYS" else "sa_strmap_keys"
            self.use_runtime(fn)
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @{fn}(i64 {args[0].value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "STR_LIST"))
        if member in {"CLEAR", "CLOSE", "CLEAR_STR", "CLOSE_STR"}:
            fn = {
                "CLEAR": "sa_map_clear",
                "CLOSE": "sa_map_close",
                "CLEAR_STR": "sa_strmap_clear",
                "CLOSE_STR": "sa_strmap_close",
            }[member]
            self.use_runtime(fn)
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @{fn}(i64 {args[0].value})")
            return self.i32_status(raw)
        if member in {"GET_STR", "LAST_ERROR"}:
            fn = "sa_strmap_get" if member == "GET_STR" else "sa_map_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET_STR":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value}, ptr {args[1].value})")
            else:
                self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def gui_function_call(self, expr: ast.CallExpr) -> LLVMValue | None:
        split = split_module_member(expr.name)
        if split is None or self.checked.uses.get(split[0]) != "SYS.GUI":
            return None
        member = split[1].upper()
        args = [LLVMValue("i64", "0", ast.TypeSpec("HANDLE")) if isinstance(arg, ast.NullLiteral) else self.expr(arg) for arg in expr.args]
        if member == "WINDOW":
            self.use_runtime("sa_gui_window")
            width = self.cast_to_i64(args[1])
            height = self.cast_to_i64(args[2])
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_window(ptr {args[0].value}, i64 {width.value}, i64 {height.value})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WINDOW"))
        if member == "BUTTON":
            self.use_runtime("sa_gui_button")
            values = [self.cast_to_i64(args[i]).value for i in (1, 3, 4, 5, 6)]
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_button(i64 {args[0].value}, i64 {values[0]}, ptr {args[2].value}, i64 {values[1]}, i64 {values[2]}, i64 {values[3]}, i64 {values[4]})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WIDGET"))
        if member == "LABEL":
            self.use_runtime("sa_gui_label")
            values = [self.cast_to_i64(args[i]).value for i in (2, 3, 4, 5)]
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_label(i64 {args[0].value}, ptr {args[1].value}, i64 {values[0]}, i64 {values[1]}, i64 {values[2]}, i64 {values[3]})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WIDGET"))
        if member == "TEXTBOX":
            self.use_runtime("sa_gui_textbox")
            values = [self.cast_to_i64(args[i]).value for i in (1, 2, 3, 4)]
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_textbox(i64 {args[0].value}, i64 {values[0]}, i64 {values[1]}, i64 {values[2]}, i64 {values[3]})")
            return LLVMValue("i64", temp, ast.TypeSpec("HANDLE", "WIDGET"))
        if member == "SET_TEXT":
            self.use_runtime("sa_gui_set_text")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_gui_set_text(i64 {args[0].value}, ptr {args[1].value})")
            return self.i32_status(raw)
        if member == "WAIT_EVENT":
            self.use_runtime("sa_gui_wait_event")
            temp = self.next_temp()
            self.emit(f"  {temp} = call i64 @sa_gui_wait_event()")
            return LLVMValue("i64", temp, ast.TypeSpec("NUM", "LONG"))
        if member == "CLOSE":
            self.use_runtime("sa_gui_close")
            raw = self.next_temp()
            self.emit(f"  {raw} = call i32 @sa_gui_close(i64 {args[0].value})")
            return self.i32_status(raw)
        if member in {"GET_TEXT", "LAST_ERROR"}:
            fn = "sa_gui_get_text" if member == "GET_TEXT" else "sa_gui_last_error_copy"
            self.use_runtime(fn)
            temp = self.next_temp()
            if member == "GET_TEXT":
                self.emit(f"  {temp} = call ptr @{fn}(i64 {args[0].value})")
            else:
                self.emit(f"  {temp} = call ptr @{fn}()")
            self.use_runtime("free")
            self.add_temp_cleanup(f"  call void @free(ptr {temp})")
            return LLVMValue("ptr", temp, ast.TypeSpec("STRING"))
        return None

    def coerce_str_arg(self, value: LLVMValue) -> str:
        # SLICE 的 start/count 是 i64，其余字符串参数是 ptr。这里按已求值的类型直接取值，
        # 数值参数确保是 i64（SLICE 第 2、3 参数）。
        if value.type_name == "ptr":
            return value.value
        return self.cast_to_i64(value).value
