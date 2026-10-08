"""内置调用对拥有者的影响；读写游标与只读查询不能混为一谈。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..core.names import split_module_member


class ArgumentEffect(str, Enum):
    READ = 'read'
    MUTATE = 'mutate'
    CONSUME = 'consume'
    CAPTURE = 'capture'


@dataclass(frozen=True)
class BuiltinEffect:
    arguments: tuple[ArgumentEffect, ...] = ()
    fresh_handle: str | None = None

    def argument(self, index: int) -> ArgumentEffect:
        return self.arguments[index] if index < len(self.arguments) else ArgumentEffect.READ


R = ArgumentEffect.READ
W = ArgumentEffect.MUTATE
C = ArgumentEffect.CONSUME
P = ArgumentEffect.CAPTURE

# 不列出的 API 不接受托管 HANDLE，不能因为它曾接受裸整数就偷偷放行。
_EFFECTS = {
    ('SYS.FILE', 'OPEN'): BuiltinEffect(fresh_handle='FILE'),
    ('SYS.FILE', 'READ'): BuiltinEffect((W, R)),
    ('SYS.FILE', 'WRITE'): BuiltinEffect((W, R)),
    ('SYS.FILE', 'SEEK'): BuiltinEffect((W, R, R)),
    ('SYS.FILE', 'TELL'): BuiltinEffect((R,)),
    # SIZE 临时 seek 到文件尾再恢复，会触发读写切换和刷新，不是纯只读观察。
    ('SYS.FILE', 'SIZE'): BuiltinEffect((W,)),
    ('SYS.FILE', 'CLOSE'): BuiltinEffect((C,)),
    ('SYS.BINARY', 'NEW'): BuiltinEffect(fresh_handle='BUFFER'),
    ('SYS.BINARY', 'CLOSE'): BuiltinEffect((C,)),
    ('SYS.BINARY', 'LENGTH'): BuiltinEffect((R,)),
    ('SYS.BINARY', 'SLICE'): BuiltinEffect((R, R, R), fresh_handle='BUFFER'),
    # COPY 的第三个参数是来源缓冲，只读；被写的是第一个。
    ('SYS.BINARY', 'COPY'): BuiltinEffect((W, R, R, R, R)),
    ('SYS.BINARY', 'HEX_DECODE'): BuiltinEffect(fresh_handle='BUFFER'),
    ('SYS.BINARY', 'HEX_ENCODE'): BuiltinEffect((R,)),
    ('SYS.BINARY', 'CHECKSUM8'): BuiltinEffect((R, R, R)),
    **{('SYS.BINARY', f'PACK_U{width}_{order}'): BuiltinEffect((W, R, R))
       for width in (16, 32, 64) for order in ('LE', 'BE')},
    **{('SYS.BINARY', f'UNPACK_U{width}_{order}'): BuiltinEffect((R, R))
       for width in (16, 32, 64) for order in ('LE', 'BE')},
    ('SYS.NET', 'TCP_CONNECT'): BuiltinEffect(fresh_handle='NET_STREAM'),
    ('SYS.NET', 'TLS_CONNECT'): BuiltinEffect(fresh_handle='NET_STREAM'),
    ('SYS.NET', 'TCP_LISTEN'): BuiltinEffect(fresh_handle='TCP_LISTENER'),
    ('SYS.NET', 'TCP_ACCEPT'): BuiltinEffect((W, R), fresh_handle='NET_STREAM'),
    ('SYS.NET', 'TCP_LISTENER_CLOSE'): BuiltinEffect((C,)),
    ('SYS.NET', 'LOCAL_PORT'): BuiltinEffect((R,)),
    ('SYS.NET', 'STREAM_SEND'): BuiltinEffect((W, R)),
    ('SYS.NET', 'STREAM_RECV'): BuiltinEffect((W, R)),
    ('SYS.NET', 'STREAM_SEND_BUFFER'): BuiltinEffect((W, R, R, R)),
    ('SYS.NET', 'STREAM_RECV_BUFFER'): BuiltinEffect((W, R), fresh_handle='BUFFER'),
    ('SYS.NET', 'STREAM_CLOSE'): BuiltinEffect((C,)),
    ('SYS.NET', 'UDP_OPEN'): BuiltinEffect(fresh_handle='UDP_SOCKET'),
    ('SYS.NET', 'UDP_BIND'): BuiltinEffect((W, R, R)),
    ('SYS.NET', 'UDP_CONNECT'): BuiltinEffect((W, R, R)),
    ('SYS.NET', 'UDP_SEND'): BuiltinEffect((W, R)),
    ('SYS.NET', 'UDP_SEND_TO'): BuiltinEffect((W, R, R, R)),
    ('SYS.NET', 'UDP_RECV'): BuiltinEffect((W, R)),
    ('SYS.NET', 'UDP_SEND_BUFFER'): BuiltinEffect((W, R, R, R)),
    ('SYS.NET', 'UDP_SEND_BUFFER_TO'): BuiltinEffect((W, R, R, R, R, R)),
    ('SYS.NET', 'UDP_RECV_BUFFER'): BuiltinEffect((W, R), fresh_handle='BUFFER'),
    ('SYS.NET', 'UDP_CLOSE'): BuiltinEffect((C,)),
    ('SYS.NET', 'UDP_LOCAL_PORT'): BuiltinEffect((R,)),
    # 异步 I/O 把句柄存进待完成操作里，Promise 完成前作用域析构就会关掉运行时还在用的 socket。
    # 托管句柄暂不跨这条边界：裸 HANDLE 仍可用，责任留在用户手里。
    ('SYS.NET', 'ACCEPT_ASYNC'): BuiltinEffect((P,)),
    ('SYS.NET', 'RECV_ASYNC'): BuiltinEffect((P, R)),
    ('SYS.NET', 'SEND_ASYNC'): BuiltinEffect((P, R)),
    ('SYS.NET', 'CONNECT_ASYNC'): BuiltinEffect(),
    ('SYS.GUI', 'WINDOW'): BuiltinEffect(fresh_handle='WINDOW'),
    # 控件挂进窗口的槽位表，改的是窗口；WIDGET 随父窗口销毁，不单独拥有。
    ('SYS.GUI', 'BUTTON'): BuiltinEffect((W,)),
    ('SYS.GUI', 'LABEL'): BuiltinEffect((W,)),
    ('SYS.GUI', 'TEXTBOX'): BuiltinEffect((W,)),
    ('SYS.GUI', 'CLOSE'): BuiltinEffect((C,)),
    # 托管容器。这些条目只在首参真的是托管容器时起作用——旧 `HANDLE AS LIST` 走的是手动
    # 管理那条路，查 effect 的两个地方都先过 is_managed_resource。
    # 元素的复制与移动由容器操作本身检查；这里记录首参容器的读写影响。
    **{(module, member): effect
       for module in ('SYS.LIST', 'SYS.MAP')
       for member, effect in (('LENGTH', BuiltinEffect((R,))),
                              ('HAS', BuiltinEffect((R,))),
                              ('GET', BuiltinEffect((R,))),
                              ('BORROW', BuiltinEffect((R,))),
                              ('PUSH', BuiltinEffect((W,))),
                              ('PUSH_MOVE', BuiltinEffect((W,))),
                              ('INSERT', BuiltinEffect((W,))),
                              ('INSERT_MOVE', BuiltinEffect((W,))),
                              ('SET', BuiltinEffect((W,))),
                              ('SET_MOVE', BuiltinEffect((W,))),
                              ('TAKE', BuiltinEffect((W,))),
                              ('CLEAR', BuiltinEffect((W,))),
                              ('REMOVE', BuiltinEffect((W,))),
                              ('CLOSE', BuiltinEffect((C,))))},
}


def builtin_effect(name: str, uses: dict[str, str]) -> BuiltinEffect | None:
    split = split_module_member(name)
    if split is None:
        return None
    module = uses.get(split[0])
    return _EFFECTS.get((module, split[1].upper())) if module is not None else None
