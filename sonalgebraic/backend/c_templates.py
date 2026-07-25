from __future__ import annotations

from jinja2 import Environment, BaseLoader


_ENV = Environment(loader=BaseLoader(), trim_blocks=True, lstrip_blocks=True)


HEADER_TEMPLATE = _ENV.from_string(
    """#ifndef {{ guard }}
#define {{ guard }}

#include "sa_runtime.h"

{% for line in entity_lines %}
{{ line }}
{% endfor %}
{% for line in const_lines %}
{{ line }}
{% endfor %}
{% for line in sub_lines %}
{{ line }}
{% endfor %}

#endif
"""
)


MODULE_C_TEMPLATE = _ENV.from_string(
    """#include "{{ header_name }}"

{% for include in includes %}
#include "{{ include }}"
{% endfor %}

{{ body }}
"""
)


def render_header(guard: str, entity_lines: list[str], const_lines: list[str], sub_lines: list[str]) -> str:
    return HEADER_TEMPLATE.render(
        guard=guard,
        entity_lines=entity_lines,
        const_lines=const_lines,
        sub_lines=sub_lines,
    )


def render_module_c(header_name: str, includes: list[str], body: str) -> str:
    return MODULE_C_TEMPLATE.render(header_name=header_name, includes=includes, body=body)
