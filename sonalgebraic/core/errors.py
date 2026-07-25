class SonCompileError(Exception):
    """Compilation error with optional source line context."""

    def __init__(self, message: str, line_no: int | None = None):
        self.message = message
        self.line_no = line_no
        if line_no is None:
            super().__init__(message)
        else:
            super().__init__(f"line {line_no}: {message}")


def module_cycle_error(stack: list[str], module: str) -> SonCompileError:
    key = module.lower()
    start = next((i for i, item in enumerate(stack) if item.lower() == key), 0)
    chain = [*stack[start:], module]
    return SonCompileError(f"模块循环依赖: {' -> '.join(chain)}")
