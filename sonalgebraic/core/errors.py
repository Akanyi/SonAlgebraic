class SonCompileError(Exception):
    """Compilation error with optional source line context.

    origin_path / origin_text 用来标记错误真正来自哪个文件。依赖模块里的错误如果
    不带这个信息，就会被渲染成主文件同名行号的位置——文件、行内容、下划线三者全错。
    """

    def __init__(
        self,
        message: str,
        line_no: int | None = None,
        origin_path: str | None = None,
        origin_text: str | None = None,
    ):
        self.message = message
        self.line_no = line_no
        self.origin_path = origin_path
        self.origin_text = origin_text
        if line_no is None:
            super().__init__(message)
        else:
            super().__init__(f"line {line_no}: {message}")


def module_cycle_error(stack: list[str], module: str) -> SonCompileError:
    key = module.lower()
    start = next((i for i, item in enumerate(stack) if item.lower() == key), 0)
    chain = [*stack[start:], module]
    return SonCompileError(f"模块循环依赖: {' -> '.join(chain)}")
