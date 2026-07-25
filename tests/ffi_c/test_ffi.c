/*
 * 反向 FFI 测试：C 程序作为消费方，调用 SonAlgebraic 编译出的动态库 mathffi。
 *
 * 验证链路：.sa -> C11 -> DLL(+import lib) -> 被外部 C 程序 #include 头 + 链接调用。
 * 注意：SA 模块的 CONST 真实值由 <module>_init() 设置，使用常量前必须先调用 init。
 */
#include <stdio.h>
#include <math.h>

#include "sa_user_mathffi.h"

static int g_failures = 0;

static void check_double(const char *name, double actual, double expected) {
    /* SA 的 DOUBLE 直接映射 C double，浮点比较留个小容差即可 */
    if (fabs(actual - expected) > 1e-9) {
        printf("[FAIL] %s: got %.10f, want %.10f\n", name, actual, expected);
        g_failures++;
    } else {
        printf("[ OK ] %s = %.10f\n", name, actual);
    }
}

static void check_long(const char *name, long long actual, long long expected) {
    if (actual != expected) {
        printf("[FAIL] %s: got %lld, want %lld\n", name, actual, expected);
        g_failures++;
    } else {
        printf("[ OK ] %s = %lld\n", name, actual);
    }
}

int main(void) {
    /* 初始化模块：填充 CONST 等模块级状态 */
    sa_mod_mathffi_init();

    /* 1. 模块导出的常量 */
    check_double("CONST E_APPROX", sa_mod_mathffi_const_e_approx, 2.718281828);

    /* 2. DOUBLE 返回值函数 */
    check_double("add(1.5, 2.25)", sa_mod_mathffi_sub_add(1.5, 2.25), 3.75);
    check_double("scale(4.0, 2.5)", sa_mod_mathffi_sub_scale(4.0, 2.5), 10.0);

    /* 3. LONG(long long) 返回值函数，含 SA 内部 GOTO 循环 */
    check_long("factorial(5)", sa_mod_mathffi_sub_factorial(5), 120);
    check_long("factorial(0)", sa_mod_mathffi_sub_factorial(0), 1);

    /* 4. 返回布尔语义的 LONG */
    check_long("is_even(10)", sa_mod_mathffi_sub_is_even(10), 1);
    check_long("is_even(7)", sa_mod_mathffi_sub_is_even(7), 0);

    /* 5. 组合调用：把一个 SA 函数的结果喂给另一个 */
    double combined = sa_mod_mathffi_sub_scale(sa_mod_mathffi_sub_add(2.0, 3.0), 2.0);
    check_double("scale(add(2,3), 2)", combined, 10.0);

    sa_mod_mathffi_free();

    if (g_failures == 0) {
        printf("\nALL FFI CHECKS PASSED\n");
        return 0;
    }
    printf("\n%d FFI CHECK(S) FAILED\n", g_failures);
    return 1;
}
